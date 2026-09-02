#!/usr/bin/env python3
"""Instagram DM → Telegram bridge (پیج عمومی اینستاگرام).

کاربرانِ یک پیج اینستاگرام عمومی، لینک ریلز/پست/استوری (یا خودِ پست را با
Share به دایرکت پیج) می‌فرستند؛ این ماژول دایرکت پیج را پایش می‌کند، لینک را
با «همان متدهای فعلی» ربات دانلود می‌کند و فایل را به چت تلگرامیِ کاربر
می‌فرستد.

جریان اتصال (pairing):
  1) کاربر در ربات تلگرام دستور /link را می‌زند و یک کد ۶ رقمی می‌گیرد.
  2) کاربر همان کد را به دایرکت پیج اینستاگرام می‌فرستد.
  3) حساب اینستاگرامش به چت تلگرامش وصل می‌شود؛ از این به بعد هر لینک/شیری
     که به دایرکت پیج بفرستد، فایلش را در تلگرام دریافت می‌کند.

نکتهٔ مهم: خواندن دایرکت اینستاگرام «بدون لاگین» ممکن نیست (API رسمی
اینستاگرام اجازهٔ خواندن DM را نمی‌دهد)، پس فقط همین ماژول با حساب پیج
لاگین می‌کند و سشن را در فایل ذخیره/بازیابی می‌کند تا هر بار لاگین تکرار
نشود. «دانلود» خودِ محتوا هیچ سشنی لازم ندارد و دقیقاً از همان زنجیرهٔ
AHM7 → Apify → تلگرام‌بات‌های کمکی → yt-dlp استفاده می‌کند.

این ماژول کاملاً اختیاری است؛ اگر فعال نشود، ربات دقیقاً مثل قبل کار
می‌کند (import آن در bot.py با try/except محافظت شده است).

Environment variables:
  IG_USERNAME          نام کاربری پیج اینستاگرام (برای فعال‌سازی لازم)
  IG_PASSWORD          رمز عبور پیج
  IG_TOTP_SECRET       (اختیاری) سکرت 2FA برای لاگین خودکار
  IG_DM_ENABLED        (اختیاری) 0 برای غیرفعال‌سازی اجباری
  IG_DM_PAGE_HINT      (اختیاری) یوزرنیم پیج برای نمایش در پیام /link
  IG_DM_POLL_SECONDS   فاصلهٔ پایش دایرکت (پیش‌فرض 10)
  IG_DM_MAX_THREADS    تعداد آخرین چت‌هایی که هر دور بررسی می‌شود (پیش‌فرض 20)
  IG_DM_SESSION_FILE   مسیر فایل سشن اینستاگرام (پیش‌فرض ig_session.json)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable
from pathlib import Path
from urllib.parse import urlsplit

from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from config import PROJECT_DIR, SETTINGS

logger = logging.getLogger("MZDownloader.ig_dm")

# ────────────────────────────── Configuration ──────────────────────────────

IG_USERNAME = os.getenv("IG_USERNAME", "").strip().lstrip("@")
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()
IG_TOTP_SECRET = os.getenv("IG_TOTP_SECRET", "").strip().replace(" ", "")
# پراکسی مخصوص اینستاگرام (مثلاً socks5://user:pass@host:port یا http://host:port).
# روی Railway معمولاً لازم است چون IP دیتاسنتر توسط اینستاگرام رد/محدود می‌شود.
IG_PROXY = os.getenv("IG_PROXY", "").strip()
# کوکی sessionid از مرورگر خودت (F12 → Application → Cookies → instagram.com).
# با این متغیر «هیچ لاگین رمزی» لازم نیست — حتی روی Railway. روش پیشنهادی.
IG_SESSIONID = os.getenv("IG_SESSIONID", "").strip().strip('"').strip("'")
# سشن آماده به‌صورت base64 (خروجی ig_session_helper.py) — بدون نیاز به Volume
# روی Railway، بعد از هر ری‌دیپلوی سشن از همین متغیر بازسازی می‌شود.
IG_SESSION_B64 = os.getenv("IG_SESSION_B64", "").strip()
IG_DM_PAGE_HINT = os.getenv("IG_DM_PAGE_HINT", "").strip().lstrip("@") or IG_USERNAME
IG_DM_POLL_SECONDS = max(5, int(os.getenv("IG_DM_POLL_SECONDS", "10") or 10))
IG_DM_MAX_THREADS = max(5, int(os.getenv("IG_DM_MAX_THREADS", "20") or 20))
IG_DM_SESSION_FILE = Path(
    os.getenv("IG_DM_SESSION_FILE", str(PROJECT_DIR / "ig_session.json"))
)
if not IG_DM_SESSION_FILE.is_absolute():
    IG_DM_SESSION_FILE = PROJECT_DIR / IG_DM_SESSION_FILE

IG_LINKS_FILE = PROJECT_DIR / "ig_links.json"
IG_DM_STATE_FILE = PROJECT_DIR / "ig_dm_state.json"

CODE_TTL_SECONDS = 15 * 60  # 15 دقیقه اعتبار کد اتصال
MAX_URLS_PER_DM = 2         # حداکثر لینک پردازش‌شده در هر پیام دایرکت
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # بدون 0/O/1/I/L
CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-HJ-NP-Z2-9]{6})(?![A-Za-z0-9])")
URL_RE = re.compile(r"https?://[^\s<>\"'،؛\)\]]+", re.IGNORECASE)
IG_HOSTS = ("instagram.com", "instagr.am")
SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
MAX_SCAN_NODES = 600

# فاصلهٔ ارسال مجدد پیام‌های راهنما (ثانیه)
INSTRUCTION_COOLDOWN = 6 * 60 * 60   # راهنمای اتصال: هر ۶ ساعت به ازای هر کاربر
NO_LINK_COOLDOWN = 30 * 60           # «لینکی ندیدم»: هر ۳۰ دقیقه
BUSY_COOLDOWN = 5 * 60               # «یکی یکی»: هر ۵ دقیقه
RATELIMIT_COOLDOWN = 5 * 60          # پیام محدودیت نرخ


def feature_enabled() -> bool:
    """فعال بودن پل دایرکت: با IG_SESSIONID یا با IG_USERNAME + IG_PASSWORD."""
    forced_off = os.getenv("IG_DM_ENABLED", "").strip().lower() in {"0", "false", "no", "off"}
    if forced_off:
        return False
    return bool(IG_USERNAME and IG_PASSWORD) or bool(IG_SESSIONID)


# ────────────────────────────── TOTP (stdlib, بدون وابستگی) ──────────────────────────────

def totp_now(secret: str) -> str | None:
    """ساخت کد TOTP ۶ رقمی فقط با کتابخانهٔ استاندارد پایتون."""
    if not secret:
        return None
    try:
        cleaned = secret.replace("-", "").upper()
        padding = (8 - len(cleaned) % 8) % 8
        key = base64.b32decode(cleaned + "=" * padding)
        counter = int(time.time()) // 30
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[19] & 0x0F
        value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return f"{value % 1_000_000:06d}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig-dm: invalid TOTP secret: %s", exc)
        return None


# ────────────────────────────── Instagram helpers ──────────────────────────────

_IG_IMPORT_CACHE: tuple[Any, Any] | None = None


def ig_imports() -> tuple[Any, Any]:
    """import تنبل instagrapi — اگر نصب نبود، قابلیت با پیام واضح خاموش می‌شود."""
    global _IG_IMPORT_CACHE
    if _IG_IMPORT_CACHE is None:
        try:
            from instagrapi import Client  # type: ignore
            import instagrapi.exceptions as ig_exc  # type: ignore

            _IG_IMPORT_CACHE = (Client, ig_exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ig-dm: instagrapi not installed (%s). Run: pip install instagrapi", exc
            )
            _IG_IMPORT_CACHE = (None, None)
    return _IG_IMPORT_CACHE


def _proxy_url() -> str | None:
    """پراکسی مخصوص اینستاگرام: اول IG_PROXY، بعد پراکسی عمومی ربات."""
    if IG_PROXY:
        return IG_PROXY
    if not SETTINGS.use_proxy:
        return None
    return f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"


def _mask_proxy(proxy: str | None) -> str:
    """نمایش ایمن پراکسی در لاگ — بدون نام‌کاربری/رمز."""
    if not proxy:
        return "off"
    try:
        parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        auth = "***@" if parts.username else ""
        if parts.port:
            return f"{parts.scheme}://{auth}{parts.hostname}:{parts.port}"
        return f"{parts.scheme}://{auth}{parts.hostname}"
    except Exception:  # noqa: BLE001
        return "set"


def _seed_session_from_env() -> bool:
    """اگر IG_SESSION_B64 تنظیم شده باشد، فایل سشن را از آن بازسازی می‌کند.

    فقط وقتی فایل فعلی ناموجود یا نامعتبر است می‌نویسد تا سشن تازه‌ترِ
    اجرای قبلی از بین نرود. خروجی True یعنی سشن از env بازسازی شد.
    """
    if not IG_SESSION_B64:
        return False
    if IG_DM_SESSION_FILE.exists():
        try:
            json.loads(IG_DM_SESSION_FILE.read_text(encoding="utf-8"))
            return False  # فایل موجود و معتبر است — دست نمی‌زنیم
        except Exception:  # noqa: BLE001
            pass  # فایل خراب است → با سشن env بازنویسی می‌شود
    try:
        raw = base64.b64decode(IG_SESSION_B64, validate=False)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("session JSON must be an object")
        IG_DM_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        IG_DM_SESSION_FILE.write_text(json.dumps(payload), encoding="utf-8")
        logger.info(
            "ig-dm: session seeded from IG_SESSION_B64 → %s", IG_DM_SESSION_FILE.name
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig-dm: IG_SESSION_B64 invalid: %s", exc)
        return False


def session_file_b64() -> str | None:
    """محتوای فایل سشن به‌صورت base64 (برای دستور /igsession)."""
    try:
        if not IG_DM_SESSION_FILE.exists():
            return None
        raw = IG_DM_SESSION_FILE.read_bytes()
        if not raw.strip():
            return None
        json.loads(raw.decode("utf-8"))  # فقط اعتبارسنجی
        return base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig-dm: could not read session for export: %s", exc)
        return None


def _clean_url(candidate: str) -> str:
    return candidate.rstrip(".,;:!؟?،؛)")


def _is_ig_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return False
    return any(host == h or host.endswith(f".{h}") for h in IG_HOSTS)


def extract_ig_urls(text: str | None) -> list[str]:
    """استخراج لینک‌های اینستاگرام از متن پیام (به‌همراه حفظ query مثل img_index)."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.findall(text):
        url = _clean_url(match)
        if _is_ig_url(url) and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def _scan_strings(payload: Any) -> list[str]:
    """همهٔ مقدارهای رشته‌ایِ داخل یک payload تودرتو (برای یافتن URL)."""
    strings: list[str] = []
    stack, seen = [payload], set()
    while stack and len(seen) < MAX_SCAN_NODES:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                stack.append(value)
    return strings


def _scan_media_dicts(payload: Any) -> list[dict[str, Any]]:
    """همهٔ dict‌هایی که شبیه «مدیای اینستاگرام»اند (code + media_type/product_type)."""
    hits: list[dict[str, Any]] = []
    stack, seen = [payload], set()
    while stack and len(seen) < MAX_SCAN_NODES:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        if isinstance(node, dict):
            code = node.get("code")
            if isinstance(code, str) and SHORTCODE_RE.match(code) and (
                "media_type" in node or "product_type" in node
            ):
                hits.append(node)
            for value in node.values():
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                stack.append(value)
    return hits


def _media_path(d: dict[str, Any]) -> str:
    """ساخت مسیر URL بر اساس نوع مدیا (رِیل / پست / IGTV)."""
    code = str(d.get("code") or "")
    product_type = str(d.get("product_type") or "").lower()
    media_type = d.get("media_type")
    try:
        media_type = int(media_type)
    except (TypeError, ValueError):
        media_type = 0
    if product_type == "igtv":
        return f"tv/{code}/"
    if product_type == "clips" or (media_type == 2 and product_type == "clips"):
        return f"reel/{code}/"
    if media_type == 2:
        # ویدیوی معمولی پست‌شده → همان مسیر پست (Apify هر دو را پشتیبانی می‌کند)
        return f"p/{code}/"
    return f"p/{code}/"


def _url_from_payload(payload: dict[str, Any]) -> str | None:
    """استخراج URL از یک اشتراک (media_share / clip / xma_share / reel_share …)."""
    # 1) اول خود URLهای اینستاگرام داخل payload
    for text in _scan_strings(payload):
        for match in URL_RE.findall(text):
            url = _clean_url(match)
            if _is_ig_url(url) and "/stories/" not in url.lower():
                return url
    # 2) بعد مدیای ساختاریافته با shortcode
    for media in _scan_media_dicts(payload):
        if media.get("code"):
            return f"https://www.instagram.com/{_media_path(media)}"
    return None


def _story_url_from_payload(payload: dict[str, Any]) -> str | None:
    """ساخت URL استوری از story_share: نیاز به username + pk مدیا دارد."""
    stack, seen = [payload], set()
    while stack and len(seen) < MAX_SCAN_NODES:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        if isinstance(node, dict):
            media = node.get("media")
            if isinstance(media, dict):
                user = media.get("user")
                username = None
                if isinstance(user, dict):
                    username = user.get("username") or user.get("short_name")
                username = username or media.get("username")
                pk = media.get("pk") or media.get("id")
                if username and pk:
                    try:
                        pk = int(str(pk).split("_")[0])
                        return f"https://www.instagram.com/stories/{username}/{pk}/"
                    except (TypeError, ValueError):
                        pass
            for value in node.values():
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                stack.append(value)
    return None


def urls_from_dm(dm: Any) -> list[str]:
    """همهٔ URLهای اینستاگرام قابل‌پردازش از یک پیام دایرکت (متن + اشتراک‌ها)."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    # متن پیام (لینک تایپ‌شده/پیست‌شده)
    for url in extract_ig_urls(getattr(dm, "text", None)):
        add(url)

    # لینک ضمیمه (item_type == link)
    link = getattr(dm, "link", None)
    if isinstance(link, dict):
        add(_clean_url(str(link.get("link_url") or "")) or None)

    # اشتراک‌های مختلف (Share از داخل اپ اینستاگرام)
    for attr in ("media_share", "clip", "reel_share", "xma_share", "felix_share"):
        payload = getattr(dm, attr, None)
        if isinstance(payload, dict) and payload:
            add(_url_from_payload(payload))

    # استوری (آخر اولویت؛ پشتیبانی استوری بستگی به زنجیرهٔ فعلی ربات دارد)
    story_payload = getattr(dm, "story_share", None)
    if isinstance(story_payload, dict) and story_payload:
        add(_story_url_from_payload(story_payload) or _url_from_payload(story_payload))

    return urls[:MAX_URLS_PER_DM]


# ────────────────────────────── Persistent stores ──────────────────────────────

class PairingStore:
    """کد اتصال ↔ چت تلگرام + نگاشت حساب اینستاگرام به چت تلگرام (JSON محلی)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._links: dict[str, dict[str, Any]] = {}
        self._codes: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._links = data.get("links", {}) or {}
                self._codes = data.get("codes", {}) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("ig-dm: could not load %s: %s", self._path, exc)

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"links": self._links, "codes": self._codes},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ig-dm: could not save %s: %s", self._path, exc)

    def prune(self) -> None:
        now = time.time()
        expired = [c for c, rec in self._codes.items() if rec.get("expires", 0) < now]
        for code in expired:
            self._codes.pop(code, None)
        if expired:
            self._save()

    def create_code(self, chat_id: int, user_id: int) -> str:
        self.prune()
        # کدهای قبلی همین کاربر را باطل کن
        for code in [c for c, r in self._codes.items() if r.get("user_id") == user_id]:
            self._codes.pop(code, None)
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if code not in self._codes:
                break
        self._codes[code] = {
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "expires": time.time() + CODE_TTL_SECONDS,
        }
        self._save()
        return code

    def consume_code(self, raw_code: str) -> dict[str, Any] | None:
        self.prune()
        code = raw_code.strip().lstrip("#").upper()
        rec = self._codes.pop(code, None)
        if rec is None:
            return None
        if rec.get("expires", 0) < time.time():
            self._save()
            return None
        self._save()
        return rec

    def link(self, ig_pk: str, chat_id: int, user_id: int, username: str) -> None:
        self._links[str(ig_pk)] = {
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "username": username,
            "linked_at": time.time(),
        }
        self._save()

    def unlink_by_chat(self, chat_id: int) -> int:
        keys = [k for k, r in self._links.items() if r.get("chat_id") == int(chat_id)]
        for key in keys:
            self._links.pop(key, None)
        if keys:
            self._save()
        return len(keys)

    def link_for(self, ig_pk: Any) -> dict[str, Any] | None:
        return self._links.get(str(ig_pk))


class DmState:
    """آخرین پیام دیده‌شدهٔ هر چت + محدودیت ارسال پیام‌های راهنما (JSON محلی)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._threads: dict[str, int] = {}
        self._cooldowns: dict[str, float] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._threads = data.get("threads", {}) or {}
                self._cooldowns = data.get("cooldowns", {}) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("ig-dm: could not load %s: %s", self._path, exc)

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"threads": self._threads, "cooldowns": self._cooldowns},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)
            self._dirty = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("ig-dm: could not save %s: %s", self._path, exc)

    def last_seen(self, thread_id: str) -> int:
        return int(self._threads.get(str(thread_id), 0))

    def mark_seen(self, thread_id: str, item_id: int) -> None:
        key = str(thread_id)
        if int(self._threads.get(key, 0)) < int(item_id):
            self._threads[key] = int(item_id)
            self._dirty = True
            self.save()  # فوری ذخیره کن تا با کرش، پیام‌ها دوباره پردازش نشوند

    def remember(self, key: str, ttl_seconds: float) -> None:
        self._cooldowns[key] = time.monotonic() + ttl_seconds
        self._dirty = True
        self.save()  # فوری ذخیره کن تا پیام‌های راهنما دوباره اسپم نشوند

    def remembered(self, key: str) -> bool:
        until = self._cooldowns.get(key)
        if until is None:
            return False
        if until < time.monotonic():
            self._cooldowns.pop(key, None)
            self._dirty = True
            return False
        return True

    def prune(self) -> None:
        now = time.monotonic()
        stale = [k for k, v in self._cooldowns.items() if v < now]
        for key in stale:
            self._cooldowns.pop(key, None)
        if stale:
            self._dirty = True
        if len(self._threads) > 500:
            # فقط آخرین ۵۰۰ چت فعال را نگه دار
            ordered = sorted(self._threads.items(), key=lambda kv: kv[1])
            self._threads = dict(ordered[-500:])
            self._dirty = True


# ────────────────────────────── Messages (فارسی) ──────────────────────────────

def _instructions_text() -> str:
    page = f"@{IG_DM_PAGE_HINT}" if IG_DM_PAGE_HINT else "پیج"
    return (
        "سلام 👋\n"
        "من ربات دانلود هستم! برای اینکه فایل‌ها توی تلگرامت برسه:\n"
        "۱️⃣ تو ربات تلگرام، دستور /link رو بزن\n"
        "۲️⃣ کد ۶ رقمی که می‌ده رو همین‌جا بفرست\n"
        "۳️⃣ از این به بعد هر ریلز/پست/استوری رو که به دایرکت "
        f"{page} بفرستی، فایلش رو توی تلگرام می‌گیری 📥"
    )


def _linked_ack_text() -> str:
    return "📥 گرفتم! دارم پردازشش می‌کنم — نتیجه رو توی تلگرام برات می‌فرستم ✅"


def _no_link_text() -> str:
    return (
        "🙃 لینک اینستاگرام پیدا نکردم.\n"
        "لینک ریلز/پست رو بفرست یا خود پست رو با Share برام بفرست 📩"
    )


def _linked_success_text(page_hint: str) -> str:
    page = f"@{page_hint}" if page_hint else "پیج"
    return (
        "✅ اتصال برقرار شد!\n"
        "از این به بعد هر ریلز/پست/استوری رو که به دایرکت "
        f"{page} بفرستی، فایلش رو توی تلگرام دریافت می‌کنی 📥"
    )


# ────────────────────────────── Bridge ──────────────────────────────

class InstagramDmBridge:
    """پایش دایرکت پیج اینستاگرام و تحویل فایل به تلگرام با متدهای فعلی ربات."""

    def __init__(
        self,
        application: Any,
        process_url: Callable[..., Any],
        allow_requests: Callable[[tuple[int, int], int], bool],
        active_requests: dict[tuple[int, int], set],
        store: PairingStore,
        state: DmState,
    ) -> None:
        self._application = application
        self._process_url = process_url
        self._allow_requests = allow_requests
        self._active_requests = active_requests
        self._store = store
        self._state = state
        self._client: Any = None
        self._ig_exc: Any = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ig-dm")
        self._admin_notified_at: dict[str, float] = {}
        self._login_failures = 0

    # ── helpers ──

    @property
    def bot(self) -> Any:
        return self._application.bot

    def _bot_username(self) -> str:
        return getattr(self.bot, "username", "") or ""

    async def _run_ig(self, fn: Callable[..., Any], *args: Any) -> Any:
        """اجرای فراخوانی‌های blocking اینستاگرام در thread جدا (بدون بلاک کردن ربات)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args))

    async def notify_admin(self, text: str, key: str, cooldown: float = 1800.0) -> None:
        if not SETTINGS.bot_admin_chat_id:
            return
        now = time.monotonic()
        if now - self._admin_notified_at.get(key, 0.0) < cooldown:
            return
        self._admin_notified_at[key] = now
        try:
            await self.bot.send_message(
                chat_id=SETTINGS.bot_admin_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning("ig-dm: admin notify failed: %s", exc)

    # ── login ──

    async def _ensure_client(self) -> Any | None:
        global IG_DM_PAGE_HINT
        if self._client is not None:
            return self._client
        Client, ig_exc = ig_imports()
        if Client is None:
            return None
        self._ig_exc = ig_exc

        def _build_and_login() -> Any:
            _seed_session_from_env()
            cl = Client()
            cl.delay_range = [1, 2]
            proxy = _proxy_url()
            if proxy:
                # مهم: در instagrapi 2.x انتساب مستقیم «cl.proxy = ...» اثر
                # ندارد (attribute ساده است و سشن‌های requests آپدیت نمی‌شوند)؛
                # باید حتماً set_proxy صدا زده شود.
                try:
                    cl.set_proxy(proxy)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ig-dm: could not set proxy: %s", exc)
            session_source = "fresh"
            if IG_DM_SESSION_FILE.exists():
                try:
                    cl.load_settings(str(IG_DM_SESSION_FILE))
                    session_source = "file"
                    logger.info("ig-dm: session file loaded (%s)", IG_DM_SESSION_FILE.name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ig-dm: session file invalid: %s", exc)
            if IG_SESSIONID:
                # روش پیشنهادی: ورود فقط با کوکی sessionid مرورگر — بدون رمز.
                # مرورگر خودت سشن معتبر و قابل‌اعتماد دارد؛ اینجا فقط همان
                # سشن به کلاینت منتقل می‌شود (هیچ درخواست لاگین رمزی زده
                # نمی‌شود و IP دیتاسنتر هم مشکلی ایجاد نمی‌کند).
                cl.login_by_sessionid(IG_SESSIONID)
                # اعتبارسنجی سخت — اطمینان از اینکه سشن واقعاً زنده است
                cl.current_user()
                session_source = "sessionid"
            else:
                # نکته: login() در instagrapi 2.18 خودش سشن بارگذاری‌شده را
                # اعتبارسنجی می‌کند — اگر سشن معتبر باشد هیچ درخواست لاگین زده
                # نمی‌شود؛ اگر منقضی باشد، با «همان فینگرپرینت دستگاه» دوباره
                # لاگین می‌کند (نه دستگاه تازه) که برای اعتماد اینستاگرام حیاتی است.
                verification_code = totp_now(IG_TOTP_SECRET) or ""
                cl.login(IG_USERNAME, IG_PASSWORD, verification_code=verification_code)
            try:
                cl.dump_settings(str(IG_DM_SESSION_FILE))
            except Exception as exc:  # noqa: BLE001
                logger.warning("ig-dm: could not dump session: %s", exc)
            device_model = ""
            try:
                device_model = str((getattr(cl, "device_settings", None) or {}).get("model", ""))
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                "ig-dm: login ok (session=%s, proxy=%s, device=%s)",
                session_source,
                _mask_proxy(proxy),
                device_model or "?",
            )
            return cl

        try:
            self._client = await self._run_ig(_build_and_login)
            self._login_failures = 0
            pk = getattr(self._client, "user_id", 0)
            # در حالت فقط-sessionid، یوزرنیم پیج ممکن است خالی باشد — از خود کلاینت پر می‌کنیم
            page_name = IG_USERNAME or str(getattr(self._client, "username", "") or "")
            if page_name and not IG_DM_PAGE_HINT:
                IG_DM_PAGE_HINT = page_name
            logger.info("ig-dm: logged in as @%s (pk=%s)", page_name, pk)
            await self.notify_admin(
                "🟢 پل دایرکت اینستاگرام وصل شد (@" + page_name + ")",
                "login_ok",
                cooldown=6 * 60 * 60,
            )
            return self._client
        except Exception as exc:  # noqa: BLE001
            self._login_failures += 1
            name = type(exc).__name__
            two_fa = self._ig_exc is not None and isinstance(
                exc, getattr(self._ig_exc, "TwoFactorRequired", ())
            )
            challenge = self._ig_exc is not None and isinstance(
                exc, (getattr(self._ig_exc, "ChallengeRequired", ()), getattr(self._ig_exc, "ChallengeError", ()))
            )
            throttled = (
                self._ig_exc is not None
                and isinstance(exc, getattr(self._ig_exc, "ClientThrottledError", ()))
            ) or "429" in str(exc)
            bad_password = self._ig_exc is not None and isinstance(
                exc, getattr(self._ig_exc, "BadPassword", ())
            )
            proxy = _proxy_url()
            session_ready = IG_DM_SESSION_FILE.exists()
            if IG_SESSIONID:
                hint_ip = (
                    "sessionid این پیج پذیرفته نشد — یعنی منقضی/ناقص کپی شده یا از مرورگر دیگری است. "
                    "از همان مرورگری که پیج در آن لوگین است، دوباره کوکی sessionid را بردار "
                    "(F12 → Application → Cookies → instagram.com) و IG_SESSIONID را در Railway عوض کن."
                )
            else:
                hint_ip = (
                    "راه‌حل: کوکی sessionid مرورگر را در IG_SESSIONID بگذار (روش پیشنهادی، بدون رمز) "
                    "یا سشن را با ig_session_helper.py بساز؛ یا پراکسی مسکونی در IG_PROXY تنظیم کن."
                )
            if IG_SESSIONID:
                # در حالت sessionid تقریباً هر خطایی یعنی «کوکی قابل قبول نیست»
                detail = hint_ip
            elif two_fa:
                detail = "کد 2FA لازم است — IG_TOTP_SECRET را تنظیم کن یا لاگین دستی بزن."
            elif challenge:
                detail = "چالش تأیید اینستاگرام فعال شد — یک‌بار با مرورگر/اپ وارد پیج شو و تأیید کن."
            elif throttled:
                detail = (
                    "اینستاگرام درخواست‌های لاگین از این IP را محدود کرده (429). " + hint_ip
                )
            elif bad_password:
                detail = (
                    "BadPassword — اگر رمز درست است، اینستاگرام IP دیتاسنتر/فینگرپرینت را رد کرده. "
                    + hint_ip
                )
            else:
                detail = "رمز/کاربر را چک کن یا چند دقیقه بعد دوباره تلاش کن."
            logger.error("ig-dm: login failed (%s): %s — %s", name, exc, detail)
            await self.notify_admin(
                f"🔴 <b>لاگین دایرکت اینستاگرام ناموفق</b>\n"
                f"حساب: <code>{IG_USERNAME}</code>\n"
                f"خطا: <code>{name}: {html_escape(str(exc)[:200])}</code>\n"
                f"پراکسی: <code>{_mask_proxy(proxy)}</code> | سشن: "
                f"<code>{'دارد' if session_ready else 'ندارد'}</code>\n{detail}",
                "login_fail",
                cooldown=1800.0,
            )
            # backoff فزاینده بین تلاش‌های لاگین (برای محدودیت 429 مدت طولانی‌تر)
            cap = 1800 if throttled else 900
            await asyncio.sleep(min(120 * self._login_failures, cap))
            return None

    # ── send into DM thread ──

    async def dm_reply(self, thread_id: str, text: str) -> None:
        client = self._client
        if client is None:
            return
        try:
            await self._run_ig(
                lambda: client.direct_send(text, thread_ids=[str(thread_id)])
            )
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if self._ig_exc is not None and isinstance(
                exc, getattr(self._ig_exc, "ClientLoginRequired", ())
            ):
                logger.warning("ig-dm: DM send hit expired session; will re-login")
                self._client = None
                return
            logger.warning("ig-dm: DM send failed (%s): %s", name, exc)

    async def maybe_dm(self, thread_id: str, key: str, text: str, cooldown: float) -> bool:
        """ارسال پیام راهنما با محدودیت تکرار؛ True یعنی ارسال شد."""
        if self._state.remembered(key):
            return False
        await self.dm_reply(thread_id, text)
        self._state.remember(key, cooldown)
        self._state.save()
        return True

    # ── download pipeline (متدهای فعلی ربات) ──

    def _fake_update(self, chat_id: int, user_id: int) -> SimpleNamespace:
        # _process_url فقط از effective_chat.id و effective_user.id استفاده می‌کند.
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            effective_user=SimpleNamespace(id=user_id),
        )

    def _fake_context(self) -> SimpleNamespace:
        return SimpleNamespace(bot=self.bot)

    async def download_for(self, url: str, rec: dict[str, Any], thread_id: str) -> None:
        chat_id = int(rec["chat_id"])
        user_id = int(rec["user_id"])
        key = (chat_id, user_id)
        task = asyncio.current_task()
        if task is None:
            return

        active = self._active_requests.get(key)
        if active:
            await self.maybe_dm(
                thread_id,
                f"busy:{user_id}",
                "⏳ درخواست قبلی‌ات هنوز در حال پردازشه؛ چند لحظه صبر کن 😊",
                BUSY_COOLDOWN,
            )
            return
        if not self._allow_requests(key, 1):
            await self.maybe_dm(
                thread_id,
                f"ratelimit:{user_id}",
                "⏱ کمی آهسته‌تر 🙂 چند لحظه بعد دوباره بفرست.",
                RATELIMIT_COOLDOWN,
            )
            return

        # پیش‌چک تلگرام: اگر کاربر ربات را Start نکرده/بلاک کرده، نتیجه نمی‌گیرد.
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=(
                    "📨 درخواست از دایرکت اینستاگرام رسید:\n"
                    f"🔗 <code>{html_escape(url)}</code>\n"
                    "⏳ شروع می‌کنم…"
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Forbidden:
            await self.dm_reply(
                thread_id,
                "⚠️ نتونستم بهت پیام بدم! اول تو ربات تلگرام Start بزن بعد دوباره امتحان کن.",
            )
            return
        except TelegramError as exc:
            logger.warning("ig-dm: pre-check send failed: %s", exc)

        bucket = self._active_requests.setdefault(key, set())
        bucket.add(task)
        try:
            await self.dm_reply(thread_id, _linked_ack_text())
            await self._process_url(
                self._fake_update(chat_id, user_id),
                self._fake_context(),
                url,
                None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ig-dm: download failed for %s: %s", url, exc)
        finally:
            bucket.discard(task)
            if not bucket:
                self._active_requests.pop(key, None)

    # ── pairing & message handling ──

    async def _handle_pairing(
        self, thread_id: str, sender_pk: Any, username: str, text: str
    ) -> bool:
        # کد را فقط از «متن بدون لینک» پیدا کن تا با shortcode اینستاگرام قاطی نشود
        text_without_urls = URL_RE.sub(" ", text or "")
        match = CODE_RE.search(text_without_urls)
        if not match:
            return False
        rec = self._store.consume_code(match.group(1))
        if rec is None:
            await self.maybe_dm(
                thread_id,
                f"badcode:{sender_pk}",
                "🤔 این کد معتبر نیست یا منقضی شده. تو ربات تلگرام دوباره /link بزن و کد جدید رو بفرست.",
                30 * 60,
            )
            return True
        self._store.link(str(sender_pk), rec["chat_id"], rec["user_id"], username)
        await self.dm_reply(thread_id, _linked_success_text(IG_DM_PAGE_HINT))
        try:
            account_label = f"@{username}" if username else "اینستاگرام کاربر"
            await self.bot.send_message(
                chat_id=rec["chat_id"],
                text=(
                    "✅ <b>اتصال دایرکت اینستاگرام برقرار شد</b>\n"
                    f"حساب: {account_label}\n"
                    "از این به بعد لینک‌هایی که به دایرکت پیج می‌فرستد، همین‌جا دریافت می‌شود."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc:
            logger.warning("ig-dm: pairing TG notify failed: %s", exc)
        logger.info(
            "ig-dm: linked IG pk=%s (%s) → TG chat=%s", sender_pk, username, rec["chat_id"]
        )
        return True

    async def handle_message(self, thread: Any, dm: Any, my_pk: Any) -> None:
        sender_pk = getattr(dm, "user_id", None)
        if sender_pk is None or (my_pk and str(sender_pk) == str(my_pk)):
            return  # پیام خود بات/پیج

        thread_id = str(getattr(thread, "id", "") or "")
        text = getattr(dm, "text", None) or ""
        item_type = str(getattr(dm, "item_type", "") or "")
        logger.info(
            "ig-dm: message from pk=%s type=%s thread=%s", sender_pk, item_type, thread_id
        )

        username = ""
        for user in getattr(thread, "users", None) or []:
            if str(getattr(user, "pk", "")) == str(sender_pk):
                username = getattr(user, "username", "") or ""
                break

        # 1) جفت‌سازی با کد
        if text and await self._handle_pairing(thread_id, sender_pk, username, text):
            # اگر همراه کد، لینک هم فرستاده شده بود، ادامه بده
            pass

        # 2) استخراج لینک‌ها (متن یا Share)
        urls = urls_from_dm(dm)
        rec = self._store.link_for(sender_pk)
        if not urls:
            if rec:
                if item_type == "text" and text and not CODE_RE.search(text):
                    await self.maybe_dm(
                        thread_id,
                        f"nolink:{sender_pk}",
                        _no_link_text(),
                        NO_LINK_COOLDOWN,
                    )
            elif item_type in {"text", "media_share", "clip", "reel_share", "xma_share", "story_share", "link"}:
                await self.maybe_dm(
                    thread_id,
                    f"guide:{sender_pk}",
                    _instructions_text(),
                    INSTRUCTION_COOLDOWN,
                )
            return

        if rec is None:
            guide = _instructions_text()
            if self._bot_username():
                guide += f"\n\n🤖 ربات تلگرام: @{self._bot_username()}"
            await self.maybe_dm(thread_id, f"guide:{sender_pk}", guide, INSTRUCTION_COOLDOWN)
            return

        for url in urls:
            await self.download_for(url, rec, thread_id)

    # ── polling ──

    def _thread_messages(self, thread: Any) -> list[Any]:
        messages = getattr(thread, "messages", None) or getattr(thread, "items", None) or []
        ordered = sorted(messages, key=lambda m: int(str(getattr(m, "id", "0")) or 0))
        return ordered

    async def poll_once(self, client: Any) -> None:
        threads = await self._run_ig(lambda: client.direct_threads(amount=IG_DM_MAX_THREADS))
        my_pk = getattr(client, "user_id", None)
        for thread in threads or []:
            thread_id = str(getattr(thread, "id", "") or "")
            if not thread_id:
                continue
            # فقط چت‌های خصوصی (نه گروه)
            thread_type = str(getattr(thread, "thread_type", "") or "")
            users = getattr(thread, "users", None) or []
            if thread_type and thread_type != "one_to_one" and len(users) != 1:
                continue

            messages = self._thread_messages(thread)
            if not messages:
                continue
            newest = int(str(messages[-1].id))
            if not self._state.last_seen(thread_id):
                # اولین دیدن این چت → فقط baseline بساز (هیچ پیام قدیمی پردازش نشود)
                self._state.mark_seen(thread_id, newest)
                self._state.save()
                continue
            fresh = [m for m in messages if int(str(m.id)) > self._state.last_seen(thread_id)]
            if not fresh:
                continue
            # چت کامل را بگیر تا پیام‌هایی که بین دو پول آمده‌اند از دست نروند
            try:
                full = await self._run_ig(
                    lambda: client.direct_thread(thread_id, amount=10)
                )
                if full is not None:
                    complete = self._thread_messages(full)
                    if len(complete) > len(fresh):
                        fresh = [
                            m
                            for m in complete
                            if int(str(m.id)) > self._state.last_seen(thread_id)
                        ]
            except Exception as exc:  # noqa: BLE001
                logger.debug("ig-dm: direct_thread fetch failed: %s", exc)

            for dm in fresh:
                try:
                    await self.handle_message(thread, dm, my_pk)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("ig-dm: message handling failed: %s", exc)
                self._state.mark_seen(thread_id, int(str(getattr(dm, "id", "0")) or 0))
            self._state.mark_seen(thread_id, newest)
            self._state.save()

    async def run(self) -> None:
        """حلقهٔ اصلی پایش دایرکت — با backoff برای خطاها."""
        await asyncio.sleep(5)  # اجازه بده ربات اول بالا بیاید
        Client, ig_exc = ig_imports()
        if Client is None:
            logger.error(
                "ig-dm: instagrapi نصب نیست؛ پل دایرکت خاموش است. pip install instagrapi"
            )
            return
        self._ig_exc = ig_exc
        error_streak = 0
        logger.info(
            "ig-dm: bridge started (poll=%ss, threads=%s, page=@%s)",
            IG_DM_POLL_SECONDS,
            IG_DM_MAX_THREADS,
            IG_USERNAME,
        )
        while True:
            try:
                client = await self._ensure_client()
                if client is None:
                    await asyncio.sleep(60)
                    continue
                await self.poll_once(client)
                error_streak = 0
                self._state.prune()
                self._state.save()
                await asyncio.sleep(IG_DM_POLL_SECONDS + secrets.randbelow(3))
            except asyncio.CancelledError:
                logger.info("ig-dm: bridge stopped")
                raise
            except Exception as exc:  # noqa: BLE001
                error_streak += 1
                name = type(exc).__name__
                rate_limited = self._ig_exc is not None and isinstance(
                    exc, getattr(self._ig_exc, "RateLimitError", ())
                )
                unauthorized = self._ig_exc is not None and isinstance(
                    exc, (getattr(self._ig_exc, "ClientLoginRequired", ()),)
                )
                if unauthorized:
                    logger.warning("ig-dm: session expired → re-login next loop")
                    self._client = None
                delay = min(30 * (2 ** min(error_streak, 4)), 600)
                if rate_limited:
                    delay = max(delay, 120)
                    logger.warning("ig-dm: rate limited by Instagram → sleep %ss", delay)
                logger.error(
                    "ig-dm: poll error (%s): %s → retry in %ss", name, exc, delay
                )
                await self.notify_admin(
                    f"⚠️ پل دایرکت اینستاگرام خطا داد: <code>{name}</code> — ادامه می‌دهم.",
                    f"poll_error:{name}",
                    cooldown=3600.0,
                )
                await asyncio.sleep(delay)


# ────────────────────────────── Telegram command handlers ──────────────────────────────

_BRIDGE: InstagramDmBridge | None = None
_STORE: PairingStore | None = None
_STATE: DmState | None = None


def html_escape(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ensure_stores() -> None:
    global _STORE, _STATE
    if _STORE is None:
        _STORE = PairingStore(IG_LINKS_FILE)
    if _STATE is None:
        _STATE = DmState(IG_DM_STATE_FILE)


async def link_command(update: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دستور /link — ساخت کد اتصال تلگرام ↔ دایرکت پیج اینستاگرام."""
    _ensure_stores()
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return
    if not feature_enabled():
        await message.reply_text(
            "😕 این قابلیت فعلاً غیرفعال است.",
            parse_mode=ParseMode.HTML,
        )
        return
    code = _STORE.create_code(chat.id, user.id)
    page = f"@{IG_DM_PAGE_HINT}" if IG_DM_PAGE_HINT else "پیج اینستاگرام"
    await message.reply_text(
        "🔗 <b>اتصال دایرکت اینستاگرام</b>\n\n"
        "۱. این کد را به دایرکت "
        f"{page} بفرست:\n"
        f"📬 <code>{code}</code>\n\n"
        f"۲. از این به بعد هر ریلز/پست/استوری را که به دایرکت {page} بفرستی، "
        "فایلش را همین‌جا در تلگرام دریافت می‌کنی 📥\n\n"
        f"⏳ اعتبار کد: {CODE_TTL_SECONDS // 60} دقیقه",
        parse_mode=ParseMode.HTML,
    )


async def unlink_command(update: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دستور /unlink — قطع اتصال این چت تلگرام از دایرکت پیج."""
    _ensure_stores()
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return
    removed = _STORE.unlink_by_chat(chat.id)
    if removed:
        await message.reply_text(
            "🔌 اتصال این چت از دایرکت پیج اینستاگرام قطع شد.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "ℹ️ هیچ اتصالی برای این چت پیدا نشد.",
            parse_mode=ParseMode.HTML,
        )


async def igsession_command(update: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دستور ادمین /igsession — خروجی base64 سشن فعلی برای متغیر IG_SESSION_B64."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not SETTINGS.bot_admin_chat_id or chat.id != SETTINGS.bot_admin_chat_id:
        await message.reply_text("🔒 این دستور فقط برای ادمین ربات است.")
        return
    if not feature_enabled():
        await message.reply_text(
            "😕 قابلیت دایرکت اینستاگرام فعال نیست (IG_USERNAME / IG_PASSWORD تنظیم نشده)."
        )
        return
    b64 = session_file_b64()
    if b64 is None:
        await message.reply_text(
            "ℹ️ هنوز فایل سشن اینستاگرام ساخته نشده است.\n"
            "یا لاگین هنوز موفق نشده، یا فایل سشن خالی است."
        )
        return
    # پیام تلگرام ۴۰۹۶ نویسه سقف دارد → تکه‌تکه می‌فرستیم
    chunk_size = 3500
    chunks = [b64[i : i + chunk_size] for i in range(0, len(b64), chunk_size)]
    await message.reply_text(
        f"📦 سشن فعلی اینستاگرام — base64 ({len(chunks)} بخش؛ همه را پشت‌سرهم "
        "کپی کن و به متغیر <code>IG_SESSION_B64</code> در Railway بده):",
        parse_mode=ParseMode.HTML,
    )
    for index, chunk in enumerate(chunks, 1):
        await message.reply_text(
            f"<code>{chunk}</code>" if len(chunks) == 1 else f"({index}/{len(chunks)})\n<code>{chunk}</code>",
            parse_mode=ParseMode.HTML,
        )


# ────────────────────────────── Setup hook (از post_init ربات) ──────────────────────────────

def maybe_start(
    application: Any,
    process_url: Callable[..., Any],
    allow_requests: Callable[[tuple[int, int], int], bool],
    active_requests: dict[tuple[int, int], set],
) -> asyncio.Task | None:
    """اگر قابلیت فعال باشد، پل دایرکت را در پس‌زمینه اجرا می‌کند."""
    global _BRIDGE
    if not feature_enabled():
        logger.info("ig-dm: disabled (no IG_SESSIONID and no IG_USERNAME / IG_PASSWORD)")
        return None
    Client, _ = ig_imports()
    if Client is None:
        logger.error("ig-dm: instagrapi not installed → bridge disabled")
        return None
    _ensure_stores()
    _seed_session_from_env()
    proxy = _proxy_url()
    auth_mode = "sessionid" if IG_SESSIONID else "password"
    logger.info(
        "ig-dm: enabled (page=@%s, auth=%s, session_file=%s, proxy=%s)",
        IG_USERNAME or IG_DM_PAGE_HINT or "?",
        auth_mode,
        IG_DM_SESSION_FILE.name,
        _mask_proxy(proxy),
    )
    if auth_mode == "password" and not proxy and not IG_DM_SESSION_FILE.exists():
        logger.warning(
            "ig-dm: هشدار — لاگین رمزی از IP دیتاسنتر به احتمال زیاد رد می‌شود "
            "(BadPassword / 429). راه بهتر: کوکی sessionid مرورگر را در متغیر "
            "IG_SESSIONID بگذار (بدون رمز، بدون پراکسی). راهنما: IG_DM_SETUP_FA.md"
        )
    _BRIDGE = InstagramDmBridge(
        application, process_url, allow_requests, active_requests, _STORE, _STATE
    )
    return asyncio.create_task(_BRIDGE.run(), name="ig-dm-bridge")
