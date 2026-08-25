#!/usr/bin/env python3
"""MZ Downloader Bot with isolated requests and correlated downloader replies."""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
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
import httpx
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
    TypeHandler,
    filters,
)
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import ConfigError, PROJECT_DIR, SETTINGS
from apify_gateway import APIFY_PROVIDER, ApifyGateway, apify_health_check, option_size_hint
from apify_platforms import NEW_APIFY_PLATFORMS
import users_db
import store
import token_alerts
import user_features
import ai_service
from feature_flags import FLAGS
from structured_logging import setup_structured_logging
from perf import CLIENTS as PERF_CLIENTS
from media_size import fmt_size_exact
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
from instagram_profile import (
    InstagramProfileError,
    InstagramProfileNotFound,
    InstagramProfilePrivate,
    fetch_profile,
    fetch_latest_post_url,
    fetch_stories,
    format_profile_caption,
    download_media as ig_download_media,
)
from routing import Platform, all_providers, detect_platform, is_instagram_image_post, is_instagram_reel, platform_info, providers_for_platform, spotify_resource_type
from spotisaver import SpotisaverAlbumDownloader, _zip_and_remove as _zip_tracks
from social_gateway import (
    SOCIAL_PROVIDER,
    SocialSitesGateway,
    social_health_check,
)
from ahm7_gateway import (
    AHM7_PLATFORMS as AHM7_SUPPORTED_PLATFORMS,
    AHM7_PROVIDER,
    Ahm7Gateway,
    ahm7_health_check,
)
from yoinku_gateway import (
    YOINKU_PROVIDER,
    YoinkuGateway,
    yoinku_health_check,
)
from voiddl_gateway import (
    VOIDDL_PROVIDER,
    VoidDLGateway,
    download_youtube_thumbnail,
    voiddl_health_check,
)
from youtube_search import (
    MAX_RESULTS as YOUTUBE_SEARCH_MAX_RESULTS,
    RESULTS_PER_PAGE as YOUTUBE_RESULTS_PER_PAGE,
    YouTubeFormatSize,
    YouTubeSearchError,
    YouTubeSearchResult,
    YouTubeSearchService,
    estimate_youtube_size,
    normalize_search_query,
)
from mz_shazam_search import (
    MAX_RESULTS as SHAZAM_SEARCH_MAX_RESULTS,
    RESULTS_PER_PAGE as SHAZAM_RESULTS_PER_PAGE,
    ShazamSearchError,
    ShazamSearchResult,
    ShazamSearchService,
    normalize_song_query,
    youtube_url_for_song,
)
from youtube_subtitle import (
    LANGUAGE_ENGLISH as SUBTITLE_LANG_EN,
    LANGUAGE_PERSIAN as SUBTITLE_LANG_FA,
    YouTubeSubtitleError,
    YouTubeSubtitleNotFound,
    extract_youtube_video_id as _extract_youtube_video_id,
    fetch_youtube_subtitle,
    is_youtube_shorts_url,
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
# Apify Actors are an optional first-choice gateway for public YouTube and
# Instagram URLs. APIFY_TOKENS is round-robin with failover on token-side
# errors; all existing paths remain available as fallbacks.
# 1404 upgrade: also serves Spotify / SoundCloud / Twitter / Facebook /
# Pinterest (feature-flagged) and reports token failures to the admin PV.
APIFY_GATEWAY: ApifyGateway | None = None
if SETTINGS.apify_enabled and SETTINGS.apify_tokens:
    APIFY_GATEWAY = ApifyGateway(
        tokens=SETTINGS.apify_tokens,
        run_timeout=SETTINGS.apify_run_timeout,
        poll_interval=SETTINGS.apify_poll_interval,
        token_cooldown=SETTINGS.apify_token_cooldown,
        max_download_size=SETTINGS.max_download_size,
        on_token_failure=token_alerts.on_token_failure,
        on_token_success=token_alerts.on_token_success,
    )
logger.info("Apify gateway %s (new platforms: %s)", apify_health_check(APIFY_GATEWAY), FLAGS.apify_new_platforms)

# YouTube-sites gateway (loader.to rotational scraper) was the previous
# primary downloader for YouTube; it has been REPLACED by Yoinku (above)
# and removed from this file. The file `youtube_sites_gateway.py` itself
# was deleted in the same cleanup commit — see git history.

# ── AHM7 gateway (primary downloader for TikTok / Instagram / Facebook /
# X-Twitter / Reddit / Snapchat / SoundCloud / CapCut / SnackVideo /
# Douyin via https://ahm7xmakki.com/api/alldl). When the API returns a
# videoUrl but no audioUrl and the user picked MP3, ffmpeg extracts the
# audio track (``ffmpeg -i input.mp4 -vn -c:a libmp3lame -b:a 192k
# output.mp3``). Fallback chain: AHM7 → Apify → Telegram bots → error.
AHM7_GATEWAY: Ahm7Gateway | None = None
if SETTINGS.ahm7_enabled:
    AHM7_GATEWAY = Ahm7Gateway(
        api_url=SETTINGS.ahm7_api_url,
        proxy_url=(
            f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
            if SETTINGS.use_proxy
            else None
        ),
        max_download_size=SETTINGS.max_download_size,
    )
logger.info("ahm7 gateway %s", ahm7_health_check(AHM7_GATEWAY))

# ── VoidDL gateway (PRIMARY downloader for YouTube via
# https://voiddl.app). Per-key caps: 20 downloads/minute AND 10 GB of
# daily bandwidth. Multiple keys rotate instantly the moment one key
# hits either cap (429 → next key; bandwidth spent → next key until
# UTC midnight). Fallback chain: VoidDL → Yoinku → Apify → Telegram
# bots → error.
VOIDDL_GATEWAY: VoidDLGateway | None = None
if SETTINGS.voiddl_enabled and SETTINGS.voiddl_api_keys:
    VOIDDL_GATEWAY = VoidDLGateway(
        api_base=SETTINGS.voiddl_api_base,
        api_keys=SETTINGS.voiddl_api_keys,
        daily_bandwidth=SETTINGS.voiddl_daily_bandwidth_mb * 1024 * 1024,
        per_minute_limit=SETTINGS.voiddl_per_minute_limit,
        proxy_url=(
            f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
            if SETTINGS.use_proxy
            else None
        ),
        max_download_size=SETTINGS.max_download_size,
    )
logger.info(
    "voiddl gateway %s (keys=%d, per_minute=%d)",
    voiddl_health_check(VOIDDL_GATEWAY),
    len(SETTINGS.voiddl_api_keys),
    SETTINGS.voiddl_per_minute_limit,
)

# ── Yoinku gateway (fallback #1 for YouTube via
# https://yoinku.com/api/v1). Per-key caps: 30 requests/day AND 5
# requests/minute. Multiple keys rotate so the 31st daily request and the
# 6th per-minute request both automatically use the next key. Fallback
# chain: VoidDL → Yoinku → Apify → Telegram bots → error.
YOINKU_GATEWAY: YoinkuGateway | None = None
if SETTINGS.yoinku_enabled and SETTINGS.yoinku_api_keys:
    YOINKU_GATEWAY = YoinkuGateway(
        api_base=SETTINGS.yoinku_api_base,
        api_keys=SETTINGS.yoinku_api_keys,
        daily_limit=SETTINGS.yoinku_daily_limit,
        per_minute_limit=SETTINGS.yoinku_per_minute_limit,
        proxy_url=(
            f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
            if SETTINGS.use_proxy
            else None
        ),
        max_download_size=SETTINGS.max_download_size,
    )
logger.info(
    "yoinku gateway %s (daily_limit=%d, per_minute=%d)",
    yoinku_health_check(YOINKU_GATEWAY),
    SETTINGS.yoinku_daily_limit,
    SETTINGS.yoinku_per_minute_limit,
)

# ── Social-sites gateway (TikTok via tikwm.com, SoundCloud via yt-dlp,
# Instagram via yt-dlp-with-cookies). None of these need a Telegram
# account. Instagram requires a cookies.txt file — if absent, Instagram
# URLs fall back to the Telegram downloader bots.
SOCIAL_GATEWAY: SocialSitesGateway | None = None
_instagram_cookies = PROJECT_DIR / "cookies.txt" if (PROJECT_DIR / "cookies.txt").exists() else None
if _instagram_cookies is not None:
    logger.info("Instagram cookies.txt found — Instagram will use yt-dlp with cookies")
else:
    logger.info("Instagram cookies.txt not found — Instagram will fall back to Telegram downloader bots")
SOCIAL_GATEWAY = SocialSitesGateway(
    instagram_cookies_path=_instagram_cookies,
    max_download_size=SETTINGS.max_download_size,
)
logger.info(
    "social-sites gateway enabled (TikTok=tikwm.com, SoundCloud=yt-dlp, Instagram=%s)",
    "yt-dlp+cookies" if _instagram_cookies is not None else "Telegram-bots-only",
)
YOUTUBE_SEARCH = YouTubeSearchService(
    proxy_url=(
        f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        if SETTINGS.use_proxy
        else None
    )
)
SHAZAM_SEARCH = ShazamSearchService(
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
    ("@MZDNLD_upnews", "https://t.me/MZDNLD_upnews", "MZ Downloader | Updates"),
)
MEMBERSHIP_CACHE_TTL = 5 * 60
FEEDBACK_STICKER_SET = "MZDownloader"
DEFAULT_UPLOAD_RATE = 2 * 1024 * 1024
UPLOAD_PROGRESS_INTERVAL = 1.0
# Minimum gap between two progress-bar edits. Telegram allows ~30
# edits per minute per message — 0.4s keeps us comfortably under the
# limit while still feeling live to the user.
PROGRESS_MIN_EDIT_INTERVAL = 0.4
YOUTUBE_SEARCH_TTL = 10 * 60
YOUTUBE_SEARCH_RATE_LIMIT = 5
YOUTUBE_SEARCH_RATE_WINDOW = 60.0
MAX_YOUTUBE_SEARCH_SESSIONS = 512
SHAZAM_SEARCH_TTL = 10 * 60
SHAZAM_SEARCH_RATE_LIMIT = 5
SHAZAM_SEARCH_RATE_WINDOW = 60.0
MAX_SHAZAM_SEARCH_SESSIONS = 512


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
    # None when this selection is being served by a direct HTTP gateway
    # (AHM7 / Yoinku / social) which does the download via HTTP,
    # not via a Telegram account. The Telegram-bot path keeps a real
    # WorkerLease here.
    lease: WorkerLease | None
    attempt_directory: Path
    # True when this session should be fulfilled by SOCIAL_GATEWAY
    # (TikTok via tikwm.com / SoundCloud via yt-dlp / Instagram via
    # yt-dlp-with-cookies) instead of GATEWAY.
    use_social_sites: bool = False
    # True when this session should be fulfilled by AHM7_GATEWAY
    # (TikTok / Instagram / Facebook / X / Reddit / Snapchat /
    # SoundCloud / CapCut / SnackVideo / Douyin) instead of GATEWAY.
    use_ahm7: bool = False
    # True when this session should be fulfilled by VOIDDL_GATEWAY
    # (YouTube primary path via voiddl.app) instead of GATEWAY.
    use_voiddl: bool = False
    # True when this session should be fulfilled by YOINKU_GATEWAY
    # (YouTube fallback #1) instead of GATEWAY.
    use_yoinku: bool = False
    # True when the selected quality is fulfilled by APIFY_GATEWAY, which
    # starts the Actor only after the user chooses a button.
    use_apify: bool = False
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


@dataclass
class ShazamSearchSession:
    token: str
    created_at: float
    chat_id: int
    user_id: int
    reply_to: int | None
    query: str
    results: tuple[ShazamSearchResult, ...]
    current_page: int = 0
    busy: bool = False
    selected: bool = False


PENDING_SELECTIONS: dict[str, PendingSelection] = {}
YOUTUBE_SEARCH_SESSIONS: dict[str, YouTubeSearchSession] = {}
SHAZAM_SEARCH_SESSIONS: dict[str, ShazamSearchSession] = {}
ACTIVE_REQUESTS: dict[tuple[int, int], set[asyncio.Task[Any]]] = {}
USER_RATE_LIMITS: dict[tuple[int, int], deque[float]] = defaultdict(deque)
YOUTUBE_SEARCH_RATE_LIMITS: dict[tuple[int, int], deque[float]] = defaultdict(deque)
SHAZAM_SEARCH_RATE_LIMITS: dict[tuple[int, int], deque[float]] = defaultdict(deque)
ACTIVE_YOUTUBE_SEARCHES: set[tuple[int, int]] = set()
ACTIVE_SHAZAM_SEARCHES: set[tuple[int, int]] = set()
MEMBERSHIP_CACHE: dict[int, float] = {}
# token → (url, created_at, chat_id, user_id)
REEL_MUSIC_URLS: dict[str, tuple[str, float, int, int]] = {}
REEL_MUSIC_TTL = 600  # 10 minutes
# token → (youtube_url, created_at, chat_id, user_id)
# Used by the subtitle follow-up message so the callback handler can recover
# the original YouTube URL when the user clicks 🇮🇷 فارسی or 🇬🇧 English.
YOUTUBE_SUBTITLE_URLS: dict[str, tuple[str, float, int, int]] = {}
YOUTUBE_SUBTITLE_TTL = 600  # 10 minutes
# token → (username, created_at, chat_id, user_id)
IG_PROFILE_SESSIONS: dict[str, tuple[str, float, int, int]] = {}
IG_PROFILE_TTL = 600  # 10 minutes
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
FEATURES_SCHEDULER_TASK: asyncio.Task[Any] | None = None


# Telegram hard-limits inline-button ``text`` to **64 UTF-8 bytes**. Going
# over raises ``BadRequest: button_text_invalid`` from the Telegram API,
# which the bot would surface as a silent "menu didn't appear" failure.
#
# Persian characters are 2 bytes each in UTF-8 (Arabic block U+0600–U+06FF),
# so a 40-character Persian label is already 80 bytes — well over the
# limit. This helper counts bytes (not chars) and appends a single ``…``
# (3 bytes) ellipsis when the original wouldn't fit.
BUTTON_TEXT_MAX_BYTES = 60  # 4-byte safety margin under Telegram's 64


def _truncate_button_label(text: str, *, max_bytes: int = BUTTON_TEXT_MAX_BYTES) -> str:
    """Truncate a button label so it fits Telegram's 64-byte cap.

    Counts UTF-8 bytes (not characters) so multi-byte Persian/emoji labels
    are handled correctly. When truncation is needed, the result is cut at
    a character boundary (no partial multi-byte sequences) and a single
    ellipsis ``…`` is appended.
    """
    if not text:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Reserve 3 bytes for the trailing ``…`` (U+2026 = E2 80 A6).
    budget = max_bytes - 3
    if budget <= 0:
        return "…"
    # Walk character-by-character and accumulate bytes; stop before we'd
    # split a multi-byte sequence or exceed the budget.
    result: list[str] = []
    used = 0
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if used + ch_bytes > budget:
            break
        result.append(ch)
        used += ch_bytes
    return "".join(result) + "…"


@dataclass
class RuntimeStats:
    requests: int = 0
    successful: int = 0
    failed: int = 0
    bytes_sent: int = 0


def _option_prefix(option) -> str:
    """Pick the leading emoji for a quality-menu button.

    PHOTO options get 📷 so Instagram image carousel posts — which are now
    routed through Apify as their primary downloader — show a clear
    photo label instead of the default 🎬 video icon.
    """
    if option.expected_kind == MediaKind.AUDIO:
        return "🎵"
    if option.expected_kind == MediaKind.PHOTO:
        return "📷"
    return "🎬"


def _youtube_button_label(option) -> str:
    """Pure-quality label for a YouTube menu button (480 / 720 / MP3).

    YouTube quality buttons carry ONLY the quality number — no emoji,
    no size, no container. Sizes and other details belong in the caption
    under the thumbnail photo. Falls back to the raw label only when no
    quality can be derived at all.
    """
    if option.expected_kind == MediaKind.AUDIO:
        return "MP3"
    height = getattr(option, "expected_height", None)
    if height:
        return str(int(height))
    label = str(getattr(option, "label", "") or "").strip()
    match = re.match(r"^\D*(\d{3,4})\s*p?\b", label, re.IGNORECASE)
    if match:
        return match.group(1)
    return label


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
    # Internal download-provider sentinels are not real Telegram accounts;
    # never render them as a visible @link in the user-facing caption.
    if username in {AHM7_PROVIDER, YOINKU_PROVIDER}:
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
        # High-water mark — the bar NEVER goes backwards. Once we reach
        # 40% (e.g. download done, entering upload phase) we won't drop
        # back to 12% even if a downstream callback fires an old value.
        self.max_percent = -1
        self.lock = asyncio.Lock()

    async def update(self, percent: int, title: str, detail: str = "", *, force: bool = False) -> None:
        value = max(0, min(100, int(percent)))
        now = time.monotonic()
        # Never go backwards. The download→upload→processing transitions
        # can fire out-of-order callbacks (e.g. a stale download progress
        # arriving after the upload has already begun); clamp to max_percent.
        if value < self.max_percent:
            value = self.max_percent
        else:
            self.max_percent = value
        if not force and value == self.last_percent:
            return
        if not force and now - self.last_edit < PROGRESS_MIN_EDIT_INTERVAL and value < 100:
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

    async def apify_download(self, current: int, total: int) -> None:
        """Show Apify CDN transfer in the 60–70% progress range.

        The Apify Actor itself occupies 16–58%; the real byte transfer then
        owns 60–70% before the existing Telegram-upload phase starts at 70%.
        """
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
            60 + int(max(0.0, min(1.0, ratio)) * 10),
            "⬇️ فایل آماده‌شده را دریافت می‌کنم…",
            f"{size_line}\n{timing}",
        )

    async def processing(self, percent: int, title: str, detail: str = "", *, force: bool = False) -> None:
        """Show a 'processing' status — percent bar WITHOUT byte counts.

        Used by the YouTube-sites gateway during the server-side extraction
        phase (polling the progress_url). The server returns a 0..1000
        extraction-progress counter, NOT a byte count — feeding that into
        `download()` was what caused the bar to show garbage like
        "488 B از 1000 B" and then JUMP BACKWARDS when the actual CDN
        download started.

        This method shows the percent bar + an arbitrary detail line (e.g.
        "پردازش سرور: 42%") with no byte / time-remaining fields.
        """
        await self.update(percent, title, detail, force=force)

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


# --- Shazam song search helpers -----------------------------------------

def allow_shazam_search(key: tuple[int, int]) -> bool:
    now = time.monotonic()
    history = SHAZAM_SEARCH_RATE_LIMITS[key]
    while history and now - history[0] > SHAZAM_SEARCH_RATE_WINDOW:
        history.popleft()
    if len(history) >= SHAZAM_SEARCH_RATE_LIMIT:
        return False
    history.append(now)
    return True


def shazam_search_page_count(session: ShazamSearchSession) -> int:
    return max(1, math.ceil(len(session.results) / SHAZAM_RESULTS_PER_PAGE))


def _truncate_button_text(text: str, max_len: int = 38) -> str:
    """Trim a song label so the inline button stays readable on mobile."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def shazam_search_keyboard(session: ShazamSearchSession, page: int) -> InlineKeyboardMarkup:
    """Inline keyboard with one 'glass button' per song on the current page.

    Telegram renders inline buttons with a translucent/frosted look by default,
    which gives the 'دکمه شیشه‌ای' appearance the user asked for.
    """
    start = page * SHAZAM_RESULTS_PER_PAGE
    stop = min(start + SHAZAM_RESULTS_PER_PAGE, len(session.results))
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for result_index in range(start, stop):
        result = session.results[result_index]
        label = _truncate_button_text(result.label)
        row.append(
            InlineKeyboardButton(
                f"🎵 {label}",
                callback_data=f"ss:{session.token}:{result_index}",
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
            InlineKeyboardButton("⬅️ صفحه قبل", callback_data=f"sp:{session.token}:{page - 1}")
        )
    if page + 1 < shazam_search_page_count(session):
        navigation.append(
            InlineKeyboardButton("صفحه بعد ➡️", callback_data=f"sp:{session.token}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(rows)


def shazam_search_caption(session: ShazamSearchSession, page: int) -> str:
    page_count = shazam_search_page_count(session)
    return (
        "🎵 <b>نتایج جست‌وجوی آهنگ</b>\n"
        f"عبارت: <b>{html_escape(session.query)}</b>\n"
        f"صفحه {persian_number(page + 1)} از {persian_number(page_count)}"
        " • روی آهنگ موردنظر بزن تا دانلود شه."
    )


def prune_shazam_search_sessions() -> None:
    now = time.monotonic()
    for token, session in list(SHAZAM_SEARCH_SESSIONS.items()):
        if now - session.created_at >= SHAZAM_SEARCH_TTL:
            SHAZAM_SEARCH_SESSIONS.pop(token, None)
    overflow = len(SHAZAM_SEARCH_SESSIONS) - MAX_SHAZAM_SEARCH_SESSIONS
    if overflow > 0:
        oldest = sorted(SHAZAM_SEARCH_SESSIONS.values(), key=lambda item: item.created_at)[:overflow]
        for session in oldest:
            SHAZAM_SEARCH_SESSIONS.pop(session.token, None)
    for key, history in list(SHAZAM_SEARCH_RATE_LIMITS.items()):
        while history and now - history[0] > SHAZAM_SEARCH_RATE_WINDOW:
            history.popleft()
        if not history:
            SHAZAM_SEARCH_RATE_LIMITS.pop(key, None)


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
    """Edit either a plain status message or a thumbnail-card caption."""
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        else:
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


async def send_youtube_quality_card(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to: int | None,
    *,
    status_message: Any,
    menu_text: str,
    rows: list[list[InlineKeyboardButton]],
    preview: DownloadedMedia | None,
    request_id: str,
) -> Any:
    """Send the YouTube quality menu as a photo card.

    The quality buttons are attached to the THUMBNAIL message itself
    (as its caption / inline keyboard) — exactly the UX requested for
    YouTube links: the best-quality thumbnail is sent as a photo and
    the quality buttons live under it. When no thumbnail could be
    downloaded the menu falls back to editing the plain status message.
    Returns the message object that now carries the menu (used as the
    session's status_message so later edits target the photo caption).
    """
    keyboard = InlineKeyboardMarkup(rows)
    if preview is not None:
        try:
            with preview.path.open("rb") as preview_handle:
                card_message = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=preview_handle,
                    caption=menu_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    reply_to_message_id=reply_to,
                )
            with contextlib.suppress(TelegramError):
                await status_message.delete()
            return card_message
        except TelegramError as exc:
            logger.warning("YouTube thumbnail card send failed for %s: %s", request_id, exc)
    await edit_status(status_message, menu_text, keyboard)
    return status_message


async def _ensure_sticker_ids(
    context: ContextTypes.DEFAULT_TYPE,
    count: int = 5,
) -> None:
    """Populate FEEDBACK_STICKER_IDS if not yet cached (or if more are needed)."""
    global FEEDBACK_STICKER_IDS
    if len(FEEDBACK_STICKER_IDS) >= count:
        return
    try:
        sticker_set = await asyncio.wait_for(
            context.bot.get_sticker_set(FEEDBACK_STICKER_SET),
            timeout=8.0,
        )
        FEEDBACK_STICKER_IDS = tuple(
            sticker.file_id
            for sticker in getattr(sticker_set, "stickers", ())[:count]
            if getattr(sticker, "file_id", None)
        )
    except (asyncio.TimeoutError, TelegramError):
        pass


async def send_link_feedback(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to: int | None,
    *,
    valid: bool,
) -> None:
    """Reply with the matching sticker without ever blocking link processing."""
    try:
        await _ensure_sticker_ids(context)
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


async def send_feedback_sticker(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    index: int,
    reply_to: int | None = None,
) -> None:
    """Send a sticker from the feedback set by zero-based index (non-blocking)."""
    try:
        await _ensure_sticker_ids(context)
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


def is_youtube_long_video(url: str, result: GatewayResult) -> bool:
    """Decide whether *url* (a YouTube URL whose download just finished) is a
    *long* video — i.e. NOT a Short — so the subtitle follow-up should fire.

    A video is considered long when:
      1. The URL path is NOT ``/shorts/{id}``, AND
      2. Either the downloaded media has no duration info, OR duration > 60s.
    """
    if is_youtube_shorts_url(url):
        return False
    if result.media:
        duration = getattr(result.media[0], "duration", None)
        if duration is not None and duration <= 60:
            return False
    return True


async def fetch_subtitle_for_user(url: str, language: str) -> bytes:
    """Wrapper around :func:`fetch_youtube_subtitle` that injects the project
    proxy. Returns the raw SRT bytes. Raises :class:`YouTubeSubtitleError`
    on any failure.
    """
    return await fetch_youtube_subtitle(
        url,
        language,
        proxy_url=_caption_proxy_url(),
    )


async def send_subtitle_followup(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    reply_to: int | None,
    youtube_url: str,
    result: GatewayResult,
) -> None:
    """Send the 🇮🇷 فارسی / 🇬🇧 English subtitle follow-up message after a
    long YouTube video has been successfully delivered to the user.

    Shared between :func:`_process_url` (direct-download path) and
    :func:`on_selection` (quality-selection path) so both code paths trigger
    the same follow-up.

    Ownership model: we store ``user_id = -1`` so the callback handler skips
    the per-user check (chat_id match is enough because only the original
    chat can see the inline button).
    """
    if not is_youtube_long_video(youtube_url, result):
        return
    yt_sub_token = uuid.uuid4().hex[:12]
    YOUTUBE_SUBTITLE_URLS[yt_sub_token] = (
        youtube_url,
        time.monotonic(),
        chat_id,
        -1,  # skip per-user check; chat_id match is sufficient
    )
    with contextlib.suppress(TelegramError):
        await context.bot.send_message(
            chat_id=chat_id,
            text=status_card(
                "📝 زیرنویس این ویدیو رو هم می‌خوای؟",
                "اگه ساب‌تایتل این ویدیو رو هم می‌خوای، دکمه هر زبانی که می‌خوای بزن تا بفرستم.",
            ),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🇮🇷 فارسی",
                    callback_data=f"yt_sub:{yt_sub_token}:{SUBTITLE_LANG_FA}",
                ),
                InlineKeyboardButton(
                    "🇬🇧 English",
                    callback_data=f"yt_sub:{yt_sub_token}:{SUBTITLE_LANG_EN}",
                ),
            ]]),
        )


def is_active_channel_member(member: Any) -> bool:
    status = getattr(member, "status", "")
    if status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}:
        return True
    return status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))


def membership_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(_truncate_button_label(f"عضویت در {label}"), url=url)]
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
            "برای اینکه بتونی از همه‌ی امکانات استفاده کنی، توی کانال زیر عضو شو.",
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


def _extract_file_id(message: Any) -> tuple[str, str, int, str] | None:
    """Extract (file_id, mime_type, size, kind) from a sent Telegram message."""
    if message is None:
        return None
    try:
        if getattr(message, "video", None):
            return message.video.file_id, message.video.mime_type or "video/mp4", message.video.file_size or 0, "video"
        if getattr(message, "audio", None):
            return message.audio.file_id, message.audio.mime_type or "audio/mpeg", message.audio.file_size or 0, "audio"
        if getattr(message, "document", None):
            return message.document.file_id, message.document.mime_type or "application/octet-stream", message.document.file_size or 0, "document"
        if getattr(message, "photo", None):
            photo = message.photo[-1]
            return photo.file_id, "image/jpeg", photo.file_size or 0, "photo"
    except Exception:  # noqa: BLE001
        return None
    return None


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
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Send one media file natively. Returns the sent Message when possible
    so callers can capture Telegram file_id for the duplicate-detection cache."""
    async def send_native() -> Any:
        with item.path.open("rb") as file_handle:
            if item.kind == MediaKind.VIDEO:
                return await context.bot.send_video(
                    chat_id=chat_id,
                    video=file_handle,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    reply_to_message_id=reply_to,
                    reply_markup=reply_markup,
                )
            if item.kind == MediaKind.AUDIO:
                return await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=file_handle,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to,
                    reply_markup=reply_markup,
                )
            return await context.bot.send_document(
                chat_id=chat_id,
                document=file_handle,
                filename=item.path.name,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to,
                reply_markup=reply_markup,
            )
    async def upload_file() -> Any:
        try:
            return await telegram_retry(send_native)
        except TelegramError:
            if item.kind not in {MediaKind.VIDEO, MediaKind.AUDIO}:
                raise

            async def send_as_document() -> Any:
                with item.path.open("rb") as file_handle:
                    return await context.bot.send_document(
                        chat_id=chat_id,
                        document=file_handle,
                        filename=item.path.name,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to,
                        reply_markup=reply_markup,
                    )

            return await telegram_retry(send_as_document)

    if progress is None:
        return await upload_file()
    return await progress.upload(upload_file, size=item.size, label=upload_label)





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
                        "روی دکمه زیر بزن تا فایل رو دانلود کنی.\n"
                        "وقتی وارد لینک شدی، بالای صفحه سمت چپ روی سه خط بزن و بعد گزینه Download.",
                        "لینک موقته و بعد از ۳۰ دقیقه به‌صورت خودکار پاک می‌شه.",
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
    extra_markup: InlineKeyboardMarkup | None = None,
    source_url: str = "",
    platform_value: str = "",
    user_id: int | None = None,
) -> None:
    media = result.media
    if not media:
        raise RuntimeError("Ready result did not contain media")
    chat_id = update.effective_chat.id
    # ── 1404 upgrade: duplicate detection ─────────────────────────────
    # Same source URL + quality served before? Resend by cached file_id
    # instantly, without touching the source platform again.
    if user_id is not None and source_url and FLAGS.dedupe:
        if await user_features.try_send_deduped(context, chat_id, source_url, quality or "", reply_to):
            STATS.successful += 1
            await edit_status(
                status_message,
                status_card(
                    "✅ از کش سرور ارسال شد",
                    "این محتوا قبلاً دانلود شده بود؛ بدون دانلود مجدد برایت فرستادم.",
                ),
            )
            return
    total_size = sum(item.size for item in media)
    bot_username = getattr(context.bot, "username", None)
    captured_file_ids: list[tuple[str, str, int, str]] = []
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
                sent_message = await send_regular_file(
                    context,
                    chat_id=chat_id,
                    item=item,
                    caption=caption,
                    reply_to=reply_to,
                    progress=progress,
                    upload_label=label,
                    reply_markup=extra_markup,
                )
                captured = _extract_file_id(sent_message)
                if captured is not None:
                    captured_file_ids.append(captured)
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
        await user_features.maybe_send_summarize_button(context, chat_id, reply_to, instagram_caption)
    # ── 1404 upgrade: stats / dedupe memory / auto-share / bookmark offer ──
    if user_id is not None:
        with contextlib.suppress(Exception):
            await user_features.after_success(
                context,
                user_id=user_id,
                chat_id=chat_id,
                source_url=source_url,
                platform_value=platform_value,
                media=media,
                quality=quality,
                file_ids=captured_file_ids or None,
            )
            await user_features.offer_bookmark(
                context,
                chat_id=chat_id,
                user_id=user_id,
                source_url=source_url,
                platform_value=platform_value,
                media=media,
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
    await send_feedback_sticker(context, chat_id, index=3)


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
    # YouTube-sites sessions don't hold a Telegram worker lease — skip the
    # pool release in that case.
    if session.lease is not None:
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
    *,
    skip_apify: bool = False,
    skip_voiddl: bool = False,
    skip_yoinku: bool = False,
    skip_ahm7: bool = False,
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
                "لینک رو درست بفرست — باید با <code>http://</code> یا <code>https://</code> شروع بشه.",
            ),
            reply_to,
        )
        return
    spotify_collection_type = spotify_resource_type(url)
    is_spotify_collection = platform == Platform.SPOTIFY and spotify_collection_type in {"album", "playlist"}
    normal_providers = () if is_spotify_collection else providers_for_platform(platform, SETTINGS)
    providers = tuple(dict.fromkeys((*normal_providers, *all_providers(SETTINGS))))
    # Platforms handled entirely by SOCIAL_GATEWAY (Pinterest, generic
    # yt-dlp URLs) have NO Telegram fallback. Don't bail out just because
    # `providers` is empty — we'll try yt-dlp first and only fail later
    # if yt-dlp also fails.
    _social_only = platform in {
        Platform.PINTEREST,
        Platform.YTDLP_GENERIC,
    }
    # Apify can serve the supported platforms without a Telethon worker.
    # It is deliberately optional: when no APIFY_TOKEN is configured, all
    # existing routing and fallback behavior is retained. The 1404 upgrade
    # adds Spotify / SoundCloud / Twitter / Facebook / Pinterest behind the
    # APIFY_NEW_PLATFORMS_ENABLED flag — on any failure the request falls back
    # to the exact pre-existing chain for that platform.
    use_apify = not skip_apify and APIFY_GATEWAY is not None and (
        platform in {Platform.YOUTUBE, Platform.INSTAGRAM}
        or (FLAGS.apify_new_platforms and platform in NEW_APIFY_PLATFORMS)
    )
    # VoidDL is the PRIMARY downloader for YouTube (https://voiddl.app).
    # When VOIDDL_GATEWAY is configured, YouTube links hit VoidDL first;
    # the fallback chain (Yoinku → Apify → Telegram bots) only kicks in
    # when VoidDL fails or returns no menu.
    use_voiddl = (
        not skip_voiddl
        and VOIDDL_GATEWAY is not None
        and platform == Platform.YOUTUBE
        and not is_spotify_collection
    )
    # Yoinku is fallback #1 for YouTube. When YOINKU_GATEWAY
    # is configured, YouTube links hit Yoinku after VoidDL; the fallback
    # chain (Apify → Telegram bots) only kicks in when Yoinku fails or
    # returns no menu.
    use_yoinku = (
        not skip_yoinku
        and YOINKU_GATEWAY is not None
        and platform == Platform.YOUTUBE
        and not is_spotify_collection
    )
    # AHM7 is the PRIMARY downloader for the 10 supported platforms
    # (TikTok / Instagram / Facebook / X / Reddit / Snapchat / SoundCloud /
    # CapCut / SnackVideo / Douyin). The fallback chain (Apify where
    # applicable → Telegram bots) only kicks in when AHM7 fails.
    #
    # Instagram image carousels (URLs carrying ``img_index``) are an
    # exception: AHM7's ``alldl`` endpoint only returns ``videoUrl`` /
    # ``audioUrl`` and cannot serve photo carousels, so these posts are
    # routed through Apify's ``instagram-scraper`` Actor as their
    # *primary* downloader. Instagram Reels and other non-carousel posts
    # still hit AHM7 first.
    use_ahm7 = (
        not skip_ahm7
        and AHM7_GATEWAY is not None
        and platform in AHM7_SUPPORTED_PLATFORMS
        and not is_spotify_collection
        and not is_instagram_image_post(url)
    )
    if not providers and not is_spotify_collection and not _social_only and not use_apify and not use_voiddl and not use_yoinku and not use_ahm7:
        STATS.failed += 1
        return
    info = platform_info(platform)
    # Platforms that don't need a Telegram worker (handled entirely by
    # VOIDDL / YOINKU / AHM7 / Apify / SOCIAL_GATEWAY) skip the queue /
    # pool availability checks so users can download even when no
    # Telegram account is configured.
    _workerless = is_spotify_collection or _social_only or use_apify or use_voiddl or use_yoinku or use_ahm7
    if not _workerless and ACCOUNT_POOL.queue_length >= SETTINGS.max_queue_size:
        STATS.failed += 1
        await send_status(
            context,
            chat_id,
            status_card("⏳ یکم شلوغه", "صف فعلاً پر شده؛ چند لحظه دیگه دوباره امتحان کن."),
            reply_to,
        )
        return
    if not _workerless and ACCOUNT_POOL.total == 0:
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

    queued = (
        not _workerless
        and ACCOUNT_POOL.total > 0
        and ACCOUNT_POOL.busy_count >= ACCOUNT_POOL.total
    )
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
        # ── VoidDL: PRIMARY downloader for YouTube ──────────────────────
        # https://voiddl.app — multi-key rotation (20 downloads/minute AND
        # 10 GB/day bandwidth per key; a limited key is skipped instantly
        # in favour of the next one). When VOIDDL_GATEWAY is configured,
        # YouTube links hit VoidDL FIRST; on failure the request falls
        # through to Yoinku → Apify → Telegram bots, preserving the
        # fallback chain.
        if use_voiddl and VOIDDL_GATEWAY is not None:
            await progress.update(
                15,
                "▶️ در حال آماده‌سازی ویدیو…",
                f"{info.icon} {info.label} • مسیر اصلی",
                force=True,
            )
            voiddl_attempt_dir = create_attempt_directory(
                SETTINGS.download_root, request_id, "voiddl",
            )
            try:
                voiddl_result = await VOIDDL_GATEWAY.request(
                    url=url,
                    platform=platform,
                    attempt_directory=voiddl_attempt_dir,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("VoidDL gateway crashed for %s: %s", request_id, exc)
                voiddl_result = GatewayResult(
                    status="error",
                    bot_username=VOIDDL_PROVIDER,
                    reason="voiddl_error",
                )

            if voiddl_result.status == "needs_selection":
                displayed_options = voiddl_result.options[:24]
                token = uuid.uuid4().hex[:12]
                # Quality buttons carry ONLY the quality (e.g. 480 / 720 /
                # MP3) — sizes and every other detail live in the caption
                # under the thumbnail photo.
                rows: list[list[InlineKeyboardButton]] = []
                row: list[InlineKeyboardButton] = []
                for option_index, option in enumerate(displayed_options):
                    row.append(InlineKeyboardButton(
                        option.label,
                        callback_data=f"sel:{token}:{option_index}",
                    ))
                    if len(row) == 3:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                card_intro = (voiddl_result.text or "").strip()
                if len(card_intro) > 700:
                    card_intro = card_intro[:697].rstrip() + "…"
                menu_text = status_card(
                    "🎚 کدوم کیفیت رو می‌خوای؟",
                    f"منبع: <code>{html_escape(host)}</code>\n" + card_intro,
                    f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه برای انتخاب کیفیت وقت داری",
                )
                status_message = await send_youtube_quality_card(
                    context,
                    chat_id,
                    reply_to,
                    status_message=status_message,
                    menu_text=menu_text,
                    rows=rows,
                    preview=voiddl_result.preview,
                    request_id=request_id,
                )
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
                    bot_username=VOIDDL_PROVIDER,
                    request_message_id=0,
                    menu_message_id=0,
                    options=displayed_options,
                    lease=None,
                    attempt_directory=voiddl_attempt_dir,
                    use_voiddl=True,
                    fallback_text=voiddl_result.text,
                )
                PENDING_SELECTIONS[token] = session
                hold_lease = True
                return

            # voiddl_result.status == "error" — fall through to Yoinku → Apify → Telegram bots
            logger.info(
                "VoidDL gateway failed for %s (reason=%s); falling back to Yoinku / Apify / Telegram bots",
                request_id, voiddl_result.reason,
            )
            cleanup_request_directory(voiddl_attempt_dir, SETTINGS.download_root)
            reasons.append(voiddl_result.reason or "voiddl_error")
            await progress.update(
                20,
                "🔄 مسیر اصلی جواب نداد، امتحان می‌کنم با مسیرهای بعدی…",
                f"{info.icon} {info.label} • مسیر جایگزین",
                force=True,
            )

        # ── Yoinku: fallback #1 for YouTube ─────────────────────────────
        # https://yoinku.com/api/v1 — multi-key rotation (30 requests/day
        # AND 5 requests/minute per key). YouTube links reach Yoinku after
        # VoidDL failed; on failure the request falls through to Apify →
        # Telegram bots, preserving the fallback chain.
        if use_yoinku and YOINKU_GATEWAY is not None:
            await progress.update(
                15,
                "▶️ در حال آماده‌سازی ویدیو…",
                f"{info.icon} {info.label} • مسیر اصلی",
                force=True,
            )
            yoinku_attempt_dir = create_attempt_directory(
                SETTINGS.download_root, request_id, "yoinku",
            )
            try:
                yoinku_result = await YOINKU_GATEWAY.request(
                    url=url,
                    platform=platform,
                    attempt_directory=yoinku_attempt_dir,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Yoinku gateway crashed for %s: %s", request_id, exc)
                yoinku_result = GatewayResult(
                    status="error",
                    bot_username=YOINKU_PROVIDER,
                    reason="yoinku_error",
                )

            if yoinku_result.status == "ready":
                await send_result_to_user(
                    update, context, status_message, yoinku_result,
                    reply_to=reply_to, request_id=request_id, progress=progress,
                    source_url=url, platform_value=platform.value,
                    user_id=update.effective_user.id,
                )
                await send_subtitle_followup(
                    context, chat_id=chat_id, reply_to=reply_to,
                    youtube_url=url, result=yoinku_result,
                )
                cleanup_request_directory(yoinku_attempt_dir, SETTINGS.download_root)
                return

            if yoinku_result.status == "needs_selection":
                displayed_options = yoinku_result.options[:24]
                token = uuid.uuid4().hex[:12]
                # Quality buttons carry ONLY the quality (e.g. 480 / 720 /
                # MP3) — sizes and every other detail live in the caption
                # under the thumbnail photo.
                rows: list[list[InlineKeyboardButton]] = []
                row: list[InlineKeyboardButton] = []
                for option_index, option in enumerate(displayed_options):
                    row.append(InlineKeyboardButton(
                        option.label,
                        callback_data=f"sel:{token}:{option_index}",
                    ))
                    if len(row) == 3:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                card_intro = (yoinku_result.text or "").strip()
                if len(card_intro) > 700:
                    card_intro = card_intro[:697].rstrip() + "…"
                menu_text = status_card(
                    "🎚 کدوم کیفیت رو می‌خوای؟",
                    f"منبع: <code>{html_escape(host)}</code>\n" + card_intro,
                    f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه برای انتخاب کیفیت وقت داری",
                )
                # Best-quality YouTube thumbnail for the card (Yoinku
                # itself does not provide one).
                yoinku_preview = yoinku_result.preview
                if yoinku_preview is None:
                    yoinku_preview = await download_youtube_thumbnail(
                        url, yoinku_attempt_dir, proxy_url=_caption_proxy_url(),
                    )
                status_message = await send_youtube_quality_card(
                    context,
                    chat_id,
                    reply_to,
                    status_message=status_message,
                    menu_text=menu_text,
                    rows=rows,
                    preview=yoinku_preview,
                    request_id=request_id,
                )
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
                    bot_username=YOINKU_PROVIDER,
                    request_message_id=0,
                    menu_message_id=0,
                    options=displayed_options,
                    lease=None,
                    attempt_directory=yoinku_attempt_dir,
                    use_yoinku=True,
                    fallback_text=yoinku_result.text,
                )
                PENDING_SELECTIONS[token] = session
                hold_lease = True
                return

            # yoinku_result.status == "error" — fall through to Apify → Telegram bots
            logger.info(
                "Yoinku gateway failed for %s (reason=%s); falling back to Apify / Telegram bots",
                request_id, yoinku_result.reason,
            )
            cleanup_request_directory(yoinku_attempt_dir, SETTINGS.download_root)
            reasons.append(yoinku_result.reason or "yoinku_error")
            await progress.update(
                20,
                "🔄 مسیر اصلی جواب نداد، امتحان می‌کنم با مسیرهای بعدی…",
                f"{info.icon} {info.label} • مسیر جایگزین",
                force=True,
            )

        # ── AHM7: PRIMARY downloader for TikTok / Instagram / Facebook / X
        # / Reddit / Snapchat / SoundCloud / CapCut / SnackVideo / Douyin
        # via https://ahm7xmakki.com/api/alldl. When the user picks MP3 but
        # the API returned no audioUrl, ffmpeg extracts the audio track
        # (``ffmpeg -i input.mp4 -vn -c:a libmp3lame -b:a 192k output.mp3``).
        if use_ahm7 and AHM7_GATEWAY is not None:
            await progress.update(
                15,
                "▶️ در حال آماده‌سازی…",
                f"{info.icon} {info.label} • مسیر اصلی",
                force=True,
            )
            ahm7_attempt_dir = create_attempt_directory(
                SETTINGS.download_root, request_id, "ahm7",
            )
            try:
                ahm7_result = await AHM7_GATEWAY.request(
                    url=url,
                    platform=platform,
                    attempt_directory=ahm7_attempt_dir,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("AHM7 gateway crashed for %s: %s", request_id, exc)
                ahm7_result = GatewayResult(
                    status="error",
                    bot_username=AHM7_PROVIDER,
                    reason="ahm7_error",
                )

            if ahm7_result.status == "ready":
                instagram_caption = await caption_task if caption_task is not None else ""
                caption_task = None
                await send_result_to_user(
                    update, context, status_message, ahm7_result,
                    reply_to=reply_to, request_id=request_id,
                    instagram_caption=instagram_caption,
                    progress=progress,
                    source_url=url, platform_value=platform.value,
                    user_id=update.effective_user.id,
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
                cleanup_request_directory(ahm7_attempt_dir, SETTINGS.download_root)
                return

            if ahm7_result.status == "needs_selection":
                displayed_options = ahm7_result.options[:24]
                token = uuid.uuid4().hex[:12]
                rows: list[list[InlineKeyboardButton]] = []
                video_choices = [
                    (i, o) for i, o in enumerate(displayed_options)
                    if o.action == "media" and o.expected_kind == MediaKind.VIDEO
                ]
                audio_choices = [
                    (i, o) for i, o in enumerate(displayed_options)
                    if o.action == "media" and o.expected_kind == MediaKind.AUDIO
                ]
                quick_row: list[InlineKeyboardButton] = []
                if video_choices:
                    best_index, best_option = video_choices[0]
                    quick_row.append(InlineKeyboardButton(
                        _truncate_button_label(f"⭐ بهترین کیفیت — {best_option.label}"),
                        callback_data=f"sel:{token}:{best_index}",
                    ))
                if audio_choices:
                    audio_index, audio_option = audio_choices[0]
                    quick_row.append(InlineKeyboardButton(
                        _truncate_button_label(f"🎵 فقط صدا — {audio_option.label}"),
                        callback_data=f"sel:{token}:{audio_index}",
                    ))
                if quick_row:
                    rows.append(quick_row)
                row: list[InlineKeyboardButton] = []
                for option_index, option in enumerate(displayed_options):
                    prefix = _option_prefix(option)
                    row.append(InlineKeyboardButton(
                        _truncate_button_label(f"{prefix} {option.label}"),
                        callback_data=f"sel:{token}:{option_index}",
                    ))
                    if len(row) == 2:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                if platform == Platform.INSTAGRAM:
                    rows.append([InlineKeyboardButton("📝 کپشن این پست", callback_data=f"caption:{token}")])
                    if is_instagram_reel(url):
                        REEL_MUSIC_URLS[token] = (url, time.monotonic(), chat_id, update.effective_user.id)
                        rows.append([InlineKeyboardButton("🎵 موزیک ریلز", callback_data=f"reel_music:{token}")])
                rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                card_intro = (ahm7_result.text or "").strip()
                if len(card_intro) > 500:
                    card_intro = card_intro[:497].rstrip() + "…"
                menu_text = status_card(
                    "🎚 کدوم خروجی رو می‌خوای؟",
                    (card_intro + "\n\n" if card_intro else "")
                    + "کیفیت یا صدا رو انتخاب کن.",
                    f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه وقت داری",
                )
                keyboard = InlineKeyboardMarkup(rows)
                if ahm7_result.preview is not None:
                    try:
                        with ahm7_result.preview.path.open("rb") as preview_handle:
                            card_message = await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=preview_handle,
                                caption=menu_text,
                                parse_mode=ParseMode.HTML,
                                reply_markup=keyboard,
                                reply_to_message_id=reply_to,
                            )
                        with contextlib.suppress(TelegramError):
                            await status_message.delete()
                        status_message = card_message
                    except TelegramError as exc:
                        logger.warning("AHM7 thumbnail send failed for %s: %s", request_id, exc)
                        await edit_status(status_message, menu_text, keyboard)
                else:
                    await edit_status(status_message, menu_text, keyboard)
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
                    bot_username=AHM7_PROVIDER,
                    request_message_id=0,
                    menu_message_id=0,
                    options=displayed_options,
                    lease=None,
                    attempt_directory=ahm7_attempt_dir,
                    use_ahm7=True,
                    fallback_text=ahm7_result.text,
                    caption_task=caption_task,
                )
                caption_task = None
                PENDING_SELECTIONS[token] = session
                hold_lease = True
                return

            # ahm7_result.status == "error" — fall through to Apify / Telegram bots
            logger.info(
                "AHM7 gateway failed for %s (reason=%s); falling back to next chain",
                request_id, ahm7_result.reason,
            )
            cleanup_request_directory(ahm7_attempt_dir, SETTINGS.download_root)
            reasons.append(ahm7_result.reason or "ahm7_error")
            await progress.update(
                20,
                "🔄 مسیر اصلی جواب نداد، امتحان می‌کنم با مسیرهای بعدی…",
                f"{info.icon} {info.label} • مسیر جایگزین",
                force=True,
            )

        # ── Apify: choose first; start an Actor only after the click ─────
        # This preserves the existing inline-button UX and avoids spending an
        # Apify credit before the user has selected video quality or audio.
        if use_apify and APIFY_GATEWAY is not None:
            apify_attempt_dir = create_attempt_directory(
                SETTINGS.download_root, request_id, "apify",
            )
            apify_result = await APIFY_GATEWAY.request(
                url=url,
                platform=platform,
                attempt_directory=apify_attempt_dir,
                progress_callback=progress.download,
            )
            if apify_result.status == "needs_selection":
                displayed_options = apify_result.options[:24]
                token = uuid.uuid4().hex[:12]
                rows: list[list[InlineKeyboardButton]] = []
                if platform == Platform.YOUTUBE:
                    # YouTube: quality buttons carry ONLY the quality
                    # (480 / 720 / MP3) — size hints and other details live
                    # in the caption under the thumbnail photo.
                    size_lines: list[str] = []
                    row: list[InlineKeyboardButton] = []
                    for option_index, option in enumerate(displayed_options):
                        display_label = _youtube_button_label(option)
                        row.append(InlineKeyboardButton(
                            display_label,
                            callback_data=f"sel:{token}:{option_index}",
                        ))
                        if len(row) == 3:
                            rows.append(row)
                            row = []
                        size_lines.append(f"• {display_label} — {option_size_hint(option)}")
                    if row:
                        rows.append(row)
                    rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                    card_intro = (apify_result.text or "").strip()
                    if len(card_intro) > 400:
                        card_intro = card_intro[:397].rstrip() + "…"
                    size_block = "📦 حجم تقریبی هر کیفیت (در هر دقیقه):\n" + "\n".join(size_lines)
                    menu_text = status_card(
                        "🎚 کدوم کیفیت رو می‌خوای؟",
                        f"منبع: <code>{html_escape(host)}</code>\n"
                        + (card_intro + "\n\n" if card_intro else "")
                        + size_block,
                        f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه برای انتخاب کیفیت وقت داری",
                    )
                    # Best-quality YouTube thumbnail for the card.
                    apify_preview = apify_result.preview
                    if apify_preview is None:
                        apify_preview = await download_youtube_thumbnail(
                            url, apify_attempt_dir, proxy_url=_caption_proxy_url(),
                        )
                    status_message = await send_youtube_quality_card(
                        context,
                        chat_id,
                        reply_to,
                        status_message=status_message,
                        menu_text=menu_text,
                        rows=rows,
                        preview=apify_preview,
                        request_id=request_id,
                    )
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
                        bot_username=APIFY_PROVIDER,
                        request_message_id=0,
                        menu_message_id=0,
                        options=displayed_options,
                        lease=None,
                        attempt_directory=apify_attempt_dir,
                        use_apify=True,
                        caption_task=caption_task,
                    )
                    caption_task = None
                    PENDING_SELECTIONS[token] = session
                    hold_lease = True
                    return
                video_choices = [
                    (index, option)
                    for index, option in enumerate(displayed_options)
                    if option.action == "media" and option.expected_kind == MediaKind.VIDEO
                ]
                audio_choices = [
                    (index, option)
                    for index, option in enumerate(displayed_options)
                    if option.action == "media" and option.expected_kind == MediaKind.AUDIO
                ]
                quick_row: list[InlineKeyboardButton] = []
                if video_choices:
                    best_index, best_option = max(
                        video_choices,
                        key=lambda item: item[1].expected_height or 0,
                    )
                    quick_row.append(InlineKeyboardButton(
                        _truncate_button_label(f"⭐ بهترین • {option_size_hint(best_option)}"),
                        callback_data=f"sel:{token}:{best_index}",
                    ))
                if audio_choices:
                    audio_index, audio_option = max(
                        audio_choices,
                        key=lambda item: item[1].expected_bitrate_kbps or 0,
                    )
                    quick_row.append(InlineKeyboardButton(
                        _truncate_button_label(f"🎵 MP3 • {option_size_hint(audio_option)}"),
                        callback_data=f"sel:{token}:{audio_index}",
                    ))
                if quick_row:
                    rows.append(quick_row)
                row: list[InlineKeyboardButton] = []
                for option_index, option in enumerate(displayed_options):
                    prefix = _option_prefix(option)
                    row.append(InlineKeyboardButton(
                        _truncate_button_label(f"{prefix} {option.label} • {option_size_hint(option)}"),
                        callback_data=f"sel:{token}:{option_index}",
                    ))
                    if len(row) == 2:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                if platform == Platform.INSTAGRAM:
                    rows.append([InlineKeyboardButton("📝 کپشن این پست", callback_data=f"caption:{token}")])
                    if is_instagram_reel(url):
                        REEL_MUSIC_URLS[token] = (url, time.monotonic(), chat_id, update.effective_user.id)
                        rows.append([InlineKeyboardButton("🎵 موزیک ریلز", callback_data=f"reel_music:{token}")])
                rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                card_intro = (apify_result.text or "").strip()
                if len(card_intro) > 500:
                    card_intro = card_intro[:497].rstrip() + "…"
                menu_text = status_card(
                    "🎚 کدوم خروجی رو می‌خوای؟",
                    (card_intro + "\n\n" if card_intro else "")
                    + "حجم‌های کنار دکمه‌ها تقریبی و برای هر دقیقه هستند.\n"
                    + "کیفیت یا صدا رو انتخاب کن.",
                    f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه وقت داری",
                )
                keyboard = InlineKeyboardMarkup(rows)
                if apify_result.preview is not None:
                    try:
                        with apify_result.preview.path.open("rb") as preview_handle:
                            card_message = await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=preview_handle,
                                caption=menu_text,
                                parse_mode=ParseMode.HTML,
                                reply_markup=keyboard,
                                reply_to_message_id=reply_to,
                            )
                        with contextlib.suppress(TelegramError):
                            await status_message.delete()
                        status_message = card_message
                    except TelegramError as exc:
                        logger.warning("Apify thumbnail send failed for %s: %s", request_id, exc)
                        await edit_status(status_message, menu_text, keyboard)
                else:
                    await edit_status(status_message, menu_text, keyboard)
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
                    bot_username=APIFY_PROVIDER,
                    request_message_id=0,
                    menu_message_id=0,
                    options=displayed_options,
                    lease=None,
                    attempt_directory=apify_attempt_dir,
                    use_apify=True,
                    caption_task=caption_task,
                )
                caption_task = None
                PENDING_SELECTIONS[token] = session
                hold_lease = True
                return
            logger.info(
                "Apify gateway could not create a menu for %s (reason=%s); using existing fallback chain",
                request_id,
                apify_result.reason,
            )
            reasons.append(apify_result.reason or "apify_error")
            cleanup_request_directory(apify_attempt_dir, SETTINGS.download_root)
            await progress.update(
                20,
                "🔄 مسیر اصلی جواب نداد؛ مسیرهای قبلی را امتحان می‌کنم…",
                f"{info.icon} {info.label} • مسیر جایگزین",
                force=True,
            )

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
                        source_url=url,
                        platform_value=platform.value,
                        user_id=update.effective_user.id,
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
                    source_url=url,
                    platform_value=platform.value,
                    user_id=update.effective_user.id,
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

        # ── Social-gateway platforms (Pinterest + generic yt-dlp URLs):
        # try the social gateway FIRST ──
        # The social gateway uses yt-dlp for Pinterest and for anything
        # else it supports (Vimeo, Dailymotion, Twitch, etc.). It does
        # not need a Telegram worker. The TikTok / Instagram / Twitter /
        # Facebook / SoundCloud / Reddit / Snapchat / CapCut / SnackVideo
        # / Douyin paths are handled by AHM7 first (above), so this branch
        # is now Pinterest + generic-only. If yt-dlp fails (e.g. no
        # matching cookies in cookies.txt), the request surfaces the
        # yt-dlp error directly — these platforms have NO Telegram fallback.
        if (
            platform in {
                Platform.PINTEREST,
                Platform.YTDLP_GENERIC,
            }
            and SOCIAL_GATEWAY is not None
            and not is_spotify_collection
        ):
            await progress.update(
                15,
                "▶️ دارم از طریق سرویس‌های آنلاین آماده‌اش می‌کنم…",
                f"{info.icon} {info.label} • مسیر اصلی (social)",
                force=True,
            )
            social_attempt_dir = create_attempt_directory(
                SETTINGS.download_root, request_id, "social",
            )
            try:
                social_result = await SOCIAL_GATEWAY.request(
                    url=url,
                    platform=platform,
                    attempt_directory=social_attempt_dir,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("social gateway crashed for %s: %s", request_id, exc)
                social_result = GatewayResult(
                    status="error",
                    bot_username=SOCIAL_PROVIDER,
                    reason="social_error",
                )

            if social_result.status == "ready":
                # Rare (the gateway returns needs_selection first), but handle it.
                await send_result_to_user(
                    update, context, status_message, social_result,
                    reply_to=reply_to, request_id=request_id, progress=progress,
                    source_url=url, platform_value=platform.value,
                    user_id=update.effective_user.id,
                )
                cleanup_request_directory(social_attempt_dir, SETTINGS.download_root)
                return

            if social_result.status == "needs_selection":
                # Show the quality menu WITHOUT acquiring a Telegram worker.
                displayed_options = social_result.options[:24]
                token = uuid.uuid4().hex[:12]
                rows: list[list[InlineKeyboardButton]] = []
                video_choices = [
                    (i, o) for i, o in enumerate(displayed_options)
                    if o.action == "media" and o.expected_kind == MediaKind.VIDEO
                ]
                audio_choices = [
                    (i, o) for i, o in enumerate(displayed_options)
                    if o.action == "media" and o.expected_kind == MediaKind.AUDIO
                ]
                quick_row: list[InlineKeyboardButton] = []
                if video_choices:
                    best_index, best_option = max(
                        video_choices,
                        key=lambda item: item[1].expected_height or 0,
                    )
                    quick_row.append(InlineKeyboardButton(
                        _truncate_button_label(f"⭐ بهترین کیفیت — {best_option.label}"),
                        callback_data=f"sel:{token}:{best_index}",
                    ))
                if audio_choices:
                    audio_index, audio_option = max(
                        audio_choices,
                        key=lambda item: item[1].expected_bitrate_kbps or 0,
                    )
                    quick_row.append(InlineKeyboardButton(
                        _truncate_button_label(f"🎵 فقط صدا — {audio_option.label}"),
                        callback_data=f"sel:{token}:{audio_index}",
                    ))
                if quick_row:
                    rows.append(quick_row)
                row: list[InlineKeyboardButton] = []
                for option_index, option in enumerate(displayed_options):
                    prefix = _option_prefix(option)
                    raw_label = option.label if len(option.label) <= 40 else option.label[:37] + "…"
                    row.append(InlineKeyboardButton(
                        _truncate_button_label(f"{prefix} {raw_label}"),
                        callback_data=f"sel:{token}:{option_index}",
                    ))
                    if len(row) == 2:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                menu_text = status_card(
                    "🎚 کدوم خروجی رو می‌خوای؟",
                    f"منبع: <code>{html_escape(host)}</code>\nکیفیت یا صدا رو انتخاب کن.",
                    f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه وقت داری",
                )
                if social_result.preview is not None:
                    try:
                        with social_result.preview.path.open("rb") as preview_handle:
                            await context.bot.send_photo(
                                chat_id=chat_id, photo=preview_handle,
                                caption=f"🖼 پیش‌نمایش • {host}",
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
                    bot_username=SOCIAL_PROVIDER,
                    request_message_id=0,
                    menu_message_id=0,
                    options=displayed_options,
                    lease=None,  # No Telegram worker — social gateway does the download
                    attempt_directory=social_attempt_dir,
                    use_social_sites=True,
                    fallback_text=social_result.text,
                )
                PENDING_SELECTIONS[token] = session
                await edit_status(status_message, menu_text, InlineKeyboardMarkup(rows))
                hold_lease = True  # Prevents the finally block from cleaning up
                return

            # social_result.status == "error" — fall through to Telegram bots
            logger.info(
                "social gateway failed for %s (reason=%s); falling back to Telegram bots",
                request_id, social_result.reason,
            )
            cleanup_request_directory(social_attempt_dir, SETTINGS.download_root)
            reasons.append(social_result.reason or "social_error")
            # Pinterest and generic yt-dlp URLs have NO Telegram fallback
            # (no dedicated downloader bots exist for them). If providers
            # is empty, surface a clean error to the user instead of
            # trying to acquire a Telegram worker we'll never use.
            if not providers:
                await edit_status(
                    status_message,
                    failure_text(reasons, request_id),
                )
                STATS.failed += 1
                await send_feedback_sticker(context, chat_id, index=4)
                return
            await progress.update(
                20,
                "🔄 سرویس آنلاین جواب نداد، امتحان می‌کنم با بات‌های دانلود…",
                f"{info.icon} {info.label} • مسیر جایگزین",
                force=True,
            )

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
                    source_url=url,
                    platform_value=platform.value,
                    user_id=update.effective_user.id,
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
                elif platform == Platform.YOUTUBE:
                    # Subtitle follow-up: offer Persian & English SRT download.
                    await send_subtitle_followup(
                        context,
                        chat_id=chat_id,
                        reply_to=reply_to,
                        youtube_url=url,
                        result=result,
                    )
                return
            if result.status == "needs_selection":
                displayed_options = tuple(
                    option
                    for option in result.options
                    if not (platform == Platform.INSTAGRAM and option.action == "caption")
                )[:24]
                youtube_formats: tuple[YouTubeFormatSize, ...] = ()
                if platform == Platform.YOUTUBE:
                    youtube_formats = await YOUTUBE_SEARCH.format_sizes(url)
                appended_size_note = False
                token = uuid.uuid4().hex[:12]
                rows: list[list[InlineKeyboardButton]] = []
                if platform == Platform.YOUTUBE:
                    # YouTube: quality buttons carry ONLY the quality
                    # (480 / 720 / MP3) — estimated sizes and other details
                    # live in the caption under the thumbnail photo.
                    size_lines: list[str] = []
                    row: list[InlineKeyboardButton] = []
                    for option_index, option in enumerate(displayed_options):
                        display_label = _youtube_button_label(option)
                        row.append(InlineKeyboardButton(
                            display_label,
                            callback_data=f"sel:{token}:{option_index}",
                        ))
                        if len(row) == 3:
                            rows.append(row)
                            row = []
                        option_size = estimate_youtube_size(
                            youtube_formats,
                            is_audio=option.expected_kind == MediaKind.AUDIO,
                            target_height=option.expected_height,
                            target_bitrate_kbps=option.expected_bitrate_kbps,
                        )
                        size_text = f"≈{fmt_size(option_size)}" if option_size else "تقریبی"
                        size_lines.append(f"• {display_label} — {size_text}")
                    if row:
                        rows.append(row)
                    rows.append([InlineKeyboardButton("لغو درخواست", callback_data=f"cancel:{token}")])
                    size_block = "📦 حجم تقریبی هر کیفیت:\n" + "\n".join(size_lines)
                    menu_text = status_card(
                        "🎚 کدوم کیفیت رو می‌خوای؟",
                        f"منبع: <code>{html_escape(host)}</code>\n" + size_block,
                        f"تا {int(SETTINGS.selection_ttl // 60)} دقیقه برای انتخاب کیفیت وقت داری",
                    )
                    # Best-quality YouTube thumbnail for the card (the
                    # downloader bot's own preview is used when available).
                    telegram_preview = result.preview
                    if telegram_preview is None:
                        telegram_preview = await download_youtube_thumbnail(
                            url, attempt_directory, proxy_url=_caption_proxy_url(),
                        )
                    status_message = await send_youtube_quality_card(
                        context,
                        chat_id,
                        reply_to,
                        status_message=status_message,
                        menu_text=menu_text,
                        rows=rows,
                        preview=telegram_preview,
                        request_id=request_id,
                    )
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
                    hold_lease = True
                    return
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
                    best_index, best_option = max(
                        video_choices,
                        key=lambda item: item[1].expected_height or 0,
                    )
                    best_label = "⭐ بهترین کیفیت"
                    best_size = estimate_youtube_size(
                        youtube_formats,
                        is_audio=False,
                        target_height=best_option.expected_height,
                    )
                    if best_size:
                        best_label = f"{best_label} - {fmt_size(best_size)}"
                        appended_size_note = True
                    quick_row.append(
                        InlineKeyboardButton(_truncate_button_label(best_label), callback_data=f"sel:{token}:{best_index}")
                    )
                if audio_choices:
                    audio_index, audio_option = max(
                        audio_choices,
                        key=lambda item: item[1].expected_bitrate_kbps or 0,
                    )
                    audio_label = "🎵 فقط صدا"
                    audio_size = estimate_youtube_size(
                        youtube_formats,
                        is_audio=True,
                        target_bitrate_kbps=audio_option.expected_bitrate_kbps,
                    )
                    if audio_size:
                        audio_label = f"{audio_label} - {fmt_size(audio_size)}"
                        appended_size_note = True
                    quick_row.append(
                        InlineKeyboardButton(_truncate_button_label(audio_label), callback_data=f"sel:{token}:{audio_index}")
                    )
                if quick_row:
                    rows.append(quick_row)
                row: list[InlineKeyboardButton] = []
                for option_index, option in enumerate(displayed_options):
                    if option.action == "caption":
                        label = "📝 دریافت کپشن"
                    else:
                        prefix = _option_prefix(option)
                        raw_label = option.label if len(option.label) <= 40 else option.label[:37] + "…"
                        label = f"{prefix} {raw_label}"
                        option_size = estimate_youtube_size(
                            youtube_formats,
                            is_audio=option.expected_kind == MediaKind.AUDIO,
                            target_height=option.expected_height,
                            target_bitrate_kbps=option.expected_bitrate_kbps,
                        )
                        if option_size:
                            label = f"{label} - {fmt_size(option_size)}"
                            appended_size_note = True
                    row.append(
                        InlineKeyboardButton(_truncate_button_label(label), callback_data=f"sel:{token}:{option_index}")
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
                if appended_size_note:
                    with contextlib.suppress(TelegramError):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text="⚠️ حجم‌های نوشته‌شده روی دکمه‌ها تقریبی‌اند و ممکنه با فایل نهایی که دریافت می‌کنی کمی فرق داشته باشن.",
                            reply_to_message_id=reply_to,
                        )
                hold_lease = True
                return

            reasons.append(result.reason or "service_error")
            cleanup_request_directory(attempt_directory, SETTINGS.download_root)
            attempt_directory = None

        STATS.failed += 1
        await edit_status(status_message, failure_text(reasons, request_id))
        await send_feedback_sticker(context, chat_id, index=4)
    except (PoolUnavailable, asyncio.TimeoutError):
        STATS.failed += 1
        await edit_status(
            status_message,
            status_card("🛠 بخش دانلود آماده نیست", "لطفاً کمی بعد دوباره امتحان کن."
        ))
        await send_feedback_sticker(context, chat_id, index=4)
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
        await send_feedback_sticker(context, chat_id, index=4)
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


# --- Shazam song search command + callbacks -----------------------------

async def run_shazam_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_query: str,
) -> None:
    """Run a Shazam song search and send the first page (cover collage + buttons)."""
    message = update.effective_message
    key = request_owner_key(update)
    try:
        query_text = normalize_song_query(raw_query)
    except ShazamSearchError:
        await message.reply_text(
            status_card(
                "🎵 چی رو جست‌وجو کنم؟",
                "نام خواننده یا آهنگ رو بنویس؛ مثلاً <code>/song Coldplay Yellow</code>.",
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    if key in ACTIVE_SHAZAM_SEARCHES:
        await message.reply_text(
            status_card("⏳ جست‌وجوی قبلی هنوز ادامه دارد", "چند لحظه صبر کن تا نتیجه‌ها آماده شوند."),
            parse_mode=ParseMode.HTML,
        )
        return
    if not allow_shazam_search(key):
        await message.reply_text(
            status_card("⏱ کمی آهسته‌تر", "تا یک دقیقهٔ دیگر دوباره جست‌وجو کن."),
            parse_mode=ParseMode.HTML,
        )
        return

    prune_shazam_search_sessions()
    MEMBERSHIP_CACHE[update.effective_user.id] = max(
        MEMBERSHIP_CACHE.get(update.effective_user.id, 0),
        time.monotonic() + SHAZAM_SEARCH_TTL,
    )
    ACTIVE_SHAZAM_SEARCHES.add(key)
    status_message: Any | None = None
    session: ShazamSearchSession | None = None
    try:
        status_message = await send_status(
            context,
            update.effective_chat.id,
            status_card(
                "🎵 دارم آهنگ‌ها رو می‌گردم…",
                f"عبارت: <b>{html_escape(query_text)}</b>\nتا ۳۰ نتیجه رو با کاور و اسم آماده می‌کنم.",
            ),
            message.message_id,
        )
        results = await SHAZAM_SEARCH.search(query_text)
        if not results:
            await edit_status(
                status_message,
                status_card(
                    "😕 نتیجه‌ای پیدا نشد",
                    f"برای <b>{html_escape(query_text)}</b> آهنگی پیدا نشد.",
                    "عبارت دیگری را امتحان کن.",
                ),
            )
            return
        token = uuid.uuid4().hex[:12]
        session = ShazamSearchSession(
            token=token,
            created_at=time.monotonic(),
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id,
            reply_to=message.message_id,
            query=query_text,
            results=results[:SHAZAM_SEARCH_MAX_RESULTS],
        )
        page_image = await SHAZAM_SEARCH.build_page_image(session.results, 0)
        SHAZAM_SEARCH_SESSIONS[token] = session
        prune_shazam_search_sessions()

        async def send_results() -> Any:
            return await context.bot.send_photo(
                chat_id=session.chat_id,
                photo=page_image,
                filename=f"shazam-search-{token}-1.jpg",
                caption=shazam_search_caption(session, 0),
                parse_mode=ParseMode.HTML,
                reply_markup=shazam_search_keyboard(session, 0),
                reply_to_message_id=session.reply_to,
            )

        await telegram_retry(send_results)
        with contextlib.suppress(TelegramError, AttributeError):
            await status_message.delete()
    except ShazamSearchError as exc:
        logger.info("Shazam search unavailable: %s", exc)
        if status_message is not None:
            await edit_status(
                status_message,
                status_card(
                    "😕 نتیجه‌ای آماده نشد",
                    "جست‌وجوی آهنگ موقتاً پاسخ نداد یا نتیجه‌ای پیدا نشد.",
                    "عبارت دیگری را امتحان کن.",
                ),
            )
    except TelegramError as exc:
        logger.warning("Shazam search result could not be sent: %s", exc)
        if session is not None:
            SHAZAM_SEARCH_SESSIONS.pop(session.token, None)
        if status_message is not None:
            await edit_status(
                status_message,
                status_card("😕 ارسال نتیجه‌ها ناموفق بود", "چند لحظهٔ دیگر دوباره امتحان کن."),
            )
    except Exception as exc:
        logger.exception("Unexpected Shazam search failure: %s", exc)
        if session is not None:
            SHAZAM_SEARCH_SESSIONS.pop(session.token, None)
        if status_message is not None:
            await edit_status(
                status_message,
                status_card("😕 جست‌وجو انجام نشد", "چند لحظهٔ دیگر دوباره امتحان کن."),
            )
    finally:
        ACTIVE_SHAZAM_SEARCHES.discard(key)


@membership_required
async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /song <query> — search songs via ShazamIO (with iTunes fallback)."""
    query_text = " ".join(context.args or ())
    if not query_text and update.effective_message.reply_to_message is not None:
        replied = update.effective_message.reply_to_message
        query_text = replied.text or replied.caption or ""
    await run_shazam_search(update, context, query_text)


@membership_required
async def on_shazam_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses for the Shazam song search results."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    token = parts[1] if len(parts) > 1 else ""
    prune_shazam_search_sessions()
    session = SHAZAM_SEARCH_SESSIONS.get(token)
    if session is None:
        await query.answer("این جست‌وجو منقضی شده؛ دوباره /song بزن.", show_alert=True)
        return
    if session.chat_id != update.effective_chat.id or session.user_id != update.effective_user.id:
        await query.answer("این نتیجه‌ها متعلق به شما نیست.", show_alert=True)
        return
    if session.selected:
        await query.answer("این آهنگ قبلاً انتخاب شده است.", show_alert=True)
        return
    try:
        value = int(parts[2])
    except (IndexError, TypeError, ValueError):
        await query.answer("دکمه معتبر نیست.", show_alert=True)
        return

    # --- Song selection (ss:) ---
    if data.startswith("ss:"):
        if session.busy:
            with contextlib.suppress(TelegramError):
                await query.answer("صبر کن صفحه کامل آماده شود.")
            return
        if value < 0 or value >= len(session.results):
            await query.answer("این آهنگ در دسترس نیست.", show_alert=True)
            return
        session.selected = True
        selected = session.results[value]
        with contextlib.suppress(TelegramError):
            await query.answer(f"🎵 {selected.label} — دارم پیدا می‌کنم…")
        # Look up the song on YouTube so the existing download pipeline can
        # take over (the bot already knows how to download from YouTube).
        proxy_url = (
            f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
            if SETTINGS.use_proxy
            else None
        )
        youtube_url = await youtube_url_for_song(
            selected.artist_name,
            selected.track_name,
            proxy_url=proxy_url,
        )
        if not youtube_url:
            session.selected = False
            with contextlib.suppress(TelegramError):
                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=status_card(
                        "😕 لینک دانلود پیدا نشد",
                        f"برای <b>{html_escape(selected.label)}</b> نسخهٔ YouTube پیدا نشد.",
                        "آهنگ دیگری را انتخاب کن.",
                    ),
                    reply_to_message_id=session.reply_to,
                    parse_mode=ParseMode.HTML,
                )
            return
        try:
            accepted = await process_urls(update, context, (youtube_url,), session.reply_to)
        except BaseException:
            session.selected = False
            raise
        if accepted:
            SHAZAM_SEARCH_SESSIONS.pop(token, None)
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
        else:
            session.selected = False
        return

    # --- Pagination (sp:) ---
    if not data.startswith("sp:"):
        await query.answer()
        return
    page_count = shazam_search_page_count(session)
    if value < 0 or value >= page_count:
        await query.answer("این صفحه وجود ندارد.", show_alert=True)
        return
    if session.busy:
        await query.answer("دارم صفحه را می‌سازم…")
        return
    if value == session.current_page:
        await query.answer()
        return

    session.busy = True
    try:
        with contextlib.suppress(TelegramError):
            await query.answer("دارم صفحه را می‌سازم…")
        page_image = await SHAZAM_SEARCH.build_page_image(session.results, value)
        if SHAZAM_SEARCH_SESSIONS.get(token) is not session or session.selected:
            return
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=page_image,
                filename=f"shazam-search-{token}-{value + 1}.jpg",
                caption=shazam_search_caption(session, value),
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=shazam_search_keyboard(session, value),
        )
        session.current_page = value
    except (ShazamSearchError, TelegramError) as exc:
        logger.warning("Shazam search page %d failed: %s", value, exc)
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
    "📝 دانلود ساب‌تایتل فارسی و انگلیسی برای ویدیوهای یوتیوب\n"
    "🎵 جست‌وجوی آهنگ با /song (کاور + خواننده - اسم آهنگ)\n"
    "🎧 آهنگ تکی، آلبوم و پلی‌لیست ZIP\n"
    "📸 کپشن پست‌های Instagram\n"
    "🖼 پیش‌نمایش، فقط صدا و دانلود چندتایی\n"
    "🎶 تشخیص موزیک ریلز اینستاگرام\n"
    "📦 ارسال مرتب عکس‌ها به‌صورت پک",
    "برای جست‌وجو عبارت را بنویس یا /search را بزن • راهنما: /help",
)

WELCOME_GROUP = status_card(
    "👋 MZ Downloader اومد توی گروه!",
    "برای دانلود <code>/dl لینک</code> رو بفرست؛ یا روی پیام لینک‌دار ریپلای کن و <code>/dl</code> بزن.",
    "برای خلوت موندن گروه، لینک‌های عادی خودکار دانلود نمی‌شن.",
)


# ── Instagram Profile Feature ─────────────────────────────────────────


@membership_required
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /profile <username> — fetch and display an Instagram public profile."""
    message = update.effective_message
    args = (message.text or message.caption or "").split(None, 1)
    username = args[1].strip() if len(args) > 1 else ""

    if not username:
        await message.reply_text(
            status_card(
                "نام‌کاربری نیومد",
                "بعد از <code>/profile</code> نام‌کاربری اینستاگرام رو بنویس. مثال: <code>/profile natgeo</code>",
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    # Clean @ prefix
    username = username.strip("@/").strip()

    status_message = await message.reply_text(
        status_card(
            "🔍 دارم پروفایل رو می‌گیرم…",
            f"در حال دریافت اطلاعات صفحه عمومی <b>@{html_escape(username)}</b>",
        ),
        parse_mode=ParseMode.HTML,
    )

    proxy_url = (
        f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        if SETTINGS.use_proxy
        else None
    )

    try:
        profile = await fetch_profile(username, proxy_url=proxy_url)
    except InstagramProfileNotFound:
        await edit_status(
            status_message,
            status_card(
                "❌ پیج پیدا نشد",
                f"نام‌کاربری <b>@{html_escape(username)}</b> وجود ندارد یا حذف شده است.",
            ),
        )
        return
    except InstagramProfilePrivate:
        await edit_status(
            status_message,
            status_card(
                "🔒 پیج خصوصی",
                f"پیج <b>@{html_escape(username)}</b> خصوصی است و اطلاعات آن در دسترس نیست.",
            ),
        )
        return
    except InstagramProfileError as exc:
        await edit_status(
            status_message,
            status_card(
                "❌ خطا",
                str(exc),
            ),
        )
        return
    except Exception as exc:
        logger.exception("Profile fetch failed for @%s", username)
        await edit_status(
            status_message,
            status_card(
                "❌ خطای غیرمنتظره",
                "در دریافت پروفایل خطایی رخ داد. لطفاً بعداً دوباره امتحان کن.",
            ),
        )
        return

    # Build caption
    caption = format_profile_caption(profile)

    # Build glass-style inline keyboard
    token = uuid.uuid4().hex[:12]
    IG_PROFILE_SESSIONS[token] = (username, time.monotonic(), update.effective_chat.id, update.effective_user.id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📸 دریافت آخرین پست", callback_data=f"ig_prof:{token}:post"),
        ],
        [
            InlineKeyboardButton("📖 دریافت آخرین استوری‌ها", callback_data=f"ig_prof:{token}:stories"),
        ],
    ])

    # Send profile photo with caption + buttons
    try:
        if profile.avatar_url:
            await status_message.delete()
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=profile.avatar_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await edit_status(
                status_message,
                caption,
                reply_markup=keyboard,
            )
    except TelegramError:
        # If sending the photo fails, send as text
        with contextlib.suppress(TelegramError):
            await status_message.delete()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


@membership_required
async def on_ig_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Instagram profile callback buttons (latest post / stories)."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        await query.answer("درخواست نامعتبر است.", show_alert=True)
        return

    token = parts[1]
    action = parts[2]  # "post" or "stories"

    entry = IG_PROFILE_SESSIONS.get(token)
    if entry is None:
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return

    username, created_at, orig_chat_id, orig_user_id = entry
    if time.monotonic() - created_at > IG_PROFILE_TTL:
        IG_PROFILE_SESSIONS.pop(token, None)
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return
    if update.effective_user.id != orig_user_id or update.effective_chat.id != orig_chat_id:
        await query.answer("این درخواست متعلق به شما نیست.", show_alert=True)
        return

    # Don't pop the token — the user may click the other button too.

    proxy_url = (
        f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        if SETTINGS.use_proxy
        else None
    )
    chat_id = update.effective_chat.id
    reply_to = query.message.message_id if query.message else None

    if action == "post":
        await query.answer("دارم آخرین پست رو پیدا می‌کنم…")
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)

        post_url = await fetch_latest_post_url(username, proxy_url=proxy_url)
        if not post_url:
            await context.bot.send_message(
                chat_id=chat_id,
                text=status_card(
                    "❌ پستی پیدا نشد",
                    f"پیج <b>@{html_escape(username)}</b> پستی ندارد یا اطلاعات در دسترس نیست.",
                ),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to,
            )
            return

        # Use the existing download flow to download and send the post
        await process_urls(
            update,
            context,
            [post_url],
            reply_to,
        )

    elif action == "stories":
        await query.answer("دارم استوری‌ها رو می‌گیرم…")
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)

        stories = await fetch_stories(username, proxy_url=proxy_url)
        if not stories:
            await context.bot.send_message(
                chat_id=chat_id,
                text=status_card(
                    "📖 استوری فعالی نیست",
                    f"پیج <b>@{html_escape(username)}</b> فعلاً استوری فعالی نداره یا کوکی‌ها تنظیم نشده‌اند.",
                ),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to,
            )
            return

        status_msg = await send_status(
            context,
            chat_id,
            status_card(
                f"📖 دارم {len(stories)} استوری رو دانلود می‌کنم",
                f"در حال دریافت تمام استوری‌های فعال <b>@{html_escape(username)}</b>…",
            ),
            reply_to,
        )

        sent_count = 0
        for i, story in enumerate(stories):
            try:
                media_bytes = await ig_download_media(story.url, proxy_url=proxy_url)

                if story.media_type == "video":
                    if len(media_bytes) > 50 * 1024 * 1024:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=io.BytesIO(media_bytes),
                            filename=f"story_{username}_{i + 1}.mp4",
                            caption=f"📖 استوری {i + 1} از @{username}",
                            reply_to_message_id=reply_to if sent_count == 0 else None,
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=io.BytesIO(media_bytes),
                            caption=f"📖 استوری {i + 1} از @{username}",
                            reply_to_message_id=reply_to if sent_count == 0 else None,
                            supports_streaming=True,
                        )
                else:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=io.BytesIO(media_bytes),
                        caption=f"📖 استوری {i + 1} از @{username}",
                        reply_to_message_id=reply_to if sent_count == 0 else None,
                    )
                sent_count += 1
            except Exception as exc:
                logger.warning("Failed to send story %d for @%s: %s", i + 1, username, exc)
                continue

        with contextlib.suppress(TelegramError):
            await status_msg.delete()

        if sent_count == 0:
            await context.bot.send_message(
                chat_id=chat_id,
                text=status_card(
                    "❌ ارسال استوری شکست خورد",
                    "در دانلود استوری‌ها خطایی رخ داد. لطفاً بعداً دوباره امتحان کن.",
                ),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to,
            )
    else:
        await query.answer("عملیات نامعتبر.", show_alert=True)



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
    await send_feedback_sticker(
        context, update.effective_chat.id, index=2,
    )


def _is_admin(user: Any) -> bool:
    return bool(user and (user.username or "").lower() == ADMIN_USERNAME.lower())


_BROADCAST_PREFIX_RE = re.compile(r"^\s*/broadcast(?:@\w+)?\s*", re.IGNORECASE)


def _strip_broadcast_command(text: str) -> str:
    """Remove the /broadcast (or /broadcast@botname) prefix and return the rest.

    Handles edge cases the old `split(None, 1)` approach missed:
      - Leading whitespace before the command
      - /broadcast@BotName (group form)
      - /broadcast followed by a newline (no space)
      - /BROADCAST (case-insensitive — Telegram allows this in private chats)
      - Trailing whitespace/newlines after the message body
    """
    if not text:
        return ""
    # Strip the command prefix. If the entire text is JUST the command
    # (with optional whitespace), return "".
    stripped = _BROADCAST_PREFIX_RE.sub("", text, count=1)
    return stripped.strip()


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message (text / photo / video / audio / voice / document / animation / sticker)
    to all registered users. Admin-only.

    Supported forms:
      /broadcast some text                       -> plain text message
      photo with caption "/broadcast some text"  -> photo + caption
      video with caption "/broadcast ..."        -> video + caption
      animation / audio / voice / document / sticker with caption "/broadcast ..."
      sticker (no caption) with reply /broadcast -> forwarded sticker
      /broadcast (as a reply to any message)     -> copies that target message
    """
    if not _is_admin(update.effective_user):
        await update.effective_message.reply_text(
            status_card("⛔ دسترسی ممنوع", "این دستور فقط برای ادمین ربات قابل استفاده است."),
            parse_mode=ParseMode.HTML,
        )
        return

    msg = update.effective_message

    # 1) Reply form: /broadcast as a reply to some other message -> copy that target
    is_reply = msg.reply_to_message is not None
    target_msg = msg.reply_to_message if is_reply else msg

    caption_text = _strip_broadcast_command(msg.text or msg.caption or "")

    has_media = any(
        getattr(target_msg, attr, None) is not None
        for attr in ("photo", "video", "audio", "voice", "document", "animation", "sticker")
    )

    # Text-only broadcast path:
    #   - Admin sent "/broadcast <text>" → broadcast the stripped text.
    #   - Admin replied to a text message with "/broadcast" (no extra text)
    #     → broadcast the replied message's text.
    if not has_media and not caption_text and is_reply and target_msg.text:
        caption_text = target_msg.text

    is_text_only = not has_media and bool(caption_text)

    if not has_media and not is_text_only:
        await msg.reply_text(
            status_card(
                "📢 ارسال پیام همگانی",
                "نحوه استفاده:\n"
                "<code>/broadcast متن پیام</code>\n"
                "یا یک عکس/ویدیو/فایل/استیکر بفرست و کپشنش را <code>/broadcast متن</code> بگذار.\n"
                "یا روی هر پیامی ریپلای کن و <code>/broadcast</code> بزن.",
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    all_ids = users_db.all_user_ids()
    total = len(all_ids)
    status_msg = await msg.reply_text(
        status_card("📢 در حال ارسال…", f"تعداد گیرنده: <b>{total}</b> نفر"),
        parse_mode=ParseMode.HTML,
    )

    sent = 0
    failed = 0
    for uid in all_ids:
        try:
            if is_text_only:
                await context.bot.send_message(chat_id=uid, text=caption_text)
            else:
                # copy_message preserves photo/video/voice/audio/document/animation/sticker
                # and the original caption. If admin provided custom caption text via
                # the /broadcast command, we override the caption with it.
                kwargs = {"chat_id": uid, "from_chat_id": target_msg.chat_id, "message_id": target_msg.message_id}
                if caption_text:
                    kwargs["caption"] = caption_text
                await context.bot.copy_message(**kwargs)
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
        "• برای افزودن بات به گروه روی پروفایل بات بزن و گزینه Add to Group or Channel رو بزن و گروه موردنظرت رو انتخاب کن؛ بعدش بات رو به‌عنوان ادمین به گروه اضافه کن.\n\n"
        "<b>فرمان‌ها</b>\n"
        "<code>/search عبارت</code> جست‌وجوی YouTube\n"
        "<code>/song عبارت</code> جست‌وجوی آهنگ (کاور + خواننده - اسم آهنگ)\n"
        "<code>/cancel</code> توقف دانلودهای خودت\n"
        "<code>/status</code> وضعیت صف\n"
        "<code>/platforms</code> پلتفرم‌های قابل استفاده\n"
        "<code>/stats</code> آمار همین اجرا\n\n"
        "<b>قابلیت‌های جدید</b>\n"
        "<code>/bookmarks</code> محتواهای ذخیره‌شده‌ات\n"
        "<code>/mystats</code> آمار شخصی دانلودها\n"
        "<code>/autoshare</code> ارسال خودکار به کانال/گروهت\n"
        "<code>/schedule لینک 7d</code> دانلود زمان‌بندی‌شده\n"
        "<code>/ask سوال</code> پاسخ سریع از دستیار ربات\n\n"
        "<code>/caption لینک</code> فقط کپشن Instagram رو می‌فرسته.\n\n"
        "توی منوی ویدیو هم می‌تونی بهترین کیفیت، فقط صدا یا کپشن رو انتخاب کنی.\n\n"
        f"فایل‌های بزرگ‌تر از {max_mb} مگابایت در فضای ابری آپلود می‌شن و لینک دانلود برات ارسال می‌شه.",
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
        "🟢 Spotify  •  ☁️ SoundCloud  •  📌 Pinterest\n"
        "🟠 Reddit • 👻 Snapchat • 🎬 CapCut\n"
        "🍫 SnackVideo • 🎵 Douyin • 🔗 +۱۸ سایت دیگر\n\n"
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
        or data.startswith("reel_music:")
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
        await edit_status(
            query.message,
            status_card("⏹ درخواست متوقف شد", "انتخاب کیفیت لغو شد."),
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
    await edit_status(
        query.message,
        status_card(
            selection_title,
            f"انتخابت: <b>{html_escape(selection_label)}</b>\nمنبع: <code>{html_escape(session.source_host)}</code>",
            "برای توقف: /cancel"
        ),
    )

    try:
        selection_progress = ProgressReporter(query.message, session.request_id)
        await selection_progress.update(10, selection_title, selection_label, force=True)
        if session.use_voiddl and VOIDDL_GATEWAY is not None:
            await selection_progress.processing(
                12,
                "▶️ دارم خروجی رو آماده می‌کنم…",
                "ارتباط با سرور…",
                force=True,
            )
            result = await VOIDDL_GATEWAY.select(
                url=session.source_url,
                platform=session.platform,
                option=option,
                attempt_directory=session.attempt_directory,
                progress_callback=selection_progress.download,
                processing_callback=selection_progress.processing,
            )
        elif session.use_yoinku and YOINKU_GATEWAY is not None:
            await selection_progress.processing(
                12,
                "▶️ دارم خروجی رو آماده می‌کنم…",
                "ارتباط با سرور…",
                force=True,
            )
            result = await YOINKU_GATEWAY.select(
                url=session.source_url,
                platform=session.platform,
                option=option,
                attempt_directory=session.attempt_directory,
                progress_callback=selection_progress.download,
                processing_callback=selection_progress.processing,
            )
        elif session.use_ahm7 and AHM7_GATEWAY is not None:
            await selection_progress.processing(
                12,
                "▶️ دارم خروجی رو آماده می‌کنم…",
                "ارتباط با سرور…",
                force=True,
            )
            result = await AHM7_GATEWAY.select(
                url=session.source_url,
                platform=session.platform,
                option=option,
                attempt_directory=session.attempt_directory,
                progress_callback=selection_progress.download,
                processing_callback=selection_progress.processing,
            )
        elif session.use_apify and APIFY_GATEWAY is not None:
            await selection_progress.processing(
                12,
                "☁️ دارم خروجی انتخاب‌شده رو آماده می‌کنم…",
                "در حال آماده‌سازی",
                force=True,
            )
            result = await APIFY_GATEWAY.select(
                url=session.source_url,
                platform=session.platform,
                option=option,
                attempt_directory=session.attempt_directory,
                progress_callback=selection_progress.apify_download,
                processing_callback=selection_progress.processing,
            )
        elif session.use_social_sites and SOCIAL_GATEWAY is not None:
            # Show an immediate "processing" status so the user sees the bar
            # start moving before yt-dlp / tikwm.com returns.
            await selection_progress.processing(
                12,
                "⚙️ دارم ویدیو رو آماده می‌کنم…",
                "ارتباط با سرور…",
                force=True,
            )
            # Social selection path: no Telegram worker, just HTTP / yt-dlp.
            result = await SOCIAL_GATEWAY.select(
                url=session.source_url,
                platform=session.platform,
                option=option,
                attempt_directory=session.attempt_directory,
                progress_callback=selection_progress.download,
                processing_callback=selection_progress.processing,
            )
        else:
            if session.lease is None:
                raise RuntimeError("Selection has no Telegram worker lease and no direct gateway is enabled")
            if session.lease.worker.lease_id != session.lease.lease_id:
                raise RuntimeError("Selection lease is no longer active")
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
            # 1404 upgrade: the Twitter "text" option reuses this branch —
            # label it correctly instead of "کپشن".
            is_tweet_text = session.use_apify and session.platform == Platform.TWITTER
            first_heading = "🐦 متن و جزییات توییت\n\n" if is_tweet_text else "📝 کپشن\n\n"
            later_heading = "🐦 ادامه توییت\n\n" if is_tweet_text else "📝 ادامه کپشن\n\n"
            for index, chunk in enumerate(chunks):
                heading = first_heading if index == 0 else later_heading
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=heading + chunk,
                    reply_to_message_id=session.reply_to,
                    disable_web_page_preview=True,
                )
            await user_features.maybe_send_summarize_button(
                context, update.effective_chat.id, session.reply_to, result.text
            )
            await edit_status(
                query.message,
                status_card(
                    "✅ متن ارسال شد" if is_tweet_text else "✅ کپشن ارسال شد",
                    f"تعداد بخش: <b>{len(chunks)}</b>",
                ),
            )
            return
        if result.status != "ready":
            if session.use_voiddl:
                await edit_status(
                    query.message,
                    status_card(
                        "🔄 مسیر اصلی جواب نداد",
                        "دارم مسیرهای جایگزین ربات رو امتحان می‌کنم…",
                    ),
                )
                # Re-enter the flow without VoidDL so a VoidDL-side failure
                # (all keys exhausted, service down, etc.) cannot remove
                # the established Yoinku / Apify / Telegram-bot fallbacks.
                await _process_url(
                    update,
                    context,
                    session.source_url,
                    session.reply_to,
                    skip_voiddl=True,
                )
                return
            if session.use_yoinku:
                await edit_status(
                    query.message,
                    status_card(
                        "🔄 مسیر اصلی جواب نداد",
                        "دارم مسیرهای قبلی ربات رو امتحان می‌کنم…",
                    ),
                )
                # Re-enter the flow without Yoinku so a Yoinku-side failure
                # (key exhausted, CDN down, etc.) cannot remove the established
                # Apify / Telegram-bot fallbacks.
                await _process_url(
                    update,
                    context,
                    session.source_url,
                    session.reply_to,
                    skip_yoinku=True,
                )
                return
            if session.use_ahm7:
                await edit_status(
                    query.message,
                    status_card(
                        "🔄 مسیر اصلی جواب نداد",
                        "دارم مسیرهای قبلی ربات رو امتحان می‌کنم…",
                    ),
                )
                # Re-enter the flow without AHM7 so an AHM7-side failure cannot
                # remove the established Apify / Telegram-bot fallbacks.
                await _process_url(
                    update,
                    context,
                    session.source_url,
                    session.reply_to,
                    skip_ahm7=True,
                )
                return
            if session.use_apify:
                await edit_status(
                    query.message,
                    status_card(
                        "🔄 مسیر اصلی جواب نداد",
                        "دارم مسیرهای قبلی ربات رو امتحان می‌کنم…",
                    ),
                )
                # Re-enter the normal flow without Apify so a total token/Actor
                # failure cannot remove the site's established fallbacks.
                await _process_url(
                    update,
                    context,
                    session.source_url,
                    session.reply_to,
                    skip_apify=True,
                )
                return
            await edit_status(
                query.message,
                failure_text([result.reason or "service_error"], session.request_id),
            )
            await send_feedback_sticker(context, session.chat_id, index=4)
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
            source_url=session.source_url,
            platform_value=session.platform.value,
            user_id=session.user_id,
        )
        # 1404 upgrade: enriched caption (title/artist/album) for the new
        # Apify platforms arrives as result.text on ready results.
        if result.status == "ready" and session.use_apify and result.text:
            with contextlib.suppress(TelegramError):
                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=result.text,
                    reply_to_message_id=session.reply_to,
                )
        # Subtitle follow-up for long YouTube videos (selected-quality path).
        # This mirrors the direct-download path in _process_url.
        if session.platform == Platform.YOUTUBE and option.action == "media":
            await send_subtitle_followup(
                context,
                chat_id=session.chat_id,
                reply_to=session.reply_to,
                youtube_url=session.source_url,
                result=result,
            )
    except asyncio.CancelledError:
        with contextlib.suppress(TelegramError):
            await edit_status(
                query.message,
                status_card("⏹ درخواست متوقف شد", "دانلود کیفیت انتخابی لغو شد."),
            )
    except Exception as exc:
        logger.exception("Selection %s failed: %s", session.request_id, exc)
        with contextlib.suppress(TelegramError):
            await edit_status(
                query.message,
                status_card(
                    "❌ خطا در دانلود",
                    "هیچ فایل ناقصی ارسال نشد. لینک را دوباره امتحان کن.",
                ),
            )
        with contextlib.suppress(Exception):
            await send_feedback_sticker(context, session.chat_id, index=4)
    finally:
        release_pending_selection(session)


@membership_required
async def on_reel_music_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, force_url: str = None, force_request_id: str = None) -> None:
    """Handle 🎵 موزیک ریلز button presses."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    token = parts[1] if len(parts) > 1 else ""

    entry = REEL_MUSIC_URLS.get(token)
    if entry is None and not force_url:
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return
    
    if force_url:
        url = force_url
        request_id = force_request_id
    else:
        url, created_at, orig_chat_id, orig_user_id = entry
        if time.monotonic() - created_at > REEL_MUSIC_TTL:
            REEL_MUSIC_URLS.pop(token, None)
            await query.answer("این درخواست منقضی شده است.", show_alert=True)
            return
        if update.effective_user.id != orig_user_id or update.effective_chat.id != orig_chat_id:
            await query.answer("این درخواست متعلق به شما نیست.", show_alert=True)
            return
        REEL_MUSIC_URLS.pop(token, None)
        request_id = uuid.uuid4().hex[:8]

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
                    f"🎵 دارم آهنگ رو دریافت می‌کنم…",
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

                if result.status == "ready":
                    await send_result_to_user(
                        update,
                        context,
                        status_message,
                        result,
                        reply_to=reply_to,
                        request_id=request_id,
                        progress=progress,
                        source_url=url,
                        platform_value=platform.value,
                        user_id=update.effective_user.id,
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


# Help text shown after sending an SRT subtitle file. Exact wording per spec.
SUBTITLE_HELP_TEXT = (
    "📄 راهنمای استفاده از فایل زیرنویس\n"
    "\n"
    "فایل SRT شامل متن زیرنویس به‌همراه زمان‌بندی دقیق هر جمله است.\n"
    "\n"
    "🎬 برای استفاده، فایل SRT را همراه با ویدیوی خود در برنامه‌هایی مثل VLC، "
    "MX Player، PotPlayer یا نرم‌افزارهای ادیت ویدیو مثل CapCut و Premiere وارد کنید.\n"
    "\n"
    "💡 اگر فقط متن زیرنویس را می‌خواهید، می‌توانید فایل SRT را با هر ویرایشگر متنی باز کنید.\n"
    "\n"
    "⚠️ فایل SRT به‌تنهایی ویدیو نیست؛ فقط متن و زمان‌بندی زیرنویس را شامل می‌شود."
)


async def on_youtube_subtitle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 🇮🇷 فارسی / 🇬🇧 English subtitle button presses.

    Callback data format: ``yt_sub:{token}:{language}`` where language is "fa" or "en".
    Recovers the YouTube URL from :data:`YOUTUBE_SUBTITLE_URLS`, fetches the SRT
    via :mod:`youtube_subtitle`, sends it as a document, then sends the help text.
    """
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    # Expected parts: ["yt_sub", token, language]
    if len(parts) < 3:
        await query.answer("درخواست نامعتبر است.", show_alert=True)
        return
    token = parts[1]
    language = parts[2]

    if language not in (SUBTITLE_LANG_FA, SUBTITLE_LANG_EN):
        await query.answer("زبان انتخابی پشتیبانی نمی‌شود.", show_alert=True)
        return

    entry = YOUTUBE_SUBTITLE_URLS.get(token)
    if entry is None:
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return

    url, created_at, orig_chat_id, orig_user_id = entry
    if time.monotonic() - created_at > YOUTUBE_SUBTITLE_TTL:
        YOUTUBE_SUBTITLE_URLS.pop(token, None)
        await query.answer("این درخواست منقضی شده است.", show_alert=True)
        return
    # When orig_user_id == -1 we only enforce chat_id match (used by the
    # quality-selection path where the original user_id is not stored).
    if orig_user_id != -1 and update.effective_user.id != orig_user_id:
        await query.answer("این درخواست متعلق به شما نیست.", show_alert=True)
        return
    if update.effective_chat.id != orig_chat_id:
        await query.answer("این درخواست متعلق به این چت نیست.", show_alert=True)
        return

    # Don't pop the token yet — allow the user to also click the *other* language
    # button on the same follow-up message. The token naturally expires via TTL.

    chat_id = update.effective_chat.id
    reply_to = query.message.message_id if query.message else None

    lang_label = "فارسی" if language == SUBTITLE_LANG_FA else "English"
    await query.answer(f"📝 دارم زیرنویس {lang_label} رو آماده می‌کنم…")

    # Replace the keyboard with a "preparing" indicator so the user knows it's working
    with contextlib.suppress(TelegramError):
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"⏳ در حال آماده‌سازی زیرنویس {lang_label}…",
                    callback_data="yt_sub:noop",
                ),
            ]])
        )

    status_message = await send_status(
        context,
        chat_id,
        status_card(
            f"📝 در حال دانلود زیرنویس {lang_label}",
            "از روی زیرنویس‌های YouTube استخراج می‌شه. ممکنه چند ثانیه طول بکشه.",
        ),
        reply_to,
    )

    try:
        srt_bytes = await fetch_subtitle_for_user(url, language)
    except YouTubeSubtitleNotFound:
        await edit_status(
            status_message,
            status_card(
                "⚠️ زیرنویس پیدا نشد",
                f"برای این ویدیو زیرنویس {lang_label} در دسترس نیست.\n"
                "ممکنه ویدیو زیرنویس نداشته باشه یا YouTube در حال حاضر اجازه نده.",
            ),
        )
        # Restore the original language buttons so the user can try the other language
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🇮🇷 فارسی",
                        callback_data=f"yt_sub:{token}:{SUBTITLE_LANG_FA}",
                    ),
                    InlineKeyboardButton(
                        "🇬🇧 English",
                        callback_data=f"yt_sub:{token}:{SUBTITLE_LANG_EN}",
                    ),
                ]])
            )
        return
    except YouTubeSubtitleError as exc:
        logger.warning("YouTube subtitle fetch failed for %s: %s", url, exc)
        await edit_status(
            status_message,
            status_card(
                "⚠️ دریافت زیرنویس ناموفق بود",
                f"یه مشکلی پیش اومد. دوباره تلاش کن.\n"
                f"جزئیات: {html.escape(str(exc))}",
            ),
        )
        # Restore the original language buttons
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🇮🇷 فارسی",
                        callback_data=f"yt_sub:{token}:{SUBTITLE_LANG_FA}",
                    ),
                    InlineKeyboardButton(
                        "🇬🇧 English",
                        callback_data=f"yt_sub:{token}:{SUBTITLE_LANG_EN}",
                    ),
                ]])
            )
        return

    # Send the SRT file as a document
    video_id_for_name = _extract_youtube_video_id(url) or ""
    filename = f"subtitle_{video_id_for_name or 'video'}_{language}.srt"
    document = io.BytesIO(srt_bytes)
    document.name = filename

    try:
        await context.bot.send_document(
            chat_id=chat_id,
            document=document,
            filename=filename,
            caption=f"📝 زیرنویس {lang_label} • <code>{html.escape(video_id_for_name or url)}</code>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to,
        )
    except TelegramError as exc:
        logger.warning("Failed to send SRT document: %s", exc)
        await edit_status(
            status_message,
            status_card(
                "⚠️ ارسال فایل زیرنویس ناموفق بود",
                f"یه مشکلی توی ارسال فایل پیش اومد. دوباره تلاش کن.\nجزئیات: {html.escape(str(exc))}",
            ),
        )
        return

    # Edit status message to "done"
    await edit_status(
        status_message,
        status_card(
            f"✅ زیرنویس {lang_label} ارسال شد",
            "فایل زیرنویس رو بالا می‌بینی. راهنمای استفاده‌اش رو هم بعدش فرستادم.",
        ),
    )

    # Send the help text — exact wording per spec
    with contextlib.suppress(TelegramError):
        await context.bot.send_message(
            chat_id=chat_id,
            text=SUBTITLE_HELP_TEXT,
            reply_to_message_id=reply_to,
        )


async def tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin dashboard: Apify token health + open alerts (1404 upgrade)."""
    if not _is_admin(update.effective_user):
        await update.effective_message.reply_text("این دستور فقط برای مدیر ربات است.")
        return
    text = await token_alerts.tokens_dashboard_text()
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def build_health_report() -> str:
    """Comprehensive self-check report (startup health test / /health)."""
    from perf import all_breakers

    uptime_minutes = int((time.monotonic() - STARTED_AT) // 60)
    flags_on = [name for name, on in FLAGS.as_dict().items() if on]
    breakers = all_breakers()
    open_breakers = [name for name, info in breakers.items() if info["state"] != "closed"] or "—"
    ai = await ai_service.ai_health()
    voiddl_line = f"▶️ کلیدهای VoidDL (مسیر اصلی یوتیوب): <b>{len(SETTINGS.voiddl_api_keys)}</b>"
    if VOIDDL_GATEWAY is not None:
        voiddl_status = await VOIDDL_GATEWAY.pool.status()
        voiddl_used_gb = sum(entry["daily_bytes_used"] for entry in voiddl_status) / (1024 ** 3)
        voiddl_line += (
            f" — مصرف روزه: <b>{voiddl_used_gb:.2f}</b> از"
            f" <b>{SETTINGS.voiddl_daily_bandwidth_mb // 1024}</b> گیگابایت"
        )
    lines = [
        "🩺 <b>گزارش سلامت ربات</b>",
        f"🕒 زمان راه‌اندازی: {time.strftime('%Y-%m-%d %H:%M:%S')} (پایدار: {uptime_minutes} دقیقه)",
        "",
        f"👤 اکانت‌های دانلود متصل: <b>{ACCOUNT_POOL.total}</b> (شلوغ: {ACCOUNT_POOL.busy_count} • صف: {ACCOUNT_POOL.queue_length})",
        f"☁️ توکن‌های سرویس ابری: <b>{len(SETTINGS.apify_tokens)}</b>"
        + (f" (پلتفرم‌های جدید: {'فعال' if FLAGS.apify_new_platforms else 'غیرفعال'})" if SETTINGS.apify_tokens else ""),
        voiddl_line,
    ]
    # Store self-check (probe query — safe on an empty DB too)
    try:
        await store.user_stats(0, days=1)
        db_state = "✅ سالم"
    except Exception as exc:  # noqa: BLE001
        db_state = f"⚠️ خطا: {exc}"
    lines.append(f"🗄 دیتابیس: {db_state}")
    ai_enabled = bool(ai.get("enabled"))
    lines.append(
        "🤖 هوش مصنوعی: "
        + (f"✅ فعال ({ai.get('provider')} / {ai.get('model')})" if ai_enabled else "⛔ غیرفعال (AI_API_KEY تنظیم نشده)")
    )
    if ai_enabled:
        lines.append(
            f"     درخواست‌ها: {ai.get('calls_total', 0)} • خطاها: {ai.get('failures', 0)} • کش: {ai.get('cache_hits', 0)}"
        )
    lines.append(f"⚡ بریکرهای باز: {open_breakers}")
    lines.append("")
    lines.append(f"🚩 قابلیت‌های فعال: {len(flags_on)}/{len(FLAGS.as_dict())}")
    lines.append("ممنون که مرا روشن نگه می‌دارید! 🌱")
    return "\n".join(lines)


async def send_admin_health_pv(bot: Any) -> None:
    """Send the health report to the main admin's private chat (best-effort)."""
    admin_chat_id = SETTINGS.bot_admin_chat_id
    if not admin_chat_id or admin_chat_id <= 0:
        logger.info("Health PV skipped — BOT_ADMIN_CHAT_ID is not set")
        return
    try:
        report = await build_health_report()
        await bot.send_message(
            chat_id=admin_chat_id,
            text=report,
            parse_mode=ParseMode.HTML,
        )
        logger.info("Health report delivered to admin PV (chat %s)", admin_chat_id)
    except Exception as exc:  # noqa: BLE001 — never blocks startup
        logger.warning("Could not deliver health PV: %s", exc)


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: run the self-check; report is shown here and sent to your PV."""
    if not _is_admin(update.effective_user):
        await update.effective_message.reply_text("این دستور فقط برای مدیر ربات است.")
        return
    await update.effective_message.reply_text(
        await build_health_report(),
        parse_mode=ParseMode.HTML,
    )
    if update.effective_chat.id != SETTINGS.bot_admin_chat_id:
        await send_admin_health_pv(context.bot)


async def _admin_seen_watcher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group -1 pre-handler: any admin interaction implies PV alerts were read."""
    user = update.effective_user
    if user is not None and FLAGS.token_alerts:
        with contextlib.suppress(Exception):
            await token_alerts.mark_admin_seen(user.id)


class _ScheduledChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id
        self.type = "private"


class _ScheduledUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _ScheduledUpdate:
    """Minimal Update shim so scheduled jobs can reuse _process_url."""

    def __init__(self, chat_id: int, user_id: int) -> None:
        self.effective_chat = _ScheduledChat(chat_id)
        self.effective_user = _ScheduledUser(user_id)


async def features_scheduler_loop(application: Application) -> None:
    """1404 upgrade: run due scheduled downloads every minute."""
    while True:
        await asyncio.sleep(user_features.SCHEDULER_TICK_SECONDS)
        if not FLAGS.scheduler:
            continue

        async def run_job(job: dict[str, Any]) -> str:
            shim = _ScheduledUpdate(int(job["chat_id"]), int(job["user_id"]))
            try:
                await asyncio.wait_for(
                    _process_url(shim, application, str(job["url"]), None),
                    timeout=25 * 60,
                )
                return "ok"
            except asyncio.TimeoutError:
                return "timeout"

        with contextlib.suppress(asyncio.CancelledError):
            await user_features.scheduler_tick(run_job)


async def maintenance_loop() -> None:
    """1404 upgrade: hourly store maintenance (prune dedupe/AI caches)."""
    while True:
        await asyncio.sleep(3600)
        with contextlib.suppress(Exception):
            pruned = await store.prune_all()
            if any(pruned.values()):
                logger.info("maintenance pruned: %s", pruned)


async def post_init(application: Application) -> None:
    global SELECTION_REAPER_TASK, HEALTH_SERVER, FEATURES_SCHEDULER_TASK
    setup_structured_logging()
    await store.init_store()
    token_alerts.initialize(
        application.bot,
        SETTINGS.bot_admin_chat_id,
        SETTINGS.apify_tokens,
    )
    token_alerts.start()
    FEATURES_SCHEDULER_TASK = asyncio.create_task(
        features_scheduler_loop(application), name="features-scheduler"
    )
    asyncio.get_running_loop().create_task(maintenance_loop(), name="store-maintenance")
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
        BotCommand("mystats", "آمار شخصی من"),
        BotCommand("bookmarks", "محتواهای ذخیره‌شده من"),
        BotCommand("autoshare", "اشتراک‌گذاری خودکار"),
        BotCommand("schedule", "دانلود زمان‌بندی‌شده"),
        BotCommand("ask", "پرسش از دستیار هوشمند"),
        BotCommand("caption", "دریافت کپشن Instagram"),
        BotCommand("profile", "پروفایل Instagram"),
        BotCommand("search", "جست‌وجو در YouTube"),
        BotCommand("song", "جست‌و‌جو آهنگ"),
        BotCommand("cancel", "توقف دانلودهای من"),
        BotCommand("dl", "دانلود لینک"),
    ]
    group_commands = [
        BotCommand("dl", "دانلود لینک یا پیام ریپلای‌شده"),
        BotCommand("search", "جست‌وجو در YouTube"),
        BotCommand("song", "جست‌و‌جو آهنگ"),
        BotCommand("cancel", "توقف دانلودهای من"),
        BotCommand("status", "وضعیت صف"),
        BotCommand("platforms", "پلتفرم‌ها"),
        BotCommand("caption", "دریافت کپشن Instagram"),
        BotCommand("profile", "پروفایل Instagram"),
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
    logger.info(
        "1404-upgrade flags: %s | AI: %s",
        ", ".join(name for name, on in FLAGS.as_dict().items() if on),
        ai_service.ai_available() or "off",
    )
    # 1404 upgrade v3: startup health test → deliver a self-check report to
    # the main admin's private chat (best-effort, never blocks startup).
    asyncio.get_running_loop().create_task(
        send_admin_health_pv(application.bot), name="startup-health-pv"
    )
    # Best-effort connectivity probe for the social-sites gateway backends.
    if SOCIAL_GATEWAY is not None:
        try:
            social_status = await asyncio.wait_for(social_health_check(), timeout=8.0)
            logger.info("social-sites health: tiktok=%s soundcloud=%s instagram=%s",
                        social_status.get("tiktok"), social_status.get("soundcloud"),
                        social_status.get("instagram"))
        except Exception as exc:
            logger.debug("social-sites health probe failed: %s", exc)


async def post_shutdown(application: Application) -> None:
    global SELECTION_REAPER_TASK, HEALTH_SERVER, FEATURES_SCHEDULER_TASK
    if SELECTION_REAPER_TASK:
        SELECTION_REAPER_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await SELECTION_REAPER_TASK
        SELECTION_REAPER_TASK = None
    if FEATURES_SCHEDULER_TASK is not None:
        FEATURES_SCHEDULER_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await FEATURES_SCHEDULER_TASK
        FEATURES_SCHEDULER_TASK = None
    await token_alerts.stop()
    await store.close_store()
    await PERF_CLIENTS.aclose_all()
    user_features.AI_TEXTS.clear()
    user_features.BOOKMARK_OFFERS.clear()
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
    SHAZAM_SEARCH_SESSIONS.clear()
    ACTIVE_SHAZAM_SEARCHES.clear()
    IG_PROFILE_SESSIONS.clear()
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
        .connect_timeout(15)
        .read_timeout(120)
        .write_timeout(120)
        .media_write_timeout(300)
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
    # PTB's CommandHandler and filters.Regex only inspect message.text, NOT message.caption.
    # For a photo/video/etc with caption "/broadcast ...", we need filters.CaptionRegex.
    application.add_handler(
        MessageHandler(
            filters.CAPTION & filters.CaptionRegex(r"^/broadcast(?:@\w+)?(?:\s|$)"),
            broadcast_command,
        )
    )
    application.add_handler(CommandHandler("adduser", adduser_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("services", services_command))
    application.add_handler(CommandHandler("platforms", services_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("caption", caption_command))
    application.add_handler(CommandHandler(("search", "yt"), search_command))
    application.add_handler(CommandHandler("song", song_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("dl", dl_command))
    application.add_handler(CommandHandler("profile", profile_command))
    # ── 1404 upgrade: user features + admin tooling (all flag-gated inside) ──
    application.add_handler(TypeHandler(Update, _admin_seen_watcher), group=-1)
    application.add_handler(CommandHandler("bookmarks", user_features.bookmarks_command))
    application.add_handler(CommandHandler(("mystats", "mystatistics"), user_features.stats_command))
    application.add_handler(CommandHandler("autoshare", user_features.autoshare_command))
    application.add_handler(CommandHandler("schedule", user_features.schedule_command))
    application.add_handler(CommandHandler("ask", user_features.ask_command))
    application.add_handler(CommandHandler("tokens", tokens_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CallbackQueryHandler(user_features.bookmarks_callback, pattern=r"^bm:"))
    application.add_handler(CallbackQueryHandler(user_features.bookmark_offer_callback, pattern=r"^bks:"))
    application.add_handler(CallbackQueryHandler(user_features.autoshare_callback, pattern=r"^sh:"))
    application.add_handler(CallbackQueryHandler(user_features.schedule_callback, pattern=r"^sc:"))
    application.add_handler(CallbackQueryHandler(user_features.ai_summary_callback, pattern=r"^ai:"))
    application.add_handler(CallbackQueryHandler(token_alerts.handle_ack_callback, pattern=r"^ack:"))
    application.add_handler(CallbackQueryHandler(on_selection, pattern=r"^(?:sel|cancel|info|caption):"))
    application.add_handler(CallbackQueryHandler(on_reel_music_callback, pattern=r"^reel_music:"))
    application.add_handler(CallbackQueryHandler(on_youtube_search_callback, pattern=r"^(?:ys|yp):"))
    application.add_handler(CallbackQueryHandler(on_shazam_search_callback, pattern=r"^(?:ss|sp):"))
    application.add_handler(CallbackQueryHandler(on_youtube_subtitle_callback, pattern=r"^yt_sub:"))
    application.add_handler(CallbackQueryHandler(on_ig_profile_callback, pattern=r"^ig_prof:"))
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
