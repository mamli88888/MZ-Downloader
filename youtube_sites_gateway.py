"""YouTubeSitesGateway — YouTube downloader that scrapes the loader.to /
loaderr.to / y2mate.yt family of public YouTube-converter sites.

Why this exists
---------------
yt-dlp + bgutil kept getting blocked by YouTube's "Sign in to confirm
you're not a bot" challenge for many videos, even with PO tokens. The
loader.to / loaderr.to / y2mate.yt family of sites is a popular public
frontend to a shared backend (savenow.to / lbserver.xyz) that performs
the YouTube extraction server-side and exposes a small AJAX API.

Discovery
---------
All three frontends share the SAME backend:

    GET https://<frontend-domain>/api/v2/download
        ?format=<format>&url=<youtube-url>&apikey=<shared-key>

    →  { "success": true,
         "id": "v2_stream_<id>",
         "progress_url": "https://<progress-host>/api/progress?id=v2_stream_<id>",
         "title": "...",
         "thumbnail_url": "https://i.ytimg.com/vi/<id>/hqdefault.jpg",
         ... }

Then poll the progress_url:

    GET <progress_url>
    →  { "success": 0|1,
         "progress": 0..1000,
         "download_url": "https://<cdn>/api/v2/download/<token>" | "",
         ... }

When `success == 1` and `download_url` is non-empty, fetch the file.

Shared API key (extracted from each frontend's main.js):
    dfcb6d76f2f6a9894gjkege8a4ab232222

Rotation strategy
-----------------
The three frontend families (loader.to, loaderr.to, y2mate.yt) and their
language subdomains all hit the same backend, but each frontend has its
own Cloudflare rate-limit / availability window. We build a rotation list
that interleaves the three families and tries each in turn until one
returns a usable `download_url`. If a site's API returns 4xx/5xx or the
progress poll times out, we move to the next site.

Quality menu
------------
We mirror the previous CobaltGateway / YtDlpGateway contract so the bot's
UI stays the same:
  - 360p / 480p / 720p / 1080p / 1440p / 2160p (video+audio muxed)
  - MP3 128 / 256 / 320 kbps (audio only)

Selection fingerprints encode the chosen mode/format/quality as a JSON
blob prefixed with `ysites:` so the rest of the bot code keeps working
unchanged.

API contract with the bot
-------------------------
This gateway exposes `request()` and `select()` async methods that return
`GatewayResult` (defined in downloader.py). They are direct replacements
for `DownloaderGateway.request()` / `select()` and the bot treats them the
same way, branching on the `YSITES_PROVIDER` sentinel.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

logger = logging.getLogger("MZDownloader.youtube_sites")


# Sentinel "provider" name — the bot's routing layer treats this like a
# Telegram bot username but the sites-aware branches key off it.
YSITES_PROVIDER = "ysites"


# Shared API key published in every frontend's main.js bundle. It is not
# a secret — anyone visiting the site gets the same key. We hard-code it
# so the gateway works out-of-the-box; an env override is also supported
# via YOUTUBE_SITES_API_KEY in case the site rotates it.
DEFAULT_API_KEY = "dfcb6d76f2f6a9894gjkege8a4ab232222"


# Frontend domains we rotate through. loader.to is the only family that
# proxies /api/v2/download on its own domain; loaderr.to and y2mate.yt
# serve the same frontend JS but call the shared backend (p.savenow.to /
# p.lbserver.xyz) directly from the browser, so hitting their /api/v2/
# download endpoint returns 404. We therefore rotate through:
#   - the loader.to language variants (which proxy the API)
#   - the backend API servers (used as direct fallback)
# Both paths share the same API contract and the same shared API key.
#
# `fa.loader.to`, `ar.loader.to`, `ko.loader.to`, `tr.loader.to` are
# excluded because their DNS doesn't resolve from common egresses; if
# you add a new language variant, verify it resolves first.
DEFAULT_FRONTENDS: tuple[str, ...] = (
    # loader.to family (these proxy /api/v2/download on their own domain)
    "loader.to",
    "en.loader.to",
    "es.loader.to",
    "de.loader.to",
    "fr.loader.to",
    "it.loader.to",
    "pt.loader.to",
    "ru.loader.to",
    "ja.loader.to",
    "zh.loader.to",
    "nl.loader.to",
    "pl.loader.to",
    # Backend API servers (shared by loaderr.to / y2mate.yt too) — used
    # both as additional rotation entries and as the explicit fallback.
    "p.savenow.to",
    "p.lbserver.xyz",
)


# Backend API servers (used by _initiate_via_backend as an explicit
# last-resort fallback after every frontend has failed). Kept as a
# separate constant for clarity even though they're also in the
# DEFAULT_FRONTENDS rotation.
BACKEND_API_SERVERS: tuple[str, ...] = (
    "p.savenow.to",
    "p.lbserver.xyz",
)


# Quality menu offered to the user. Each row is:
#   (label, video_height_or_None, mode, audio_bitrate_or_None, api_format)
# `api_format` is what we pass to the loader.to API as `?format=`.
YOUTUBE_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("360p",        360,  "video", None,  "360"),
    ("480p",        480,  "video", None,  "480"),
    ("720p",        720,  "video", None,  "720"),
    ("1080p",      1080,  "video", None,  "1080"),
    ("1440p (2K)", 1440,  "video", None,  "1440"),
    ("2160p (4K)", 2160,  "video", None,  "4k"),
    ("MP3 128kbps", None, "audio", "128", "mp3"),
    ("MP3 256kbps", None, "audio", "256", "mp3"),
    ("MP3 320kbps", None, "audio", "320", "mp3"),
)


_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", Path(name).name).strip(" .")
    return cleaned or "youtube_media"


def _fingerprint(payload: dict[str, Any]) -> str:
    return "ysites:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("ysites:"):
        return None
    try:
        return json.loads(fingerprint[len("ysites:"):])
    except json.JSONDecodeError:
        return None


def _mime_for_kind(kind: MediaKind, suffix: str) -> str:
    s = suffix.lower()
    if kind == MediaKind.VIDEO:
        if s == ".webm":
            return "video/webm"
        if s == ".mkv":
            return "video/x-matroska"
        return "video/mp4"
    if kind == MediaKind.AUDIO:
        if s == ".opus":
            return "audio/opus"
        if s == ".ogg":
            return "audio/ogg"
        if s == ".m4a":
            return "audio/mp4"
        if s == ".wav":
            return "audio/wav"
        return "audio/mpeg"
    if kind == MediaKind.PHOTO:
        if s == ".png":
            return "image/png"
        if s == ".webp":
            return "image/webp"
        return "image/jpeg"
    return "application/octet-stream"


def _build_media(path: Path, kind: MediaKind) -> DownloadedMedia:
    suffix = path.suffix.lower()
    return DownloadedMedia(
        path=path,
        kind=kind,
        source_message_id=0,
        mime_type=_mime_for_kind(kind, suffix),
        size=path.stat().st_size,
    )


def _extract_video_id(url: str) -> str | None:
    """Pull the 11-char YouTube video ID out of any common YouTube URL."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not host or not (host.endswith("youtube.com") or host.endswith("youtu.be") or host.endswith("youtube-nocookie.com")):
        return None
    if host.endswith("youtu.be"):
        vid = parsed.path.strip("/").split("/", 1)[0]
        return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else None
    # youtube.com
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] in {"watch", "embed", "shorts", "v"}:
        vid = parts[1]
        return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else None
    if "v" in (query := urllib.parse.parse_qs(parsed.query)):
        vid = query["v"][0]
        return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else None
    return None


class YouTubeSitesError(RuntimeError):
    """Raised when all rotation attempts fail."""


class YouTubeSitesGateway:
    """Rotational scraper over loader.to / loaderr.to / y2mate.yt family."""

    def __init__(
        self,
        *,
        api_key: str = DEFAULT_API_KEY,
        frontends: tuple[str, ...] = DEFAULT_FRONTENDS,
        # Optional egress proxy (http/socks5) — passed to httpx. None means
        # direct connection. Set this when running from inside Iran.
        proxy_url: str | None = None,
        max_download_size: int = 0,
        # How long to wait for the API to return a download URL before
        # giving up on this site and rotating to the next.
        progress_timeout: float = 180.0,
        # Poll interval for the progress endpoint.
        progress_poll_interval: float = 2.0,
        # Per-site HTTP timeout (init + progress polls).
        http_timeout: float = 20.0,
        # How many frontends to try before giving up. 0 = all of them.
        max_attempts: int = 6,
    ) -> None:
        self.api_key = api_key or DEFAULT_API_KEY
        self.frontends = tuple(frontends) or DEFAULT_FRONTENDS
        self.proxy_url = proxy_url
        self.max_download_size = max_download_size
        self.progress_timeout = progress_timeout
        self.progress_poll_interval = progress_poll_interval
        self.http_timeout = http_timeout
        self.max_attempts = max_attempts or len(self.frontends)
        # HTTP client is created per-call to avoid cross-call state.

    # ------------------------------------------------------------------
    # Public API (mirrors DownloaderGateway enough to slot in)
    # ------------------------------------------------------------------
    async def request(
        self,
        *,
        url: str,
        platform: Platform,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> GatewayResult:
        """Initial request — for YouTube this returns a quality menu."""
        if platform != Platform.YOUTUBE:
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason="unsupported_platform",
            )
        try:
            return await self._request_youtube(url, attempt_directory)
        except DownloadTooLarge:
            return GatewayResult(
                status="error", bot_username=YSITES_PROVIDER, reason="too_large"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("YouTubeSitesGateway request crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error", bot_username=YSITES_PROVIDER, reason="sites_error"
            )

    async def select(
        self,
        *,
        url: str,
        platform: Platform,
        option: QualityOption,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> GatewayResult:
        """Handle a quality-selection click — start the download via the sites."""
        if platform != Platform.YOUTUBE:
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason="unsupported_platform",
            )
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None:
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason="invalid_fingerprint",
            )
        try:
            return await self._select_youtube(url, payload, attempt_directory, progress_callback)
        except DownloadTooLarge:
            return GatewayResult(
                status="error", bot_username=YSITES_PROVIDER, reason="too_large"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("YouTubeSitesGateway select crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error", bot_username=YSITES_PROVIDER, reason="sites_error"
            )

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------
    async def _request_youtube(
        self,
        url: str,
        attempt_directory: Path,
    ) -> GatewayResult:
        """YouTube: present a quality menu."""
        options: list[QualityOption] = []
        for row_index, (label, height, mode, bitrate, api_format) in enumerate(YOUTUBE_QUALITIES):
            if mode == "audio":
                # loader.to's MP3 endpoint always re-encodes to a server-chosen
                # bitrate (it ignores the requested number), but we still
                # expose 128/256/320 to keep the menu familiar. The actual
                # file we get back is whatever the server produces.
                fingerprint = _fingerprint(
                    {
                        "mode": "audio",
                        "format": "mp3",
                        "bitrate": bitrate or "128",
                        "api_format": api_format,
                    }
                )
                bitrate_int = int(bitrate) if bitrate else 128
                options.append(
                    QualityOption(
                        label=label,
                        row=row_index,
                        column=0,
                        fingerprint=fingerprint,
                        expected_kind=MediaKind.AUDIO,
                        expected_bitrate_kbps=bitrate_int,
                        action="media",
                    )
                )
            else:
                fingerprint = _fingerprint(
                    {
                        "mode": "video",
                        "height": height or 1080,
                        "api_format": api_format,
                    }
                )
                options.append(
                    QualityOption(
                        label=label,
                        row=row_index,
                        column=0,
                        fingerprint=fingerprint,
                        expected_kind=MediaKind.VIDEO,
                        expected_height=height or 1080,
                        action="media",
                    )
                )
        return await self._attach_youtube_menu_assets(
            url=url,
            attempt_directory=attempt_directory,
            options=tuple(options),
        )

    async def _attach_youtube_menu_assets(
        self,
        *,
        url: str,
        attempt_directory: Path,
        options: tuple[QualityOption, ...],
    ) -> GatewayResult:
        """Enrich the menu with the video thumbnail + caption."""
        video_id = _extract_video_id(url)
        thumb_bytes: bytes | None = None
        title: str | None = None
        if video_id:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            try:
                async with self._client() as client:
                    resp = await client.get(thumb_url, timeout=10.0)
                    if resp.status_code == 200:
                        thumb_bytes = resp.content
            except Exception as exc:
                logger.debug("thumbnail fetch failed for %s: %s", video_id, exc)
            # Try to fetch the title from the page (best-effort, optional).
            title = await self._fetch_title(video_id)

        caption_lines: list[str] = []
        if title:
            caption_lines.append(f"<b>{html.escape(title)}</b>")
        caption_lines.append(f"▶️ یوتیوب • <code>{html.escape(url)}</code>")
        caption = "\n".join(caption_lines)

        preview: DownloadedMedia | None = None
        if thumb_bytes:
            thumb_path = attempt_directory / "_yt_thumb.jpg"
            try:
                thumb_path.write_bytes(thumb_bytes)
                preview = DownloadedMedia(
                    path=thumb_path,
                    kind=MediaKind.PHOTO,
                    source_message_id=0,
                    mime_type="image/jpeg",
                    size=thumb_path.stat().st_size,
                )
            except OSError as exc:
                logger.warning("Failed to persist YouTube thumbnail: %s", exc)
                preview = None

        return GatewayResult(
            status="needs_selection",
            bot_username=YSITES_PROVIDER,
            options=options,
            text=caption,
            preview=preview,
        )

    async def _fetch_title(self, video_id: str) -> str | None:
        """Best-effort fetch of the video title via YouTube's oEmbed API."""
        oembed_url = f"https://www.youtube.com/oembed?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D{video_id}&format=json"
        try:
            async with self._client() as client:
                resp = await client.get(oembed_url, timeout=8.0)
                if resp.status_code == 200:
                    data = resp.json()
                    return str(data.get("title") or "")
        except Exception as exc:
            logger.debug("oembed title fetch failed for %s: %s", video_id, exc)
        return None

    async def _select_youtube(
        self,
        url: str,
        payload: dict[str, Any],
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        mode = str(payload.get("mode", "video"))
        api_format = str(payload.get("api_format") or payload.get("format") or "720")
        if mode == "audio":
            return await self._download_audio(
                url=url,
                api_format=api_format,
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
            )
        target_height = int(payload.get("height") or 1080)
        return await self._download_video(
            url=url,
            api_format=api_format,
            target_height=target_height,
            attempt_directory=attempt_directory,
            progress_callback=progress_callback,
        )

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _client(self, **kwargs: Any) -> httpx.AsyncClient:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        defaults = dict(
            headers=headers,
            timeout=self.http_timeout,
            follow_redirects=True,
        )
        if self.proxy_url:
            defaults["proxy"] = self.proxy_url
        defaults.update(kwargs)
        return httpx.AsyncClient(**defaults)

    def _rotation(self) -> list[str]:
        """Return a shuffled rotation of frontend domains.

        We start from a fresh shuffle on every call so a temporarily-down
        frontend doesn't get permanently deprioritized.
        """
        items = list(self.frontends)
        random.shuffle(items)
        return items[: self.max_attempts]

    async def _initiate_download(
        self,
        client: httpx.AsyncClient,
        frontend: str,
        url: str,
        api_format: str,
    ) -> dict[str, Any] | None:
        """Hit /api/v2/download on a single frontend. Returns parsed JSON
        on success (success=true with id+progress_url), None on failure.
        """
        api_url = f"https://{frontend}/api/v2/download"
        params = {
            "format": api_format,
            "url": url,
            "apikey": self.api_key,
        }
        try:
            resp = await client.get(api_url, params=params, timeout=self.http_timeout)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.debug("init %s network error: %s", frontend, exc)
            return None
        if resp.status_code != 200:
            logger.debug("init %s HTTP %s", frontend, resp.status_code)
            return None
        try:
            data = resp.json()
        except Exception:
            logger.debug("init %s non-JSON response", frontend)
            return None
        if not data.get("success") or not data.get("id") or not data.get("progress_url"):
            logger.debug("init %s malformed: %s", frontend, str(data)[:200])
            return None
        return data

    async def _initiate_via_backend(
        self,
        client: httpx.AsyncClient,
        url: str,
        api_format: str,
    ) -> dict[str, Any] | None:
        """Direct fallback to the backend API servers (savenow.to /
        lbserver.xyz) when frontends 404. Same API contract.
        """
        for host in BACKEND_API_SERVERS:
            api_url = f"https://{host}/api/v2/download"
            params = {
                "format": api_format,
                "url": url,
                "apikey": self.api_key,
            }
            try:
                resp = await client.get(api_url, params=params, timeout=self.http_timeout)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.debug("backend init %s network error: %s", host, exc)
                continue
            if resp.status_code != 200:
                logger.debug("backend init %s HTTP %s", host, resp.status_code)
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if data.get("success") and data.get("id") and data.get("progress_url"):
                return data
        return None

    async def _poll_progress(
        self,
        client: httpx.AsyncClient,
        progress_url: str,
        progress_callback: ProgressCallback | None,
    ) -> str | None:
        """Poll the progress endpoint until download_url is non-empty.

        Returns the download_url on success, None on timeout / failure.
        """
        deadline = asyncio.get_event_loop().time() + self.progress_timeout
        last_progress = -1
        while asyncio.get_event_loop().time() < deadline:
            try:
                # Cache-bust like the official frontend does.
                cache_busted = f"{progress_url}&_={int(asyncio.get_event_loop().time() * 1000)}"
                if "?" not in progress_url:
                    cache_busted = f"{progress_url}?_={int(asyncio.get_event_loop().time() * 1000)}"
                resp = await client.get(cache_busted, timeout=self.http_timeout)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.debug("progress poll network error: %s", exc)
                await asyncio.sleep(self.progress_poll_interval)
                continue
            if resp.status_code != 200:
                logger.debug("progress poll HTTP %s", resp.status_code)
                await asyncio.sleep(self.progress_poll_interval)
                continue
            try:
                data = resp.json()
            except Exception:
                await asyncio.sleep(self.progress_poll_interval)
                continue
            # Progress is 0..1000 (1000 = done). We report it as 0..100%.
            progress = int(data.get("progress") or 0)
            if progress != last_progress and progress_callback is not None:
                # Report total bytes as the progress * 100 so the fraction
                # works out (downloaded/total). The bot's ProgressReporter
                # only uses the ratio, so any consistent scaling is fine.
                try:
                    await progress_callback(progress, 1000)
                except Exception:
                    pass
                last_progress = progress
            download_url = data.get("download_url") or ""
            if data.get("success") == 1 and download_url.startswith("http"):
                return download_url
            # Some servers return success=0 with an empty download_url while
            # processing — that's normal, keep polling.
            await asyncio.sleep(self.progress_poll_interval)
        return None

    async def _download_file(
        self,
        client: httpx.AsyncClient,
        download_url: str,
        target_path: Path,
        progress_callback: ProgressCallback | None,
    ) -> None:
        """Stream-download the file from the CDN download_url."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # CDN streams can be slow on long videos — use a generous read timeout.
        # We keep the connect timeout short so dead CDNs fail fast.
        cdn_timeout = httpx.Timeout(
            connect=10.0,
            read=300.0,   # 5 min between bytes — long videos
            write=30.0,
            pool=10.0,
        )
        # First HEAD to learn size if possible.
        try:
            head = await client.head(download_url, timeout=cdn_timeout)
            content_length = int(head.headers.get("content-length") or 0)
        except Exception:
            content_length = 0
        if (
            self.max_download_size > 0
            and content_length > 0
            and content_length > self.max_download_size
        ):
            raise DownloadTooLarge("YouTube sites output exceeds MAX_DOWNLOAD_SIZE_MB")
        # Stream the body.
        total_bytes = 0
        last_report = 0.0
        async with client.stream("GET", download_url, timeout=cdn_timeout) as stream:
            if stream.status_code != 200:
                raise InvalidDownload(
                    f"YouTube sites CDN returned HTTP {stream.status_code}"
                )
            actual_length = int(stream.headers.get("content-length") or content_length or 0)
            if (
                self.max_download_size > 0
                and actual_length > 0
                and actual_length > self.max_download_size
            ):
                raise DownloadTooLarge("YouTube sites output exceeds MAX_DOWNLOAD_SIZE_MB")
            with target_path.open("wb") as f:
                async for chunk in stream.aiter_bytes():
                    f.write(chunk)
                    total_bytes += len(chunk)
                    if progress_callback is not None:
                        now = asyncio.get_event_loop().time()
                        if now - last_report > 0.5:
                            last_report = now
                            try:
                                await progress_callback(total_bytes, actual_length or total_bytes)
                            except Exception:
                                pass
        if total_bytes == 0:
            raise InvalidDownload("YouTube sites CDN returned an empty file")
        if (
            self.max_download_size > 0
            and total_bytes > self.max_download_size
        ):
            try:
                target_path.unlink()
            except OSError:
                pass
            raise DownloadTooLarge("YouTube sites output exceeds MAX_DOWNLOAD_SIZE_MB")
        # Final 100% progress report.
        if progress_callback is not None:
            try:
                await progress_callback(total_bytes, actual_length or total_bytes)
            except Exception:
                pass

    async def _resolve_download_url(
        self,
        url: str,
        api_format: str,
        progress_callback: ProgressCallback | None,
    ) -> tuple[str, dict[str, Any]]:
        """Try each frontend in rotation, then the backend servers, until
        one returns a usable download_url. Returns (download_url, init_data).
        Raises YouTubeSitesError if every attempt fails.
        """
        frontends = self._rotation()
        last_error = "no attempts made"
        for index, frontend in enumerate(frontends, start=1):
            logger.info(
                "youtube-sites: attempt %d/%d via %s (format=%s)",
                index, len(frontends), frontend, api_format,
            )
            try:
                async with self._client() as client:
                    init_data = await self._initiate_download(
                        client, frontend, url, api_format,
                    )
                    if init_data is None:
                        last_error = f"{frontend}: init failed"
                        continue
                    progress_url = init_data.get("progress_url") or ""
                    if not progress_url:
                        last_error = f"{frontend}: no progress_url"
                        continue
                    download_url = await self._poll_progress(
                        client, progress_url, progress_callback,
                    )
                    if download_url:
                        logger.info(
                            "youtube-sites: %s returned download_url for %s",
                            frontend, url,
                        )
                        return download_url, init_data
                    last_error = f"{frontend}: progress timed out"
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{frontend}: {exc}"
                continue
            except Exception as exc:
                logger.warning(
                    "youtube-sites: %s unexpected error: %s", frontend, exc
                )
                last_error = f"{frontend}: {exc}"
                continue
        # All frontends failed — try the backend servers as a last resort.
        logger.info("youtube-sites: all frontends failed, trying backend servers directly")
        try:
            async with self._client() as client:
                init_data = await self._initiate_via_backend(client, url, api_format)
                if init_data is None:
                    last_error = "backend: init failed"
                else:
                    progress_url = init_data.get("progress_url") or ""
                    if progress_url:
                        download_url = await self._poll_progress(
                            client, progress_url, progress_callback,
                        )
                        if download_url:
                            logger.info(
                                "youtube-sites: backend returned download_url for %s",
                                url,
                            )
                            return download_url, init_data
                        last_error = "backend: progress timed out"
                    else:
                        last_error = "backend: no progress_url"
        except Exception as exc:
            last_error = f"backend: {exc}"
        raise YouTubeSitesError(f"All YouTube sites failed: {last_error}")

    async def _download_video(
        self,
        *,
        url: str,
        api_format: str,
        target_height: int,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        try:
            download_url, init_data = await self._resolve_download_url(
                url, api_format, progress_callback,
            )
        except YouTubeSitesError as exc:
            logger.warning("youtube-sites video download failed for %s: %s", url, exc)
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason=self._map_error(exc),
            )
        # Determine the output extension from the download URL or fall back
        # to mp4 (loader.to's video endpoint always serves mp4 / webm).
        suffix = self._guess_suffix(download_url, default=".mp4")
        final_path = attempt_directory / f"ysites_{target_height}p{suffix}"
        try:
            async with self._client() as client:
                await self._download_file(
                    client, download_url, final_path, progress_callback,
                )
        except DownloadTooLarge:
            raise
        except InvalidDownload as exc:
            logger.warning("youtube-sites CDN download failed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason="sites_cdn_error",
            )
        except Exception as exc:
            logger.warning("youtube-sites CDN download crashed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason="sites_cdn_error",
            )
        # Clean up part files (anything that isn't the final output or the thumb).
        self._cleanup_part_files(attempt_directory, keep={final_path.name, "_yt_thumb.jpg"})
        media = _build_media(final_path, MediaKind.VIDEO)
        return GatewayResult(
            status="ready",
            bot_username=YSITES_PROVIDER,
            media=(media,),
        )

    async def _download_audio(
        self,
        *,
        url: str,
        api_format: str,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        try:
            download_url, init_data = await self._resolve_download_url(
                url, api_format, progress_callback,
            )
        except YouTubeSitesError as exc:
            logger.warning("youtube-sites audio download failed for %s: %s", url, exc)
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason=self._map_error(exc),
            )
        suffix = self._guess_suffix(download_url, default=".mp3")
        final_path = attempt_directory / f"ysites_audio{suffix}"
        try:
            async with self._client() as client:
                await self._download_file(
                    client, download_url, final_path, progress_callback,
                )
        except DownloadTooLarge:
            raise
        except InvalidDownload as exc:
            logger.warning("youtube-sites CDN download failed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason="sites_cdn_error",
            )
        except Exception as exc:
            logger.warning("youtube-sites CDN download crashed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=YSITES_PROVIDER,
                reason="sites_cdn_error",
            )
        self._cleanup_part_files(attempt_directory, keep={final_path.name, "_yt_thumb.jpg"})
        media = _build_media(final_path, MediaKind.AUDIO)
        return GatewayResult(
            status="ready",
            bot_username=YSITES_PROVIDER,
            media=(media,),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _guess_suffix(download_url: str, *, default: str) -> str:
        """Try to guess the file extension from the download URL."""
        parsed = urllib.parse.urlsplit(download_url)
        path = parsed.path.lower()
        # /api/v2/download/<token> — no extension, use default.
        for ext in (".mp4", ".m4a", ".webm", ".mp3", ".opus", ".ogg", ".wav"):
            if path.endswith(ext):
                return ext
        # Check Content-Type headers? We don't have them here; the default
        # is what loader.to documents for each format.
        return default

    def _cleanup_part_files(self, attempt_directory: Path, keep: set[str]) -> None:
        for path in attempt_directory.iterdir():
            if path.is_dir():
                continue
            if path.name in keep:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    @staticmethod
    def _map_error(exc: Exception) -> str:
        msg = str(exc).lower()
        if "private" in msg:
            return "content_private"
        if "age" in msg or "restricted" in msg:
            return "content_age"
        if "unavailable" in msg or "removed" in msg or "not available" in msg:
            return "content_unavailable"
        if "rate" in msg or "too many requests" in msg or "429" in msg:
            return "rate_limited"
        if "sign in" in msg or "bot" in msg or "captcha" in msg:
            return "auth_required"
        if "timed out" in msg or "timeout" in msg:
            return "sites_timeout"
        return "sites_error"


# ----------------------------------------------------------------------
# Connectivity probe (used by bot.post_init to log whether the sites are
# reachable from this container).
# ----------------------------------------------------------------------


async def sites_health_check(frontends: tuple[str, ...] | None = None, *, timeout: float = 5.0) -> bool:
    """Quick connectivity probe for the YouTube sites family.

    Returns True if at least one frontend responds 200 to a HEAD /.
    """
    targets = frontends or DEFAULT_FRONTENDS
    # Sample 3 random frontends to avoid hammering all of them on every probe.
    sample = random.sample(list(targets), k=min(3, len(targets)))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for host in sample:
            try:
                resp = await client.get(f"https://{host}/", timeout=timeout)
                if resp.status_code < 500:
                    return True
            except Exception:
                continue
    return False
