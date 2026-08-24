"""YoinkuGateway — YouTube downloader via https://yoinku.com/api/v1.

Per API key the service allows **30 requests/day** and **5 requests/minute**
(sliding window). To stay under those caps the gateway rotates through a
list of API keys configured via ``YOINKU_API_KEYS``:

  1. For each request, pick the first key whose daily count is < 30 and
     whose per-minute bucket still has a token.
  2. Decrement both counters. On a 429 (or a server-reported rate-limit
     error), mark the key as minute-exhausted for 60 seconds, or as
     daily-exhausted until the next UTC midnight, and immediately retry
     with the next key.
  3. If every key is exhausted, ``request()`` returns a GatewayResult
     with ``reason="yoinku_all_keys_exhausted"`` so the bot's routing
     layer can fall back to Apify / Telegram bots.

API contract with the bot
-------------------------
Exposes ``request()`` and ``select()`` async methods that return
``GatewayResult`` (defined in ``downloader.py``). They are direct
replacements for the YouTube-sites gateway and the bot treats them the
same way, branching on the ``YOINKU_PROVIDER`` sentinel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import urllib.parse
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

logger = logging.getLogger("MZDownloader.yoinku")


# Sentinel "provider" name — the bot's routing layer treats this like a
# Telegram bot username but the Yoinku-aware branches key off it.
YOINKU_PROVIDER = "yoinku"

DEFAULT_API_BASE = "https://yoinku.com/api/v1"
DEFAULT_DAILY_LIMIT = 30
DEFAULT_PER_MINUTE_LIMIT = 5

# A "processing" callback is invoked during the (rare) wait-for-CDN-URL
# phase. Same contract as in youtube_sites_gateway / ahm7_gateway.
ProcessingCallback = Callable[[int, str, str], Awaitable[None]]

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ─────────────────────────────────────────────────────────────────────
# Key pool — picks the next usable API key under the daily + per-minute
# caps. Counters are in-memory (Railway disk is ephemeral and the bot
# process is the only writer); on restart all counters reset, which is
# acceptable because the daily quota also resets server-side on UTC
# midnight and Railway redeploys are infrequent.
# ─────────────────────────────────────────────────────────────────────


class _KeyState:
    __slots__ = ("key", "daily_count", "daily_date", "minute_tokens", "minute_refill_at")

    def __init__(self, key: str) -> None:
        self.key = key
        self.daily_count = 0
        self.daily_date: str = ""  # YYYY-MM-DD (UTC)
        self.minute_tokens: float = 0.0
        self.minute_refill_at: float = 0.0


class YoinkuKeyPool:
    """Round-robin key pool with per-key daily + per-minute rate limiting."""

    def __init__(
        self,
        keys: tuple[str, ...],
        *,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        per_minute_limit: int = DEFAULT_PER_MINUTE_LIMIT,
    ) -> None:
        if not keys:
            raise ValueError("YoinkuKeyPool requires at least one API key")
        self._keys = tuple(_KeyState(k) for k in dict.fromkeys(keys))
        self._daily_limit = max(1, daily_limit)
        self._per_minute_limit = max(1, per_minute_limit)
        # Pre-fill the minute bucket to capacity so the first burst works,
        # and set ``minute_refill_at`` to (now + interval) so the FIRST
        # refill only happens after ``interval`` seconds — without this,
        # the very first call to ``_refill_minute`` would immediately
        # refill a consumed token (since ``now >= minute_refill_at=0`` is
        # always True), defeating the per-minute cap.
        now = time.monotonic()
        interval = 60.0 / self._per_minute_limit
        for state in self._keys:
            state.minute_tokens = float(self._per_minute_limit)
            state.minute_refill_at = now + interval
        # Cursor for round-robin selection. Guarded by the lock.
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def total(self) -> int:
        return len(self._keys)

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _refill_minute(self, state: _KeyState, now: float) -> None:
        # Token bucket: 1 token per (60 / per_minute_limit) seconds.
        interval = 60.0 / self._per_minute_limit
        while state.minute_tokens < self._per_minute_limit and now >= state.minute_refill_at:
            state.minute_tokens = min(
                self._per_minute_limit,
                state.minute_tokens + 1,
            )
            state.minute_refill_at += interval

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
                # Reset daily count on UTC midnight rollover.
                if state.daily_date != today:
                    state.daily_date = today
                    state.daily_count = 0
                self._refill_minute(state, now)
                if state.daily_count >= self._daily_limit:
                    continue
                if state.minute_tokens < 1.0:
                    continue
                state.minute_tokens -= 1.0
                state.daily_count += 1
                return state.key
            return None

    async def mark_minute_exhausted(self, key: str) -> None:
        """Mark a key as minute-rate-limited (e.g. on a 429 response)."""
        async with self._lock:
            for state in self._keys:
                if state.key == key:
                    state.minute_tokens = 0.0
                    state.minute_refill_at = time.monotonic() + 60.0
                    break

    async def mark_daily_exhausted(self, key: str) -> None:
        """Mark a key as daily-exhausted (e.g. server says daily quota hit)."""
        async with self._lock:
            for state in self._keys:
                if state.key == key:
                    state.daily_count = self._daily_limit
                    break

    async def status(self) -> list[dict[str, Any]]:
        """Snapshot of per-key state — used by the /tokens dashboard."""
        async with self._lock:
            now = time.monotonic()
            today = self._today_utc()
            snapshot: list[dict[str, Any]] = []
            for state in self._keys:
                if state.daily_date != today:
                    state.daily_date = today
                    state.daily_count = 0
                self._refill_minute(state, now)
                snapshot.append({
                    "key": state.key[:8] + "…",
                    "daily_used": state.daily_count,
                    "daily_limit": self._daily_limit,
                    "minute_remaining": int(state.minute_tokens),
                    "minute_limit": self._per_minute_limit,
                })
            return snapshot


# ─────────────────────────────────────────────────────────────────────
# Fingerprint helpers (mirrors the other gateways)
# ─────────────────────────────────────────────────────────────────────


def _fingerprint(payload: dict[str, Any]) -> str:
    return "yoinku:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("yoinku:"):
        return None
    try:
        return json.loads(fingerprint[len("yoinku:"):])
    except json.JSONDecodeError:
        return None


def _safe_filename(name: str, *, max_len: int = 100) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip().rstrip(".")
    return cleaned[:max_len] or "video"


def _mime_for(suffix: str, *, kind: MediaKind) -> str:
    s = suffix.lower()
    if kind == MediaKind.AUDIO:
        if s == ".m4a":
            return "audio/mp4"
        if s == ".opus":
            return "audio/ogg"
        return "audio/mpeg"
    return "video/mp4"


def _build_media(path: Path, kind: MediaKind) -> DownloadedMedia:
    return DownloadedMedia(
        path=path,
        kind=kind,
        source_message_id=0,
        mime_type=_mime_for(path.suffix, kind=kind),
        size=path.stat().st_size,
    )


def _is_rate_limited(response: httpx.Response) -> bool:
    """Detect a Yoinku rate-limit response (HTTP 429 or `ok: false` w/ quota)."""
    if response.status_code == 429:
        return True
    # Yoinku returns ok:false with a message on rate-limit too.
    if response.status_code != 200:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("ok"):
        return False
    message = str(data.get("message") or "").lower()
    return any(
        keyword in message
        for keyword in ("rate", "limit", "quota", "too many", "exceeded")
    )


def _is_daily_limited(response: httpx.Response) -> bool:
    """Heuristic: distinguish a daily-quota error from a per-minute one."""
    if response.status_code != 200:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    message = str(data.get("message") or "").lower() if isinstance(data, dict) else ""
    return any(
        keyword in message
        for keyword in ("daily", "day", "24h", "per day")
    )


# ─────────────────────────────────────────────────────────────────────
# Gateway
# ─────────────────────────────────────────────────────────────────────


class YoinkuGateway:
    """YouTube downloader via https://yoinku.com/api/v1 with multi-key rotation."""

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_API_BASE,
        api_keys: tuple[str, ...] = (),
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        per_minute_limit: int = DEFAULT_PER_MINUTE_LIMIT,
        proxy_url: str | None = None,
        max_download_size: int = 0,
        request_timeout: float = 60.0,
    ) -> None:
        if not api_keys:
            raise ValueError("YoinkuGateway requires at least one API key (YOINKU_API_KEYS)")
        self._api_base = api_base.rstrip("/")
        self._pool = YoinkuKeyPool(
            api_keys,
            daily_limit=daily_limit,
            per_minute_limit=per_minute_limit,
        )
        self._proxy_url = proxy_url
        self._max_download_size = max_download_size
        self._request_timeout = request_timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def pool(self) -> YoinkuKeyPool:
        return self._pool

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": self._request_timeout,
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
        """Call ``/info`` and return a quality menu."""
        if platform != Platform.YOUTUBE:
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="unsupported_platform",
            )
        data, used_key = await self._call_with_rotation(
            lambda client, key: self._fetch_info(client, key, url),
        )
        if data is None:
            # All keys exhausted / errored → tell the bot to fall back.
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="yoinku_all_keys_exhausted" if used_key is None else "yoinku_info_error",
            )
        # /info nests the payload under `data`; /download returns fields at
        # the top level. We normalise to the inner payload here.
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(inner, dict):
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="yoinku_info_error",
            )
        return self._build_menu(url, inner, used_key or "")

    async def select(
        self,
        *,
        url: str,
        platform: Platform,
        option: QualityOption,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
        processing_callback: ProcessingCallback | None = None,
    ) -> GatewayResult:
        """Call ``/download`` for the chosen format and stream the CDN file."""
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None:
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="invalid_fingerprint",
            )
        format_id = payload.get("format_id")
        kind_label = payload.get("kind") or "video"
        if not format_id:
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="invalid_fingerprint",
            )
        expected_kind = (
            MediaKind.AUDIO if kind_label == "audio" else MediaKind.VIDEO
        )
        result, used_key = await self._call_with_rotation(
            lambda client, key: self._fetch_download(client, key, url, format_id),
        )
        if result is None:
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="yoinku_all_keys_exhausted" if used_key is None else "yoinku_download_error",
            )
        download_url = (result.get("url") or "").strip()
        filename = (result.get("filename") or "").strip()
        if not download_url:
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="yoinku_no_download_url",
            )
        suffix = self._suffix_for_filename(filename, expected_kind)
        final_path = attempt_directory / f"yoinku_{kind_label}{suffix}"
        try:
            await self._download_file(download_url, final_path, progress_callback)
        except DownloadTooLarge:
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="too_large",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Yoinku CDN download failed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="yoinku_cdn_error",
            )
        media = _build_media(final_path, expected_kind)
        return GatewayResult(
            status="ready",
            bot_username=YOINKU_PROVIDER,
            media=(media,),
        )

    # ------------------------------------------------------------------
    # Rotation driver
    # ------------------------------------------------------------------

    async def _call_with_rotation(
        self,
        call: Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response | None]],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Call ``call(client, key)`` rotating through keys until one succeeds.

        Returns ``(parsed_data, used_key)``. If all keys fail with rate
        limits / exhaustion, returns ``(None, None)`` to signal "no key
        available — fall back".
        """
        client = await self._ensure_client()
        attempted_keys: set[str] = set()
        # Try every key at most once per call (so a 429 doesn't loop forever
        # on a single key). The pool's daily counter is already incremented
        # in acquire(); a rate-limited retry should consume the next key.
        for _ in range(self._pool.total):
            key = await self._pool.acquire()
            if key is None:
                logger.info("Yoinku: all keys exhausted (daily or minute cap)")
                return None, None
            if key in attempted_keys:
                # Pool returned a key we've already tried in this call —
                # means every key is minute-rate-limited right now. Give up
                # so the bot can fall back to Apify / Telegram bots instead
                # of waiting in a busy loop.
                logger.info("Yoinku: no fresh key available (all minute-rate-limited)")
                return None, None
            attempted_keys.add(key)
            response = await call(client, key)
            if response is None:
                # Network error / non-JSON — try the next key.
                continue
            if _is_rate_limited(response):
                if _is_daily_limited(response):
                    await self._pool.mark_daily_exhausted(key)
                    logger.info("Yoinku key %s… hit daily limit", key[:8])
                else:
                    await self._pool.mark_minute_exhausted(key)
                    logger.info("Yoinku key %s… hit minute limit", key[:8])
                continue
            try:
                data = response.json()
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("ok"):
                # Return the FULL response dict. /info nests the actual
                # payload under `data` (the caller extracts it); /download
                # returns fields (``url``, ``filename``, ``expiresInSeconds``)
                # at the TOP level (no nesting) — so the caller for /download
                # can read them directly.
                return data, key
            # Server returned an ok:false error that wasn't a rate limit
            # (e.g. invalid URL, video not found). Try the next key in case
            # the error is key-specific, but log it.
            message = data.get("message") if isinstance(data, dict) else ""
            logger.info("Yoinku key %s… returned ok:false: %s", key[:8], message)
            continue
        # All keys returned rate-limit / error → fall back.
        return None, None

    # ------------------------------------------------------------------
    # HTTP calls
    # ------------------------------------------------------------------

    async def _fetch_info(
        self,
        client: httpx.AsyncClient,
        key: str,
        url: str,
    ) -> httpx.Response | None:
        try:
            return await client.get(
                f"{self._api_base}/info",
                headers={"x-api-key": key},
                params={"url": url},
            )
        except httpx.HTTPError as exc:
            logger.warning("Yoinku /info HTTP error for %s: %s", url, exc)
            return None

    async def _fetch_download(
        self,
        client: httpx.AsyncClient,
        key: str,
        url: str,
        format_id: str,
    ) -> httpx.Response | None:
        try:
            return await client.get(
                f"{self._api_base}/download",
                headers={"x-api-key": key},
                params={"url": url, "format": format_id},
            )
        except httpx.HTTPError as exc:
            logger.warning("Yoinku /download HTTP error for %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Response → menu
    # ------------------------------------------------------------------

    def _build_menu(
        self,
        url: str,
        data: dict[str, Any],
        used_key: str,
    ) -> GatewayResult:
        title = (data.get("title") or "").strip()
        thumbnail = (data.get("thumbnailUrl") or "").strip()
        formats = data.get("formats") or []
        video_formats = [f for f in formats if f.get("kind") == "video"]
        audio_formats = [f for f in formats if f.get("kind") == "audio"]
        # Sort videos by height desc (best first).
        video_formats.sort(
            key=lambda f: (f.get("height") or 0, f.get("filesizeBytes") or 0),
            reverse=True,
        )

        options: list[QualityOption] = []
        for fmt in video_formats:
            fmt_id = fmt.get("id") or ""
            if not fmt_id:
                continue
            height = fmt.get("height") or 0
            quality = fmt.get("quality") or (f"{height}p" if height else "video")
            container = fmt.get("container") or "mp4"
            size = fmt.get("filesizeBytes") or 0
            label = f"{quality} ({container})"
            if size:
                label += f" — {self._fmt_size(size)}"
            options.append(
                QualityOption(
                    label=label,
                    row=0,
                    column=0,
                    fingerprint=_fingerprint({
                        "url": url,
                        "format_id": fmt_id,
                        "kind": "video",
                    }),
                    expected_kind=MediaKind.VIDEO,
                    expected_height=height or None,
                )
            )

        # Audio options — usually a single m4a entry.
        for fmt in audio_formats:
            fmt_id = fmt.get("id") or ""
            if not fmt_id:
                continue
            quality = fmt.get("quality") or "audio"
            container = fmt.get("container") or "m4a"
            size = fmt.get("filesizeBytes") or 0
            bitrate = fmt.get("audioBitrateKbps") or 0
            label = f"MP3 {quality}"
            if size:
                label += f" — {self._fmt_size(size)}"
            options.append(
                QualityOption(
                    label=label,
                    row=0,
                    column=0,
                    fingerprint=_fingerprint({
                        "url": url,
                        "format_id": fmt_id,
                        "kind": "audio",
                    }),
                    expected_kind=MediaKind.AUDIO,
                    expected_bitrate_kbps=bitrate or None,
                )
            )

        if not options:
            return GatewayResult(
                status="error",
                bot_username=YOINKU_PROVIDER,
                reason="yoinku_no_formats",
            )

        caption_parts = ["<b>▶️ یوتیوب</b>"]
        if title:
            safe_title = title.replace("<", "&lt;").replace(">", "&gt;")[:300]
            caption_parts.append(safe_title)
        if used_key:
            caption_parts.append(f"<code>{used_key[:8]}…</code>")
        return GatewayResult(
            status="needs_selection",
            bot_username=YOINKU_PROVIDER,
            options=tuple(options),
            text="\n".join(caption_parts),
        )

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    def _suffix_for_filename(self, filename: str, kind: MediaKind) -> str:
        if "." in filename.rsplit("/", 1)[-1]:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
            if ext in {".mp4", ".m4a", ".mp3", ".webm", ".opus"}:
                return ext
        return ".mp3" if kind == MediaKind.AUDIO else ".mp4"

    def _fmt_size(self, num_bytes: int) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                if unit == "B":
                    return f"{int(size)} B"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    async def _download_file(
        self,
        url: str,
        destination: Path,
        progress_callback: ProgressCallback | None,
    ) -> None:
        client = await self._ensure_client()
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise InvalidDownload(f"HTTP {response.status_code} from CDN")
            content_length = response.headers.get("content-length")
            total = int(content_length) if content_length and content_length.isdigit() else 0
            if self._max_download_size and total and total > self._max_download_size:
                raise DownloadTooLarge(
                    f"file exceeds max download size ({total} bytes)"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            bytes_written = 0
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if (
                        self._max_download_size
                        and bytes_written > self._max_download_size
                    ):
                        raise DownloadTooLarge("streamed size exceeds max download size")
                    if progress_callback is not None:
                        try:
                            await progress_callback(bytes_written, total or bytes_written)
                        except Exception:  # pragma: no cover
                            logger.exception("progress_callback failed")
        if destination.stat().st_size == 0:
            raise InvalidDownload("downloaded file is empty")


def yoinku_health_check(gateway: YoinkuGateway | None) -> str:
    if gateway is None:
        return "disabled"
    return f"ready ({gateway.pool.total} key(s))"
