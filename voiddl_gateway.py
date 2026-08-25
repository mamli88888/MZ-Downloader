"""VoidDLGateway — YouTube downloader via https://voiddl.app.

Per API key the service allows **20 downloads/minute** and **10 GB of
download bandwidth/day** (plus a separate 30 extracts/minute limit that
we simply share with the download budget for safety). To stay under
those caps the gateway rotates through a list of API keys configured
via ``VOIDDL_API_KEYS``:

  1. For each request, pick the next key whose daily bandwidth is not
     exhausted and whose per-minute bucket still has a token.
  2. On a 429 / rate-limit response the key is marked minute-exhausted
     and the next key is tried IMMEDIATELY (no waiting), so a limited
     key never blocks the user.
  3. Bandwidth usage is tracked per key per UTC day (synced with the
     server's ``x-bandwidth-used`` / ``x-bandwidth-remaining`` headers
     whenever they are present). A key whose 10 GB daily budget is
     spent is parked until the next UTC midnight and the next key
     takes over.
  4. If every key is exhausted, ``request()`` / ``select()`` return a
     ``GatewayResult`` with ``reason="voiddl_all_keys_exhausted"`` so
     the bot's routing layer can fall back to Yoinku → Apify →
     Telegram bots.

The thumbnail shown on the quality card is downloaded with the same
URL scheme used by the YouTube-Thumbnail-Downloader web app
(https://github.com/harsh98trivedi/YouTube-Thumbnail-Downloader):
``https://i.ytimg.com/vi/{id}/maxresdefault.jpg`` with fallbacks to
``sddefault`` → ``hqdefault`` → ``mqdefault``.

API contract with the bot
-------------------------
Exposes ``request()`` and ``select()`` async methods that return
``GatewayResult`` (defined in ``downloader.py``). They are direct
replacements for the Yoinku gateway and the bot treats them the same
way, branching on the ``VOIDDL_PROVIDER`` sentinel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from downloader import (
    DownloadedMedia,
    DownloadTooLarge,
    GatewayResult,
    InvalidDownload,
    MediaKind,
    ProgressCallback,
    QualityOption,
)
from routing import Platform

logger = logging.getLogger("MZDownloader.voiddl")


# Sentinel "provider" name — the bot's routing layer treats this like a
# Telegram bot username but the VoidDL-aware branches key off it.
VOIDDL_PROVIDER = "voiddl"

DEFAULT_API_BASE = "https://voiddl.app"
DEFAULT_DAILY_BANDWIDTH = 10 * 1024 * 1024 * 1024  # 10 GB per key per day
DEFAULT_PER_MINUTE_LIMIT = 20  # 20 downloads per key per minute

# Thumbnail candidates, best first (YouTube-Thumbnail-Downloader scheme).
# maxresdefault (1280x720+) does not exist for every video — YouTube then
# serves a tiny gray placeholder, which we detect via the byte size.
_THUMBNAIL_NAMES = ("maxresdefault", "sddefault", "hqdefault", "mqdefault")
# YouTube's "no thumbnail" placeholder is a ~1 KB gray image.
_THUMBNAIL_MIN_BYTES = 10 * 1024

# Port of the getYouTubeID() regex from the YouTube-Thumbnail-Downloader
# web app (thumbnail.js) — supports youtu.be, /watch?v=, /shorts/,
# /embed/ and /v/ URLs.
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:embed/|v/|shorts/|live/|u/\w/|watch\?v=|watch\?.+&v=))"
    r"([^#&?]{11})",
    re.IGNORECASE,
)
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def extract_youtube_video_id(url: str) -> str | None:
    """Return the 11-char YouTube video id from any common URL form."""
    match = _YOUTUBE_ID_RE.search(url or "")
    if match and len(match.group(1)) == 11:
        return match.group(1)
    return None


def normalize_youtube_url(url: str) -> str:
    """Normalize youtu.be / shorts / embed URLs to a watch?v= URL.

    Mirrors ``normalize_url()`` from the reference voiddl.py CLI: the
    API accepts most YouTube URL shapes, but a canonical watch URL is
    the safest form to send.
    """
    value = (url or "").strip()
    video_id = extract_youtube_video_id(value)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return value


def _safe_filename(name: str, *, max_len: int = 100) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip().rstrip(".")
    return cleaned[:max_len] or "video"


def _fmt_size(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _mime_for(suffix: str, *, kind: MediaKind) -> str:
    s = suffix.lower()
    if kind == MediaKind.AUDIO:
        if s == ".m4a":
            return "audio/mp4"
        if s == ".opus":
            return "audio/ogg"
        if s == ".webm":
            return "audio/webm"
        return "audio/mpeg"
    if s == ".webm":
        return "video/webm"
    return "video/mp4"


def _fingerprint(payload: dict[str, Any]) -> str:
    return "voiddl:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("voiddl:"):
        return None
    try:
        return json.loads(fingerprint[len("voiddl:"):])
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────────────────────────────
# Key pool — picks the next usable API key under the daily-bandwidth +
# per-minute caps. Counters are in-memory (Railway disk is ephemeral and
# the bot process is the only writer); on restart they reset, which is
# acceptable because the server-side counters are authoritative and are
# re-synced from response headers on every call.
# ─────────────────────────────────────────────────────────────────────


class _KeyState:
    __slots__ = ("key", "daily_bytes", "daily_date", "minute_tokens", "minute_refill_at")

    def __init__(self, key: str) -> None:
        self.key = key
        self.daily_bytes = 0
        self.daily_date: str = ""  # YYYY-MM-DD (UTC)
        self.minute_tokens: float = 0.0
        self.minute_refill_at = 0.0


class VoidDLKeyPool:
    """Round-robin key pool with per-key daily-bandwidth + per-minute limits."""

    def __init__(
        self,
        keys: tuple[str, ...],
        *,
        daily_bandwidth: int = DEFAULT_DAILY_BANDWIDTH,
        per_minute_limit: int = DEFAULT_PER_MINUTE_LIMIT,
    ) -> None:
        if not keys:
            raise ValueError("VoidDLKeyPool requires at least one API key")
        self._keys = tuple(_KeyState(k) for k in dict.fromkeys(keys))
        self._daily_bandwidth = max(1, int(daily_bandwidth))
        self._per_minute_limit = max(1, per_minute_limit)
        # Pre-fill the minute bucket to capacity so the first burst works,
        # and only refill AFTER a full interval has elapsed.
        now = time.monotonic()
        interval = 60.0 / self._per_minute_limit
        for state in self._keys:
            state.minute_tokens = float(self._per_minute_limit)
            state.minute_refill_at = now + interval
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def total(self) -> int:
        return len(self._keys)

    @property
    def daily_bandwidth(self) -> int:
        return self._daily_bandwidth

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _refill_minute(self, state: _KeyState, now: float) -> None:
        interval = 60.0 / self._per_minute_limit
        while state.minute_tokens < self._per_minute_limit and now >= state.minute_refill_at:
            state.minute_tokens = min(
                self._per_minute_limit,
                state.minute_tokens + 1,
            )
            state.minute_refill_at += interval

    def _rollover_daily(self, state: _KeyState, today: str) -> None:
        if state.daily_date != today:
            state.daily_date = today
            state.daily_bytes = 0

    async def acquire(self) -> str | None:
        """Return the next usable key, or None if all keys are exhausted."""
        async with self._lock:
            now = time.monotonic()
            today = self._today_utc()
            tried = 0
            n = len(self._keys)
            while tried < n:
                state = self._keys[self._cursor % n]
                self._cursor = (self._cursor + 1) % n
                tried += 1
                self._rollover_daily(state, today)
                self._refill_minute(state, now)
                if state.daily_bytes >= self._daily_bandwidth:
                    continue
                if state.minute_tokens < 1.0:
                    continue
                state.minute_tokens -= 1.0
                return state.key
            return None

    async def record_bytes(self, key: str, byte_count: int) -> None:
        """Account downloaded bytes against the key's daily bandwidth."""
        if byte_count <= 0:
            return
        async with self._lock:
            today = self._today_utc()
            for state in self._keys:
                if state.key == key:
                    self._rollover_daily(state, today)
                    state.daily_bytes += int(byte_count)
                    break

    async def mark_minute_exhausted(self, key: str) -> None:
        """Mark a key as minute-rate-limited (e.g. on a 429 response)."""
        async with self._lock:
            for state in self._keys:
                if state.key == key:
                    state.minute_tokens = 0.0
                    state.minute_refill_at = time.monotonic() + 60.0
                    break

    async def mark_daily_exhausted(self, key: str) -> None:
        """Mark a key as bandwidth-exhausted for the rest of the UTC day."""
        async with self._lock:
            today = self._today_utc()
            for state in self._keys:
                if state.key == key:
                    self._rollover_daily(state, today)
                    state.daily_bytes = self._daily_bandwidth
                    break

    async def sync_from_headers(self, key: str, headers: httpx.Headers | dict[str, str]) -> None:
        """Adopt the server's authoritative rate/bandwidth counters.

        VoidDL reports, on every download response:
        ``x-ratelimit-remaining``, ``x-bandwidth-used`` and
        ``x-bandwidth-remaining`` (+ ``x-bandwidth-period`` = UTC date).
        Using them keeps our local counters honest across restarts.
        """
        get = headers.get if isinstance(headers, httpx.Headers) else (lambda name, default=None: headers.get(name, default))

        async def _apply() -> None:
            today = self._today_utc()
            for state in self._keys:
                if state.key != key:
                    continue
                period = (get("x-bandwidth-period") or "").strip()
                if period and period != state.daily_date:
                    # New server-side period → reset the local counter.
                    state.daily_date = period
                    state.daily_bytes = 0
                else:
                    self._rollover_daily(state, today)
                used = get("x-bandwidth-used")
                remaining = get("x-bandwidth-remaining")
                if used is not None and used.strip().isdigit():
                    state.daily_bytes = int(used.strip())
                elif remaining is not None and remaining.strip().isdigit():
                    state.daily_bytes = max(0, self._daily_bandwidth - int(remaining.strip()))
                minute_remaining = get("x-ratelimit-remaining")
                if minute_remaining is not None and minute_remaining.strip().isdigit():
                    state.minute_tokens = min(
                        float(self._per_minute_limit),
                        float(int(minute_remaining.strip())),
                    )
                break

        async with self._lock:
            await _apply()

    async def status(self) -> list[dict[str, Any]]:
        """Snapshot of per-key state — used by the /health dashboard."""
        async with self._lock:
            now = time.monotonic()
            today = self._today_utc()
            snapshot: list[dict[str, Any]] = []
            for state in self._keys:
                self._rollover_daily(state, today)
                self._refill_minute(state, now)
                snapshot.append({
                    "key": state.key[:8] + "…",
                    "daily_bytes_used": state.daily_bytes,
                    "daily_bandwidth": self._daily_bandwidth,
                    "minute_remaining": int(state.minute_tokens),
                    "minute_limit": self._per_minute_limit,
                })
            return snapshot


# ─────────────────────────────────────────────────────────────────────
# Gateway
# ─────────────────────────────────────────────────────────────────────


class VoidDLGateway:
    """YouTube downloader via https://voiddl.app with multi-key rotation."""

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_API_BASE,
        api_keys: tuple[str, ...] = (),
        daily_bandwidth: int = DEFAULT_DAILY_BANDWIDTH,
        per_minute_limit: int = DEFAULT_PER_MINUTE_LIMIT,
        proxy_url: str | None = None,
        max_download_size: int = 0,
        request_timeout: float = 60.0,
        download_read_timeout: float = 600.0,
    ) -> None:
        if not api_keys:
            raise ValueError("VoidDLGateway requires at least one API key (VOIDDL_API_KEYS)")
        self._api_base = api_base.rstrip("/")
        self._pool = VoidDLKeyPool(
            api_keys,
            daily_bandwidth=daily_bandwidth,
            per_minute_limit=per_minute_limit,
        )
        self._proxy_url = proxy_url
        self._max_download_size = max_download_size
        self._request_timeout = request_timeout
        self._download_read_timeout = download_read_timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def pool(self) -> VoidDLKeyPool:
        return self._pool

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(
                    connect=30.0,
                    read=self._download_read_timeout,
                    write=60.0,
                    pool=60.0,
                ),
                "follow_redirects": True,
            }
            if self._proxy_url:
                kwargs["proxy"] = self._proxy_url
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request(
        self,
        *,
        url: str,
        platform: Platform,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> GatewayResult:
        """Call ``POST /api/extract`` and return a quality menu."""
        if platform != Platform.YOUTUBE:
            return GatewayResult(
                status="error",
                bot_username=VOIDDL_PROVIDER,
                reason="unsupported_platform",
            )
        normalized = normalize_youtube_url(url)
        data, used_key = await self._call_with_rotation(
            lambda client, key: self._fetch_extract(client, key, normalized),
        )
        if data is None:
            return GatewayResult(
                status="error",
                bot_username=VOIDDL_PROVIDER,
                reason="voiddl_all_keys_exhausted" if used_key is None else "voiddl_extract_error",
            )
        if not isinstance(data, dict) or data.get("status") != "success":
            return GatewayResult(
                status="error",
                bot_username=VOIDDL_PROVIDER,
                reason="voiddl_extract_failed",
            )
        preview = await self._download_thumbnail(normalized, attempt_directory)
        return self._build_menu(url, data, preview)

    async def select(
        self,
        *,
        url: str,
        platform: Platform,
        option: QualityOption,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
        processing_callback: Callable[..., Awaitable[None]] | None = None,
    ) -> GatewayResult:
        """Call ``GET /api/v1/download`` for the chosen format and stream it."""
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None or not payload.get("format_id"):
            return GatewayResult(
                status="error",
                bot_username=VOIDDL_PROVIDER,
                reason="invalid_fingerprint",
            )
        format_id = str(payload["format_id"])
        expected_kind = MediaKind.AUDIO if payload.get("kind") == "audio" else MediaKind.VIDEO
        ext = "." + str(payload.get("ext") or ("m4a" if expected_kind == MediaKind.AUDIO else "mp4")).lstrip(".")
        title = str(payload.get("title") or "video")
        height = payload.get("height")
        normalized = normalize_youtube_url(url)

        quality_tag = "audio" if expected_kind == MediaKind.AUDIO else f"{height or ''}p"
        stem = _safe_filename(f"{title} [{quality_tag}]".strip())
        final_path = attempt_directory / f"{stem}{ext}"

        result = await self._download_with_rotation(
            normalized, format_id, expected_kind, final_path, progress_callback
        )
        if result is not None:
            return result
        return GatewayResult(
            status="error",
            bot_username=VOIDDL_PROVIDER,
            reason="voiddl_download_failed",
        )

    # ------------------------------------------------------------------
    # Rotation drivers
    # ------------------------------------------------------------------

    async def _call_with_rotation(
        self,
        call: Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response | None]],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Call ``call(client, key)`` rotating through keys until one succeeds.

        Returns ``(parsed_json, used_key)``; ``(None, None)`` when every key
        is exhausted or errored — the caller then falls back down the chain.
        """
        client = await self._ensure_client()
        attempted_keys: set[str] = set()
        for _ in range(self._pool.total):
            key = await self._pool.acquire()
            if key is None:
                logger.info("VoidDL: all keys exhausted (bandwidth or minute cap)")
                return None, None
            if key in attempted_keys:
                logger.info("VoidDL: no fresh key available (all minute-rate-limited)")
                return None, None
            attempted_keys.add(key)
            response = await call(client, key)
            if response is None:
                # Network error — try the next key.
                continue
            if response.status_code == 429:
                await self._pool.mark_minute_exhausted(key)
                logger.info("VoidDL key %s… hit the per-minute limit", key[:8])
                continue
            if response.status_code in {402, 403}:
                # 402/403 — quota/billing problems: park the key for the day.
                await self._pool.mark_daily_exhausted(key)
                logger.info("VoidDL key %s… rejected (HTTP %s)", key[:8], response.status_code)
                continue
            try:
                data = response.json()
            except ValueError:
                logger.info("VoidDL key %s… returned non-JSON (HTTP %s)", key[:8], response.status_code)
                continue
            if isinstance(data, dict) and data.get("status") == "success":
                return data, key
            message = str(data.get("error") or data.get("message") or "") if isinstance(data, dict) else ""
            lowered = message.lower()
            if any(word in lowered for word in ("rate", "limit", "quota", "too many", "exceeded")):
                if any(word in lowered for word in ("daily", "day", "bandwidth", "24h")):
                    await self._pool.mark_daily_exhausted(key)
                    logger.info("VoidDL key %s… hit the daily bandwidth cap", key[:8])
                else:
                    await self._pool.mark_minute_exhausted(key)
                    logger.info("VoidDL key %s… rate-limited (body)", key[:8])
                continue
            logger.info("VoidDL key %s… returned an error: %s", key[:8], message[:200])
            continue
        return None, None

    async def _download_with_rotation(
        self,
        url: str,
        format_id: str,
        expected_kind: MediaKind,
        destination: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult | None:
        """Stream ``GET /api/v1/download`` rotating keys on pre-stream errors.

        Once the byte stream has started the response belongs to a single
        key; a mid-stream failure bubbles up so the bot's fallback chain
        (Yoinku → Apify → Telegram bots) can take over.
        """
        client = await self._ensure_client()
        attempted_keys: set[str] = set()
        for _ in range(self._pool.total):
            key = await self._pool.acquire()
            if key is None:
                logger.info("VoidDL: all keys exhausted before download could start")
                return GatewayResult(
                    status="error",
                    bot_username=VOIDDL_PROVIDER,
                    reason="voiddl_all_keys_exhausted",
                )
            if key in attempted_keys:
                return GatewayResult(
                    status="error",
                    bot_username=VOIDDL_PROVIDER,
                    reason="voiddl_all_keys_exhausted",
                )
            attempted_keys.add(key)
            try:
                async with client.stream(
                    "GET",
                    f"{self._api_base}/api/v1/download",
                    headers={"Authorization": f"Bearer {key}"},
                    params={"url": url, "format_id": format_id},
                ) as response:
                    if response.status_code == 429:
                        await self._pool.mark_minute_exhausted(key)
                        logger.info("VoidDL key %s… hit the per-minute limit (download)", key[:8])
                        continue
                    if response.status_code in {402, 403}:
                        await self._pool.mark_daily_exhausted(key)
                        logger.info("VoidDL key %s… rejected on download (HTTP %s)", key[:8], response.status_code)
                        continue
                    if response.status_code >= 400:
                        # Server-side extraction error for THIS video — no
                        # point burning another key; bubble the failure up.
                        logger.warning(
                            "VoidDL download failed with HTTP %s for format %s",
                            response.status_code, format_id,
                        )
                        await response.aread()
                        return GatewayResult(
                            status="error",
                            bot_username=VOIDDL_PROVIDER,
                            reason="voiddl_http_error",
                        )
                    content_type = (response.headers.get("content-type") or "").lower()
                    if "application/json" in content_type:
                        # A JSON error disguised as a 200 — read it and fall
                        # through to the next key (it may be key-specific).
                        body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                        logger.info("VoidDL returned JSON instead of media: %s", body)
                        continue
                    # Success — account for the server-side counters first.
                    await self._pool.sync_from_headers(key, response.headers)
                    try:
                        byte_count = await self._write_stream(
                            response, destination, progress_callback
                        )
                    except DownloadTooLarge:
                        return GatewayResult(
                            status="error",
                            bot_username=VOIDDL_PROVIDER,
                            reason="too_large",
                        )
                    await self._pool.record_bytes(key, byte_count)
                    media = DownloadedMedia(
                        path=destination,
                        kind=expected_kind,
                        source_message_id=0,
                        mime_type=_mime_for(destination.suffix, kind=expected_kind),
                        size=destination.stat().st_size,
                    )
                    return GatewayResult(
                        status="ready",
                        bot_username=VOIDDL_PROVIDER,
                        media=(media,),
                    )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                logger.warning("VoidDL download stream failed: %s", exc)
                return GatewayResult(
                    status="error",
                    bot_username=VOIDDL_PROVIDER,
                    reason="voiddl_stream_error",
                )
        return GatewayResult(
            status="error",
            bot_username=VOIDDL_PROVIDER,
            reason="voiddl_all_keys_exhausted",
        )

    # ------------------------------------------------------------------
    # HTTP calls
    # ------------------------------------------------------------------

    async def _fetch_extract(
        self,
        client: httpx.AsyncClient,
        key: str,
        url: str,
    ) -> httpx.Response | None:
        try:
            return await client.post(
                f"{self._api_base}/api/extract",
                headers={"Authorization": f"Bearer {key}"},
                json={"url": url},
            )
        except httpx.HTTPError as exc:
            logger.warning("VoidDL /api/extract HTTP error for %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Response → menu
    # ------------------------------------------------------------------

    def _build_menu(
        self,
        url: str,
        data: dict[str, Any],
        preview: DownloadedMedia | None,
    ) -> GatewayResult:
        title = (data.get("title") or "").strip()
        duration = data.get("duration")
        formats = data.get("formats") or []

        video_formats: dict[int, dict[str, Any]] = {}
        audio_format: dict[str, Any] | None = None
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            format_id = str(fmt.get("format_id") or "")
            # Compound IDs (e.g. "137+140") are not served merged by the
            # API — skip them in favour of the single-format entries.
            if not format_id or "+" in format_id:
                continue
            fmt_type = str(fmt.get("type") or "").lower()
            if fmt_type == "audio" or (fmt.get("acodec") and not fmt.get("vcodec")):
                if audio_format is None or self._audio_rank(fmt) > self._audio_rank(audio_format):
                    audio_format = fmt
                continue
            height = fmt.get("height")
            try:
                height = int(height) if height is not None else 0
            except (TypeError, ValueError):
                continue
            if height <= 0:
                continue
            existing = video_formats.get(height)
            if existing is None or self._video_rank(fmt) > self._video_rank(existing):
                video_formats[height] = fmt

        options: list[QualityOption] = []
        size_lines: list[str] = []
        # Best quality first.
        for height in sorted(video_formats, reverse=True):
            fmt = video_formats[height]
            filesize = fmt.get("filesize") or 0
            try:
                filesize = int(filesize)
            except (TypeError, ValueError):
                filesize = 0
            options.append(
                QualityOption(
                    # Quality buttons carry ONLY the quality — sizes and
                    # other details live in the card caption (text below).
                    label=str(height),
                    row=0,
                    column=0,
                    fingerprint=_fingerprint({
                        "url": url,
                        "format_id": str(fmt.get("format_id")),
                        "kind": "video",
                        "ext": fmt.get("ext") or "mp4",
                        "height": height,
                        "filesize": filesize,
                        "title": title[:80],
                    }),
                    expected_kind=MediaKind.VIDEO,
                    expected_height=height,
                )
            )
            size_text = self._size_text(fmt, filesize)
            size_lines.append(f"• {height} — {size_text}" if size_text else f"• {height}")

        if audio_format is not None:
            audio_size = audio_format.get("filesize") or 0
            try:
                audio_size = int(audio_size)
            except (TypeError, ValueError):
                audio_size = 0
            options.append(
                QualityOption(
                    label="MP3",
                    row=0,
                    column=0,
                    fingerprint=_fingerprint({
                        "url": url,
                        "format_id": str(audio_format.get("format_id")),
                        "kind": "audio",
                        "ext": audio_format.get("ext") or "m4a",
                        "height": None,
                        "filesize": audio_size,
                        "title": title[:80],
                    }),
                    expected_kind=MediaKind.AUDIO,
                    expected_bitrate_kbps=128,
                )
            )
            audio_size_text = self._size_text(audio_format, audio_size)
            size_lines.append(f"• MP3 — {audio_size_text}" if audio_size_text else "• MP3")

        if not options:
            return GatewayResult(
                status="error",
                bot_username=VOIDDL_PROVIDER,
                reason="voiddl_no_formats",
            )

        caption_parts: list[str] = []
        if title:
            caption_parts.append(html_escape(title)[:200])
        duration_text = _fmt_duration(duration)
        if duration_text:
            caption_parts.append(f"⏱ مدت: {duration_text}")
        if size_lines:
            caption_parts.append("📦 حجم هر کیفیت:\n" + "\n".join(size_lines))
        return GatewayResult(
            status="needs_selection",
            bot_username=VOIDDL_PROVIDER,
            options=tuple(options),
            preview=preview,
            text="\n\n".join(caption_parts),
        )

    @staticmethod
    def _size_text(fmt: dict[str, Any], filesize: int) -> str:
        """Human size for the caption; "" when the API has no idea."""
        human = str(fmt.get("filesize_human") or "").strip()
        if human and human.lower() not in {"unknown", "n/a", "none", "null"}:
            return human
        if filesize:
            return _fmt_size(filesize)
        return ""

    @staticmethod
    def _video_rank(fmt: dict[str, Any]) -> tuple[int, int]:
        """Prefer mp4 (best Telegram compatibility), then larger files."""
        ext = str(fmt.get("ext") or "").lower()
        size = fmt.get("filesize") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        return (1 if ext == "mp4" else 0, size)

    @staticmethod
    def _audio_rank(fmt: dict[str, Any]) -> tuple[int, int]:
        """Prefer m4a/mp4 audio, then larger (higher-bitrate) files."""
        ext = str(fmt.get("ext") or "").lower()
        size = fmt.get("filesize") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        return (1 if ext in {"m4a", "mp4"} else 0, size)

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    async def _write_stream(
        self,
        response: httpx.Response,
        destination: Path,
        progress_callback: ProgressCallback | None,
    ) -> int:
        content_length = response.headers.get("content-length")
        total = int(content_length) if content_length and content_length.isdigit() else 0
        if self._max_download_size and total and total > self._max_download_size:
            raise DownloadTooLarge(f"file exceeds max download size ({total} bytes)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        bytes_written = 0
        with destination.open("wb") as handle:
            async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                bytes_written += len(chunk)
                if self._max_download_size and bytes_written > self._max_download_size:
                    raise DownloadTooLarge("streamed size exceeds max download size")
                if progress_callback is not None:
                    try:
                        await progress_callback(bytes_written, total or bytes_written)
                    except Exception:  # pragma: no cover
                        logger.exception("progress_callback failed")
        if destination.stat().st_size == 0:
            raise InvalidDownload("downloaded file is empty")
        return bytes_written

    async def _download_thumbnail(
        self,
        url: str,
        attempt_directory: Path,
    ) -> DownloadedMedia | None:
        """Download the best-quality YouTube thumbnail (best-effort).

        Uses the YouTube-Thumbnail-Downloader URL scheme:
        maxresdefault → sddefault → hqdefault → mqdefault. A candidate
        smaller than 10 KB is YouTube's gray placeholder → try the next.
        """
        video_id = extract_youtube_video_id(url)
        if not video_id:
            return None
        client = await self._ensure_client()
        for name in _THUMBNAIL_NAMES:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"
            try:
                response = await client.get(thumb_url, timeout=15.0)
                if response.status_code != 200:
                    continue
                payload = response.content
                if len(payload) < _THUMBNAIL_MIN_BYTES:
                    # Placeholder image — the real thumbnail doesn't exist.
                    continue
                attempt_directory.mkdir(parents=True, exist_ok=True)
                path = attempt_directory / "thumbnail.jpg"
                path.write_bytes(payload)
                return DownloadedMedia(
                    path=path,
                    kind=MediaKind.PHOTO,
                    source_message_id=0,
                    mime_type="image/jpeg",
                    size=len(payload),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — thumbnail is best-effort
                logger.debug("VoidDL thumbnail candidate %s failed: %s", name, exc)
                continue
        return None


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def voiddl_health_check(gateway: VoidDLGateway | None) -> str:
    if gateway is None:
        return "disabled"
    return (
        f"ready ({gateway.pool.total} key(s), "
        f"{gateway.pool.daily_bandwidth // (1024 * 1024 * 1024)}GB/day per key)"
    )


# ─────────────────────────────────────────────────────────────────────
# Shared helper — used by the OTHER YouTube fallback paths too
# ─────────────────────────────────────────────────────────────────────


async def download_youtube_thumbnail(
    url: str,
    attempt_directory: Path,
    *,
    proxy_url: str | None = None,
) -> DownloadedMedia | None:
    """Standalone best-quality YouTube thumbnail downloader.

    Shared by the Yoinku / Apify / Telegram-bot fallback menus so EVERY
    YouTube quality card shows the thumbnail with the buttons attached,
    exactly like the primary VoidDL path. Best-effort: returns None on
    any failure (the caller then shows a plain text menu).
    """
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return None
    kwargs: dict[str, Any] = {
        "timeout": 15.0,
        "follow_redirects": True,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**kwargs) as client:
        for name in _THUMBNAIL_NAMES:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"
            try:
                response = await client.get(thumb_url)
                if response.status_code != 200:
                    continue
                payload = response.content
                if len(payload) < _THUMBNAIL_MIN_BYTES:
                    continue
                attempt_directory.mkdir(parents=True, exist_ok=True)
                path = attempt_directory / "thumbnail.jpg"
                path.write_bytes(payload)
                return DownloadedMedia(
                    path=path,
                    kind=MediaKind.PHOTO,
                    source_message_id=0,
                    mime_type="image/jpeg",
                    size=len(payload),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("YouTube thumbnail candidate %s failed: %s", name, exc)
                continue
    return None
