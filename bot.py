#!/usr/bin/env python3
"""MZ Downloader Bot with isolated requests and correlated downloader replies."""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import json
import logging
import math
import os
import re
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import socks
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MessageEntity,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import ConfigError, PROJECT_DIR, SETTINGS
import users_db
from pixeldrain_upload import PixeldrainUploader, build_pixeldrain_worker_url
from downloader import (
    AccountPool,
    AccountWorker,
    CooldownRegistry,
    DownloadedMedia,
    DownloaderGateway,
    DrDownloaderError,
    GatewayResult,
    MediaKind,
    PoolUnavailable,
    QualityOption,
    WorkerLease,
    cleanup_request_directory,
    create_attempt_directory,
    request_dr_downloader_album,
)
from instagram_caption import InstagramCaptionError, fetch_instagram_caption
from routing import Platform, all_providers, detect_platform, is_instagram_reel, platform_info, providers_for_platform, spotify_resource_type
from spotisaver import SpotisaverAlbumDownloader, _zip_and_remove as _zip_tracks
from youtube_search import (
    MAX_RESULTS as YOUTUBE_SEARCH_MAX_RESULTS,
    RESULTS_PER_PAGE as YOUTUBE_RESULTS_PER_PAGE,
    YouTubeSearchError,
    YouTubeSearchResult,
    YouTubeSearchService,
    normalize_search_query,
)


LOG_HANDLERS: list[logging.Handler] = [logging.StreamHandler()]
if not os.getenv("RAILWAY_ENVIRONMENT"):
    LOG_HANDLERS.append(
        RotatingFileHandler(
            PROJECT_DIR / "bot.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    )
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=LOG_HANDLERS,
)
logger = logging.getLogger("MZDownloader")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SETTINGS.download_root.mkdir(parents=True, exist_ok=True)
ACCOUNT_POOL = AccountPool()
COOLDOWNS = CooldownRegistry(SETTINGS.late_response_cooldown)
GATEWAY = DownloaderGateway(
    wait_timeout=SETTINGS.wait_timeout,
    preview_grace=SETTINGS.preview_grace,
    album_window=SETTINGS.album_collect_window,
    max_download_size=SETTINGS.max_download_size,
    cooldowns=COOLDOWNS,
    http_proxy_url=(
        f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        if SETTINGS.use_proxy
        else None
    ),
)
YOUTUBE_SEARCH = YouTubeSearchService(
    proxy_url=(
        f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        if SETTINGS.use_proxy
        else None
    )
)

URL_REGEX = re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,!?:;،؛؟]}>\"'"
MAX_ACTIVE_TASKS_PER_USER = 2
REQUIRED_CHANNELS = (
    ("@MZBOTS_Monitor", "https://t.me/MZBOTS_Monitor", "MZ Bots Monitoring"),
)
MEMBERSHIP_CACHE_TTL = 5 * 60
FEEDBACK_STICKER_SET = "MZDownloader"
DEFAULT_UPLOAD_RATE = 2 * 1024 * 1024
UPLOAD_PROGRESS_INTERVAL = 2.0
YOUTUBE_SEARCH_TTL = 10 * 60
YOUTUBE_SEARCH_RATE_LIMIT = 5
YOUTUBE_SEARCH_RATE_WINDOW = 60.0
MAX_YOUTUBE_SEARCH_SESSIONS = 512


@dataclass
class PendingSelection:
    token: str
    created_at: float
    chat_id: int
    user_id: int
    status_message_id: int
    reply_to: int | None
    request_id: str
    source_host: str
    source_url: str
    platform: Platform
    bot_username: str
    request_message_id: int
    menu_message_id: int
    options: tuple[QualityOption, ...]
    lease: WorkerLease
    attempt_directory: Path
    fallback_text: str = ""
    instagram_caption: str = ""
    caption_task: asyncio.Task[str] | None = None
    processing: bool = False
    processing_task: asyncio.Task[Any] | None = None


@dataclass
class YouTubeSearchSession:
    token: str
    created_at: float
    chat_id: int
    user_id: int
    reply_to: int | None
    query: str
    results: tuple[YouTubeSearchResult, ...]
    current_page: int = 0
    busy: bool = False
    selected: bool = False


PENDING_SELECTIONS: dict[str, PendingSelection] = {}
YOUTUBE_SEARCH_SESSIONS: dict[str, YouTubeSearchSession] = {}
ACTIVE_REQUESTS: dict[tuple[int, int], set[asyncio.Task[Any]]] = {}
USER_RATE_LIMITS: dict[tuple[int, int], deque[float]] = defaultdict(deque)
YOUTUBE_SEARCH_RATE_LIMITS: dict[tuple[int, int], deque[float]] = defaultdict(deque)
ACTIVE_YOUTUBE_SEARCHES: set[tuple[int, int]] = set()
MEMBERSHIP_CACHE: dict[int, float] = {}
# token → (url, created_at, chat_id, user_id)
REEL_MUSIC_URLS: dict[str, tuple[str, float, int, int]] = {}
REEL_MUSIC_TTL = 600  # 10 minutes
ADMIN_USERNAME = "iR0nin"  # only this user can use /broadcast

# Pixeldrain uploader
PIXELDRAIN_UPLOADER = PixeldrainUploader(
    api_key=SETTINGS.pixeldrain_api_key,
    proxy_url=(
        f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        if SETTINGS.use_proxy
        else None
    ),
)


FEEDBACK_STICKER_IDS: tuple[str, ...] = ()
SELECTION_REAPER_TASK: asyncio.Task[Any] | None = None
HEALTH_SERVER: asyncio.AbstractServer | None = None
STARTED_AT = time.monotonic()


@dataclass
class RuntimeStats:
    requests: int = 0
    successful: int = 0
    failed: int = 0
    bytes_sent: int = 0


STATS = RuntimeStats()


def build_telethon_proxy() -> tuple[Any, ...] | None:
    if not SETTINGS.use_proxy:
        return None
    proxy_kind = socks.SOCKS5 if SETTINGS.proxy_type == "socks5" else socks.HTTP
    return proxy_kind, SETTINGS.proxy_host, SETTINGS.proxy_port


def normalize_url(candidate: str) -> str | None:
    value = (candidate or "").strip().strip("<>")
    while value and value[-1] in TRAILING_URL_PUNCTUATION:
        value = value[:-1]
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    if value.lower().startswith("www."):
        value = "https://" + value
    if len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").strip(".").lower()
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return None
        if parsed.username or parsed.password or hostname == "localhost" or hostname.endswith(".local"):
            return None
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
            return None
        # Accessing .port validates malformed ports before a link is forwarded.
        _ = parsed.port
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError):
        return None


def extract_urls(text: str | None) -> tuple[str, ...]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_REGEX.finditer(text or ""):
        normalized = normalize_url(match.group(0))
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return tuple(urls)


def extract_urls_from_message(message: Any) -> tuple[str, ...]:
    candidates = list(extract_urls(getattr(message, "text", None) or getattr(message, "caption", None)))
    parsers = (
        getattr(message, "parse_entities", None),
        getattr(message, "parse_caption_entities", None),
    )
    for parser in parsers:
        if not callable(parser):
            continue
        try:
            entities = parser(types=[MessageEntity.URL, MessageEntity.TEXT_LINK])
        except (RuntimeError, ValueError):
            continue
        for entity, displayed_text in entities.items():
            candidate = entity.url if entity.type == MessageEntity.TEXT_LINK else displayed_text
            normalized = normalize_url(candidate)
            if normalized:
                candidates.append(normalized)
    return tuple(dict.fromkeys(candidates))


def source_host(url: str) -> str:
    return (urlsplit(url).hostname or "لینک").lower().removeprefix("www.")


def html_escape(value: str) -> str:
    return html.escape(value or "", quote=False)


def fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def fmt_duration(seconds: float) -> str:
    value = max(0, int(math.ceil(seconds)))
    if value < 60:
        return f"{max(1, value)} ثانیه"
    minutes, remainder = divmod(value, 60)
    if minutes < 60:
        return f"{minutes} دقیقه" + (f" و {remainder} ثانیه" if remainder else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ساعت" + (f" و {minutes} دقیقه" if minutes else "")


def bot_attribution(bot_username: str | None) -> str:
    username = (bot_username or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return "دانلود شده توسط <b>MZ Downloader</b>"
    return (
        "دانلود شده توسط "
        f'<a href="https://t.me/{username}">@{html_escape(username)}</a>'
    )


def status_card(title: str, body: str = "", footer: str = "") -> str:
    lines = [f"<b>{title}</b>"]
    if body:
        lines.extend(("", body))
    if footer:
        lines.extend(("", f"<i>{footer}</i>"))
    return "\n".join(lines)


def progress_bar(percent: int) -> str:
    value = max(0, min(100, int(percent)))
    filled = min(12, round(value / 100 * 12))
    return f"[{'█' * filled}{'░' * (12 - filled)}] <b>{value}%</b>"


class ProgressReporter:
    def __init__(self, message: Any, request_id: str) -> None:
        self.message = message
        self.request_id = request_id
        self.started_at = time.monotonic()
        self.download_started_at: float | None = None
        self.download_last_current = 0
        self.upload_started_at: float | None = None
        self.upload_total = 0
        self.upload_completed = 0
        self.upload_rate = float(DEFAULT_UPLOAD_RATE)
        self.last_edit = 0.0
        self.last_percent = -1
        self.lock = asyncio.Lock()

    async def update(self, percent: int, title: str, detail: str = "", *, force: bool = False) -> None:
        value = max(0, min(100, int(percent)))
        now = time.monotonic()
        if not force and value == self.last_percent:
            return
        if not force and now - self.last_edit < 1.0 and value < 100:
            return
        async with self.lock:
            await edit_status(
                self.message,
                status_card(title, f"{progress_bar(value)}\n{html_escape(detail)}"
            ))
            self.last_edit = time.monotonic()
            self.last_percent = value

    async def download(self, current: int, total: int) -> None:
        current = max(0, int(current or 0))
        total = max(current, int(total or 0))
        now = time.monotonic()
        if self.download_started_at is None or current < self.download_last_current:
            self.download_started_at = now
        self.download_last_current = current
        elapsed = max(0.001, now - self.download_started_at)
        ratio = current / total if total > 0 else 0.0
        rate = current / elapsed if current > 0 else 0.0
        remaining = (total - current) / rate if total > current and rate > 0 else 0.0
        size_line = fmt_size(current)
        if total > 0:
            size_line += f" از {fmt_size(total)}"
        timing = f"⏱ گذشته: {fmt_duration(elapsed)}"
        if remaining > 0:
            timing += f" • باقی‌مونده حدودی: {fmt_duration(remaining)}"
        await self.update(
            12 + int(max(0.0, min(1.0, ratio)) * 56),
            "⬇️ دارم فایل رو می‌گیرم…",
            f"{size_line}\n{timing}",
        )

    async def begin_upload(self, total_size: int) -> None:
        self.upload_started_at = time.monotonic()
        self.upload_total = max(1, int(total_size))
        self.upload_completed = 0
        estimated = self.upload_total / max(self.upload_rate, 1.0)
        await self.update(
            70,
            "📤 حالا دارم برات می‌فرستم…",
            f"حجم کل: {fmt_size(total_size)}\n⏱ زمان تقریبی ارسال: {fmt_duration(estimated)}",
            force=True,
        )

    async def upload(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        size: int,
        label: str,
    ) -> Any:
        item_size = max(1, int(size))
        if self.upload_started_at is None:
            await self.begin_upload(item_size)
        base = self.upload_completed
        item_started = time.monotonic()
        task = asyncio.create_task(operation())
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=UPLOAD_PROGRESS_INTERVAL)
                if task in done:
                    break
                elapsed_item = max(0.001, time.monotonic() - item_started)
                expected_item = item_size / max(self.upload_rate, 1.0)
                estimated_fraction = min(0.95, elapsed_item / max(expected_item, 0.001))
                estimated_sent = base + int(item_size * estimated_fraction)
                overall_ratio = min(0.99, estimated_sent / max(self.upload_total, 1))
                remaining_bytes = max(0, self.upload_total - estimated_sent)
                remaining = remaining_bytes / max(self.upload_rate, 1.0)
                elapsed_total = time.monotonic() - (self.upload_started_at or item_started)
                await self.update(
                    70 + int(overall_ratio * 29),
                    "📤 دارم برات می‌فرستم…",
                    (
                        f"{html_escape(label)} • حدود {fmt_size(estimated_sent)} از {fmt_size(self.upload_total)}\n"
                        f"⏱ گذشته: {fmt_duration(elapsed_total)} • باقی‌مونده حدودی: {fmt_duration(remaining)}"
                    ),
                    force=True,
                )
            result = await task
        except BaseException:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

        elapsed_item = max(0.001, time.monotonic() - item_started)
        measured_rate = item_size / elapsed_item
        if 32 * 1024 <= measured_rate <= 128 * 1024 * 1024:
            self.upload_rate = self.upload_rate * 0.4 + measured_rate * 0.6
        self.upload_completed = min(self.upload_total, base + item_size)
        overall_ratio = self.upload_completed / max(self.upload_total, 1)
        remaining = (self.upload_total - self.upload_completed) / max(self.upload_rate, 1.0)
        elapsed_total = time.monotonic() - (self.upload_started_at or item_started)
        await self.update(
            min(99, 70 + int(overall_ratio * 29)),
            "📤 دارم برات می‌فرستم…",
            (
                f"{html_escape(label)} • {fmt_size(self.upload_completed)} از {fmt_size(self.upload_total)}\n"
                f"⏱ گذشته: {fmt_duration(elapsed_total)}"
                + (f" • باقی‌مونده حدودی: {fmt_duration(remaining)}" if remaining > 0 else "")
            ),
            force=True,
        )
        return result


def pool_status_line() -> str:
    if ACCOUNT_POOL.total == 0:
        return "⛔ بخش دانلود فعلاً آماده نیست"
    available = ACCOUNT_POOL.total - ACCOUNT_POOL.busy_count
    return (
        f"🚀 ظرفیت آزاد: <b>{available}/{ACCOUNT_POOL.total}</b>"
        f" • منتظر: <b>{ACCOUNT_POOL.queue_length}</b>"
    )


def allow_requests(key: tuple[int, int], count: int) -> bool:
    now = time.monotonic()
    history = USER_RATE_LIMITS[key]
    while history and now - history[0] > SETTINGS.rate_limit_window:
        history.popleft()
    if len(history) + count > SETTINGS.rate_limit_requests:
        return False
    history.extend([now] * count)
    return True


def allow_youtube_search(key: tuple[int, int]) -> bool:
    now = time.monotonic()
    history = YOUTUBE_SEARCH_RATE_LIMITS[key]
    while history and now - history[0] > YOUTUBE_SEARCH_RATE_WINDOW:
        history.popleft()
    if len(history) >= YOUTUBE_SEARCH_RATE_LIMIT:
        return False
    history.append(now)
    return True


def persian_number(value: int) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def youtube_search_page_count(session: YouTubeSearchSession) -> int:
    return max(1, math.ceil(len(session.results) / YOUTUBE_RESULTS_PER_PAGE))


def youtube_search_keyboard(session: YouTubeSearchSession, page: int) -> InlineKeyboardMarkup:
    start = page * YOUTUBE_RESULTS_PER_PAGE
    stop = min(start + YOUTUBE_RESULTS_PER_PAGE, len(session.results))
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for result_index in range(start, stop):
        row.append(
            InlineKeyboardButton(
                f"محتوا {persian_number(result_index + 1)}",
                callback_data=f"ys:{session.token}:{result_index}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("⬅️ صفحه قبل", callback_data=f"yp:{session.token}:{page - 1}")
        )
    if page + 1 < youtube_search_page_count(session):
        navigation.append(
            InlineKeyboardButton("صفحه بعد ➡️", callback_data=f"yp:{session.token}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(rows)


def youtube_search_caption(session: YouTubeSearchSession, page: int) -> str:
    page_count = youtube_search_page_count(session)
    return (
        "🔎 <b>نتایج جست‌وجوی YouTube</b>\n"
        f"عبارت: <b>{html_escape(session.query)}</b>\n"
        f"صفحه {persian_number(page + 1)} از {persian_number(page_count)}"
        " • یکی از محتواها را انتخاب کن."
    )


def prune_youtube_search_sessions() -> None:
    now = time.monotonic()
    for token, session in list(YOUTUBE_SEARCH_SESSIONS.items()):
        if now - session.created_at >= YOUTUBE_SEARCH_TTL:
            YOUTUBE_SEARCH_SESSIONS.pop(token, None)
    overflow = len(YOUTUBE_SEARCH_SESSIONS) - MAX_YOUTUBE_SEARCH_SESSIONS
    if overflow > 0:
        oldest = sorted(YOUTUBE_SEARCH_SESSIONS.values(), key=lambda item: item.created_at)[:overflow]
        for session in oldest:
            YOUTUBE_SEARCH_SESSIONS.pop(session.token, None)
    for key, history in list(YOUTUBE_SEARCH_RATE_LIMITS.items()):
        while history and now - history[0] > YOUTUBE_SEARCH_RATE_WINDOW:
            history.popleft()
        if not history:
            YOUTUBE_SEARCH_RATE_LIMITS.pop(key, None)


async def send_long_text(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    heading: str,
    text: str,
    reply_to: int | None,
) -> None:
    clean = (text or "").strip()
    if not clean:
        return
    chunks = [clean[index:index + 3500] for index in range(0, len(clean), 3500)]
    for index, chunk in enumerate(chunks):
        prefix = heading if index == 0 else f"{heading} (ادامه)"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{prefix}\n\n{chunk}",
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
        )


async def health_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=2)
        path = request_line.decode("ascii", errors="ignore").split(" ")[1]
    except (asyncio.TimeoutError, IndexError):
        path = "/"
    ready = ACCOUNT_POOL.total > 0
    status = 200 if path in {"/", "/health"} and ready else 503 if path == "/health" else 404
    payload = json.dumps(
        {
            "status": "ok" if ready else "starting",
            "accounts": ACCOUNT_POOL.total,
            "active": ACCOUNT_POOL.busy_count,
            "uptime_seconds": int(time.monotonic() - STARTED_AT),
        }
    ).encode("utf-8")
    writer.write(
        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Service Unavailable' if status == 503 else 'Not Found'}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii")
        + payload
    )
    await writer.drain()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def edit_status(message: Any, text: str, reply_markup: Any = None) -> None:
    try:
        await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Status edit failed: %s", exc)


async def send_status(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_to: int | None,
) -> Any:
    try:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
        )
    except TelegramError:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def send_link_feedback(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to: int | None,
    *,
    valid: bool,
) -> None:
    """Reply with the matching sticker without ever blocking link processing."""
    global FEEDBACK_STICKER_IDS
    try:
        if len(FEEDBACK_STICKER_IDS) < 2:
            sticker_set = await asyncio.wait_for(
                context.bot.get_sticker_set(FEEDBACK_STICKER_SET),
                timeout=8.0,
            )
            FEEDBACK_STICKER_IDS = tuple(
                sticker.file_id
                for sticker in getattr(sticker_set, "stickers", ())[:2]
                if getattr(sticker, "file_id", None)
            )
        index = 0 if valid else 1
        if len(FEEDBACK_STICKER_IDS) <= index:
            return

        async def send() -> None:
            await context.bot.send_sticker(
                chat_id=chat_id,
                sticker=FEEDBACK_STICKER_IDS[index],
                reply_to_message_id=reply_to,
            )

        await telegram_retry(send)
    except (asyncio.TimeoutError, TelegramError) as exc:
        logger.debug("Feedback sticker could not be sent: %s", exc)


def links_are_supported(urls: tuple[str, ...]) -> bool:
    return bool(urls) and all(detect_platform(url) is not None for url in urls)


def _caption_proxy_url() -> str | None:
    if not SETTINGS.use_proxy:
        return None
    return f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"


async def scrape_instagram_caption(url: str) -> str:
    try:
        return await fetch_instagram_caption(url, proxy_url=_caption_proxy_url())
    except InstagramCaptionError as exc:
        logger.info("Instagram caption unavailable: %s", exc)
    except Exception as exc:
        logger.warning("Instagram caption scraper failed: %s", exc)
    return ""


def is_active_channel_member(member: Any) -> bool:
    status = getattr(member, "status", "")
    if status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}:
        return True
    return status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))


def membership_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"عضویت در {label}", url=url)]
            for _, url, label in REQUIRED_CHANNELS
        ]
    )


async def ensure_required_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return False
    now = time.monotonic()
    if len(MEMBERSHIP_CACHE) >= 4096:
        for cached_user, expires_at in list(MEMBERSHIP_CACHE.items()):
            if expires_at <= now:
                MEMBERSHIP_CACHE.pop(cached_user, None)
    if MEMBERSHIP_CACHE.get(user.id, 0) > now:
        return True
    missing = []
    for channel, _, label in REQUIRED_CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user.id)
        except TelegramError as exc:
            logger.warning("Membership check failed for %s: %s", channel, exc)
            missing.append(label)
            continue
        if not is_active_channel_member(member):
            missing.append(label)
    if not missing:
        MEMBERSHIP_CACHE[user.id] = now + MEMBERSHIP_CACHE_TTL
        return True
    if update.callback_query is not None:
        with contextlib.suppress(TelegramError):
            await update.callback_query.answer("ابتدا عضو هر دو کانال شو.", show_alert=True)
    await message.reply_text(
        status_card(
            "اول یه عضویت کوچولو 😊",
            "برای اینکه بتونی از همه‌ی امکانات استفاده کنی، توی هر دو کانال زیر عضو شو.",
            "عضو که شدی، دوباره /start رو بزن تا بریم سراغ دانلود!",
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=membership_keyboard(),
        disable_web_page_preview=True,
    )
    return False


def membership_required(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]:
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        if not await ensure_required_membership(update, context):
            return None
        return await handler(update, context)

    return wrapped


async def telegram_retry(operation: Callable[[], Awaitable[Any]], attempts: int = 3) -> Any:
    last_error: TelegramError | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except RetryAfter as exc:
            last_error = exc
            delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
        except (NetworkError, TimedOut) as exc:
            last_error = exc
            delay = 1.5 * (attempt + 1)
        if attempt == attempts - 1:
            raise last_error
        await asyncio.sleep(min(delay, 15.0))


def split_file(file_path: Path, chunk_size: int) -> tuple[Path, ...]:
    file_size = file_path.stat().st_size
    if file_size <= chunk_size:
        return (file_path,)
    total_parts = math.ceil(file_size / chunk_size)
    parts: list[Path] = []
    with file_path.open("rb") as source:
        for index in range(1, total_parts + 1):
            part = file_path.with_name(f"{file_path.name}.part{index:03d}")
            with part.open("wb") as target:
                target.write(source.read(chunk_size))
            parts.append(part)
    return tuple(parts)


def create_file_part(file_path: Path, chunk_size: int, index: int) -> Path:
    part = file_path.with_name(f"{file_path.name}.part{index:03d}")
    with file_path.open("rb") as source, part.open("wb") as target:
        source.seek((index - 1) * chunk_size)
        target.write(source.read(chunk_size))
    return part


def media_caption(
    item: DownloadedMedia,
    quality: str | None,
    index: int,
    total: int,
    bot_username: str | None = None,
) -> str:
    details = []
    if quality:
        details.append(html_escape(quality))
    details.append(f"<b>{fmt_size(item.size)}</b>")
    if total > 1:
        details.append(f"{index}/{total}")
    return "✅ " + " • ".join(details) + f"\n{bot_attribution(bot_username)}"


async def send_regular_file(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    item: DownloadedMedia,
    caption: str,
    reply_to: int | None,
    progress: ProgressReporter | None = None,
    upload_label: str = "فایل",
) -> None:
    async def send_native() -> None:
        with item.path.open("rb") as file_handle:
            if item.kind == MediaKind.VIDEO:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=file_handle,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    reply_to_message_id=reply_to,
                )
                return
            if item.kind == MediaKind.AUDIO:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=file_handle,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to,
                )
                return
            await context.bot.send_document(
                chat_id=chat_id,
                document=file_handle,
                filename=item.path.name,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to,
            )
    async def upload_file() -> None:
        try:
            await telegram_retry(send_native)
        except TelegramError:
            if item.kind not in {MediaKind.VIDEO, MediaKind.AUDIO}:
                raise

            async def send_as_document() -> None:
                with item.path.open("rb") as file_handle:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=file_handle,
                        filename=item.path.name,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to,
                    )

            await telegram_retry(send_as_document)

    if progress is None:
        await upload_file()
    else:
        await progress.upload(upload_file, size=item.size, label=upload_label)





async def _pixeldrain_delayed_delete(file_id: str) -> None:
    """Delete a Pixeldrain file after the configured delay."""
    await asyncio.sleep(SETTINGS.pixeldrain_delete_delay)
    if PIXELDRAIN_UPLOADER is not None:
        await PIXELDRAIN_UPLOADER.delete(file_id)


async def send_large_file(
    context: ContextTypes.DEFAULT_TYPE,
    status_message: Any,
    *,
    chat_id: int,
    item: DownloadedMedia,
    reply_to: int | None,
    request_id: str,
    bot_username: str | None = None,
    progress: ProgressReporter | None = None,
) -> None:
    # ── Try Pixeldrain upload (Direct Link) ───────────────────────────────────
    if PIXELDRAIN_UPLOADER is not None:
        try:
            if progress is not None:
                await progress.update(40, "☁️ در حال آپلود روی فضای ابری…", "", force=True)
            
            file_id = await PIXELDRAIN_UPLOADER.upload(item.path)
            # Direct link to Pixeldrain download page
            download_url = f"https://pixeldrain.com/u/{file_id}"

            async def send_pixeldrain_link() -> None:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=status_card(
                        "☁️ فایل آماده دانلوده!",
                        f"حجم: <b>{fmt_size(item.size)}</b>\n\n"
                        "روی دکمه زیر بزن تا فایل رو دانلود کنی:",
                        "لینک موقته و بعد از مدتی به‌صورت خودکار پاک می‌شه.",
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("☁️ دانلود فایل", url=download_url)]]
                    ),
                    reply_to_message_id=reply_to,
                    disable_web_page_preview=True,
                )

            with contextlib.suppress(TelegramError):
                await telegram_retry(send_pixeldrain_link)
            
            # Start delayed delete task
            asyncio.create_task(
                _pixeldrain_delayed_delete(file_id),
                name=f"pixeldrain-del-{file_id[:8]}",
            )
            return
        except Exception as exc:
            logger.warning("Pixeldrain upload failed; falling back to parts: %s", exc)

    # ── Fall back to part splitting ───────────────────────────────────────────
    total_parts = math.ceil(item.size / SETTINGS.max_file_size)
    for index in range(1, total_parts + 1):
        part = await asyncio.to_thread(create_file_part, item.path, SETTINGS.max_file_size, index)
        try:
            part_size = part.stat().st_size

            async def send_part() -> None:
                with part.open("rb") as file_handle:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=file_handle,
                        filename=part.name,
                        caption=(
                            f"✅ <b>{fmt_size(part_size)}</b> • بخش {index}/{total_parts}\n"
                            f"{bot_attribution(bot_username)}"
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to,
                    )

            if progress is None:
                await telegram_retry(send_part)
            else:
                await progress.upload(
                    lambda: telegram_retry(send_part),
                    size=part_size,
                    label=f"بخش {index}/{total_parts}",
                )
            await asyncio.sleep(0.3)
        finally:
            part.unlink(missing_ok=True)

    # ── Send joining guide for ALL file types ─────────────────────────────────
    extension = item.path.suffix.lower()
    if extension == ".mp4":
        result_note = "در پایان، فایل ویدیویی <b>MP4</b> دانلود می‌شه و می‌تونی مستقیم پخشش کنی. 🎬"
    elif extension == ".zip":
        result_note = (
            "در پایان، یک فایل <b>ZIP</b> دانلود می‌شه؛ بعدش می‌تونی خیلی راحت "
            "با فایل‌منیجر گوشی Extractش کنی و آهنگ‌ها رو برداری. 🎵 "
            "بعضی وقت‌ها فایل‌منیجر گوشی برای Extract کردن آهنگ‌ها ارور می‌ده؛ اگه ارور داد از برنامه ZArchiver استفاده کن."
        )
    elif extension in {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac"}:
        result_note = f"در پایان، فایل صوتی <b>{html_escape(extension)}</b> دانلود می‌شه. 🎵"
    else:
        ext_label = html_escape(extension) if extension else "اصلی"
        result_note = f"در پایان، فایل با پسوند <b>{ext_label}</b> دانلود می‌شه."

    guide = status_card(
        "🧩 حالا پارت‌ها رو به فایل اصلی تبدیل کن",
        f"همه‌ی <b>{total_parts}</b> پارت رو کامل داخل پوشه‌ی Downloads گوشیت ذخیره کن و اسمشون رو تغییر نده.\n\n"
        "بعد این کارها رو انجام بده:\n"
        "1️⃣ سایت زیر رو باز کن.\n"
        "2️⃣ همه‌ی پارت‌ها رو با هم انتخاب و Upload کن.\n"
        "3️⃣ وقتی آماده شد، دکمه‌ی <b>Save</b> رو بزن و فایل نهایی رو دانلود کن.\n\n"
        f"{result_note}",
        "تا وقتی همه‌ی پارت‌ها کامل دانلود نشده‌ن، فایل نهایی ساخته نمی‌شه.",
    )

    async def send_join_guide() -> None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=guide,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🧩 اتصال پارت‌ها و دانلود فایل", url="https://www.toolsley.com/split.html")]]
            ),
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
        )

    with contextlib.suppress(TelegramError):
        await telegram_retry(send_join_guide)


async def send_result_to_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_message: Any,
    result: GatewayResult,
    *,
    reply_to: int | None,
    request_id: str,
    quality: str | None = None,
    instagram_caption: str = "",
    progress: ProgressReporter | None = None,
) -> None:
    media = result.media
    if not media:
        raise RuntimeError("Ready result did not contain media")
    chat_id = update.effective_chat.id
    total_size = sum(item.size for item in media)
    bot_username = getattr(context.bot, "username", None)
    if progress is not None:
        await progress.begin_upload(total_size)
    else:
        await edit_status(
            status_message,
            status_card(
                "📤 آماده شد؛ دارم برات می‌فرستم…",
                f"تعداد: <b>{len(media)}</b> • حجم: <b>{fmt_size(total_size)}</b>",
            ),
        )

    async def run_upload(
        operation: Callable[[], Awaitable[Any]],
        size: int,
        label: str,
    ) -> Any:
        if progress is None:
            return await operation()
        return await progress.upload(operation, size=size, label=label)

    async def send_photo_item(item: DownloadedMedia, caption: str, label: str) -> None:
        async def send_photo() -> None:
            with item.path.open("rb") as file_handle:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=file_handle,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to,
                )

        async def send_document() -> None:
            with item.path.open("rb") as file_handle:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=file_handle,
                    filename=item.path.name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to,
                )

        async def send_with_fallback() -> None:
            try:
                await telegram_retry(send_photo)
            except TelegramError:
                await telegram_retry(send_document)

        await run_upload(send_with_fallback, item.size, label)

    if all(item.kind == MediaKind.PHOTO for item in media):
        if len(media) == 1:
            item = media[0]
            await send_photo_item(
                item,
                media_caption(item, quality, 1, 1, bot_username),
                "عکس",
            )
        else:
            group_count = math.ceil(len(media) / 10)
            for offset in range(0, len(media), 10):
                group = media[offset:offset + 10]
                if len(group) == 1:
                    item = group[0]
                    global_index = offset + 1
                    await send_photo_item(
                        item,
                        media_caption(item, quality, global_index, len(media), bot_username),
                        f"عکس {global_index}/{len(media)}",
                    )
                    continue

                async def send_group() -> None:
                    handles = [item.path.open("rb") for item in group]
                    try:
                        payload = []
                        for local_index, file_handle in enumerate(handles):
                            global_index = offset + local_index + 1
                            caption = media_caption(
                                group[local_index],
                                quality,
                                global_index,
                                len(media),
                                bot_username,
                            )
                            payload.append(
                                InputMediaPhoto(media=file_handle, caption=caption, parse_mode=ParseMode.HTML)
                            )
                        await context.bot.send_media_group(
                            chat_id=chat_id,
                            media=payload,
                            reply_to_message_id=reply_to,
                        )
                    finally:
                        for file_handle in handles:
                            file_handle.close()

                group_number = offset // 10 + 1
                label = f"پک {group_number}/{group_count} ({len(group)} عکس)"
                try:
                    await run_upload(
                        lambda: telegram_retry(send_group),
                        sum(item.size for item in group),
                        label,
                    )
                except TelegramError:
                    logger.info("Photo pack failed; sending its items separately")
                    for local_index, item in enumerate(group):
                        global_index = offset + local_index + 1
                        await send_photo_item(
                            item,
                            media_caption(item, quality, global_index, len(media), bot_username),
                            f"عکس {global_index}/{len(media)}",
                        )
    else:
        for index, item in enumerate(media, start=1):
            caption = media_caption(item, quality, index, len(media), bot_username)
            label = f"فایل {index}/{len(media)}" if len(media) > 1 else "فایل"
            if item.kind == MediaKind.PHOTO:
                await send_photo_item(item, caption, label)
            elif item.size <= SETTINGS.max_file_size:
                await send_regular_file(
                    context,
                    chat_id=chat_id,
                    item=item,
                    caption=caption,
                    reply_to=reply_to,
                    progress=progress,
                    upload_label=label,
                )
            else:
                await send_large_file(
                    context,
                    status_message,
                    chat_id=chat_id,
                    item=item,
                    reply_to=reply_to,
                    request_id=request_id,
                    bot_username=bot_username,
                    progress=progress,
                )

    if instagram_caption:
        await send_long_text(
            context,
            chat_id,
            "📝 کپشن اینستاگرام",
            instagram_caption,
            reply_to,
        )
    STATS.successful += 1
    STATS.bytes_sent += total_size
    quality_text = f" • کیفیت: {quality}" if quality else ""
    if progress is not None:
        total_elapsed = time.monotonic() - progress.started_at
        await progress.update(
            100,
            "✅ تموم شد؛ نوش جونت!",
            (
                f"{len(media)} فایل • {fmt_size(total_size)}{quality_text}\n"
                f"⏱ زمان کل دانلود و ارسال: {fmt_duration(total_elapsed)}"
            ),
            force=True,
        )
    else:
        quality_line = f"\nکیفیت: <b>{html_escape(quality)}</b>" if quality else ""
        await edit_status(
            status_message,
            status_card(
                "✅ تموم شد؛ نوش جونت!",
                f"تعداد فایل: <b>{len(media)}</b> • حجم کل: <b>{fmt_size(total_size)}</b>{quality_line}",
            ),
        )


def failure_text(reasons: list[str], request_id: str) -> str:
    if "too_large" in reasons:
        body = "حجم این محتوا فعلاً قابل پردازش نبود."
    elif "ad_required" in reasons:
        body = "این لینک از مسیر فعلی قابل دریافت نبود و تلاش‌های بعدی هم نتیجه نداد."
    elif reasons and all(reason == "cooldown" for reason in reasons):
        body = "مسیر دانلود موقتاً شلوغه؛ چند دقیقه دیگه دوباره امتحانش کن."
    elif "timeout" in reasons:
        body = "آماده‌کردن این لینک بیشتر از حد معمول طول کشید؛ یک بار دیگه بفرستش."
    elif "service_rejected" in reasons:
        body = "احتمالاً لینک خصوصی، حذف‌شده یا محدود شده و نمی‌تونم به محتواش برسم."
    else:
        body = "نتونستم از این لینک فایل سالمی بگیرم؛ لینک رو چک کن و دوباره بفرست."
    return status_card(
        "😕 این یکی دانلود نشد",
        body,
    )


def _selection_is_owned(session: PendingSelection, update: Update) -> bool:
    return (
        update.effective_chat is not None
        and update.effective_user is not None
        and session.chat_id == update.effective_chat.id
        and session.user_id == update.effective_user.id
    )


def release_pending_selection(session: PendingSelection) -> None:
    current = PENDING_SELECTIONS.get(session.token)
    if current is session:
        PENDING_SELECTIONS.pop(session.token, None)
    if session.caption_task is not None and not session.caption_task.done():
        session.caption_task.cancel()
    ACCOUNT_POOL.release(session.lease)
    cleanup_request_directory(session.attempt_directory, SETTINGS.download_root)


async def get_session_instagram_caption(session: PendingSelection) -> str:
    if session.instagram_caption:
        return session.instagram_caption
    if session.caption_task is not None:
        try:
            session.instagram_caption = await session.caption_task
        except asyncio.CancelledError:
            session.instagram_caption = ""
        finally:
            session.caption_task = None
    if not session.instagram_caption:
        session.instagram_caption = await scrape_instagram_caption(session.source_url)
    return session.instagram_caption


async def expire_stale_selections(application: Application) -> None:
    now = time.monotonic()
    stale = [
        session
        for session in PENDING_SELECTIONS.values()
        if not session.processing and now - session.created_at >= SETTINGS.selection_ttl
    ]
    for session in stale:
        release_pending_selection(session)
        with contextlib.suppress(TelegramError):
            await application.bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.status_message_id,
                text=status_card("⌛ زمان انتخاب تمام شد", "لینک را دوباره بفرست تا گزینه‌های تازه دریافت شوند."),
                parse_mode=ParseMode.HTML,
            )


async def selection_reaper(application: Application) -> None:
    try:
        while True:
            await asyncio.sleep(min(30.0, max(5.0, SETTINGS.selection_ttl / 4)))
            await expire_stale_selections(application)
            prune_youtube_search_sessions()
    except asyncio.CancelledError:
        return


async def _process_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    reply_to: int | None,
) -> None:
    request_id = uuid.uuid4().hex[:10]
    host = source_host(url)
    chat_id = update.effective_chat.id
    STATS.requests += 1
    platform = detect_platform(url)
    if platform is None:
        STATS.failed += 1
        await send_status(
            context,
            chat_id,
            status_card(
                "🙃 این لینک رو نمی‌شناسم",
                "فعلاً لینک‌های اینستاگرام، یوتیوب، تیک‌تاک، توییتر/X، فیسبوک، VK، اسپاتیفای و ساوندکلاد رو بفرست.",
            ),
            reply_to,
        )
        return
    spotify_collection_type = spotify_resource_type(url)
    is_spotify_collection = platform == Platform.SPOTIFY and spotify_collection_type in {"album", "playlist"}
    normal_providers = () if is_spotify_collection else providers_for_platform(platform, SETTINGS)
    providers = tuple(dict.fromkeys((*normal_providers, *all_providers(SETTINGS))))
    if not providers:
        STATS.failed += 1
        return
    info = platform_info(platform)
    if not is_spotify_collection and ACCOUNT_POOL.queue_length >= SETTINGS.max_queue_size:
        STATS.failed += 1
        await send_status(
            context,
            chat_id,
            status_card("⏳ یکم شلوغه", "صف فعلاً پر شده؛ چند لحظه دیگه دوباره امتحان کن."),
            reply_to,
        )
        return
    if not is_spotify_collection and ACCOUNT_POOL.total == 0:
        STATS.failed += 1
        await send_status(
            context,
            chat_id,
            status_card(
                "🛠 بخش دانلود موقتاً آماده نیست",
                "دارم آماده‌اش می‌کنم؛ لطفاً کمی بعد دوباره امتحان کن.",
            ),
            reply_to,
        )
        return

    queued = ACCOUNT_POOL.total > 0 and ACCOUNT_POOL.busy_count >= ACCOUNT_POOL.total
    initial = status_card(
        "⏳ لینک رفت توی صف" if queued else "🔎 بذار لینکت رو بررسی کنم…",
        (
                f"{info.icon} {info.label} • <code>{html_escape(host)}</code>\n"
            + (f"نوبت تقریبی: <b>{ACCOUNT_POOL.queue_length + 1}</b>" if queued else "دارم بهترین خروجی رو برات آماده می‌کنم…")
        ),
        f"برای توقف: /cancel"
    )
    status_message = await send_status(context, chat_id, initial, reply_to)
    progress = ProgressReporter(status_message, request_id)
    caption_task: asyncio.Task[str] | None = None
    if platform == Platform.INSTAGRAM:
        caption_task = asyncio.create_task(
            scrape_instagram_caption(url),
            name=f"instagram-caption-{request_id}",
        )

    lease: WorkerLease | None = None
    attempt_directory: Path | None = None
    hold_lease = False
    reasons: list[str] = []
    try:
        if is_spotify_collection:
            collection_label = "آلبوم" if spotify_collection_type == "album" else "پلی‌لیست"

            # ── Attempt 1: Dr_downloader_bot (needs a Telethon account) ──────
            if ACCOUNT_POOL.total > 0 and ACCOUNT_POOL.queue_length < SETTINGS.max_queue_size:
                dr_lease: WorkerLease | None = None
                dr_dir: Path | None = None
                try:
                    dr_lease = await asyncio.wait_for(ACCOUNT_POOL.acquire(), timeout=15.0)
                    dr_dir = create_attempt_directory(
                        SETTINGS.download_root, request_id, f"dr-{spotify_collection_type}"
                    )
                    await progress.update(
                        5,
                        f"🎵 دارم {collection_label} Spotify رو آماده می‌کنم…",
                        "دریافت آهنگ‌ها…",
                        force=True,
                    )
                    tracks_media = await request_dr_downloader_album(
                        dr_lease.worker.client,
                        SETTINGS.spotify_collection_primary_bot,
                        url,
                        dr_dir,
                        wait_timeout=SETTINGS.wait_timeout,
                        track_timeout=SETTINGS.wait_timeout,
                        max_download_size=SETTINGS.max_download_size,
                    )
                    ACCOUNT_POOL.release(dr_lease)
                    dr_lease = None
                    await progress.update(
                        82,
                        f"🎵 دارم {collection_label} Spotify رو آماده می‌کنم…",
                        "ساخت فایل ZIP…",
                        force=True,
                    )
                    zip_path = dr_dir / f"{collection_label}.zip"
                    await asyncio.to_thread(_zip_tracks, [m.path for m in tracks_media], zip_path)
                    dr_media = DownloadedMedia(
                        path=zip_path,
                        kind=MediaKind.DOCUMENT,
                        source_message_id=0,
                        mime_type="application/zip",
                        size=zip_path.stat().st_size,
                    )
                    await send_result_to_user(
                        update,
                        context,
                        status_message,
                        GatewayResult(status="ready", bot_username="", media=(dr_media,)),
                        reply_to=reply_to,
                        request_id=request_id,
                        progress=progress,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Dr_downloader_bot collection failed for %s: %s", request_id, exc
                    )
                    if dr_lease is not None:
                        ACCOUNT_POOL.release(dr_lease)
                    if dr_dir is not None:
                        cleanup_request_directory(dr_dir, SETTINGS.download_root)

            # ── Attempt 2: Spotisaver website (no Telethon needed) ────────────
            attempt_directory = create_attempt_directory(
                SETTINGS.download_root,
                request_id,
                f"spotisaver-{spotify_collection_type}",
            )

            async def collection_progress(percent: int, detail: str) -> None:
                safe_detail = detail.replace("Spotisaver", "منبع آهنگ")
                await progress.update(
                    5 + int(percent * 0.65),
                    f"🎵 دارم {collection_label} Spotify رو آماده می‌کنم…",
                    safe_detail,
                    force=percent in {2, 90, 100},
                )

            try:
                collection = await SpotisaverAlbumDownloader(
                    proxy_url=_caption_proxy_url(),
                ).download_collection(url, attempt_directory, progress=collection_progress)
                collection_media = DownloadedMedia(
                    path=collection.path,
                    kind=MediaKind.DOCUMENT,
                    source_message_id=0,
                    mime_type="application/zip",
                    size=collection.path.stat().st_size,
                )
                await send_result_to_user(
                    update,
                    context,
                    status_message,
                    GatewayResult(status="ready", bot_username="", media=(collection_media,)),
                    reply_to=reply_to,
                    request_id=request_id,
                    progress=progress,
                )
                if collection.failed_tracks:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=status_card(
                            "⚠️ چند ترک پیدا نشد",
                            f"{collection.downloaded_tracks} از {collection.total_tracks} ترک داخل ZIP قرار گرفت.",
                            "بقیه‌ی ترک‌ها قابل دریافت نبودند.",
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to,
                    )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Spotisaver %s failed for %s: %s", spotify_collection_type, request_id, exc)
                reasons.append("spotisaver_error")
                cleanup_request_directory(attempt_directory, SETTINGS.download_root)
                attempt_directory = None

        if ACCOUNT_POOL.total == 0:
            raise PoolUnavailable("No Telegram fallback account is connected")
        if ACCOUNT_POOL.queue_length >= SETTINGS.max_queue_size:
            raise PoolUnavailable("Fallback queue is full")
        lease = await asyncio.wait_for(
            ACCOUNT_POOL.acquire(),
            timeout=SETTINGS.worker_acquire_timeout,
        )
        for index, bot_username in enumerate(providers, start=1):
            phase = "مسیر اصلی" if index <= len(normal_providers) else "مسیر جایگزین"
            await progress.update(
                10,
                "🔄 دارم محتوا رو آماده می‌کنم…",
                f"{info.icon} {info.label} • {phase} {index}/{len(providers)}",
                force=True,
            )
            attempt_directory = create_attempt_directory(
                SETTINGS.download_root,
                request_id,
                f"{index}-{bot_username}",
            )
            result = await GATEWAY.request(
                client=lease.worker.client,
                worker_name=lease.worker.name,
                bot_username=bot_username,
                url=url,
                attempt_directory=attempt_directory,
                progress_callback=progress.download,
            )
            if result.status == "ready":
                instagram_caption = await caption_task if caption_task is not None else ""
                caption_task = None
                await send_result_to_user(
                    update,
                    context,
                    status_message,
                    result,
                    reply_to=reply_to,
                    request_id=request_id,
                    instagram_caption=instagram_caption,
                    progress=progress,
                )
                if platform == Platform.INSTAGRAM and is_instagram_reel(url):
                    reel_token = uuid.uuid4().hex[:12]
                    REEL_MUSIC_URLS[reel_token] = (url, time.monotonic(), chat_id, update.effective_user.id)
                    with contextlib.suppress(TelegramError):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=status_card(
                                "🎵 موزیک این ریلز رو می‌خوای؟",
                                "دکمه زیر رو بزن تا آهنگ برات استخراج و ارسال بشه.",
                            ),
                            parse_mode=ParseMode.HTML,
                            reply_to_message_id=reply_to,
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("🎵 دریافت موزیک ریلز", callback_data=f"reel_music:{reel_token}")
                            ]]),
                        )
                return
            if result.status == "needs_selection":
                displayed_options = tuple(
                    option
                    for option in result.options
                    if not (platform == Platform.INSTAGRAM and option.action == "caption")
                )[:24]
                token = uuid.uuid4().hex[:12]
                rows: list[list[InlineKeyboardButton]] = []
                video_choices = [
                    (option_index, option)
                    for option_index, option in enumerate(displayed_options)
                    if option.action == "media" and option.expected_kind == MediaKind.VIDEO
                ]
                audio_choices = [
                    (option_index, option)
                    for option_index, option in enumerate(displayed_options)
                    if option.action == "media" and option.expected_kind == MediaKind.AUDIO
                ]
                quick_row: list[InlineKeyboardButton] = []
                if video_choices:
                    best_index, _ = max(
                        video_choices,
                        key=lambda item: item[1].expected_height or 0,
                    )
                    quick_row.append(
                        InlineKeyboardButton("⭐ بهترین کیفیت", callback_data=f"sel:{token}:{best_index}")
                    )
                if audio_choices:
                    audio_index, _ = max(
                        audio_choices,
                        key=lambda item: item[1].expected_bitrate_kbps or 0,
                    )
                    quick_row.append(
                        InlineKeyboardButton("🎵 فقط صدا", callback_data=f"sel:{token}:{audio_index}")
                    )
                if quick_row:
                    rows.append(quick_row)
                row: list[InlineKeyboardButton] = []
                for option_index, option in enumerate(displayed_options):
                    if option.action == "caption":
                        label = "📝 دریافت کپشن"
                    else:
                        prefix = "🎵" if option.expected_kind == MediaKind.AUDIO else "🎬"
                        raw_label = option.label if len(option.label) <= 40 else option.label[:37] + "…"
                        label = f"{prefix} {raw_label}"
                    row.append(
                        InlineKeyboardButton(label, callback_data=f"sel:{token}:{option_index}")
                    )
                    if len(row) == 2:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                if platform == Platform.INSTAGRAM:
                    rows.append(
                        [
                            InlineKeyboardButton(
                                "📝 کپشن این پست",
                                callback_data=f"caption:{token}",
                            )
                        ]
                    )
                    if is_instagram_reel(url):
                        REEL_MUSIC_URLS[token] = (url, time.monotonic(), chat_id, update.effective_user.id)
                        rows.append(
                            [
                                InlineKeyboardButton(
                                    "🎵 موزیک ریلز",
                                    callback_data=f"reel_music:{token}",
                                )
                            ]
                        )
                elif not any(option.action == "caption" for option in displayed_options) and result.text:
                    rows.append(
                        [InlineKeyboardButton("📝 متن/اطلاعات پست", callback_data=f"info:{token}")]
                    )
                rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                menu_text = status_card(
                    "🎚 کدوم خروجی رو می‌خوای؟",
                    f"منبع: <code>{html_escape(host)}</code>\nکیفیت، صدا یا کپشن رو انتخاب کن.",
                    f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه وقت داری"
                )
                if result.preview is not None:
                    try:
                        with result.preview.path.open("rb") as preview_handle:
                            await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=preview_handle,
                                caption=f"🖼 پیش‌نمایش ویدیو • {host}",
                                reply_to_message_id=reply_to,
                            )
                        with contextlib.suppress(TelegramError):
                            await status_message.delete()
                        status_message = await send_status(context, chat_id, menu_text, None)
                    except TelegramError as exc:
                        logger.warning("Thumbnail send failed for %s: %s", request_id, exc)
                session = PendingSelection(
                    token=token,
                    created_at=time.monotonic(),
                    chat_id=chat_id,
                    user_id=update.effective_user.id,
                    status_message_id=status_message.message_id,
                    reply_to=reply_to,
                    request_id=request_id,
                    source_host=host,
                    source_url=url,
                    platform=platform,
                    bot_username=bot_username,
                    request_message_id=int(result.request_message_id or 0),
                    menu_message_id=int(result.menu_message_id or 0),
                    options=displayed_options,
                    lease=lease,
                    attempt_directory=attempt_directory,
                    fallback_text=result.text if platform != Platform.INSTAGRAM else "",
                    caption_task=caption_task,
                )
                caption_task = None
                PENDING_SELECTIONS[token] = session
                await edit_status(status_message, menu_text, InlineKeyboardMarkup(rows))
                hold_lease = True
                return

            reasons.append(result.reason or "service_error")
            cleanup_request_directory(attempt_directory, SETTINGS.download_root)
            attempt_directory = None

        STATS.failed += 1
        await edit_status(status_message, failure_text(reasons, request_id))
    except (PoolUnavailable, asyncio.TimeoutError):
        STATS.failed += 1
        await edit_status(
            status_message,
            status_card("🛠 بخش دانلود آماده نیست", "لطفاً کمی بعد دوباره امتحان کن."
        ))
    except asyncio.CancelledError:
        await edit_status(
            status_message,
            status_card("⏹ متوقف شد", "دانلود این لینک رو لغو کردم."
        ))
        raise
    except Exception as exc:
        STATS.failed += 1
        logger.exception("Request %s failed: %s", request_id, exc)
        await edit_status(
            status_message,
            status_card(
                "😕 یه مشکلی پیش اومد",
                "نگران نباش؛ فایل ناقصی نفرستادم. لینک رو یک بار دیگه امتحان کن.",

            ),
        )
    finally:
        if caption_task is not None:
            if not caption_task.done():
                caption_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await caption_task
        if not hold_lease:
            if attempt_directory is not None:
                cleanup_request_directory(attempt_directory, SETTINGS.download_root)
            if lease is not None:
                ACCOUNT_POOL.release(lease)


def request_owner_key(update: Update) -> tuple[int, int]:
    return update.effective_chat.id, update.effective_user.id


async def process_urls(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    urls: tuple[str, ...],
    reply_to: int | None,
) -> bool:
    key = request_owner_key(update)
    current_task = asyncio.current_task()
    if current_task is None:
        return False
    active = ACTIVE_REQUESTS.setdefault(key, set())
    pending_count = sum(
        session.chat_id == key[0] and session.user_id == key[1]
        for session in PENDING_SELECTIONS.values()
    )
    if len(active) >= MAX_ACTIVE_TASKS_PER_USER or pending_count >= 1:
        await update.effective_message.reply_text(
            status_card(
                "⏳ درخواست‌های قبلی هنوز فعال‌اند",
                "صبر کن یا با /cancel درخواست‌های قبلی را متوقف کن.",
            ),
            parse_mode=ParseMode.HTML,
        )
        return False
    selected_urls = urls[: SETTINGS.max_links_per_message]
    if not allow_requests(key, len(selected_urls)):
        await update.effective_message.reply_text(
            status_card(
                "⏱ کمی آهسته‌تر",
                f"حداکثر {SETTINGS.rate_limit_requests} لینک در {int(SETTINGS.rate_limit_window)} ثانیه قابل پردازش است.",
            ),
            parse_mode=ParseMode.HTML,
        )
        return False
    active.add(current_task)
    try:
        if len(urls) > len(selected_urls):
            await update.effective_message.reply_text(
                f"فقط {SETTINGS.max_links_per_message} لینک اول این پیام پردازش می‌شود.",
            )
        for url in selected_urls:
            await _process_url(update, context, url, reply_to)
    finally:
        active.discard(current_task)
        if not active:
            ACTIVE_REQUESTS.pop(key, None)
    return True


async def run_youtube_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_query: str,
) -> None:
    message = update.effective_message
    key = request_owner_key(update)
    try:
        query_text = normalize_search_query(raw_query)
    except YouTubeSearchError:
        await message.reply_text(
            status_card(
                "🔎 چی رو جست‌وجو کنم؟",
                "عبارت موردنظرت رو بنویس؛ مثلاً <code>/search موسیقی بی‌کلام</code>.",
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    if key in ACTIVE_YOUTUBE_SEARCHES:
        await message.reply_text(
            status_card("⏳ جست‌وجوی قبلی هنوز ادامه دارد", "چند لحظه صبر کن تا نتیجه‌ها آماده شوند."),
            parse_mode=ParseMode.HTML,
        )
        return
    if not allow_youtube_search(key):
        await message.reply_text(
            status_card("⏱ کمی آهسته‌تر", "تا یک دقیقهٔ دیگر دوباره جست‌وجو کن."),
            parse_mode=ParseMode.HTML,
        )
        return

    prune_youtube_search_sessions()
    MEMBERSHIP_CACHE[update.effective_user.id] = max(
        MEMBERSHIP_CACHE.get(update.effective_user.id, 0),
        time.monotonic() + YOUTUBE_SEARCH_TTL,
    )
    ACTIVE_YOUTUBE_SEARCHES.add(key)
    status_message: Any | None = None
    session: YouTubeSearchSession | None = None
    try:
        status_message = await send_status(
            context,
            update.effective_chat.id,
            status_card(
                "🔎 دارم توی YouTube می‌گردم…",
                f"عبارت: <b>{html_escape(query_text)}</b>\n۳۰ نتیجهٔ مرتبط رو مرتب می‌کنم.",
            ),
            message.message_id,
        )
        results = await YOUTUBE_SEARCH.search(query_text)
        token = uuid.uuid4().hex[:12]
        session = YouTubeSearchSession(
            token=token,
            created_at=time.monotonic(),
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id,
            reply_to=message.message_id,
            query=query_text,
            results=results[:YOUTUBE_SEARCH_MAX_RESULTS],
        )
        page_image = await YOUTUBE_SEARCH.build_page_image(session.results, 0)
        YOUTUBE_SEARCH_SESSIONS[token] = session
        prune_youtube_search_sessions()

        async def send_results() -> Any:
            return await context.bot.send_photo(
                chat_id=session.chat_id,
                photo=page_image,
                filename=f"youtube-search-{token}-1.jpg",
                caption=youtube_search_caption(session, 0),
                parse_mode=ParseMode.HTML,
                reply_markup=youtube_search_keyboard(session, 0),
                reply_to_message_id=session.reply_to,
            )

        await telegram_retry(send_results)
        with contextlib.suppress(TelegramError, AttributeError):
            await status_message.delete()
    except YouTubeSearchError as exc:
        logger.info("YouTube search unavailable: %s", exc)
        if status_message is not None:
            await edit_status(
                status_message,
                status_card(
                    "😕 نتیجه‌ای آماده نشد",
                    "جست‌وجوی YouTube موقتاً پاسخ نداد یا نتیجه‌ای پیدا نشد.",
                    "عبارت دیگری را امتحان کن.",
                ),
            )
    except TelegramError as exc:
        logger.warning("YouTube search result could not be sent: %s", exc)
        if session is not None:
            YOUTUBE_SEARCH_SESSIONS.pop(session.token, None)
        if status_message is not None:
            await edit_status(
                status_message,
                status_card("😕 ارسال نتیجه‌ها ناموفق بود", "چند لحظهٔ دیگر دوباره امتحان کن."),
            )
    except Exception as exc:
        logger.exception("Unexpected YouTube search failure: %s", exc)
        if session is not None:
            YOUTUBE_SEARCH_SESSIONS.pop(session.token, None)
        if status_message is not None:
            await edit_status(
                status_message,
                status_card("😕 جست‌وجو انجام نشد", "چند لحظهٔ دیگر دوباره امتحان کن."),
            )
    finally:
        ACTIVE_YOUTUBE_SEARCHES.discard(key)


@membership_required
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_text = " ".join(context.args or ())
    if not query_text and update.effective_message.reply_to_message is not None:
        replied = update.effective_message.reply_to_message
        query_text = replied.text or replied.caption or ""
    await run_youtube_search(update, context, query_text)


@membership_required
async def on_youtube_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    token = parts[1] if len(parts) > 1 else ""
    prune_youtube_search_sessions()
    session = YOUTUBE_SEARCH_SESSIONS.get(token)
    if session is None:
        await query.answer("این جست‌وجو منقضی شده؛ دوباره جست‌وجو کن.", show_alert=True)
        return
    if session.chat_id != update.effective_chat.id or session.user_id != update.effective_user.id:
        await query.answer("این نتیجه‌ها متعلق به شما نیست.", show_alert=True)
        return
    if session.selected:
        await query.answer("این محتوا قبلاً انتخاب شده است.", show_alert=True)
        return
    try:
        value = int(parts[2])
    except (IndexError, TypeError, ValueError):
        await query.answer("دکمه معتبر نیست.", show_alert=True)
        return

    if data.startswith("ys:"):
        if session.busy:
            with contextlib.suppress(TelegramError):
                await query.answer("صبر کن صفحه کامل آماده شود.")
            return
        if value < 0 or value >= len(session.results):
            await query.answer("این محتوا در دسترس نیست.", show_alert=True)
            return
        session.selected = True
        selected = session.results[value]
        selected_url = normalize_url(selected.url)
        if selected_url is None or detect_platform(selected_url) != Platform.YOUTUBE:
            session.selected = False
            await query.answer("لینک این محتوا معتبر نیست.", show_alert=True)
            return
        with contextlib.suppress(TelegramError):
            await query.answer(f"محتوا {persian_number(value + 1)} انتخاب شد؛ بررسی می‌کنم 🚀")
        try:
            accepted = await process_urls(update, context, (selected_url,), session.reply_to)
        except BaseException:
            session.selected = False
            raise
        if accepted:
            YOUTUBE_SEARCH_SESSIONS.pop(token, None)
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
        else:
            session.selected = False
        return

    if not data.startswith("yp:"):
        await query.answer()
        return
    page_count = youtube_search_page_count(session)
    if value < 0 or value >= page_count:
        await query.answer("این صفحه وجود ندارد.", show_alert=True)
        return
    if session.busy:
        await query.answer("دارم صفحه را آماده می‌کنم…")
        return
    if value == session.current_page:
        await query.answer()
        return

    session.busy = True
    try:
        with contextlib.suppress(TelegramError):
            await query.answer("دارم صفحه را می‌سازم…")
        page_image = await YOUTUBE_SEARCH.build_page_image(session.results, value)
        if YOUTUBE_SEARCH_SESSIONS.get(token) is not session or session.selected:
            return
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=page_image,
                filename=f"youtube-search-{token}-{value + 1}.jpg",
                caption=youtube_search_caption(session, value),
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=youtube_search_keyboard(session, value),
        )
        session.current_page = value
    except (YouTubeSearchError, TelegramError) as exc:
        logger.warning("YouTube search page %d failed: %s", value, exc)
        with contextlib.suppress(TelegramError):
            await context.bot.send_message(
                chat_id=session.chat_id,
                text=status_card("😕 صفحه آماده نشد", "دوباره روی دکمهٔ صفحه بزن."),
                reply_to_message_id=session.reply_to,
                parse_mode=ParseMode.HTML,
            )
    finally:
        session.busy = False


WELCOME_PRIVATE = status_card(
    "✨ سلام! من MZ Downloaderم",
    "لینکت رو بفرست، بقیه‌ش با من 😎\n\n"
    "🎬 ویدیو با انتخاب کیفیت\n"
    "🔎 جست‌وجوی YouTube و نمایش ۳۰ نتیجه\n"
    "🎧 آهنگ تکی، آلبوم و پلی‌لیست ZIP\n"
    "📸 کپشن پست‌های Instagram\n"
    "🖼 پیش‌نمایش، فقط صدا و دانلود چندتایی\n"
    "📦 ارسال مرتب عکس‌ها به‌صورت پک",
    "برای جست‌وجو عبارت را بنویس یا /search را بزن • راهنما: /help",
)

WELCOME_GROUP = status_card(
    "👋 MZ Downloader اومد توی گروه!",
    "برای دانلود <code>/dl لینک</code> رو بفرست؛ یا روی پیام لینک‌دار ریپلای کن و <code>/dl</code> بزن.",
    "برای خلوت موندن گروه، لینک‌های عادی خودکار دانلود نمی‌شن.",
)


@membership_required
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and update.effective_chat.type == ChatType.PRIVATE:
        users_db.register(user.id)
    text = WELCOME_PRIVATE if update.effective_chat.type == ChatType.PRIVATE else WELCOME_GROUP
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def _is_admin(user: Any) -> bool:
    return bool(user and (user.username or "").lower() == ADMIN_USERNAME.lower())


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message to all registered users. Admin-only."""
    if not _is_admin(update.effective_user):
        await update.effective_message.reply_text(
            status_card("⛔ دسترسی ممنوع", "این دستور فقط برای ادمین ربات قابل استفاده است."),
            parse_mode=ParseMode.HTML,
        )
        return

    # Use the full text from the message to preserve newlines, excluding the command itself
    raw_text = update.effective_message.text or ""
    parts = raw_text.split(None, 1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await update.effective_message.reply_text(
            status_card(
                "📢 ارسال پیام همگانی",
                "متن پیام را بعد از دستور بنویس:\n<code>/broadcast متن پیام</code>",
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    all_ids = users_db.all_user_ids()
    total = len(all_ids)
    status_msg = await update.effective_message.reply_text(
        status_card("📢 در حال ارسال…", f"تعداد گیرنده: <b>{total}</b> نفر"),
        parse_mode=ParseMode.HTML,
    )

    sent = 0
    failed = 0
    for uid in all_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except TelegramError:
            failed += 1

    await edit_status(
        status_msg,
        status_card(
            "✅ ارسال پیام همگانی تموم شد",
            f"ارسال‌شده: <b>{sent}</b>\nناموفق: <b>{failed}</b>",
        ),
    )


async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually register one or more user IDs. Admin-only.

    Usage:  /adduser 123456789 987654321 …
    After adding, the bot prints the full KNOWN_USERS string so you can
    copy it into the Railway Variable to survive the next redeploy.
    """
    if not _is_admin(update.effective_user):
        await update.effective_message.reply_text(
            status_card("⛔ دسترسی ممنوع", "این دستور فقط برای ادمین ربات قابل استفاده است."),
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            status_card(
                "➕ افزودن کاربر دستی",
                "آی‌دی عددی کاربر(ان) را بعد از دستور بنویس:\n"
                "<code>/adduser 123456789</code>\n"
                "<code>/adduser 123456789 987654321</code>",
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    added_ids: list[int] = []
    invalid: list[str] = []
    for arg in context.args:
        if arg.lstrip("-").isdigit():
            uid = int(arg)
            if users_db.register(uid):
                added_ids.append(uid)
        else:
            invalid.append(arg)

    lines: list[str] = []
    if added_ids:
        lines.append(f"اضافه‌شده: <b>{len(added_ids)}</b> کاربر")
    if invalid:
        lines.append(f"نامعتبر (نادیده گرفته شد): {', '.join(html_escape(v) for v in invalid)}")

    total = users_db.count()
    known_users_str = users_db.all_user_ids_str()
    lines.append(f"\nمجموع کاربران ذخیره‌شده: <b>{total}</b>")
    lines.append(
        f"\nبرای پایداری بعد از ری‌دیپلوی Railway، این مقدار را در متغیر "
        f"<code>KNOWN_USERS</code> ست کن:\n"
        f"<code>{html_escape(known_users_str)}</code>"
    )

    await update.effective_message.reply_text(
        status_card("✅ ثبت کاربر دستی", "\n".join(lines)),
        parse_mode=ParseMode.HTML,
    )


@membership_required
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    max_mb = SETTINGS.max_file_size // (1024 * 1024)
    text = status_card(
        "🧭 خیلی راحت دانلود کن",
        "<b>چت خصوصی</b>\n"
        f"تا {SETTINGS.max_links_per_message} لینک را در یک پیام بفرست.\n"
        "برای جست‌وجوی YouTube فقط عبارت را بنویس.\n\n"
        "<b>گروه</b>\n"
        "• <code>/dl لینک</code>\n"
        "• ریپلای روی پیام لینک‌دار و ارسال <code>/dl</code>\n"
        "• منشن ربات در کنار لینک\n\n"
        "<b>فرمان‌ها</b>\n"
        "<code>/search عبارت</code> جست‌وجوی YouTube\n"
        "<code>/cancel</code> توقف دانلودهای خودت\n"
        "<code>/status</code> وضعیت صف\n"
        "<code>/platforms</code> پلتفرم‌های قابل استفاده\n"
        "<code>/stats</code> آمار همین اجرا\n\n"
        "<code>/caption لینک</code> فقط کپشن Instagram رو می‌فرسته.\n\n"
        "توی منوی ویدیو هم می‌تونی بهترین کیفیت، فقط صدا یا کپشن رو انتخاب کنی.\n\n"
        f"فایل‌های بزرگ‌تر از {max_mb} مگابایت به بخش‌های <code>.part</code> تقسیم می‌شن.",
        "محدودیت داخلی حجم دانلود خاموشه؛ محدودیت‌های فنی Telegram و فضای سرور همچنان وجود دارن.",
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


@membership_required
async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    urls = extract_urls_from_message(message)
    replied = message.reply_to_message
    if not urls and replied:
        urls = extract_urls_from_message(replied)
    instagram_url = next((url for url in urls if detect_platform(url) == Platform.INSTAGRAM), None)
    if instagram_url is None:
        await message.reply_text(
            status_card(
                "لینک Instagram پیدا نشد",
                "بعد از <code>/caption</code> لینک یک پست، Reel یا IGTV عمومی را بفرست.",
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    status_message = await message.reply_text(
        status_card("📝 یه لحظه، کپشنش رو پیدا کنم…", "دارم متن اصلی پست رو می‌گیرم."),
        parse_mode=ParseMode.HTML,
    )
    caption = await scrape_instagram_caption(instagram_url)
    if not caption:
        await edit_status(
            status_message,
            status_card(
                "کپشن در دسترس نیست",
                "پست خصوصی، حذف‌شده یا موقتاً توسط Instagram محدود شده است.",
                "کمی بعد دوباره امتحان کن.",
            ),
        )
        return
    await send_long_text(
        context,
        update.effective_chat.id,
        "📝 کپشن اینستاگرام",
        caption,
        replied.message_id if replied else message.message_id,
    )
    await edit_status(status_message, status_card("✅ کپشن رو فرستادم", "همون متن اصلی پست، تمیز و آماده‌ست."))


@membership_required
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active_jobs = sum(len(tasks) for tasks in ACTIVE_REQUESTS.values())
    text = status_card(
        "وضعیت ربات",
        f"{pool_status_line()}\n"
        f"درخواست فعال: <b>{active_jobs}</b>\n"
        f"منتظر انتخاب کیفیت: <b>{len(PENDING_SELECTIONS)}</b>\n"
        f"مسیرهای محافظتی موقت: <b>{COOLDOWNS.active_count()}</b>\n"
        f"زمان فعالیت: <b>{int((time.monotonic() - STARTED_AT) // 60)}</b> دقیقه",
        "حالت محافظتی پس از timeout مانع پذیرش پاسخ دیرهنگام می‌شود.",
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


@membership_required
async def services_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = status_card(
        "🌐 از اینجاها دانلود می‌کنم",
        "📸 Instagram  •  ▶️ YouTube  •  🎵 TikTok\n"
        "𝕏 Twitter/X  •  📘 Facebook  •  🔵 VK\n"
        "🟢 Spotify  •  ☁️ SoundCloud\n\n"
        "فقط کافیه لینک رو بفرستی تا بهترین خروجی ممکن رو آماده کنم.",
        "لینک خصوصی، حذف‌شده یا محدودشده ممکنه قابل دریافت نباشه.",
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


@membership_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    success_rate = (STATS.successful / STATS.requests * 100) if STATS.requests else 0
    text = status_card(
        "📊 آمار این اجرا",
        f"درخواست‌ها: <b>{STATS.requests}</b>\n"
        f"موفق: <b>{STATS.successful}</b>\n"
        f"ناموفق: <b>{STATS.failed}</b>\n"
        f"نرخ موفقیت: <b>{success_rate:.0f}%</b>\n"
        f"حجم ارسال‌شده: <b>{fmt_size(STATS.bytes_sent)}</b>",
        "این آمار با هر راه‌اندازی مجدد صفر می‌شود.",
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


@membership_required
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = request_owner_key(update)
    tasks = [task for task in ACTIVE_REQUESTS.get(key, set()) if not task.done()]
    selection_count = 0
    for session in list(PENDING_SELECTIONS.values()):
        if session.chat_id != key[0] or session.user_id != key[1]:
            continue
        selection_count += 1
        if session.processing_task and not session.processing_task.done():
            session.processing_task.cancel()
        else:
            release_pending_selection(session)
            with contextlib.suppress(TelegramError):
                await context.bot.edit_message_text(
                    chat_id=session.chat_id,
                    message_id=session.status_message_id,
                    text=status_card("⏹ درخواست متوقف شد", "انتخاب کیفیت لغو شد."),
                    parse_mode=ParseMode.HTML,
                )
    for task in tasks:
        task.cancel()
    await asyncio.sleep(0)
    total = len(tasks) + selection_count
    if total:
        body = f"تعداد درخواست متوقف‌شده: <b>{total}</b>"
    else:
        body = "درخواست فعالی برای توقف پیدا نشد."
    await update.effective_message.reply_text(status_card("توقف دانلود", body), parse_mode=ParseMode.HTML)


@membership_required
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    urls = extract_urls_from_message(update.effective_message)
    if not urls:
        search_text = update.effective_message.text or update.effective_message.caption or ""
        await run_youtube_search(update, context, search_text)
        return
    await send_link_feedback(
        context,
        update.effective_chat.id,
        update.effective_message.message_id,
        valid=links_are_supported(urls),
    )
    await process_urls(update, context, urls, update.effective_message.message_id)


@membership_required
async def dl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    urls = extract_urls_from_message(message)
    replied = message.reply_to_message
    if not urls and replied:
        urls = extract_urls_from_message(replied)
    feedback_reply_to = replied.message_id if replied else message.message_id
    await send_link_feedback(
        context,
        update.effective_chat.id,
        feedback_reply_to,
        valid=links_are_supported(urls),
    )
    if not urls:
        await message.reply_text(
            status_card(
                "🙃 لینکی ندیدم",
                "بعد از <code>/dl</code> لینک رو بنویس، یا روی پیام لینک‌دار ریپلای کن.",
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    reply_to = replied.message_id if replied else message.message_id
    await process_urls(update, context, urls, reply_to)


@membership_required
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = (message.text or message.caption or "").lower()
    username = (context.bot.username or "").lower()
    if not username or f"@{username}" not in text:
        return
    urls = extract_urls_from_message(message)
    if not urls:
        await send_link_feedback(
            context,
            update.effective_chat.id,
            message.message_id,
            valid=False,
        )
        await message.reply_text(
            status_card("🙃 لینک رو جا گذاشتی", "منشن و لینک رو توی یک پیام بفرست یا از <code>/dl</code> استفاده کن."),
            parse_mode=ParseMode.HTML,
        )
        return
    await send_link_feedback(
        context,
        update.effective_chat.id,
        message.message_id,
        valid=links_are_supported(urls),
    )
    await process_urls(update, context, urls, message.message_id)


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for member in update.effective_message.new_chat_members or ():
        if member.id == context.bot.id:
            await update.effective_message.reply_text(WELCOME_GROUP, parse_mode=ParseMode.HTML)
            return


@membership_required
async def on_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if not (
        data.startswith("sel:")
        or data.startswith("cancel:")
        or data.startswith("info:")
        or data.startswith("caption:")
    ):
        await query.answer()
        return
    parts = data.split(":")
    token = parts[1] if len(parts) > 1 else ""
    session = PENDING_SELECTIONS.get(token)
    if session is None:
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return
    if not _selection_is_owned(session, update):
        await query.answer("این درخواست متعلق به شما نیست.", show_alert=True)
        return

    if data.startswith("caption:"):
        await query.answer("دارم کپشن رو پیدا می‌کنم…")
        caption = await get_session_instagram_caption(session)
        if not caption:
            unavailable = status_card(
                "کپشن در دسترس نیست",
                "پست خصوصی، حذف‌شده یا موقتاً توسط Instagram محدود شده است.",
            )
            if not session.options:
                release_pending_selection(session)
                await query.edit_message_text(unavailable, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=unavailable,
                    reply_to_message_id=session.reply_to,
                    parse_mode=ParseMode.HTML,
                )
            return
        await send_long_text(
            context,
            update.effective_chat.id,
            "📝 کپشن اینستاگرام",
            caption,
            session.reply_to,
        )
        if not session.options:
            release_pending_selection(session)
            await query.edit_message_text(
                status_card("✅ کپشن رو فرستادم", "همون متن اصلی پست آماده‌ست."),
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("info:"):
        await query.answer("متن پست ارسال شد")
        chunks = [
            session.fallback_text[index:index + 3500]
            for index in range(0, len(session.fallback_text), 3500)
        ] or ["اطلاعات متنی در دسترس نیست."]
        for index, chunk in enumerate(chunks):
            heading = "📝 متن/اطلاعات پست\n\n" if index == 0 else "📝 ادامه متن\n\n"
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=heading + chunk,
                reply_to_message_id=session.reply_to,
                disable_web_page_preview=True,
            )
        return

    if data.startswith("cancel:"):
        if session.processing_task and not session.processing_task.done():
            session.processing_task.cancel()
            await query.answer("در حال توقف دانلود…")
            return
        await query.answer("درخواست لغو شد")
        release_pending_selection(session)
        await query.edit_message_text(
            status_card("⏹ درخواست متوقف شد", "انتخاب کیفیت لغو شد."),
            parse_mode=ParseMode.HTML,
        )
        return

    if session.processing:
        await query.answer("این گزینه قبلاً ثبت شده؛ کمی صبر کن.", show_alert=True)
        return
    try:
        option_index = int(parts[2])
        option = session.options[option_index]
    except (IndexError, TypeError, ValueError):
        await query.answer("گزینه معتبر نیست.", show_alert=True)
        return

    session.processing = True
    session.processing_task = asyncio.current_task()
    await query.answer("باشه، شروع کردم 🚀")
    selection_title = "📝 دارم کپشن رو می‌گیرم…" if option.action == "caption" else "⬇️ دارم خروجی رو آماده می‌کنم…"
    selection_label = "کپشن پست" if option.action == "caption" else option.label
    await query.edit_message_text(
        status_card(
            selection_title,
            f"انتخابت: <b>{html_escape(selection_label)}</b>\nمنبع: <code>{html_escape(session.source_host)}</code>",
            "برای توقف: /cancel"
        ),
        parse_mode=ParseMode.HTML,
    )

    try:
        if session.lease.worker.lease_id != session.lease.lease_id:
            raise RuntimeError("Selection lease is no longer active")
        selection_progress = ProgressReporter(query.message, session.request_id)
        await selection_progress.update(10, selection_title, selection_label, force=True)
        result = await GATEWAY.select(
            client=session.lease.worker.client,
            worker_name=session.lease.worker.name,
            bot_username=session.bot_username,
            request_message_id=session.request_message_id,
            menu_message_id=session.menu_message_id,
            option=option,
            attempt_directory=session.attempt_directory,
            progress_callback=selection_progress.download,
        )
        if result.status == "text":
            chunks = [result.text[index:index + 3500] for index in range(0, len(result.text), 3500)] or [""]
            for index, chunk in enumerate(chunks):
                heading = "📝 کپشن\n\n" if index == 0 else "📝 ادامه کپشن\n\n"
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=heading + chunk,
                    reply_to_message_id=session.reply_to,
                    disable_web_page_preview=True,
                )
            await query.edit_message_text(
                status_card(
                    "✅ کپشن ارسال شد",
                    f"تعداد بخش: <b>{len(chunks)}</b>",
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        if result.status != "ready":
            await query.edit_message_text(
                failure_text([result.reason or "service_error"], session.request_id),
                parse_mode=ParseMode.HTML,
            )
            return
        await send_result_to_user(
            update,
            context,
            query.message,
            result,
            reply_to=session.reply_to,
            request_id=session.request_id,
            quality=option.label if option.action == "media" else None,
            instagram_caption=(
                await get_session_instagram_caption(session)
                if session.platform == Platform.INSTAGRAM
                else ""
            ),
            progress=selection_progress,
        )
    except asyncio.CancelledError:
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                status_card("⏹ درخواست متوقف شد", "دانلود کیفیت انتخابی لغو شد.",
                parse_mode=ParseMode.HTML,
            ))
    except Exception as exc:
        logger.exception("Selection %s failed: %s", session.request_id, exc)
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                status_card(
                    "❌ خطا در دانلود",
                    "هیچ فایل ناقصی ارسال نشد. لینک را دوباره امتحان کن.",
                ),
                parse_mode=ParseMode.HTML,
            )
    finally:
        release_pending_selection(session)


@membership_required
async def on_reel_music_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 🎵 موزیک ریلز button presses."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    token = parts[1] if len(parts) > 1 else ""

    entry = REEL_MUSIC_URLS.get(token)
    if entry is None:
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return
    url, created_at, orig_chat_id, orig_user_id = entry
    if time.monotonic() - created_at > REEL_MUSIC_TTL:
        REEL_MUSIC_URLS.pop(token, None)
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return
    if update.effective_user.id != orig_user_id or update.effective_chat.id != orig_chat_id:
        await query.answer("این درخواست متعلق به شما نیست.", show_alert=True)
        return

    REEL_MUSIC_URLS.pop(token, None)
    await query.answer("🎵 دارم آهنگ ریلز رو شناسایی می‌کنم…")
    with contextlib.suppress(TelegramError):
        await query.edit_message_reply_markup(reply_markup=None)

    chat_id = update.effective_chat.id
    reply_to = query.message.message_id if query.message else None
    request_id = uuid.uuid4().hex[:8]

    if ACCOUNT_POOL.total == 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text=status_card("🛠 بخش دانلود موقتاً آماده نیست", "لطفاً کمی بعد دوباره امتحان کن."),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to,
        )
        return

    queued = ACCOUNT_POOL.busy_count >= ACCOUNT_POOL.total
    status_message = await send_status(
        context,
        chat_id,
        status_card(
            "⏳ لینک رفت توی صف" if queued else "🎵 دارم آهنگ ریلز رو پیدا می‌کنم…",
            f"نوبت تقریبی: <b>{ACCOUNT_POOL.queue_length + 1}</b>" if queued
            else f"دارم آهنگ ریلز رو تشخیص می‌دم و بعدش دانلود کنم.",
            "برای توقف: /cancel",
        ),
        reply_to,
    )
    progress = ProgressReporter(status_message, request_id)

    lease: WorkerLease | None = None
    attempt_directory: Path | None = None
    try:
        lease = await asyncio.wait_for(
            ACCOUNT_POOL.acquire(),
            timeout=SETTINGS.worker_acquire_timeout,
        )

        for bot_username in SETTINGS.music_finder_bots:
            try:
                attempt_directory = create_attempt_directory(
                    SETTINGS.download_root,
                    request_id,
                    f"reel-music-{bot_username}",
                )
                await progress.update(
                    10,
                    f"🎵 دارم آهنگ رو از ربات @{bot_username} دریافت می‌کنم…",
                    force=True,
                )

                result = await GATEWAY.request(
                    client=lease.worker.client,
                    worker_name=lease.worker.name,
                    bot_username=bot_username,
                    url=url,
                    attempt_directory=attempt_directory,
                    progress_callback=progress.download,
                    expected_kind_override=MediaKind.AUDIO,
                )

                # Special handling for @Musicfindmhdbot which requires a button click
                if result.status == "needs_selection" and bot_username.lower() == "musicfindmhdbot":
                    # Look for "شناسایی موسیقی" button
                    option = next(
                        (opt for opt in result.options if "شناسایی موسیقی" in opt.label),
                        None
                    )
                    if option:
                        await progress.update(30, "🔍 دارم موزیک رو شناسایی می‌کنم…", force=True)
                        result = await GATEWAY.select(
                            client=lease.worker.client,
                            worker_name=lease.worker.name,
                            bot_username=bot_username,
                            request_message_id=result.request_message_id,
                            menu_message_id=result.menu_message_id,
                            option=option,
                            attempt_directory=attempt_directory,
                            progress_callback=progress.download,
                        )

                if result.status == "ready":
                    await send_result_to_user(
                        update,
                        context,
                        status_message,
                        result,
                        reply_to=reply_to,
                        request_id=request_id,
                        progress=progress,
                    )
                    return

                logger.info("Reel music bot @%s returned %s for %s", bot_username, result.status, request_id)
            except Exception as bot_exc:
                logger.warning("Attempt with @%s failed: %s", bot_username, bot_exc)
                continue

        await edit_status(
            status_message,
            status_card(
                "❌ آهنگ پیدا نشد",
                "ریلز ممکنه موزیک نداشته باشه یا از صدای اوریجینال استفاده شده باشه.",
            ),
        )
    except (PoolUnavailable, asyncio.TimeoutError):
        await edit_status(
            status_message,
            status_card("⏳ صف پر است", "لطفاً چند لحظه صبر کن و دوباره امتحان کن."),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Reel music callback failed for %s: %s", request_id, exc)
        with contextlib.suppress(TelegramError):
            await edit_status(
                status_message,
                status_card("❌ خطای غیرمنتظره", "لطفاً دوباره امتحان کن."),
            )
    finally:
        if attempt_directory is not None:
            cleanup_request_directory(attempt_directory, SETTINGS.download_root)
        if lease is not None:
            ACCOUNT_POOL.release(lease)


def cleanup_stale_download_directories(max_age_seconds: float = 24 * 60 * 60) -> None:
    cutoff = time.time() - max_age_seconds
    for child in SETTINGS.download_root.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            cleanup_request_directory(child, SETTINGS.download_root)


async def post_init(application: Application) -> None:
    global SELECTION_REAPER_TASK, HEALTH_SERVER
    cleanup_stale_download_directories()
    proxy = build_telethon_proxy()
    for account in SETTINGS.accounts:
        session_source = StringSession(account.string_session) if account.string_session else str(account.session_path)
        client = TelegramClient(
            session_source,
            account.api_id,
            account.api_hash,
            proxy=proxy,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.error("%s is not authorized; run account setup first", account.name)
                await client.disconnect()
                continue
            ACCOUNT_POOL.add_worker(AccountWorker(account.name, account.phone, client))
            logger.info("Connected downloader account: %s", account.name)
        except Exception as exc:
            logger.error("Failed to connect %s: %s", account.name, exc)
            with contextlib.suppress(Exception):
                await client.disconnect()

    private_commands = [
        BotCommand("start", "شروع و معرفی"),
        BotCommand("help", "راهنمای کامل"),
        BotCommand("status", "وضعیت صف و ظرفیت"),
        BotCommand("platforms", "پلتفرم‌های پشتیبانی‌شده"),
        BotCommand("stats", "آمار این اجرا"),
        BotCommand("caption", "دریافت کپشن Instagram"),
        BotCommand("search", "جست‌وجو در YouTube"),
        BotCommand("cancel", "توقف دانلودهای من"),
        BotCommand("dl", "دانلود لینک"),
    ]
    group_commands = [
        BotCommand("dl", "دانلود لینک یا پیام ریپلای‌شده"),
        BotCommand("search", "جست‌وجو در YouTube"),
        BotCommand("cancel", "توقف دانلودهای من"),
        BotCommand("status", "وضعیت صف"),
        BotCommand("platforms", "پلتفرم‌ها"),
        BotCommand("caption", "دریافت کپشن Instagram"),
        BotCommand("help", "راهنما"),
    ]
    with contextlib.suppress(TelegramError):
        await application.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        await application.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    port_value = os.getenv("PORT", "0")
    try:
        port = int(port_value)
    except ValueError:
        port = 0
    if port > 0:
        try:
            HEALTH_SERVER = await asyncio.start_server(health_client, "0.0.0.0", port)
            logger.info("Health endpoint listening on port %d", port)
        except OSError as exc:
            logger.error("Health endpoint failed to start: %s", exc)
    SELECTION_REAPER_TASK = asyncio.create_task(selection_reaper(application), name="selection-reaper")
    logger.info("Bot initialized with %d downloader account(s)", ACCOUNT_POOL.total)


async def post_shutdown(application: Application) -> None:
    global SELECTION_REAPER_TASK, HEALTH_SERVER
    if SELECTION_REAPER_TASK:
        SELECTION_REAPER_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await SELECTION_REAPER_TASK
        SELECTION_REAPER_TASK = None
    pending_tasks = {task for tasks in ACTIVE_REQUESTS.values() for task in tasks if not task.done()}
    pending_tasks.update(
        session.processing_task
        for session in PENDING_SELECTIONS.values()
        if session.processing_task and not session.processing_task.done()
    )
    for task in pending_tasks:
        task.cancel()
    if pending_tasks:
        await asyncio.gather(*pending_tasks, return_exceptions=True)
    for session in list(PENDING_SELECTIONS.values()):
        if session.processing_task:
            session.processing_task.cancel()
        release_pending_selection(session)
    YOUTUBE_SEARCH_SESSIONS.clear()
    ACTIVE_YOUTUBE_SEARCHES.clear()
    for worker in ACCOUNT_POOL.workers:
        with contextlib.suppress(Exception):
            await worker.client.disconnect()
    if HEALTH_SERVER:
        HEALTH_SERVER.close()
        await HEALTH_SERVER.wait_closed()
        HEALTH_SERVER = None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


def main() -> None:
    if not SETTINGS.bot_token:
        raise ConfigError("BOT_TOKEN is missing from the environment")
    builder = (
        Application.builder()
        .token(SETTINGS.bot_token)
        .concurrent_updates(SETTINGS.max_concurrent_updates)
        .connect_timeout(30)
        .read_timeout(90)
        .write_timeout(90)
        .media_write_timeout(180)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if SETTINGS.use_proxy:
        proxy_url = f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    application = builder.build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("adduser", adduser_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("services", services_command))
    application.add_handler(CommandHandler("platforms", services_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("caption", caption_command))
    application.add_handler(CommandHandler(("search", "yt"), search_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("dl", dl_command))
    application.add_handler(CallbackQueryHandler(on_selection, pattern=r"^(?:sel|cancel|info|caption):"))
    application.add_handler(CallbackQueryHandler(on_reel_music_callback, pattern=r"^reel_music:"))
    application.add_handler(CallbackQueryHandler(on_youtube_search_callback, pattern=r"^(?:ys|yp):"))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_private_message,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_group_message,
        )
    )
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    application.add_error_handler(error_handler)
    logger.info("MZ Downloader is starting")
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
