"""CobaltGateway: bridges cobalt's HTTP API to MZ-Downloader's GatewayResult.

The existing bot flow expects two operations on a "gateway":
  * `request(url, attempt_directory, ...)` — submit a URL, get back either a
    ready media file, a menu of quality options, or an error.
  * `select(option, attempt_directory, ...)` — pick a quality option from a
    previously-shown menu and download the corresponding media.

CobaltGateway implements the same contract but talks to a self-hosted cobalt
API instead of a Telegram downloader bot. The trick we use to stay compatible
with the existing `QualityOption` dataclass is to encode cobalt-specific
selection parameters inside the `fingerprint` field as a JSON string with a
`cobalt:` prefix. That way the rest of the bot code (which only treats
`fingerprint` as an opaque token used for equality checks) keeps working
unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cobalt_client import CobaltClient, CobaltError, CobaltResponse, CobaltTunnel
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

logger = logging.getLogger("MZDownloader.cobalt_gateway")


# Sentinel "provider" name for cobalt. Bot code treats this as a regular bot
# username for routing purposes; the cobalt-aware branches key off this value.
COBALT_PROVIDER = "cobalt"

# Quality options offered for YouTube videos. The label is shown on the button.
# Format: (label, video_quality_str_or_None, download_mode, audio_bitrate_or_None)
YOUTUBE_QUALITIES: tuple[tuple[str, str | None, str, str | None], ...] = (
    ("360p", "360", "auto", None),
    ("480p", "480", "auto", None),
    ("720p", "720", "auto", None),
    ("1080p", "1080", "auto", None),
    ("1440p (2K)", "1440", "auto", None),
    ("2160p (4K)", "2160", "auto", None),
    ("MP3 128kbps", None, "audio", "128"),
    ("MP3 256kbps", None, "audio", "256"),
    ("MP3 320kbps", None, "audio", "320"),
)

# Filename sanitisation — strip characters Windows / Telegram dislike.
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", Path(name).name).strip(" .")
    return cleaned or "cobalt_media"


def _kind_from_cobalt(kind: str) -> MediaKind:
    if kind == "audio":
        return MediaKind.AUDIO
    if kind == "photo":
        return MediaKind.PHOTO
    if kind == "gif":
        return MediaKind.DOCUMENT
    return MediaKind.VIDEO


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


def _fingerprint(payload: dict[str, Any]) -> str:
    """Encode cobalt selection params into a QualityOption.fingerprint string.

    The fingerprint must be opaque to the rest of the bot — only CobaltGateway
    parses it back. We prefix with `cobalt:` so we can sanity-check on the way
    out.
    """
    return "cobalt:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("cobalt:"):
        return None
    try:
        return json.loads(fingerprint[len("cobalt:"):])
    except json.JSONDecodeError:
        return None


class CobaltGateway:
    """Cobalt-backed implementation of the gateway contract.

    Public methods mirror `DownloaderGateway` so the bot can treat both the
    same way, branching only on the `COBALT_PROVIDER` sentinel.
    """

    def __init__(
        self,
        *,
        client: CobaltClient,
        max_download_size: int,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self.client = client
        self.max_download_size = max_download_size
        self.ffmpeg_path = ffmpeg_path

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
        """Initial request — for YouTube this returns a quality menu, for
        Instagram it tries to download immediately (single photo/video/reel)
        or returns a picker menu for carousels.
        """
        try:
            if platform == Platform.YOUTUBE:
                return await self._request_youtube(url, attempt_directory, progress_callback)
            if platform == Platform.INSTAGRAM:
                return await self._request_instagram(url, attempt_directory, progress_callback)
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason="unsupported_platform",
            )
        except DownloadTooLarge:
            return GatewayResult(status="error", bot_username=COBALT_PROVIDER, reason="too_large")
        except CobaltError as exc:
            logger.warning("Cobalt request failed for %s: %s", url, exc)
            return GatewayResult(status="error", bot_username=COBALT_PROVIDER, reason="cobalt_error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Cobalt request crashed for %s: %s", url, exc)
            return GatewayResult(status="error", bot_username=COBALT_PROVIDER, reason="cobalt_error")

    async def select(
        self,
        *,
        url: str,
        platform: Platform,
        option: QualityOption,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> GatewayResult:
        """Handle a quality-selection click — make the right cobalt call."""
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None:
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason="invalid_fingerprint",
            )
        try:
            if platform == Platform.YOUTUBE:
                return await self._select_youtube(url, payload, attempt_directory, progress_callback)
            if platform == Platform.INSTAGRAM:
                return await self._select_instagram(url, payload, attempt_directory, progress_callback)
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason="unsupported_platform",
            )
        except DownloadTooLarge:
            return GatewayResult(status="error", bot_username=COBALT_PROVIDER, reason="too_large")
        except CobaltError as exc:
            logger.warning("Cobalt select failed for %s: %s", url, exc)
            return GatewayResult(status="error", bot_username=COBALT_PROVIDER, reason="cobalt_error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Cobalt select crashed for %s: %s", url, exc)
            return GatewayResult(status="error", bot_username=COBALT_PROVIDER, reason="cobalt_error")

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------
    async def _request_youtube(
        self,
        url: str,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        """YouTube: don't download yet, just present a quality menu.

        We don't even need to probe cobalt to know the available qualities —
        cobalt will gracefully fall back to the next-best quality if the
        requested one is unavailable, so we can always offer the full ladder.
        """
        options: list[QualityOption] = []
        for row_index, (label, quality, mode, bitrate) in enumerate(YOUTUBE_QUALITIES):
            if mode == "audio":
                expected_kind = MediaKind.AUDIO
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
                        expected_kind=expected_kind,
                        expected_bitrate_kbps=bitrate_int,
                        action="media",
                    )
                )
            else:
                expected_kind = MediaKind.VIDEO
                fingerprint = _fingerprint(
                    {
                        "mode": "auto",
                        "quality": quality or "1080",
                        "codec": "h264",
                    }
                )
                options.append(
                    QualityOption(
                        label=label,
                        row=row_index,
                        column=0,
                        fingerprint=fingerprint,
                        expected_kind=expected_kind,
                        expected_height=int(quality) if quality else 1080,
                        action="media",
                    )
                )
        return GatewayResult(
            status="needs_selection",
            bot_username=COBALT_PROVIDER,
            options=tuple(options),
            text="",
        )

    async def _select_youtube(
        self,
        url: str,
        payload: dict[str, Any],
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        mode = str(payload.get("mode", "auto"))
        request_payload: dict[str, Any] = {"url": url}
        if mode == "audio":
            request_payload["downloadMode"] = "audio"
            request_payload["audioFormat"] = str(payload.get("format", "mp3"))
            request_payload["audioBitrate"] = str(payload.get("bitrate", "128"))
        else:
            request_payload["downloadMode"] = "auto"
            request_payload["videoQuality"] = str(payload.get("quality", "1080"))
            request_payload["youtubeVideoCodec"] = str(payload.get("codec", "h264"))
            # Let cobalt pick the right container for the codec
            # (h264→mp4, vp9→webm, av1→webm). Forcing mp4 with vp9 fails.
            request_payload["youtubeVideoContainer"] = "auto"
        response = await self.client.request(request_payload)
        return await self._materialize(response, attempt_directory, progress_callback)

    # ------------------------------------------------------------------
    # Instagram
    # ------------------------------------------------------------------
    async def _request_instagram(
        self,
        url: str,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        """Instagram: probe cobalt. If it returns a single file, download it
        immediately. If it returns a picker (carousel), present a selection
        menu. If it returns local-processing or an error, fall back.
        """
        request_payload = {"url": url, "alwaysProxy": True}
        response = await self.client.request(request_payload)
        if response.status == "error":
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason=_map_error(response.error_code),
            )
        if response.status == "picker" and response.tunnels:
            return self._build_instagram_picker(response)
        # Single file (tunnel/redirect/local-processing) — download now.
        return await self._materialize(response, attempt_directory, progress_callback)

    def _build_instagram_picker(self, response: CobaltResponse) -> GatewayResult:
        options: list[QualityOption] = []
        for index, tunnel in enumerate(response.tunnels):
            kind_label = "ویدیو" if tunnel.kind == "video" else (
                "عکس" if tunnel.kind == "photo" else "گیف"
            )
            label = f"{kind_label} {index + 1}"
            fingerprint = _fingerprint(
                {
                    "picker": True,
                    "url": tunnel.url,
                    "kind": tunnel.kind,
                    "filename": tunnel.filename,
                    "index": index,
                }
            )
            expected_kind = _kind_from_cobalt(tunnel.kind)
            options.append(
                QualityOption(
                    label=label,
                    row=index,
                    column=0,
                    fingerprint=fingerprint,
                    expected_kind=expected_kind,
                    action="media",
                )
            )
        return GatewayResult(
            status="needs_selection",
            bot_username=COBALT_PROVIDER,
            options=tuple(options),
            text="",
        )

    async def _select_instagram(
        self,
        url: str,
        payload: dict[str, Any],
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        if payload.get("picker"):
            # Direct download from the picker URL cobalt returned earlier.
            tunnel_url = str(payload.get("url", "") or "")
            filename = str(payload.get("filename", "") or "cobalt_media")
            kind = str(payload.get("kind", "") or "video")
            if not tunnel_url:
                return GatewayResult(
                    status="error",
                    bot_username=COBALT_PROVIDER,
                    reason="invalid_fingerprint",
                )
            path = await self.client.download_to_file(
                tunnel_url,
                attempt_directory,
                filename=filename,
                max_size=self.max_download_size,
                progress_callback=progress_callback,
            )
            media = _build_media(path, kind)
            return GatewayResult(
                status="ready",
                bot_username=COBALT_PROVIDER,
                media=(media,),
            )
        # Otherwise treat as a fresh request (e.g. user requested audio only).
        request_payload = {"url": url, "alwaysProxy": True}
        if payload.get("mode") == "audio":
            request_payload["downloadMode"] = "audio"
            request_payload["audioFormat"] = str(payload.get("format", "mp3"))
            request_payload["audioBitrate"] = str(payload.get("bitrate", "128"))
        response = await self.client.request(request_payload)
        return await self._materialize(response, attempt_directory, progress_callback)

    # ------------------------------------------------------------------
    # Materialization (cobalt response → GatewayResult with files on disk)
    # ------------------------------------------------------------------
    async def _materialize(
        self,
        response: CobaltResponse,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        if response.status == "error":
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason=_map_error(response.error_code),
            )
        if not response.tunnels:
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason="cobalt_empty",
            )
        # Local-processing: download each tunnel and merge with ffmpeg.
        if response.status == "local-processing":
            return await self._materialize_local(response, attempt_directory, progress_callback)
        # Tunnel / redirect / picker-as-single (shouldn't happen here, but handle it).
        downloaded: list[DownloadedMedia] = []
        total = len(response.tunnels)
        for index, tunnel in enumerate(response.tunnels):
            if progress_callback is not None:
                await progress_callback(index, total)
            path = await self.client.download_to_file(
                tunnel.url,
                attempt_directory,
                filename=tunnel.filename,
                max_size=self.max_download_size,
                progress_callback=progress_callback,
            )
            downloaded.append(_build_media(path, tunnel.kind))
        return GatewayResult(
            status="ready",
            bot_username=COBALT_PROVIDER,
            media=tuple(downloaded),
        )

    async def _materialize_local(
        self,
        response: CobaltResponse,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None,
    ) -> GatewayResult:
        """Local-processing: cobalt returned multiple tunnel URLs that must be
        combined client-side via ffmpeg.

        Cobalt's `type` field tells us what to do:
          * merge: combine video + audio into a single mp4/mkv.
          * mute: video only (no audio) — just take the video stream.
          * audio: audio only (optionally with cover art).
          * gif: video converted to gif.
          * remux: repackage without re-encoding.

        We download each tunnel, run ffmpeg, and return the merged file.
        """
        if not response.tunnels:
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason="cobalt_empty",
            )
        merge_type = response.merge_type or "merge"
        output_filename = _sanitize_filename(
            response.output_filename or "cobalt_local.mp4"
        )
        # Download each tunnel to a temp file.
        downloaded_paths: list[Path] = []
        for index, tunnel in enumerate(response.tunnels):
            if progress_callback is not None:
                await progress_callback(index, len(response.tunnels) + 1)
            path = await self.client.download_to_file(
                tunnel.url,
                attempt_directory,
                filename=f"_part_{index + 1}.bin",
                max_size=self.max_download_size,
                progress_callback=progress_callback,
            )
            downloaded_paths.append(path)
        if progress_callback is not None:
            await progress_callback(len(response.tunnels), len(response.tunnels) + 1)
        output_path = attempt_directory / output_filename
        try:
            await asyncio.to_thread(
                _run_ffmpeg_merge,
                self.ffmpeg_path,
                downloaded_paths,
                output_path,
                merge_type,
            )
        except subprocess.CalledProcessError as exc:
            logger.error("ffmpeg merge failed: %s\nstdout=%s\nstderr=%s",
                         exc, exc.stdout, exc.stderr)
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason="ffmpeg_failed",
            )
        # Clean up the part files — we only ship the merged output.
        for path in downloaded_paths:
            with contextlib_suppress(FileNotFoundError):
                path.unlink()
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            return GatewayResult(
                status="error",
                bot_username=COBALT_PROVIDER,
                reason="ffmpeg_empty",
            )
        if self.max_download_size > 0 and output_path.stat().st_size > self.max_download_size:
            raise DownloadTooLarge("Merged cobalt media exceeds MAX_DOWNLOAD_SIZE_MB")
        # Infer the media kind from the merge type / output filename.
        if merge_type == "audio":
            kind = "audio"
        elif output_path.suffix.lower() in {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"}:
            kind = "audio"
        elif output_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            kind = "photo"
        else:
            kind = "video"
        media = _build_media(output_path, kind)
        return GatewayResult(
            status="ready",
            bot_username=COBALT_PROVIDER,
            media=(media,),
        )


# ----------------------------------------------------------------------
# Helpers (module-level so they can be reused / monkey-patched in tests)
# ----------------------------------------------------------------------

def _build_media(path: Path, kind: str) -> DownloadedMedia:
    media_kind = _kind_from_cobalt(kind)
    suffix = path.suffix.lower()
    mime = _mime_for_kind(media_kind, suffix)
    return DownloadedMedia(
        path=path,
        kind=media_kind,
        source_message_id=0,
        mime_type=mime,
        size=path.stat().st_size,
    )


def _map_error(error_code: str) -> str:
    """Translate cobalt error codes into MZ-Downloader reason strings.

    The bot's failure_text() function looks at the reason strings to compose
    a user-facing error message. We map cobalt's specific codes to the closest
    existing reason, falling back to cobalt_error for anything unknown.
    """
    code = (error_code or "").lower()
    if "private" in code:
        return "content_private"
    if "age" in code or "restricted" in code:
        return "content_age"
    if "too_long" in code or "duration" in code:
        return "content_too_long"
    if "live" in code:
        return "content_live"
    if "rate" in code:
        return "rate_limited"
    if "unavailable" in code or "fetch.empty" in code:
        return "content_unavailable"
    if "unsupported" in code:
        return "unsupported_link"
    if "drm" in code:
        return "drm_protected"
    if "login" in code or "token_expired" in code:
        return "auth_required"
    return "cobalt_error"


def _run_ffmpeg_merge(
    ffmpeg_path: str,
    parts: list[Path],
    output: Path,
    merge_type: str,
) -> None:
    """Run ffmpeg to combine downloaded cobalt tunnel parts.

    For `merge`: video + audio → single mp4 (default cobalt case).
    For `mute`: just take the video stream, no audio.
    For `audio`: audio file (optionally with cover art, but we skip that).
    For `gif` / `remux`: re-encode or remux the single input.
    """
    if not parts:
        raise InvalidDownload("No parts to merge")
    output.parent.mkdir(parents=True, exist_ok=True)
    if merge_type == "merge" and len(parts) >= 2:
        # video is parts[0], audio is parts[1] — cobalt's documented order.
        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(parts[0]),
            "-i", str(parts[1]),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
    elif merge_type == "audio" and len(parts) >= 2:
        # audio + cover image — just take the audio.
        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(parts[0]),
            "-c", "copy",
            str(output),
        ]
    else:
        # Single-part remux / mute / gif — just remux the input.
        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(parts[0]),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
    logger.info("Running ffmpeg: %s", " ".join(cmd))
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
        if merge_type == "merge" and len(parts) >= 2:
            fallback = [
                ffmpeg_path,
                "-y",
                "-i", str(parts[0]),
                "-i", str(parts[1]),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output),
            ]
        else:
            fallback = [
                ffmpeg_path,
                "-y",
                "-i", str(parts[0]),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output),
            ]
        result = subprocess.run(
            fallback,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


class contextlib_suppress:
    """Tiny stand-in for `contextlib.suppress` to avoid an extra import
    in the merge hot-path. Keeps tracebacks cleaner for FileNotFoundError.
    """

    def __init__(self, *exceptions: type[BaseException]) -> None:
        self.exceptions = exceptions

    def __enter__(self) -> "contextlib_suppress":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, self.exceptions)
