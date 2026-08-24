"""Ahm7Gateway — multi-platform downloader via https://ahm7xmakki.com/api/alldl.

Supported platforms: TikTok, Instagram, Facebook, X/Twitter, Reddit,
Snapchat, SoundCloud, CapCut, SnackVideo, Douyin.

The site returns ``mediaInfo`` containing ``videoUrl`` and (optionally)
``audioUrl``. When the user picks the audio option but the API did not
return a direct ``audioUrl``, the audio is extracted from the downloaded
video via::

    ffmpeg -i input.mp4 -vn -c:a libmp3lame -b:a 192k output.mp3

API contract with the bot
-------------------------
Exposes ``request()`` and ``select()`` async methods that return
``GatewayResult`` (defined in ``downloader.py``). They are direct
replacements for ``DownloaderGateway.request()`` / ``select()`` and the
bot treats them the same way, branching on the ``AHM7_PROVIDER`` sentinel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

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

logger = logging.getLogger("MZDownloader.ahm7")


# Sentinel "provider" name — the bot's routing layer treats this like a
# Telegram bot username but the AHM7-aware branches key off it.
AHM7_PROVIDER = "ahm7"

DEFAULT_API_URL = "https://ahm7xmakki.com/api/alldl"

# Platforms that go through AHM7 as their primary downloader.
AHM7_PLATFORMS: frozenset[Platform] = frozenset({
    Platform.TIKTOK,
    Platform.INSTAGRAM,
    Platform.FACEBOOK,
    Platform.TWITTER,
    Platform.REDDIT,
    Platform.SOUNDCLOUD,
    Platform.SNAPCHAT,
    Platform.CAPCUT,
    Platform.SNACKVIDEO,
    Platform.DOUYIN,
})

# A "processing" callback is invoked during the ffmpeg extraction phase
# (no byte counts — just a percent bar so the user sees activity).
ProcessingCallback = Callable[[int, str, str], Awaitable[None]]

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_SUFFIX_WHITELIST = frozenset({
    ".mp4", ".m4a", ".mp3", ".webm", ".opus",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
})


def _fingerprint(payload: dict[str, Any]) -> str:
    """Encode selection metadata as a JSON blob prefixed with ``ahm7:``."""
    return "ahm7:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("ahm7:"):
        return None
    try:
        return json.loads(fingerprint[len("ahm7:"):])
    except json.JSONDecodeError:
        return None


def _safe_filename(name: str, *, max_len: int = 100) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip().rstrip(".")
    return cleaned[:max_len] or "media"


def _suffix_for_url(url: str, default: str = ".mp4") -> str:
    try:
        path = urlparse(url).path
    except ValueError:
        return default
    last = path.rsplit("/", 1)[-1]
    if "." in last:
        ext = "." + last.rsplit(".", 1)[-1].lower()
        if ext in _SUFFIX_WHITELIST:
            return ext
    return default


def _mime_for_kind(kind: MediaKind, suffix: str) -> str:
    s = suffix.lower()
    if kind == MediaKind.AUDIO:
        if s == ".m4a":
            return "audio/mp4"
        if s == ".opus":
            return "audio/ogg"
        return "audio/mpeg"
    if kind == MediaKind.PHOTO:
        if s == ".png":
            return "image/png"
        if s == ".gif":
            return "image/gif"
        if s == ".webp":
            return "image/webp"
        return "image/jpeg"
    if kind == MediaKind.DOCUMENT:
        return "application/octet-stream"
    return "video/mp4"


def _build_media(path: Path, kind: MediaKind) -> DownloadedMedia:
    return DownloadedMedia(
        path=path,
        kind=kind,
        source_message_id=0,
        mime_type=_mime_for_kind(kind, path.suffix.lower()),
        size=path.stat().st_size,
    )


def _platform_label(platform: Platform) -> str:
    mapping = {
        Platform.TIKTOK: "TikTok",
        Platform.INSTAGRAM: "Instagram",
        Platform.FACEBOOK: "Facebook",
        Platform.TWITTER: "Twitter/X",
        Platform.REDDIT: "Reddit",
        Platform.SOUNDCLOUD: "SoundCloud",
        Platform.SNAPCHAT: "Snapchat",
        Platform.CAPCUT: "CapCut",
        Platform.SNACKVIDEO: "SnackVideo",
        Platform.DOUYIN: "Douyin",
    }
    return mapping.get(platform, "media")


class Ahm7Gateway:
    """AHM7 AllDL gateway."""

    def __init__(
        self,
        *,
        api_url: str = DEFAULT_API_URL,
        proxy_url: str | None = None,
        max_download_size: int = 0,
        request_timeout: float = 60.0,
    ) -> None:
        self._api_url = api_url
        self._proxy_url = proxy_url
        self._max_download_size = max_download_size
        self._request_timeout = request_timeout
        # httpx client is created lazily so we don't bind to the event loop
        # at import time (which breaks Railway's fork-based startup).
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # AHM7's Hostinger CDN (hcdn) rejects httpx's default UA +
            # missing Accept header with HTTP 403. Send a real browser
            # User-Agent AND an explicit ``Accept: */*`` (which httpx does
            # NOT send by default, unlike python-requests).
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
            kwargs: dict[str, Any] = {
                "timeout": self._request_timeout,
                "follow_redirects": True,
                "headers": headers,
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
        """Query AHM7 for available media and return a quality menu."""
        if platform not in AHM7_PLATFORMS:
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="unsupported_platform",
            )
        try:
            data = await self._fetch_media_info(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("AHM7 info crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="ahm7_info_error",
            )
        if data is None:
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="ahm7_no_media",
            )
        media = data.get("mediaInfo") or {}
        video_url = (media.get("videoUrl") or "").strip()
        audio_url = (media.get("audioUrl") or "").strip()
        title = (media.get("title") or "").strip()
        thumbnail = (media.get("thumbnail") or "").strip()
        qualities = media.get("qualities") or []

        options: list[QualityOption] = []

        # If the API returned a qualities list (per-resolution direct URLs),
        # expose each as a separate video option.
        for index, entry in enumerate(qualities, start=1):
            if not isinstance(entry, dict):
                continue
            entry_url = (entry.get("url") or "").strip()
            if not entry_url:
                continue
            label = str(entry.get("quality") or f"کیفیت {index}")
            options.append(
                QualityOption(
                    label=label,
                    row=0,
                    column=0,
                    fingerprint=_fingerprint({
                        "url": url,
                        "video_url": entry_url,
                        "audio_url": audio_url,
                        "kind": "video",
                    }),
                    expected_kind=MediaKind.VIDEO,
                )
            )

        # Fallback single video option (the API often returns just one URL).
        if video_url and not options:
            options.append(
                QualityOption(
                    label="🎬 ویدیو",
                    row=0,
                    column=0,
                    fingerprint=_fingerprint({
                        "url": url,
                        "video_url": video_url,
                        "audio_url": audio_url,
                        "kind": "video",
                    }),
                    expected_kind=MediaKind.VIDEO,
                )
            )

        # Audio option. Always offered when a video or audio URL exists —
        # `select()` fetches audio_url directly if present, else extracts
        # via ffmpeg from the downloaded video file.
        if video_url or audio_url:
            options.append(
                QualityOption(
                    label="🎵 صدا (MP3)",
                    row=0,
                    column=0,
                    fingerprint=_fingerprint({
                        "url": url,
                        "video_url": video_url,
                        "audio_url": audio_url,
                        "kind": "audio",
                    }),
                    expected_kind=MediaKind.AUDIO,
                )
            )

        if not options:
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="ahm7_empty_media",
            )

        preview: DownloadedMedia | None = None
        if thumbnail:
            try:
                preview = await self._download_preview(thumbnail, attempt_directory)
            except Exception as exc:  # pragma: no cover — preview is best-effort
                logger.debug("AHM7 thumbnail download failed: %s", exc)

        caption_parts = [f"<b>{_platform_label(platform)}</b>"]
        if title:
            safe_title = title.replace("<", "&lt;").replace(">", "&gt;")[:300]
            caption_parts.append(safe_title)
        return GatewayResult(
            status="needs_selection",
            bot_username=AHM7_PROVIDER,
            options=tuple(options),
            text="\n".join(caption_parts),
            preview=preview,
        )

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
        """Download the file selected by ``option.fingerprint``."""
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None:
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="invalid_fingerprint",
            )
        kind = payload.get("kind")
        video_url = (payload.get("video_url") or "").strip()
        audio_url = (payload.get("audio_url") or "").strip()
        try:
            if kind == "audio":
                return await self._download_audio(
                    url, video_url, audio_url, attempt_directory,
                    progress_callback, processing_callback,
                )
            return await self._download_video(
                url, video_url, attempt_directory, progress_callback,
            )
        except DownloadTooLarge:
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="too_large",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("AHM7 select crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="ahm7_download_error",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_media_info(self, url: str) -> dict[str, Any] | None:
        client = await self._ensure_client()
        try:
            response = await client.get(self._api_url, params={"url": url})
        except httpx.HTTPError as exc:
            logger.warning("AHM7 HTTP error for %s: %s", url, exc)
            return None
        if response.status_code != 200:
            logger.info("AHM7 non-200 for %s: %d", url, response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("AHM7 invalid JSON for %s", url)
            return None
        if not isinstance(data, dict) or not data.get("success"):
            message = data.get("message") if isinstance(data, dict) else ""
            logger.info("AHM7 unsuccessful for %s: %s", url, message)
            return None
        return data

    async def _download_video(
        self,
        source_url: str,
        video_url: str,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        if not video_url:
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="ahm7_no_video_url",
            )
        suffix = _suffix_for_url(video_url, default=".mp4")
        final_path = attempt_directory / f"ahm7_video{suffix}"
        await self._download_file(video_url, final_path, progress_callback)
        media = _build_media(final_path, MediaKind.VIDEO)
        return GatewayResult(
            status="ready",
            bot_username=AHM7_PROVIDER,
            media=(media,),
        )

    async def _download_audio(
        self,
        source_url: str,
        video_url: str,
        audio_url: str,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
        processing_callback: ProcessingCallback | None,
    ) -> GatewayResult:
        # Prefer the API-provided direct audio URL when present.
        if audio_url:
            suffix = _suffix_for_url(audio_url, default=".mp3")
            final_path = attempt_directory / f"ahm7_audio{suffix}"
            await self._download_file(audio_url, final_path, progress_callback)
            media = _build_media(final_path, MediaKind.AUDIO)
            return GatewayResult(
                status="ready",
                bot_username=AHM7_PROVIDER,
                media=(media,),
            )
        # Otherwise, download the video and extract audio via ffmpeg.
        if not video_url:
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="ahm7_no_audio_source",
            )
        if processing_callback is not None:
            try:
                await processing_callback(5, "📥 دانلود ویدیو برای استخراج صدا…", "مرحله ۱ از ۲")
            except Exception:  # pragma: no cover
                logger.exception("processing_callback failed")
        video_suffix = _suffix_for_url(video_url, default=".mp4")
        video_path = attempt_directory / f"ahm7_source{video_suffix}"
        await self._download_file(video_url, video_path, progress_callback)
        audio_path = attempt_directory / "ahm7_audio.mp3"
        if processing_callback is not None:
            try:
                await processing_callback(60, "🎵 استخراج صدا با ffmpeg…", "مرحله ۲ از ۲")
            except Exception:  # pragma: no cover
                logger.exception("processing_callback failed")
        try:
            await self._extract_audio_ffmpeg(video_path, audio_path)
        except InvalidDownload as exc:
            logger.warning("AHM7 ffmpeg extract failed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=AHM7_PROVIDER,
                reason="ahm7_ffmpeg_failed",
            )
        finally:
            with contextlib.suppress(OSError):
                video_path.unlink()
        media = _build_media(audio_path, MediaKind.AUDIO)
        return GatewayResult(
            status="ready",
            bot_username=AHM7_PROVIDER,
            media=(media,),
        )

    async def _download_file(
        self,
        url: str,
        destination: Path,
        progress_callback: ProgressCallback | None,
    ) -> None:
        """Stream a URL to disk, reporting bytes via ``progress_callback``."""
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

    async def _extract_audio_ffmpeg(
        self,
        video_path: Path,
        audio_path: Path,
    ) -> None:
        """Run ``ffmpeg -i input.mp4 -vn -c:a libmp3lame -b:a 192k output.mp3``."""
        if not video_path.exists() or video_path.stat().st_size == 0:
            raise InvalidDownload("source video is missing or empty")
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(audio_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if (
            process.returncode != 0
            or not audio_path.exists()
            or audio_path.stat().st_size == 0
        ):
            snippet = (stderr or b"")[-1500:].decode("utf-8", errors="ignore")
            logger.warning("ffmpeg failed (rc=%s): %s", process.returncode, snippet)
            raise InvalidDownload("ffmpeg could not extract the audio track")

    async def _download_preview(
        self,
        thumbnail_url: str,
        attempt_directory: Path,
    ) -> DownloadedMedia | None:
        suffix = _suffix_for_url(thumbnail_url, default=".jpg")
        path = attempt_directory / f"ahm7_thumb{suffix}"
        client = await self._ensure_client()
        try:
            response = await client.get(thumbnail_url)
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or not response.content:
            return None
        path.write_bytes(response.content)
        return _build_media(path, MediaKind.PHOTO)


def ahm7_health_check(gateway: Ahm7Gateway | None) -> str:
    if gateway is None:
        return "disabled"
    return f"ready ({DEFAULT_API_URL})"
