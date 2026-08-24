"""SocialSitesGateway — multi-platform scraper gateway.

Supported platforms
-------------------
  - TikTok      → tikwm.com (free public API, returns direct CDN URLs)
  - SoundCloud  → yt-dlp (works without auth)
  - Instagram   → yt-dlp with optional cookies (falls back to Telegram
                  bots via the `instagram_no_cookies` reason if absent)
  - Pinterest   → yt-dlp with optional cookies
  - Twitter/X   → yt-dlp with optional cookies
  - Facebook    → yt-dlp with optional cookies
  - Generic     → yt-dlp for anything else it supports (Vimeo,
                  Dailymotion, Streamable, Reddit, Twitch, …)

Why yt-dlp
----------
The youtube_sites_gateway scrapes the loader.to / savenow.to backend,
which works perfectly for YouTube but FAILS for TikTok, SoundCloud, and
Instagram. Sister frontends (downcloud.cc / downtik.to / igdown.io) are
just branded skins over the same broken backend. So we use real working
backends per platform: tikwm.com for TikTok, yt-dlp for everything else.

API contract with the bot
-------------------------
This gateway mirrors YouTubeSitesGateway: `request()` returns a quality
menu (needs_selection) or a ready file; `select()` performs the actual
download. Both return `GatewayResult` (defined in downloader.py).

The bot routes by platform — it calls SocialSitesGateway for TikTok /
SoundCloud / Instagram / Pinterest / Twitter / Facebook / generic
yt-dlp URLs, and YouTubeSitesGateway for YouTube. If SocialSitesGateway
returns `status='error'` with `reason='unsupported'` or `reason='no_cookies'`,
the bot falls back to Telegram downloader bots (the existing
`providers_for_platform` path).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import shutil
import urllib.parse
from dataclasses import dataclass
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

logger = logging.getLogger("MZDownloader.social_sites")


# Sentinel "provider" name — the bot's routing layer treats this like a
# Telegram bot username but the social-aware branches key off it.
SOCIAL_PROVIDER = "social"


# A "processing" callback is invoked during the server-side extraction
# phase (before the CDN download URL is ready). Same contract as in
# youtube_sites_gateway.
ProcessingCallback = Callable[[int, str, str], Awaitable[None]]


_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", Path(name).name).strip(" .")
    return cleaned or "media"


def _fingerprint(payload: dict[str, Any]) -> str:
    return "social:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("social:"):
        return None
    try:
        return json.loads(fingerprint[len("social:"):])
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
        if s == ".flac":
            return "audio/flac"
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


def _extract_meta_property(html_text: str, key: str) -> str | None:
    """Extract a <meta property="..."> content value from HTML."""
    key_lower = key.lower()
    patterns = (
        re.compile(
            r'<meta\s+(?:property|name)=["\'](?P<key>[^"\']+)["\']\s+content=["\'](?P<val>[^"\']*)["\']',
            re.IGNORECASE,
        ),
        re.compile(
            r'<meta\s+content=["\'](?P<val>[^"\']*)["\']\s+(?:property|name)=["\'](?P<key>[^"\']+)["\']',
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(html_text):
            if match.group("key").lower() == key_lower:
                value = match.group("val").strip()
                if value:
                    return value
    return None


async def _fetch_og_meta(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None]:
    """Fetch og:image and og:title from a URL (best-effort, 8s timeout)."""
    try:
        resp = await client.get(
            url,
            timeout=8.0,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        if resp.status_code != 200:
            return None, None
        text = resp.text[:50_000]
    except Exception as exc:
        logger.debug("OG meta fetch failed for %s: %s", url, exc)
        return None, None
    image = _extract_meta_property(text, "og:image") or _extract_meta_property(text, "twitter:image")
    title = _extract_meta_property(text, "og:title") or _extract_meta_property(text, "twitter:title")
    return image, title


# ----------------------------------------------------------------------
# Quality menus per platform
# ----------------------------------------------------------------------

# Each entry: (label, video_height_or_None, mode, audio_bitrate_or_None, kind)
# `kind` is what we pass to the platform-specific downloader.
TIKTOK_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("ویدیو بدون واترمارک", 1080, "video", None, "nowm"),
    ("ویدیو با واترمارک",   1080, "video", None, "wm"),
    ("صدا (MP3)",          None, "audio", "128", "music"),
)

SOUNDCLOUD_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("MP3 128kbps",        None, "audio", "128",   "mp3"),
    ("M4A (AAC)",          None, "audio", "256",   "m4a"),
    ("WAV (lossless)",     None, "audio", "1411",  "wav"),
    ("FLAC (lossless)",    None, "audio", "1411",  "flac"),
    ("OGG (Opus)",         None, "audio", "160",   "opus"),
)

INSTAGRAM_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("ویدیو (MP4)",        1080, "video", None, "video"),
    ("صدا (MP3)",          None, "audio", "128", "audio"),
)

PINTEREST_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("ویدیو (بهترین کیفیت)", 1080, "video", None, "video"),
    ("صدا (MP3)",            None, "audio", "128", "audio"),
)

TWITTER_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("ویدیو (بهترین کیفیت)", 1080, "video", None, "video"),
    ("صدا (MP3)",            None, "audio", "128", "audio"),
)

FACEBOOK_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("ویدیو (HD)",   1080, "video", None, "video"),
    ("ویدیو (SD)",    480, "video", None, "video_sd"),
    ("صدا (MP3)",    None, "audio", "128", "audio"),
)

# Generic yt-dlp quality menu — used for Vimeo, Dailymotion, Streamable,
# Reddit, Twitch, and any other URL yt-dlp supports.
GENERIC_QUALITIES: tuple[tuple[str, int | None, str, str | None, str], ...] = (
    ("ویدیو (بهترین کیفیت)", 1080, "video", None, "video"),
    ("ویدیو ۷۲۰p",            720, "video", None, "video_720"),
    ("ویدیو ۴۸۰p",            480, "video", None, "video_480"),
    ("صدا (MP3)",            None, "audio", "128", "audio"),
)


# Platforms whose yt-dlp extractor benefits from cookies. The shared
# cookies.txt is offered to all of them; if it doesn't contain session
# cookies for that platform yt-dlp will still try and may fail, after
# which the bot falls back to Telegram downloader bots.
_PLATFORMS_WANTING_COOKIES = frozenset({
    "instagram", "pinterest", "twitter", "facebook", "ytdlp_generic",
})


# ----------------------------------------------------------------------
# TikTok via tikwm.com
# ----------------------------------------------------------------------

TIKWM_API = "https://www.tikwm.com/api/"


async def _tiktok_fetch(url: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Call tikwm.com and return the data dict, or None on failure."""
    try:
        resp = await client.post(
            TIKWM_API,
            data={"url": url, "hd": "1"},
            timeout=20.0,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("tikwm.com network error: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("tikwm.com HTTP %s", resp.status_code)
        return None
    try:
        data = resp.json()
    except Exception:
        logger.warning("tikwm.com non-JSON response")
        return None
    if data.get("code") != 0 or not data.get("data"):
        logger.warning("tikwm.com error: code=%s msg=%s", data.get("code"), data.get("msg"))
        return None
    return data["data"]


async def _tiktok_download(
    *,
    url: str,
    kind: str,
    attempt_directory: Path,
    progress_callback: ProgressCallback | None,
) -> GatewayResult:
    """Download a TikTok video / audio via tikwm.com.

    `kind` is one of: 'nowm' (no watermark), 'wm' (with watermark), 'music' (audio).
    """
    async with httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        },
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    ) as client:
        data = await _tiktok_fetch(url, client)
        if data is None:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="tiktok_fetch_failed",
            )

        # Pick the URL based on kind.
        if kind == "nowm":
            download_url = data.get("play") or ""
            media_kind = MediaKind.VIDEO
            suffix = ".mp4"
            label_suffix = "no-watermark"
        elif kind == "wm":
            download_url = data.get("wmplay") or data.get("play") or ""
            media_kind = MediaKind.VIDEO
            suffix = ".mp4"
            label_suffix = "watermark"
        elif kind == "music":
            download_url = data.get("music") or ""
            media_kind = MediaKind.AUDIO
            suffix = ".mp3"
            label_suffix = "audio"
        else:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="tiktok_unknown_kind",
            )

        if not download_url.startswith("http"):
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="tiktok_no_url",
            )

        # Save the cover image too (for the caption preview) — best effort.
        cover_url = data.get("origin_cover") or data.get("cover") or ""
        cover_path: Path | None = None
        if cover_url:
            try:
                cover_resp = await client.get(cover_url, timeout=10.0)
                if cover_resp.status_code == 200:
                    cover_path = attempt_directory / "_tt_cover.jpg"
                    cover_path.write_bytes(cover_resp.content)
            except Exception as exc:
                logger.debug("cover fetch failed: %s", exc)

        # Download the actual media file.
        author = data.get("author") or {}
        author_id = author.get("unique_id") or ""
        title_text = (data.get("title") or "").strip() or "tiktok"
        safe_name = _sanitize_filename(f"{author_id}_{title_text}"[:120]) if author_id else _sanitize_filename(title_text[:120])
        final_path = attempt_directory / f"tiktok_{safe_name}_{label_suffix}{suffix}"
        try:
            await _http_download(
                client, download_url, final_path, progress_callback,
            )
        except DownloadTooLarge:
            raise
        except InvalidDownload as exc:
            logger.warning("TikTok CDN download failed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="tiktok_cdn_error",
            )
        except Exception as exc:
            logger.warning("TikTok CDN download crashed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="tiktok_cdn_error",
            )

        # Clean up part files (anything that isn't the final output or the cover).
        keep = {final_path.name}
        if cover_path is not None:
            keep.add(cover_path.name)
        _cleanup_part_files(attempt_directory, keep)
        media = _build_media(final_path, media_kind)
        return GatewayResult(
            status="ready",
            bot_username=SOCIAL_PROVIDER,
            media=(media,),
        )


# ----------------------------------------------------------------------
# SoundCloud / Instagram via yt-dlp
# ----------------------------------------------------------------------

def _run_yt_dlp_sync(
    url: str,
    *,
    out_path: Path,
    format_spec: str,
    cookies_path: Path | None = None,
    extract_audio: bool = False,
    audio_format: str | None = None,
    audio_quality: str | None = None,
    max_download_size: int = 0,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run yt-dlp synchronously and return its info_dict.

    Raises `InvalidDownload` on yt-dlp errors.

    Speed knobs:
      - `concurrent_fragment_downloads=4` — DASH/HLS streams download in
        parallel fragments (4x faster for big videos).
      - `buffersize=1MiB` — bigger I/O buffer for less syscall overhead.
      - `retries=3` + `fragment_retries=3` — quick retry on transient
        CDN hiccups instead of failing immediately.
    """
    # Lazy import so the module loads even if yt-dlp isn't installed.
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError  # type: ignore

    opts: dict[str, Any] = {
        "outtmpl": str(out_path),
        "format": format_spec,
        "noprogress": True,
        "no_warnings": True,
        "quiet": True,
        "restrictfilenames": True,
        "noplaylist": True,
        # Skip sponsor blocks etc. — we want the raw file.
        "postprocessors": [],
        # Speed: download fragments in parallel.
        "concurrent_fragment_downloads": 4,
        # Bigger I/O buffer — less syscall overhead on large files.
        "buffersize": 1024 * 1024,
        # Quick retry on transient CDN errors.
        "retries": 3,
        "fragment_retries": 3,
        # Don't abort the whole playlist if one entry fails (defensive —
        # we always pass noplaylist=True so this is a no-op for single
        # URLs, but keeps behavior sane if yt-dlp returns a playlist).
        "ignoreerrors": False,
    }
    if max_download_size > 0:
        opts["max_filesize"] = max_download_size
    if extract_audio:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format or "mp3",
            "preferredquality": audio_quality or "0",
        }]
        opts["format"] = "bestaudio/best"
    if cookies_path is not None:
        opts["cookiefile"] = str(cookies_path)
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]
    # Use a sane UA + impersonation (if available) to dodge bot detection.
    opts["http_headers"] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    # Try impersonation if curl_cffi is available.
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget(client="chrome")
    except Exception:
        try:
            import curl_cffi  # noqa: F401
            opts["impersonate"] = "chrome"
        except ImportError:
            pass

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info or {}
    except DownloadError as exc:
        raise InvalidDownload(f"yt-dlp failed: {exc}") from exc


async def _run_yt_dlp(
    url: str,
    *,
    out_path: Path,
    format_spec: str,
    cookies_path: Path | None = None,
    extract_audio: bool = False,
    audio_format: str | None = None,
    audio_quality: str | None = None,
    max_download_size: int = 0,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Async wrapper around _run_yt_dlp_sync — runs in a thread."""
    return await asyncio.to_thread(
        _run_yt_dlp_sync,
        url,
        out_path=out_path,
        format_spec=format_spec,
        cookies_path=cookies_path,
        extract_audio=extract_audio,
        audio_format=audio_format,
        audio_quality=audio_quality,
        max_download_size=max_download_size,
        progress_hook=progress_hook,
    )


async def _soundcloud_download(
    *,
    url: str,
    kind: str,
    attempt_directory: Path,
    progress_callback: ProgressCallback | None,
    cookies_path: Path | None = None,
) -> GatewayResult:
    """Download a SoundCloud track via yt-dlp.

    `kind` is one of: 'mp3', 'm4a', 'wav', 'flac', 'opus'.
    """
    out_path = attempt_directory / f"soundcloud_%(id)s.{kind}"
    try:
        info = await _run_yt_dlp(
            url,
            out_path=out_path,
            format_spec="bestaudio/best",
            cookies_path=cookies_path,
            extract_audio=True,
            audio_format=kind,
            audio_quality="0",
        )
    except InvalidDownload as exc:
        logger.warning("SoundCloud yt-dlp failed: %s", exc)
        return GatewayResult(
            status="error",
            bot_username=SOCIAL_PROVIDER,
            reason="soundcloud_extract_failed",
        )
    except Exception as exc:
        logger.warning("SoundCloud yt-dlp crashed: %s", exc)
        return GatewayResult(
            status="error",
            bot_username=SOCIAL_PROVIDER,
            reason="soundcloud_extract_failed",
        )
    # yt-dlp writes the file to out_path with the actual extension.
    # Find the file we just downloaded.
    candidates = sorted(attempt_directory.glob(f"soundcloud_*.{kind}"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        # The outtmpl may have replaced the extension — check for any soundcloud_* file.
        candidates = sorted(attempt_directory.glob("soundcloud_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return GatewayResult(
            status="error",
            bot_username=SOCIAL_PROVIDER,
            reason="soundcloud_no_file",
        )
    final_path = candidates[0]
    media = _build_media(final_path, MediaKind.AUDIO)
    _cleanup_part_files(attempt_directory, keep={final_path.name})
    return GatewayResult(
        status="ready",
        bot_username=SOCIAL_PROVIDER,
        media=(media,),
    )


async def _instagram_download(
    *,
    url: str,
    kind: str,
    attempt_directory: Path,
    progress_callback: ProgressCallback | None,
    cookies_path: Path | None = None,
) -> GatewayResult:
    """Download an Instagram reel / post via yt-dlp.

    `kind` is one of: 'video', 'audio'.
    Requires cookies (yt-dlp can't access Instagram without auth).
    """
    if cookies_path is None or not cookies_path.exists():
        return GatewayResult(
            status="error",
            bot_username=SOCIAL_PROVIDER,
            reason="instagram_no_cookies",
        )
    if kind == "audio":
        out_path = attempt_directory / "instagram_%(id)s.mp3"
        try:
            info = await _run_yt_dlp(
                url,
                out_path=out_path,
                format_spec="bestaudio/best",
                cookies_path=cookies_path,
                extract_audio=True,
                audio_format="mp3",
                audio_quality="0",
            )
        except InvalidDownload as exc:
            logger.warning("Instagram audio yt-dlp failed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="instagram_extract_failed",
            )
        candidates = sorted(attempt_directory.glob("instagram_*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="instagram_no_file",
            )
        final_path = candidates[0]
        media = _build_media(final_path, MediaKind.AUDIO)
    else:
        out_path = attempt_directory / "instagram_%(id)s.mp4"
        try:
            info = await _run_yt_dlp(
                url,
                out_path=out_path,
                format_spec="best[ext=mp4]/best",
                cookies_path=cookies_path,
            )
        except InvalidDownload as exc:
            logger.warning("Instagram video yt-dlp failed: %s", exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="instagram_extract_failed",
            )
        candidates = sorted(attempt_directory.glob("instagram_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            # yt-dlp may have produced a different extension; fall back to any instagram_* file.
            candidates = sorted(attempt_directory.glob("instagram_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="instagram_no_file",
            )
        final_path = candidates[0]
        media = _build_media(final_path, MediaKind.VIDEO)
    _cleanup_part_files(attempt_directory, keep={final_path.name})
    return GatewayResult(
        status="ready",
        bot_username=SOCIAL_PROVIDER,
        media=(media,),
    )


# ----------------------------------------------------------------------
# HTTP download helper
# ----------------------------------------------------------------------


async def _http_download(
    client: httpx.AsyncClient,
    download_url: str,
    target_path: Path,
    progress_callback: ProgressCallback | None,
    *,
    max_download_size: int = 0,
) -> None:
    """Stream-download a URL to a file with progress reporting."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
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
        max_download_size > 0
        and content_length > 0
        and content_length > max_download_size
    ):
        raise DownloadTooLarge("Output exceeds MAX_DOWNLOAD_SIZE_MB")
    # Stream the body.
    total_bytes = 0
    last_report = 0.0
    async with client.stream("GET", download_url, timeout=cdn_timeout) as stream:
        if stream.status_code != 200:
            raise InvalidDownload(f"CDN returned HTTP {stream.status_code}")
        actual_length = int(stream.headers.get("content-length") or content_length or 0)
        if (
            max_download_size > 0
            and actual_length > 0
            and actual_length > max_download_size
        ):
            raise DownloadTooLarge("Output exceeds MAX_DOWNLOAD_SIZE_MB")
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
        raise InvalidDownload("CDN returned an empty file")
    if (
        max_download_size > 0
        and total_bytes > max_download_size
    ):
        try:
            target_path.unlink()
        except OSError:
            pass
        raise DownloadTooLarge("Output exceeds MAX_DOWNLOAD_SIZE_MB")
    if progress_callback is not None:
        try:
            await progress_callback(total_bytes, actual_length or total_bytes)
        except Exception:
            pass


def _cleanup_part_files(attempt_directory: Path, keep: set[str]) -> None:
    for path in attempt_directory.iterdir():
        if path.is_dir():
            continue
        if path.name in keep:
            continue
        try:
            path.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------
# Generic yt-dlp downloader (Pinterest / Twitter / Facebook / generic)
# ----------------------------------------------------------------------


# Map quality `kind` to a yt-dlp format string. `kind` values are the
# fifth element of each *_QUALITIES tuple entry.
_FORMAT_SPECS: dict[str, str] = {
    # Video — best MP4
    "video": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
    # Video at 720p
    "video_720": (
        "best[height<=720][ext=mp4]/"
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
        "best[height<=720]/best"
    ),
    # Video at 480p
    "video_480": (
        "best[height<=480][ext=mp4]/"
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
        "best[height<=480]/best"
    ),
    # Facebook SD (lower-bitrate variant)
    "video_sd": (
        "best[height<=480]/bestvideo[height<=480]+bestaudio/"
        "worst[ext=mp4]/best"
    ),
}


async def _ytdlp_generic_download(
    *,
    url: str,
    kind: str,
    file_prefix: str,
    attempt_directory: Path,
    progress_callback: ProgressCallback | None,
    cookies_path: Path | None = None,
    max_download_size: int = 0,
    reason_prefix: str = "ytdlp",
) -> GatewayResult:
    """Generic yt-dlp downloader used by Pinterest / Twitter / Facebook /
    generic yt-dlp URLs.

    `kind` is one of: 'video', 'video_720', 'video_480', 'video_sd', 'audio'.
    `file_prefix` is used to name the output file (e.g. 'pinterest',
    'twitter', 'facebook', 'generic').
    `reason_prefix` is used in error reason codes for logging clarity
    (e.g. 'pinterest_extract_failed', 'twitter_no_file').
    """
    is_audio = kind == "audio"

    if is_audio:
        out_path = attempt_directory / f"{file_prefix}_%(id)s.mp3"
        format_spec = "bestaudio/best"
        try:
            await _run_yt_dlp(
                url,
                out_path=out_path,
                format_spec=format_spec,
                cookies_path=cookies_path,
                extract_audio=True,
                audio_format="mp3",
                audio_quality="0",
                max_download_size=max_download_size,
            )
        except InvalidDownload as exc:
            logger.warning("%s audio yt-dlp failed: %s", file_prefix, exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason=f"{reason_prefix}_extract_failed",
            )
        except Exception as exc:
            logger.warning("%s audio yt-dlp crashed: %s", file_prefix, exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason=f"{reason_prefix}_extract_failed",
            )
        # Find the produced MP3
        candidates = sorted(
            attempt_directory.glob(f"{file_prefix}_*.mp3"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            candidates = sorted(
                attempt_directory.glob(f"{file_prefix}_*"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
        if not candidates:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason=f"{reason_prefix}_no_file",
            )
        final_path = candidates[0]
        media = _build_media(final_path, MediaKind.AUDIO)
    else:
        # Pick the format spec for the requested kind.
        format_spec = _FORMAT_SPECS.get(kind, _FORMAT_SPECS["video"])
        out_path = attempt_directory / f"{file_prefix}_%(id)s.mp4"
        try:
            info = await _run_yt_dlp(
                url,
                out_path=out_path,
                format_spec=format_spec,
                cookies_path=cookies_path,
                max_download_size=max_download_size,
            )
        except InvalidDownload as exc:
            logger.warning("%s video yt-dlp failed: %s", file_prefix, exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason=f"{reason_prefix}_extract_failed",
            )
        except Exception as exc:
            logger.warning("%s video yt-dlp crashed: %s", file_prefix, exc)
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason=f"{reason_prefix}_extract_failed",
            )
        # yt-dlp may produce a different container if MP4 isn't available
        # (e.g. .webm). Fall back to any file matching the prefix.
        candidates = sorted(
            attempt_directory.glob(f"{file_prefix}_*.mp4"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            candidates = sorted(
                attempt_directory.glob(f"{file_prefix}_*"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
        if not candidates:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason=f"{reason_prefix}_no_file",
            )
        final_path = candidates[0]
        media = _build_media(final_path, MediaKind.VIDEO)

    _cleanup_part_files(attempt_directory, keep={final_path.name})
    return GatewayResult(
        status="ready",
        bot_username=SOCIAL_PROVIDER,
        media=(media,),
    )


# ----------------------------------------------------------------------
# Gateway class
# ----------------------------------------------------------------------


class SocialSitesError(RuntimeError):
    """Raised when a social-platform download fails."""


class SocialSitesGateway:
    """Multi-platform scraper gateway.

    Supported platforms: TikTok (tikwm.com), SoundCloud (yt-dlp, no
    cookies), Instagram (yt-dlp + cookies), Pinterest / Twitter / Facebook
    / generic (yt-dlp + optional cookies).
    """

    def __init__(
        self,
        *,
        instagram_cookies_path: Path | None = None,
        max_download_size: int = 0,
        http_timeout: float = 30.0,
    ) -> None:
        # Single shared cookies path used for every yt-dlp-backed platform.
        # Instagram historically required this file; Pinterest / Twitter /
        # Facebook will use it opportunistically (if the file contains
        # session cookies for them, yt-dlp will succeed; otherwise it will
        # fail and the bot falls back to Telegram downloader bots).
        self.instagram_cookies_path = instagram_cookies_path
        self.max_download_size = max_download_size
        self.http_timeout = http_timeout

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
        """Initial request — returns a quality menu for the platform."""
        try:
            return await self._request_platform(url, platform, attempt_directory)
        except DownloadTooLarge:
            return GatewayResult(
                status="error", bot_username=SOCIAL_PROVIDER, reason="too_large"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("SocialSitesGateway request crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error", bot_username=SOCIAL_PROVIDER, reason="social_error"
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
        """Handle a quality-selection click — start the actual download."""
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="invalid_fingerprint",
            )
        try:
            return await self._select_platform(
                url, platform, payload, attempt_directory,
                progress_callback=progress_callback,
                processing_callback=processing_callback,
            )
        except DownloadTooLarge:
            return GatewayResult(
                status="error", bot_username=SOCIAL_PROVIDER, reason="too_large"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("SocialSitesGateway select crashed for %s: %s", url, exc)
            return GatewayResult(
                status="error", bot_username=SOCIAL_PROVIDER, reason="social_error"
            )

    # ------------------------------------------------------------------
    # Platform dispatch
    # ------------------------------------------------------------------

    async def _request_platform(
        self,
        url: str,
        platform: Platform,
        attempt_directory: Path,
    ) -> GatewayResult:
        """Build a quality menu for the given platform."""
        if platform == Platform.TIKTOK:
            qualities = TIKTOK_QUALITIES
        elif platform == Platform.SOUNDCLOUD:
            qualities = SOUNDCLOUD_QUALITIES
        elif platform == Platform.INSTAGRAM:
            # If no cookies available, return early so the bot falls back
            # to Telegram downloader bots.
            if self.instagram_cookies_path is None or not self.instagram_cookies_path.exists():
                return GatewayResult(
                    status="error",
                    bot_username=SOCIAL_PROVIDER,
                    reason="instagram_no_cookies",
                )
            qualities = INSTAGRAM_QUALITIES
        elif platform == Platform.PINTEREST:
            qualities = PINTEREST_QUALITIES
        elif platform == Platform.TWITTER:
            qualities = TWITTER_QUALITIES
        elif platform == Platform.FACEBOOK:
            qualities = FACEBOOK_QUALITIES
        elif platform == Platform.YTDLP_GENERIC:
            qualities = GENERIC_QUALITIES
        else:
            return GatewayResult(
                status="error",
                bot_username=SOCIAL_PROVIDER,
                reason="unsupported_platform",
            )
        options: list[QualityOption] = []
        for row_index, (label, height, mode, bitrate, kind) in enumerate(qualities):
            if mode == "audio":
                fingerprint = _fingerprint({
                    "platform": platform.value,
                    "mode": "audio",
                    "kind": kind,
                    "bitrate": bitrate or "128",
                })
                options.append(QualityOption(
                    label=label,
                    row=row_index,
                    column=0,
                    fingerprint=fingerprint,
                    expected_kind=MediaKind.AUDIO,
                    expected_bitrate_kbps=int(bitrate) if bitrate else 128,
                    action="media",
                ))
            else:
                fingerprint = _fingerprint({
                    "platform": platform.value,
                    "mode": "video",
                    "kind": kind,
                    "height": height or 1080,
                })
                options.append(QualityOption(
                    label=label,
                    row=row_index,
                    column=0,
                    fingerprint=fingerprint,
                    expected_kind=MediaKind.VIDEO,
                    expected_height=height or 1080,
                    action="media",
                ))
        return await self._attach_menu_assets(
            url=url,
            platform=platform,
            attempt_directory=attempt_directory,
            options=tuple(options),
        )

    async def _attach_menu_assets(
        self,
        *,
        url: str,
        platform: Platform,
        attempt_directory: Path,
        options: tuple[QualityOption, ...],
    ) -> GatewayResult:
        """Enrich the menu with a thumbnail + caption (best-effort)."""
        thumb_bytes: bytes | None = None
        title: str | None = None

        if platform == Platform.TIKTOK:
            # Fetch metadata from tikwm.com to get cover + title.
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=20.0,
                follow_redirects=True,
            ) as client:
                data = await _tiktok_fetch(url, client)
                if data is not None:
                    title = (data.get("title") or "").strip() or None
                    cover_url = data.get("origin_cover") or data.get("cover") or ""
                    if cover_url:
                        try:
                            cover_resp = await client.get(cover_url, timeout=10.0)
                            if cover_resp.status_code == 200:
                                thumb_bytes = cover_resp.content
                        except Exception as exc:
                            logger.debug("TikTok cover fetch failed: %s", exc)
        else:
            # SoundCloud / Instagram — fetch OG metadata.
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=15.0,
                follow_redirects=True,
            ) as client:
                og_image, og_title = await _fetch_og_meta(client, url)
                if og_image:
                    try:
                        cover_resp = await client.get(og_image, timeout=10.0)
                        if cover_resp.status_code == 200:
                            thumb_bytes = cover_resp.content
                    except Exception as exc:
                        logger.debug("OG image fetch failed for %s: %s", url, exc)
                title = og_title

        platform_labels = {
            Platform.TIKTOK: "🎵 تیک‌تاک",
            Platform.SOUNDCLOUD: "☁️ ساوندکلاد",
            Platform.INSTAGRAM: "📸 اینستاگرام",
        }
        platform_label = platform_labels.get(platform, platform.value)
        caption_lines: list[str] = []
        if title:
            caption_lines.append(f"<b>{html.escape(title)}</b>")
        caption_lines.append(f"{platform_label} • <code>{html.escape(url)}</code>")
        caption = "\n".join(caption_lines)

        preview: DownloadedMedia | None = None
        if thumb_bytes:
            thumb_path = attempt_directory / "_social_thumb.jpg"
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
                logger.warning("Failed to persist thumbnail: %s", exc)
                preview = None

        return GatewayResult(
            status="needs_selection",
            bot_username=SOCIAL_PROVIDER,
            options=options,
            text=caption,
            preview=preview,
        )

    async def _select_platform(
        self,
        url: str,
        platform: Platform,
        payload: dict[str, Any],
        attempt_directory: Path,
        *,
        progress_callback: ProgressCallback | None,
        processing_callback: ProcessingCallback | None = None,
    ) -> GatewayResult:
        kind = str(payload.get("kind", "video"))
        mode = str(payload.get("mode", "video"))
        if platform == Platform.TIKTOK:
            return await _tiktok_download(
                url=url,
                kind=kind,
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
            )
        if platform == Platform.SOUNDCLOUD:
            # Report "processing" since yt-dlp can take 10-30s.
            if processing_callback is not None:
                try:
                    await processing_callback(15, "⚙️ دارم پردازش می‌کنم…", "استخراج از SoundCloud")
                except Exception:
                    pass
            return await _soundcloud_download(
                url=url,
                kind=kind,
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
                cookies_path=None,
            )
        if platform == Platform.INSTAGRAM:
            if processing_callback is not None:
                try:
                    await processing_callback(15, "⚙️ دارم پردازش می‌کنم…", "استخراج از Instagram")
                except Exception:
                    pass
            return await _instagram_download(
                url=url,
                kind=kind,
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
                cookies_path=self.instagram_cookies_path,
            )
        # New yt-dlp-backed platforms — Pinterest / Twitter / Facebook /
        # generic. They share the same cookies.txt and download path.
        if platform == Platform.PINTEREST:
            if processing_callback is not None:
                try:
                    await processing_callback(15, "⚙️ دارم پردازش می‌کنم…", "استخراج از Pinterest")
                except Exception:
                    pass
            return await _ytdlp_generic_download(
                url=url,
                kind=kind,
                file_prefix="pinterest",
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
                cookies_path=self.instagram_cookies_path,
                max_download_size=self.max_download_size,
                reason_prefix="pinterest",
            )
        if platform == Platform.TWITTER:
            if processing_callback is not None:
                try:
                    await processing_callback(15, "⚙️ دارم پردازش می‌کنم…", "استخراج از X / توییتر")
                except Exception:
                    pass
            return await _ytdlp_generic_download(
                url=url,
                kind=kind,
                file_prefix="twitter",
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
                cookies_path=self.instagram_cookies_path,
                max_download_size=self.max_download_size,
                reason_prefix="twitter",
            )
        if platform == Platform.FACEBOOK:
            if processing_callback is not None:
                try:
                    await processing_callback(15, "⚙️ دارم پردازش می‌کنم…", "استخراج از Facebook")
                except Exception:
                    pass
            return await _ytdlp_generic_download(
                url=url,
                kind=kind,
                file_prefix="facebook",
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
                cookies_path=self.instagram_cookies_path,
                max_download_size=self.max_download_size,
                reason_prefix="facebook",
            )
        if platform == Platform.YTDLP_GENERIC:
            if processing_callback is not None:
                try:
                    await processing_callback(15, "⚙️ دارم پردازش می‌کنم…", "استخراج با yt-dlp")
                except Exception:
                    pass
            return await _ytdlp_generic_download(
                url=url,
                kind=kind,
                file_prefix="generic",
                attempt_directory=attempt_directory,
                progress_callback=progress_callback,
                cookies_path=self.instagram_cookies_path,
                max_download_size=self.max_download_size,
                reason_prefix="ytdlp",
            )
        return GatewayResult(
            status="error",
            bot_username=SOCIAL_PROVIDER,
            reason="unsupported_platform",
        )


# ----------------------------------------------------------------------
# Connectivity probe
# ----------------------------------------------------------------------


async def social_health_check(*, timeout: float = 5.0) -> dict[str, bool]:
    """Quick connectivity probe for each platform's backend.

    Returns a dict like {'tiktok': True, 'soundcloud': True, 'instagram': False}.
    """
    results = {"tiktok": False, "soundcloud": False, "instagram": False}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        # TikTok — POST to tikwm.com
        try:
            resp = await client.post(
                TIKWM_API,
                data={"url": "https://www.tiktok.com/@tiktok/video/7106594312292453675"},
                timeout=timeout,
            )
            if resp.status_code == 200:
                results["tiktok"] = True
        except Exception:
            pass
        # SoundCloud — HEAD a known track
        try:
            resp = await client.head(
                "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up",
                timeout=timeout,
            )
            if resp.status_code < 500:
                results["soundcloud"] = True
        except Exception:
            pass
        # Instagram — HEAD a known reel (won't get 200 without auth, but
        # we just need to know if the host is reachable)
        try:
            resp = await client.head(
                "https://www.instagram.com/reel/CZ8is3-pZ3Q/",
                timeout=timeout,
            )
            if resp.status_code < 500:
                results["instagram"] = True
        except Exception:
            pass
    return results
