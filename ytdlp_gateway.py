"""YtDlpGateway: a YouTube-only downloader backed by yt-dlp + bgutil PO Token.

This gateway implements the same request()/select() contract as CobaltGateway
so the bot can treat both interchangeably. The bot routes YouTube URLs to
this gateway when YTDLP_PROVIDER appears in the providers list, and falls
back to Cobalt (if configured) or Telegram bots on failure.

Architecture
------------
    Bot → YtDlpGateway.request / select
              │
              ▼
         yt-dlp (with bgutil-ytdlp-pot-provider plugin auto-discovered)
              │
              ▼
         bgutil HTTP server (Docker container, port 4416 by default)
              │
              ▼
         (optional) foreign proxy if running from inside Iran
              │
              ▼
         YouTube

The plugin is installed via `pip install bgutil-ytdlp-pot-provider`. yt-dlp
auto-discovers plugins in `yt_dlp_plugins.*` namespace packages, so no
explicit registration is needed. The only configuration we pass is the
bgutil HTTP server base URL via `extractor_args.youtubepot-bgutilhttp.base_url`.

Cookies
-------
Optional. When `cookies.txt` exists in the project root (or YTDLP_COOKIES_FILE
is set), yt-dlp uses it for age-restricted / members-only videos. Cookies
also help dodge the "Sign in to confirm you are not a bot" challenge on
videos where PO Token alone is not enough.

Quality menu
------------
We present the same quality ladder as the old Cobalt gateway so the bot's UI
stays familiar:
  - 360p / 480p / 720p / 1080p / 1440p / 2160p (video+audio muxed)
  - MP3 128 / 256 / 320 kbps (audio only)

Selection fingerprints encode the chosen mode/quality/format/bitrate as a
JSON blob prefixed with `ytdlp:` so the rest of the bot code (which only
treats fingerprints as opaque tokens) keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from youtube_search import (
    YouTubeVideoInfo,
    download_thumbnail_bytes,
    fetch_video_info,
    format_duration,
)

logger = logging.getLogger("MZDownloader.ytdlp_gateway")


# Sentinel "provider" name — the bot's routing layer treats this like a
# Telegram bot username but the yt-dlp-aware branches key off it.
YTDLP_PROVIDER = "ytdlp"

# Quality options offered for YouTube videos. The label is shown on the
# button. Format: (label, video_height_or_None, mode, audio_bitrate_or_None)
YOUTUBE_QUALITIES: tuple[tuple[str, int | None, str, str | None], ...] = (
    ("360p", 360, "video", None),
    ("480p", 480, "video", None),
    ("720p", 720, "video", None),
    ("1080p", 1080, "video", None),
    ("1440p (2K)", 1440, "video", None),
    ("2160p (4K)", 2160, "video", None),
    ("MP3 128kbps", None, "audio", "128"),
    ("MP3 256kbps", None, "audio", "256"),
    ("MP3 320kbps", None, "audio", "320"),
)

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", Path(name).name).strip(" .")
    return cleaned or "ytdlp_media"


def _fingerprint(payload: dict[str, Any]) -> str:
    """Encode yt-dlp selection params into a QualityOption.fingerprint."""
    return "ytdlp:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("ytdlp:"):
        return None
    try:
        return json.loads(fingerprint[len("ytdlp:"):])
    except json.JSONDecodeError:
        return None


def _mime_for_kind(kind: MediaKind, suffix: str) -> str:
    suffix_lower = suffix.lower()
    if kind == MediaKind.VIDEO:
        if suffix_lower == ".webm":
            return "video/webm"
        if suffix_lower == ".mkv":
            return "video/x-matroska"
        return "video/mp4"
    if kind == MediaKind.AUDIO:
        if suffix_lower == ".opus":
            return "audio/opus"
        if suffix_lower == ".ogg":
            return "audio/ogg"
        if suffix_lower == ".m4a":
            return "audio/mp4"
        if suffix_lower == ".wav":
            return "audio/wav"
        return "audio/mpeg"
    if kind == MediaKind.PHOTO:
        if suffix_lower == ".png":
            return "image/png"
        if suffix_lower == ".webp":
            return "image/webp"
        return "image/jpeg"
    return "application/octet-stream"


def _build_media(path: Path, kind: MediaKind) -> DownloadedMedia:
    suffix = path.suffix.lower()
    mime = _mime_for_kind(kind, suffix)
    return DownloadedMedia(
        path=path,
        kind=kind,
        source_message_id=0,
        mime_type=mime,
        size=path.stat().st_size,
    )


def _run_ffmpeg_merge(
    ffmpeg_path: str,
    video_path: Path,
    audio_path: Path,
    output_path: Path,
) -> None:
    """Merge video + audio into a single mp4 via ffmpeg copy."""
    if not video_path.is_file():
        raise InvalidDownload("Video part missing")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path,
        "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    logger.info("Running ffmpeg merge: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        # Fall back to re-encoding if copy failed (codec mismatch).
        logger.warning(
            "ffmpeg copy failed (exit %d), falling back to re-encode: %s",
            result.returncode,
            result.stderr[-500:],
        )
        fallback = [
            ffmpeg_path,
            "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(fallback, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _run_ffmpeg_extract_audio(
    ffmpeg_path: str,
    src_path: Path,
    output_path: Path,
    *,
    codec: str = "libmp3lame",
    bitrate: str = "192k",
) -> None:
    """Extract / transcode audio from a downloaded file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path,
        "-y",
        "-i", str(src_path),
        "-vn",
        "-acodec", codec,
        "-b:a", bitrate,
        str(output_path),
    ]
    logger.info("Running ffmpeg audio extract: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class _ProgressHook:
    """Adapter from yt-dlp's progress_hooks to our async ProgressCallback.

    yt-dlp calls the hook synchronously with a dict; we forward the byte
    counts to the bot's async callback. Because yt-dlp runs in a worker
    thread (via asyncio.to_thread), we can't `await` the callback directly,
    so we schedule it on the loop and don't wait for completion.
    """

    def __init__(self, callback: ProgressCallback | None, loop: asyncio.AbstractEventLoop) -> None:
        self.callback = callback
        self.loop = loop
        self.total_bytes: int | None = None

    def __call__(self, d: dict[str, Any]) -> None:
        if self.callback is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                self.total_bytes = int(total)
            try:
                if self.total_bytes:
                    asyncio.run_coroutine_threadsafe(
                        self.callback(int(downloaded), self.total_bytes),
                        self.loop,
                    )
            except Exception:
                pass
        elif status == "finished":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            try:
                asyncio.run_coroutine_threadsafe(
                    self.callback(int(total), int(total)),
                    self.loop,
                )
            except Exception:
                pass


class YtDlpGateway:
    """yt-dlp-backed implementation of the gateway contract (YouTube only).

    Public methods mirror CobaltGateway so the bot can treat both the same
    way, branching only on the YTDLP_PROVIDER sentinel.
    """

    def __init__(
        self,
        *,
        bgutil_base_url: str = "http://127.0.0.1:4416",
        cookies_file: Path | None = None,
        proxy_url: str | None = None,
        max_download_size: int = 0,
        ffmpeg_path: str = "ffmpeg",
        player_clients: tuple[str, ...] = ("mweb", "web"),
    ) -> None:
        self.bgutil_base_url = bgutil_base_url.rstrip("/")
        self.cookies_file = cookies_file
        self.proxy_url = proxy_url
        self.max_download_size = max_download_size
        self.ffmpeg_path = ffmpeg_path
        self.player_clients = player_clients

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
        """Initial request — for YouTube this returns a quality menu."""
        if platform != Platform.YOUTUBE:
            return GatewayResult(
                status="error",
                bot_username=YTDLP_PROVIDER,
                reason="unsupported_platform",
            )
        try:
            return await self._request_youtube(url, attempt_directory, progress_callback)
        except DownloadTooLarge:
            return GatewayResult(
                status="error", bot_username=YTDLP_PROVIDER, reason="too_large"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("YtDlpGateway request crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error", bot_username=YTDLP_PROVIDER, reason="ytdlp_error"
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
        """Handle a quality-selection click — run yt-dlp with the chosen params."""
        if platform != Platform.YOUTUBE:
            return GatewayResult(
                status="error",
                bot_username=YTDLP_PROVIDER,
                reason="unsupported_platform",
            )
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None:
            return GatewayResult(
                status="error",
                bot_username=YTDLP_PROVIDER,
                reason="invalid_fingerprint",
            )
        try:
            return await self._select_youtube(url, payload, attempt_directory, progress_callback)
        except DownloadTooLarge:
            return GatewayResult(
                status="error", bot_username=YTDLP_PROVIDER, reason="too_large"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("YtDlpGateway select crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error", bot_username=YTDLP_PROVIDER, reason="ytdlp_error"
            )

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------
    async def _request_youtube(
        self,
        url: str,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        """YouTube: present a quality menu (mirrors CobaltGateway behaviour)."""
        options: list[QualityOption] = []
        for row_index, (label, height, mode, bitrate) in enumerate(YOUTUBE_QUALITIES):
            if mode == "audio":
                fingerprint = _fingerprint(
                    {
                        "mode": "audio",
                        "format": "mp3",
                        "bitrate": bitrate or "128",
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
        """Enrich the YouTube quality menu with the video thumbnail + caption."""
        info: YouTubeVideoInfo | None = None
        try:
            info = await asyncio.to_thread(fetch_video_info, url)
        except Exception:
            logger.exception("fetch_video_info wrapper crashed for %s", url)
            info = None

        thumb_bytes: bytes | None = None
        if info is not None and info.thumbnail_url:
            thumb_bytes = await download_thumbnail_bytes(info.thumbnail_url)

        caption = self._build_youtube_caption(info) if info is not None else ""

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
            bot_username=YTDLP_PROVIDER,
            options=options,
            text=caption,
            preview=preview,
        )

    @staticmethod
    def _build_youtube_caption(info: YouTubeVideoInfo) -> str:
        lines: list[str] = []
        if info.title:
            lines.append(f"<b>{html.escape(info.title)}</b>")
        meta_parts: list[str] = []
        if info.channel:
            meta_parts.append(f"📺 {html.escape(info.channel)}")
        if info.duration > 0:
            meta_parts.append(f"⏱ {format_duration(info.duration)}")
        if meta_parts:
            lines.append(" • ".join(meta_parts))
        return "\n".join(lines)

    async def _select_youtube(
        self,
        url: str,
        payload: dict[str, Any],
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        mode = str(payload.get("mode", "video"))
        loop = asyncio.get_running_loop()
        progress_hook = _ProgressHook(progress_callback, loop)

        if mode == "audio":
            return await self._download_audio(
                url=url,
                payload=payload,
                attempt_directory=attempt_directory,
                progress_hook=progress_hook,
            )
        return await self._download_video(
            url=url,
            payload=payload,
            attempt_directory=attempt_directory,
            progress_hook=progress_hook,
        )

    # ------------------------------------------------------------------
    # Download implementations
    # ------------------------------------------------------------------
    def _base_opts(self, progress_hook: _ProgressHook) -> dict[str, Any]:
        """Build the yt-dlp options dict shared by all downloads."""
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "ignoreerrors": False,
            "cachedir": False,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "concurrent_fragment_downloads": 4,
            "buffersize": 16 * 1024,
            "progress_hooks": [progress_hook],
            "extractor_args": {
                "youtube": {
                    "player_client": list(self.player_clients),
                },
                "youtubepot-bgutilhttp": {
                    "base_url": self.bgutil_base_url,
                },
            },
        }
        if self.cookies_file is not None and self.cookies_file.is_file():
            opts["cookiefile"] = str(self.cookies_file)
            logger.debug("Using cookies file: %s", self.cookies_file)
        if self.proxy_url:
            opts["proxy"] = self.proxy_url
        return opts

    async def _download_video(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        attempt_directory: Path,
        progress_hook: _ProgressHook,
    ) -> GatewayResult:
        target_height = int(payload.get("height") or 1080)
        # yt-dlp format selector: best mp4 video ≤ target height + best m4a audio,
        # then merge via ffmpeg into a single mp4. We prefer mp4/m4a for max
        # compatibility with Telegram + ffmpeg copy (no re-encode).
        format_selector = (
            f"bestvideo[height<={target_height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={target_height}]+bestaudio"
            f"/best[height<={target_height}][ext=mp4]"
            f"/best[height<={target_height}]"
            f"/best"
        )
        outtmpl = str(attempt_directory / "ytdlp_%(id)s.%(ext)s")
        merge_output = attempt_directory / f"ytdlp_merged_{target_height}p.mp4"

        opts = self._base_opts(progress_hook)
        opts.update(
            {
                "format": format_selector,
                "outtmpl": outtmpl,
                "merge_output_format": "mp4",
                "postprocessors": [
                    {
                        "key": "FFmpegVideoConvertor",
                        "preferedformat": "mp4",
                    }
                ],
            }
        )

        try:
            info = await asyncio.to_thread(self._extract_and_download, opts, url)
        except Exception as exc:
            logger.warning("yt-dlp video download failed for %s: %s", url, exc)
            return GatewayResult(
                status="error",
                bot_username=YTDLP_PROVIDER,
                reason=self._map_ytdlp_error(exc),
            )

        # Find the final file. yt-dlp may have produced either the merged mp4
        # directly (when merge_output_format worked) or left separate video/audio
        # files for us to merge. Look for the merged file first; if missing,
        # try to find a single .mp4 with the largest size.
        final_path = self._find_final_video_path(attempt_directory, target_height)
        if final_path is None:
            logger.error("yt-dlp produced no output file in %s", attempt_directory)
            return GatewayResult(
                status="error",
                bot_username=YTDLP_PROVIDER,
                reason="ytdlp_empty",
            )

        if self.max_download_size > 0 and final_path.stat().st_size > self.max_download_size:
            try:
                final_path.unlink()
            except OSError:
                pass
            raise DownloadTooLarge("yt-dlp output exceeds MAX_DOWNLOAD_SIZE_MB")

        # Clean up part files (anything that isn't the final output or the thumb).
        self._cleanup_part_files(attempt_directory, keep={final_path.name, "_yt_thumb.jpg"})

        media = _build_media(final_path, MediaKind.VIDEO)
        return GatewayResult(
            status="ready",
            bot_username=YTDLP_PROVIDER,
            media=(media,),
        )

    async def _download_audio(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        attempt_directory: Path,
        progress_hook: _ProgressHook,
    ) -> GatewayResult:
        bitrate = str(payload.get("bitrate") or "128")
        # yt-dlp audio: bestaudio → extract to mp3 at the chosen bitrate.
        outtmpl = str(attempt_directory / "ytdlp_audio_%(id)s.%(ext)s")
        final_path = attempt_directory / f"ytdlp_audio_{bitrate}kbps.mp3"

        opts = self._base_opts(progress_hook)
        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": bitrate,
                    }
                ],
            }
        )

        try:
            await asyncio.to_thread(self._extract_and_download, opts, url)
        except Exception as exc:
            logger.warning("yt-dlp audio download failed for %s: %s", url, exc)
            return GatewayResult(
                status="error",
                bot_username=YTDLP_PROVIDER,
                reason=self._map_ytdlp_error(exc),
            )

        # yt-dlp's FFmpegExtractAudio post-processor names the output as
        # <base>.mp3 — find it.
        if not final_path.is_file():
            mp3_files = sorted(
                attempt_directory.glob("ytdlp_audio_*.mp3"),
                key=lambda p: p.stat().st_size,
                reverse=True,
            )
            if mp3_files:
                # Rename to the canonical name for predictable downstream handling.
                try:
                    if mp3_files[0] != final_path:
                        if final_path.exists():
                            final_path.unlink()
                        mp3_files[0].rename(final_path)
                except OSError:
                    final_path = mp3_files[0]
            else:
                logger.error("yt-dlp audio produced no mp3 in %s", attempt_directory)
                return GatewayResult(
                    status="error",
                    bot_username=YTDLP_PROVIDER,
                    reason="ytdlp_empty",
                )

        if self.max_download_size > 0 and final_path.stat().st_size > self.max_download_size:
            try:
                final_path.unlink()
            except OSError:
                pass
            raise DownloadTooLarge("yt-dlp audio output exceeds MAX_DOWNLOAD_SIZE_MB")

        self._cleanup_part_files(attempt_directory, keep={final_path.name, "_yt_thumb.jpg"})

        media = _build_media(final_path, MediaKind.AUDIO)
        return GatewayResult(
            status="ready",
            bot_username=YTDLP_PROVIDER,
            media=(media,),
        )

    # ------------------------------------------------------------------
    # yt-dlp wrapper (sync, called via asyncio.to_thread)
    # ------------------------------------------------------------------
    def _extract_and_download(self, opts: dict[str, Any], url: str) -> dict[str, Any]:
        """Run yt-dlp with the given options. Returns the info dict."""
        # Lazy import so the gateway module loads even if yt-dlp is missing.
        from yt_dlp import YoutubeDL

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        return info if isinstance(info, dict) else {}

    def _find_final_video_path(self, attempt_directory: Path, target_height: int) -> Path | None:
        """Locate the final video file produced by yt-dlp + ffmpeg.

        yt-dlp's `merge_output_format=mp4` + `FFmpegVideoConvertor` should
        produce a single .mp4 in the attempt directory. We pick the largest
        .mp4 (skipping tiny partials).
        """
        candidates: list[Path] = []
        for ext in ("mp4", "mkv", "webm"):
            candidates.extend(attempt_directory.glob(f"*.{ext}"))
        # Filter out the thumbnail and any obvious partial fragments.
        candidates = [
            p
            for p in candidates
            if not p.name.startswith("_") and p.name != "_yt_thumb.jpg"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        return candidates[0]

    def _cleanup_part_files(self, attempt_directory: Path, keep: set[str]) -> None:
        """Delete any file in attempt_directory whose name is not in `keep`."""
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
    def _map_ytdlp_error(exc: Exception) -> str:
        """Translate yt-dlp exceptions to MZ-Downloader reason strings."""
        msg = str(exc).lower()
        if "private" in msg:
            return "content_private"
        if "age" in msg or "restricted" in msg:
            return "content_age"
        if "live" in msg or "is streaming" in msg:
            return "content_live"
        if "unavailable" in msg or "removed" in msg or "not available" in msg:
            return "content_unavailable"
        if "rate" in msg or "too many requests" in msg or "429" in msg:
            return "rate_limited"
        if "sign in to confirm" in msg or "bot" in msg:
            return "auth_required"
        if "unsupported" in msg:
            return "unsupported_link"
        if "drm" in msg:
            return "drm_protected"
        if "format is not available" in msg or "no video formats" in msg:
            # yt-dlp returns this when YouTube bot detection blocked format
            # enumeration — surface as auth_required so the bot's failure_text
            # shows the "sign in required" message (which is what really happened).
            return "auth_required"
        return "ytdlp_error"


# ----------------------------------------------------------------------
# Connectivity probe (used by bot.post_init to disable the gateway if
# the bgutil HTTP server is unreachable).
# ----------------------------------------------------------------------


async def bgutil_health_check(base_url: str, *, timeout: float = 3.0) -> bool:
    """Quick connectivity probe for the bgutil PO Token HTTP server.

    The server exposes a `/ping` endpoint (and `GET /` returns 200) — we just
    GET the base URL and check for HTTP 200.
    """
    import httpx as _httpx

    base = base_url.rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(base + "/ping")
            if response.status_code == 200:
                return True
            # Some versions don't have /ping — try the root.
            response = await client.get(base + "/")
            return response.status_code == 200
    except Exception:
        return False
