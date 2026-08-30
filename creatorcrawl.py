"""CreatorCrawl API client — Instagram profile + latest post with key rotation.

Replaces the old direct-Instagram-scraping ``instagram_profile.py`` method.
The bot now talks to https://app.creatorcrawl.com only, using the TWO
endpoints documented in the official OpenAPI spec
(https://app.creatorcrawl.com/api/openapi.json):

    GET /api/instagram/profile?handle=<username>
        → data{handle, name, avatar_url, follower_count, post_count,
               verified, is_private, recent_posts[]}
    GET /api/instagram/user/posts?handle=<username>
        → data[{url, type, text, media[{type,url,width,height}],
                like_count, comment_count, author{handle,name,avatar_url}}]

    header: x-api-key: <KEY>

Both endpoints return a NORMALIZED envelope: ``{"data": ..., "page": {},
"meta": {}}``.  ``/profile`` is queried first — it carries the avatar,
follower/post counts AND (when the upstream includes them) recent posts, so
the whole card usually costs ONE credit.  Only when that response has no
usable latest post does the bot spend a second credit on ``/user/posts``.
Private pages skip the second call entirely.

Every successful call costs 1 credit.  Each CreatorCrawl account (free plan)
ships 50 credits, so the operator registers SEVERAL accounts and lists all
keys in ``CREATORCRAWL_API_KEYS`` as ``API_KEY|EMAIL`` pairs.  This module:

  * rotates to the next key the moment a key reaches its per-key limit
    (``CREATORCRAWL_KEY_LIMIT``, default 50) — no user-visible hiccup;
  * notifies the bot admin by Telegram PV with the exhausted key's OWNER
    EMAIL ("۵۰ درخواست با این کلید زده شد");
  * persists per-key usage counters OUTSIDE Railway so they survive
    restarts, redeploys and even deploys on a different Railway account:
      - Upstash Redis REST (UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN)
        → recommended, plain HTTPS, no SDK needed;
      - fallback: local ``cc_usage.json`` (survives restarts only).
  * self-heals: if the API itself rejects a key (billing / rate-limit /
    unauthorized) the key is marked exhausted immediately and the request
    is retried on the next key.

CreatorCrawl is ONLY used to DISCOVER the profile card and the latest
post's link.  When the user taps "دانلود آخرین پست", the bot feeds that
link into its regular download chain (AHM7 → Apify → gateway-worker
bots → yt-dlp / SOCIAL_GATEWAY) exactly as if the user had pasted the
link themselves — CreatorCrawl never downloads the media.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, replace
from html import unescape
from pathlib import Path
from typing import Any

import httpx
import telegram
from telegram.constants import ParseMode

from config import PROJECT_DIR, SETTINGS

logger = logging.getLogger("MZDownloader.creatorcrawl")

API_BASE = "https://app.creatorcrawl.com/api"
PROFILE_PATH = "/instagram/profile"
USER_POSTS_PATH = "/instagram/user/posts"

# Response-envelope tokens that mean "the KEY/account is the problem".
_KEY_ERROR_TOKENS = (
    "credit", "quota", "unauthorized", "invalid api key", "billing",
    "payment", "rate limit", "rate-limit", "ratelimit", "exceeded",
    "insufficient", "forbidden", "not authorized",
)
# Response-envelope tokens that mean "the HANDLE is the problem".
_NOT_FOUND_TOKENS = (
    "not found", "no user", "user_not_found", "doesn't exist",
    "does not exist", "unable to locate", "invalid handle",
    "unknown handle", "cannot find user", "couldn't find",
)


# ── Exceptions ───────────────────────────────────────────────────────


class CreatorCrawlError(RuntimeError):
    """Generic CreatorCrawl failure (network / 5xx / bad payload)."""


class CreatorCrawlNoKeys(CreatorCrawlError):
    """No key configured, or every key has burned through its quota."""


class CreatorCrawlNotFound(CreatorCrawlError):
    """The requested Instagram handle does not exist (or exposes nothing)."""


class _KeyRejected(Exception):
    """The API refused THIS key (quota / unauthorized) → rotate and retry."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CreatorCrawlMedia:
    url: str
    kind: str  # "photo" | "video"


@dataclass(frozen=True, slots=True)
class CreatorCrawlPost:
    shortcode: str
    url: str  # https://www.instagram.com/p/<code>/
    caption: str
    like_count: int
    comment_count: int
    media: tuple[CreatorCrawlMedia, ...]


@dataclass(frozen=True, slots=True)
class CreatorCrawlProfile:
    username: str
    full_name: str
    avatar_url: str
    followers: int
    posts: int
    is_verified: bool
    latest_post: CreatorCrawlPost | None
    is_private: bool = False


# ── Key bookkeeping ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Key:
    index: int  # 0-based position in CREATORCRAWL_API_KEYS
    secret: str
    email: str
    key_id: str  # stable digest — survives redeploys, never exposes the key

    @property
    def label(self) -> str:
        return f"#{self.index + 1}"


def _make_key(index: int, raw: str) -> _Key:
    raw = raw.strip()
    if "|" in raw:
        secret, email = raw.split("|", 1)
        secret, email = secret.strip(), email.strip()
    else:
        secret, email = raw, ""
    return _Key(
        index=index,
        secret=secret,
        email=email,
        key_id=hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16],
    )


_state: dict[str, Any] = {
    "bot": None,
    "admin_chat_id": None,
    "keys": (),
    "limit": 50,
    "kv": None,
}


# ── Persistent counters (must survive redeploy / new Railway account) ─


class _UpstashKV:
    """Minimal Upstash Redis REST client (plain HTTP, no SDK)."""

    def __init__(self, rest_url: str, rest_token: str) -> None:
        self._url = rest_url.rstrip("/")
        self._token = rest_token

    async def _command(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            resp = await client.post(
                f"{self._url}/{path}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if resp.status_code != 200:
                raise CreatorCrawlError(f"Upstash KV HTTP {resp.status_code}")
            payload = resp.json()
            if not payload.get("error"):
                return payload.get("result")
            raise CreatorCrawlError(f"Upstash KV error: {payload.get('error')}")

    async def get(self, key: str) -> str | None:
        result = await self._command(f"get/{key}")
        return None if result is None else str(result)

    async def incr(self, key: str) -> int:
        result = await self._command(f"incr/{key}")
        try:
            return int(result)
        except (TypeError, ValueError):
            return 0

    async def set(self, key: str, value: str) -> None:
        await self._command(f"set/{key}/{value}")


class _FileKV:
    """Local-JSON fallback. Survives container restarts, NOT redeploys."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._warned = False

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("cc_usage.json write failed: %s", exc)

    async def get(self, key: str) -> str | None:
        async with self._lock:
            value = self._read().get(key)
            return None if value is None else str(value)

    async def incr(self, key: str) -> int:
        async with self._lock:
            data = self._read()
            current = int(data.get(key, 0) or 0) + 1
            data[key] = current
            self._write(data)
            return current

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)


def _kv() -> Any:
    kv = _state["kv"]
    if kv is None:
        rest_url = str(SETTINGS.upstash_rest_url or "").strip()
        rest_token = str(SETTINGS.upstash_rest_token or "").strip()
        if rest_url and rest_token:
            kv = _UpstashKV(rest_url, rest_token)
            logger.info("CreatorCrawl counters → Upstash Redis (persistent across deploys)")
        else:
            kv = _FileKV(PROJECT_DIR / "cc_usage.json")
            logger.warning(
                "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not set — "
                "CreatorCrawl counters fall back to a local file and WILL reset "
                "on redeploy. Configure Upstash for full persistence."
            )
        _state["kv"] = kv
    return kv


def _count_key(key_id: str) -> str:
    return f"mzdl:cc:count:{key_id}"


_ACTIVE_KEY = "mzdl:cc:active"


# ── Admin PV notification ────────────────────────────────────────────


def initialize(
    bot: telegram.Bot | None,
    admin_chat_id: int | None,
    keys: tuple[str, ...] = (),
    key_limit: int = 50,
) -> None:
    """Wire the notifier + key list (call once from ``post_init``)."""
    _state["bot"] = bot
    _state["admin_chat_id"] = admin_chat_id if admin_chat_id and admin_chat_id > 0 else None
    _state["keys"] = tuple(_make_key(i, raw) for i, raw in enumerate(keys))
    _state["limit"] = max(1, int(key_limit))
    if not _state["keys"]:
        logger.warning("CREATORCRAWL_API_KEYS is empty — /profile is disabled")
    else:
        logger.info(
            "CreatorCrawl ready: %d key(s), limit %d req/key",
            len(_state["keys"]),
            _state["limit"],
        )


async def _notify_key_exhausted(key: _Key, reason: str, next_key: _Key | None) -> None:
    bot = _state["bot"]
    admin_chat_id = _state["admin_chat_id"]
    if bot is None or admin_chat_id is None:
        logger.info(
            "Key %s (%s) exhausted (%s); BOT_ADMIN_CHAT_ID not set — no PV sent",
            key.label, key.email or "no-email", reason,
        )
        return

    total = len(_state["keys"])
    # Count how many OTHER keys are already exhausted according to the store.
    exhausted = 0
    for other in _state["keys"]:
        if other.key_id == key.key_id:
            continue
        raw = await _kv().get(_count_key(other.key_id))
        if raw is not None and int(raw or 0) >= _state["limit"]:
            exhausted += 1

    if next_key is not None:
        next_email = f" ({html.escape(next_key.email)})" if next_key.email else ""
        switch_line = f"➡️ سوییچ شد به کلید بعدی: <b>#{next_key.index + 1}</b>{next_email}"
    else:
        switch_line = "⛔ هیچ کلید فعالی باقی نمانده — /profile تا افزودن کلید جدید خاموش است."

    body = (
        "🔑 <b>کلید CreatorCrawl پر شد و ربات روی کلید بعدی سوییچ کرد</b>\n\n"
        f"👤 ایمیل مالک کلید: <b>{html.escape(key.email) if key.email else 'نامشخص'}</b>\n"
        f"🔢 کلید: <code>{key.label} · {key.key_id[:8]}</code>\n"
        f"📊 <b>{_state['limit']} درخواست</b> با این کلید زده شده و سهمیه‌اش تمام شد.\n"
        + (f"❗ دلیل: {reason}\n" if reason and reason != "سهمیه تکمیل شد" else "")
        + f"{switch_line}\n"
        f"🧯 کلیدهای سالم باقی‌مانده: <b>{max(0, total - exhausted - 1)}</b> از {total}"
    )
    try:
        await bot.send_message(chat_id=admin_chat_id, text=body, parse_mode=ParseMode.HTML)
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        logger.warning("Could not deliver CreatorCrawl key-alert PV: %s", exc)


# ── Low-level API call ───────────────────────────────────────────────


def _validate_handle(handle: str) -> str:
    cleaned = (handle or "").strip().strip("@/").strip()
    # Accept a full profile URL as well as a bare handle.
    match = re.search(r"instagram\.com/([A-Za-z0-9._]+)/?", cleaned)
    if match:
        cleaned = match.group(1)
    cleaned = cleaned.strip("@/").lower()
    if not cleaned:
        raise CreatorCrawlError("نام‌کاربری خالی است")
    if not re.fullmatch(r"[a-z0-9._]{1,30}", cleaned):
        raise CreatorCrawlError("نام‌کاربری اینستاگرام نامعتبر است")
    return cleaned


async def _call_api(api_key: str, path: str, handle: str) -> dict[str, Any]:
    from perf import pooled_client

    try:
        client = pooled_client("creatorcrawl")
        resp = await client.get(
            f"{API_BASE}{path}",
            params={"handle": handle},
            headers={"x-api-key": api_key, "accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise CreatorCrawlError(f"خطای شبکه در تماس با CreatorCrawl: {exc}") from exc

    # 400 = "Missing or invalid handle", 404 = unknown handle (both mean the
    # HANDLE is wrong — retrying with another key would only burn credits).
    if resp.status_code in (400, 404):
        raise CreatorCrawlNotFound("handle_not_found")
    if resp.status_code in (401, 403):
        raise _KeyRejected("کلید نامعتبر یا غیرفعال شد (HTTP %d)" % resp.status_code)
    if resp.status_code == 402:
        raise _KeyRejected("اعتبار/سهمیه حساب تمام شد (HTTP 402)")
    if resp.status_code == 429:
        raise _KeyRejected("محدودیت نرخ یا سهمیه (HTTP 429)")
    if resp.status_code >= 500:
        raise CreatorCrawlError(f"خطای سرور CreatorCrawl (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise CreatorCrawlError(f"پاسخ غیرمنتظره CreatorCrawl (HTTP {resp.status_code})")

    try:
        data = resp.json()
    except ValueError as exc:
        raise CreatorCrawlError("پاسخ CreatorCrawl JSON نبود") from exc

    if not isinstance(data, dict):
        raise CreatorCrawlError("ساختار پاسخ CreatorCrawl نامعتبر بود")

    # Some plans surface quota/handle problems inside a 200 envelope.
    message = str(data.get("error") or data.get("message") or data.get("detail") or "")
    lowered = message.lower()
    if lowered:
        if any(token in lowered for token in _NOT_FOUND_TOKENS):
            raise CreatorCrawlNotFound(message[:120])
        if any(token in lowered for token in _KEY_ERROR_TOKENS):
            raise _KeyRejected(f"سرویس کلید را رد کرد: {message[:120]}")

    return data


# ── Response parsing (matches the official normalized OpenAPI shapes) ─


def _first_int(*values: Any) -> int:
    for value in values:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _dump_payload(kind: str, data: dict[str, Any]) -> None:
    """Log an unrecognized payload so Railway logs reveal the real shape.

    Never raises; the snippet contains only public post/profile data, no keys.
    """
    try:
        snippet = json.dumps(data, ensure_ascii=False)[:800]
    except Exception:  # noqa: BLE001 — logging must never break the flow
        snippet = str(data)[:800]
    logger.error(
        "CreatorCrawl %s payload mismatch — top-level keys=%s snippet=%s",
        kind, list(data.keys()), snippet,
    )


_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv|reels)/([A-Za-z0-9_-]+)")


def _extract_shortcode(url: str, item: dict[str, Any]) -> str:
    match = _SHORTCODE_RE.search(url or "")
    if match:
        return match.group(1)
    return str(item.get("code") or item.get("shortcode") or "").strip()


def _media_list_normalized(item: dict[str, Any]) -> list[CreatorCrawlMedia]:
    """Media list from the documented normalized shape.

    ``media`` is an array of ``{type: image|video|gif, url, thumbnail_url,
    width, height, duration_seconds}``; carousels arrive FLATTENED in it.
    The API's order is preserved (carousel slide order matters); entries
    without a URL are skipped, falling back to ``thumbnail_url``.
    """
    entries = item.get("media")
    if not isinstance(entries, list):
        return []
    media: list[CreatorCrawlMedia] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            url = str(entry.get("thumbnail_url") or "").strip()
        if not url:
            continue
        kind = "video" if str(entry.get("type") or "").lower() == "video" else "photo"
        media.append(CreatorCrawlMedia(url=url, kind=kind))
    return media


def _pick_media(item: dict[str, Any]) -> CreatorCrawlMedia | None:
    """LEGACY fallback: best-quality media from raw Instagram feed shapes."""
    is_video = bool(item.get("video_versions")) or item.get("media_type") == 2 or bool(item.get("video_url"))

    if is_video:
        versions = item.get("video_versions") or []
        best, best_area = "", 0
        for version in versions:
            url = str(version.get("url") or "")
            if not url:
                continue
            area = _first_int(version.get("width")) * _first_int(version.get("height"))
            if area >= best_area:
                best, best_area = url, area
        url = best or str(item.get("video_url") or "")
        if url:
            return CreatorCrawlMedia(url=url, kind="video")

    candidates = (item.get("image_versions2") or {}).get("candidates") or []
    best, best_area = "", 0
    for candidate in candidates:
        url = str(candidate.get("url") or "")
        if not url:
            continue
        area = _first_int(candidate.get("width")) * _first_int(candidate.get("height"))
        if area >= best_area:
            best, best_area = url, area
    url = best or str(item.get("display_url") or item.get("display_uri") or "")
    if url:
        return CreatorCrawlMedia(url=url, kind="photo")
    return None


def _media_list_legacy(item: dict[str, Any]) -> list[CreatorCrawlMedia]:
    """LEGACY fallback for raw Instagram feed items (carousel-aware)."""
    media: list[CreatorCrawlMedia] = []
    carousel = item.get("carousel_media")
    if not carousel and item.get("edge_sidecar_to_children"):
        carousel = (item.get("edge_sidecar_to_children") or {}).get("edges") or []
        carousel = [edge.get("node") for edge in carousel if isinstance(edge, dict)]

    if carousel:
        for child in carousel:
            if isinstance(child, dict):
                piece = _pick_media(child)
                if piece:
                    media.append(piece)
    else:
        piece = _pick_media(item)
        if piece:
            media.append(piece)
    return media


def _parse_post(item: dict[str, Any]) -> CreatorCrawlPost | None:
    """Parse ONE post — normalized shape first, raw Instagram shape second."""
    if not isinstance(item, dict):
        return None

    url = str(item.get("url") or "").strip()
    shortcode = _extract_shortcode(url, item)
    if not url and shortcode:
        url = f"https://www.instagram.com/p/{shortcode}/"

    caption_raw = item.get("text")
    if caption_raw is None:
        caption_raw = item.get("caption")
    if isinstance(caption_raw, dict):
        caption_text = str(caption_raw.get("text") or "")
    else:
        caption_text = str(caption_raw or "")

    like_count = _first_int(
        item.get("like_count"),
        (item.get("edge_liked_by") or {}).get("count"),
        (item.get("edge_media_preview_like") or {}).get("count"),
    )
    comment_count = _first_int(
        item.get("comment_count"),
        (item.get("edge_media_to_comment") or {}).get("count"),
        (item.get("edge_media_to_parent_comment") or {}).get("count"),
    )

    media = _media_list_normalized(item) or _media_list_legacy(item)

    if not media and not url:
        return None

    return CreatorCrawlPost(
        shortcode=shortcode,
        url=url,
        caption=unescape(caption_text).strip(),
        like_count=like_count,
        comment_count=comment_count,
        media=tuple(media),
    )


def _first_post(items: Any) -> CreatorCrawlPost | None:
    if not isinstance(items, list):
        return None
    for item in items:
        post = _parse_post(item)
        if post is not None:
            return post
    return None


def _unwrap_user_node(data: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the profile object inside a /profile response.

    Documented: ``data.data`` is the user node.  Defensively accepted:
    ``data.data.user``, ``data.user`` (legacy wrappers).

    Returns ``{}`` for EXPLICIT emptiness (``data: null`` / ``[]``) → the
    handle resolved to nothing; returns ``None`` only for shapes we do not
    recognize at all (honest-error path, never a fake "not found").
    """
    if "data" in data:
        raw = data.get("data")
        if isinstance(raw, dict):
            inner_user = raw.get("user")
            if isinstance(inner_user, dict) and inner_user and not (
                raw.get("handle") or raw.get("avatar_url") or raw.get("follower_count")
            ):
                return inner_user
            return raw
        if isinstance(raw, list):
            return {} if not raw else None  # empty list == explicitly nothing
        if raw is None:
            return {}  # explicit null == explicitly nothing found
        return None  # str/int/… → unrecognized shape
    legacy = data.get("user")
    if isinstance(legacy, dict):
        return legacy
    return None


def _scan_node_error(node: dict[str, Any], handle: str) -> None:
    """Raise if the user node itself carries an error / not-found marker."""
    message = str(node.get("error") or node.get("message") or node.get("detail") or "")
    lowered = message.lower()
    if not lowered:
        return
    if any(token in lowered for token in _NOT_FOUND_TOKENS):
        raise CreatorCrawlNotFound(f"پیج @{handle} پیدا نشد")
    if any(token in lowered for token in _KEY_ERROR_TOKENS):
        raise _KeyRejected(f"سرویس کلید را رد کرد: {message[:120]}")
    raise CreatorCrawlError(f"CreatorCrawl: {message[:150]}")


def _parse_profile_response(data: dict[str, Any], handle: str) -> CreatorCrawlProfile:
    """Parse ``GET /instagram/profile`` → avatar, followers, post count, recent posts."""
    message = str(data.get("error") or data.get("message") or data.get("detail") or "")
    lowered = message.lower()
    if lowered and any(token in lowered for token in _NOT_FOUND_TOKENS):
        raise CreatorCrawlNotFound(f"پیج @{handle} پیدا نشد")
    if lowered and any(token in lowered for token in _KEY_ERROR_TOKENS):
        raise _KeyRejected(f"سرویس کلید را رد کرد: {message[:120]}")

    node = _unwrap_user_node(data)
    if node is None:
        # A shape we simply do not recognize — say so honestly (with a log
        # dump) instead of lying "page not found".
        _dump_payload("profile", data)
        raise CreatorCrawlError(
            "ساختار پاسخ CreatorCrawl شناخته نشد — جزئیات در لاگ سرور ثبت شد"
        )
    if not node:
        # Explicitly empty response (null / {} / []) → the handle truly
        # resolved to nothing.
        raise CreatorCrawlNotFound(f"پیج @{handle} پیدا نشد")
    _scan_node_error(node, handle)

    username = str(node.get("handle") or node.get("username") or handle).lstrip("@")
    followers = _first_int(
        node.get("follower_count"),
        (node.get("edge_followed_by") or {}).get("count"),
        node.get("followerCount"),
    )
    posts = _first_int(
        node.get("post_count"),
        node.get("media_count"),
        (node.get("edge_owner_to_timeline_media") or {}).get("count"),
        node.get("mediaCount"),
        node.get("postCount"),
    )
    avatar = str(
        node.get("avatar_url")
        or node.get("profile_pic_url_hd")
        or node.get("profile_pic_url")
        or node.get("profile_pic_url_https")
        or ""
    )
    full_name = unescape(str(node.get("name") or node.get("full_name") or "")).strip()
    is_verified = bool(node.get("verified") or node.get("is_verified"))
    is_private = bool(node.get("is_private"))

    latest_post = _first_post(node.get("recent_posts"))

    return CreatorCrawlProfile(
        username=username,
        full_name=full_name,
        avatar_url=avatar,
        followers=followers,
        posts=posts,
        is_verified=is_verified,
        latest_post=latest_post,
        is_private=is_private,
    )


def _parse_posts_response(
    data: dict[str, Any], handle: str
) -> tuple[CreatorCrawlPost | None, dict[str, Any]]:
    """Parse ``GET /instagram/user/posts`` → (first usable post, author node).

    An empty ``data`` array is NOT an error here — existing accounts can
    simply have zero posts — so this returns (None, {}) instead of raising.
    """
    message = str(data.get("error") or data.get("message") or data.get("detail") or "")
    lowered = message.lower()
    if lowered and any(token in lowered for token in _NOT_FOUND_TOKENS):
        raise CreatorCrawlNotFound(f"پیج @{handle} پیدا نشد")
    if lowered and any(token in lowered for token in _KEY_ERROR_TOKENS):
        raise _KeyRejected(f"سرویس کلید را رد کرد: {message[:120]}")

    items: Any = None
    raw = data.get("data")
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        if isinstance(raw.get("items"), list):
            items = raw["items"]
        elif isinstance(raw.get("recent_posts"), list):
            items = raw["recent_posts"]
    elif raw is None and "data" in data:
        items = []  # explicit null == no posts
    if items is None and isinstance(data.get("items"), list):
        items = data["items"]

    if items is None:
        _dump_payload("user/posts", data)
        raise CreatorCrawlError(
            "ساختار پاسخ CreatorCrawl شناخته نشد — جزئیات در لاگ سرور ثبت شد"
        )

    for item in items:
        post = _parse_post(item)
        if post is not None:
            author = item.get("author") if isinstance(item.get("author"), dict) else {}
            return post, author
    return None, {}


# ── Public entry point ───────────────────────────────────────────────


async def _request_via_rotation(path: str, handle: str) -> dict[str, Any]:
    """Run ONE API call through the key-rotation / quota / notify machinery.

    Every HTTP-200 response burns 1 credit of the key that served it.
    """
    keys: tuple[_Key, ...] = _state["keys"]
    if not keys:
        raise CreatorCrawlNoKeys(
            "هیچ کلید CreatorCrawl تنظیم نشده است. متغیر CREATORCRAWL_API_KEYS را در Railway پر کن."
        )
    limit: int = _state["limit"]
    kv = _kv()

    active_id = await kv.get(_ACTIVE_KEY)
    start = next((k.index for k in keys if k.key_id == active_id), 0)
    ordered = keys[start:] + keys[:start] if start else keys

    async def next_available(exclude_id: str) -> _Key | None:
        """First key (in rotation order) that still has quota left."""
        for candidate in ordered:
            if candidate.key_id == exclude_id:
                continue
            raw = await kv.get(_count_key(candidate.key_id))
            if _first_int(raw) < limit:
                return candidate
        return None

    last_error: Exception | None = None
    for key in ordered:
        raw_used = await kv.get(_count_key(key.key_id))
        used = _first_int(raw_used)
        if used >= limit:
            continue  # already full — skip silently

        try:
            data = await _call_api(key.secret, path, handle)
        except CreatorCrawlNotFound:
            raise  # the handle is wrong, NOT the key — do not burn other keys
        except _KeyRejected as exc:
            logger.warning("Key %s (%s) rejected: %s", key.label, key.email, exc.reason)
            await kv.set(_count_key(key.key_id), str(limit))
            next_key = await next_available(key.key_id)
            await _notify_key_exhausted(key, exc.reason, next_key)
            if next_key is not None:
                await kv.set(_ACTIVE_KEY, next_key.key_id)
            last_error = exc
            continue
        except CreatorCrawlError as exc:
            logger.warning("CreatorCrawl call failed on key %s: %s", key.label, exc)
            last_error = exc
            continue  # transient — try the next key (possibly same one later)

        # Success — burn one credit.
        count = await kv.incr(_count_key(key.key_id))
        logger.info(
            "CreatorCrawl key %s (%s) → %d/%d used [%s]",
            key.label, key.email or "-", count, limit, path,
        )

        if count >= limit:
            # The key just hit its limit; switch + notify for the NEXT call.
            next_key = await next_available(key.key_id)
            if next_key is not None:
                await kv.set(_ACTIVE_KEY, next_key.key_id)
            await _notify_key_exhausted(key, "سهمیه تکمیل شد", next_key)

        return data

    if last_error is not None:
        raise CreatorCrawlNoKeys(
            f"همه کلیدهای CreatorCrawl پر شدن یا خطا دادند. آخرین خطا: {last_error}"
        )
    raise CreatorCrawlNoKeys(
        f"سهمیه همه {len(keys)} کلید CreatorCrawl تمام شده است. کلید جدید (با ایمیل اکانت جدید) به CREATORCRAWL_API_KEYS اضافه کن."
    )


async def get_user_posts(handle: str) -> CreatorCrawlProfile:
    """Fetch profile info + latest post, rotating keys transparently.

    Credit-efficient two-step flow (official normalized endpoints):

      1. ``/instagram/profile`` — avatar, follower/post counts, verified,
         private flag AND ``recent_posts`` when the upstream includes them
         → usually the whole card costs ONE credit.
      2. Only when step 1 yields no usable latest post (and the page is not
         private) does step 2 spend one more credit on ``/instagram/user/posts``.
    """
    username = _validate_handle(handle)
    profile_raw = await _request_via_rotation(PROFILE_PATH, username)
    try:
        profile = _parse_profile_response(profile_raw, username)
    except _KeyRejected as exc:
        # Quota markers can also hide INSIDE the data node (data.error).
        # Rotation has already returned, so surface it as a normal error
        # instead of leaking the private exception.
        logger.warning("Profile node-level key rejection for @%s: %s", username, exc.reason)
        raise CreatorCrawlError(f"کلید CreatorCrawl رد شد: {exc.reason}") from exc

    if profile.latest_post is None or not profile.latest_post.media:
        if profile.is_private:
            # Private page: posts are not public anyway — save the credit.
            logger.info("Profile @%s is private — skipping the user/posts call", username)
            return profile
        # Step 2 is best-effort: the profile card is already secured, so any
        # failure here only means "no download button", never an error card.
        try:
            posts_raw = await _request_via_rotation(USER_POSTS_PATH, username)
        except CreatorCrawlError as exc:
            logger.warning("user/posts fallback failed for @%s: %s", username, exc)
            return profile
        post, author = _parse_posts_response(posts_raw, username)
        if post is not None:
            profile = replace(profile, latest_post=post)
        if not profile.avatar_url and author:
            author_avatar = str(author.get("avatar_url") or "")
            author_name = unescape(str(author.get("name") or "")).strip()
            if author_avatar or author_name:
                profile = replace(
                    profile,
                    avatar_url=author_avatar or profile.avatar_url,
                    full_name=profile.full_name or author_name,
                )
    return profile
