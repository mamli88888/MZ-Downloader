"""Fetch YouTube subtitles via yt-dlp.

This module mirrors the structure of :mod:`instagram_caption` (async-friendly
wrapper around a synchronous yt-dlp call) and provides:

* :class:`YouTubeSubtitleError` / :class:`YouTubeSubtitleNotFound` exceptions
* :func:`fetch_youtube_subtitle` — async entry point that returns SRT bytes
  for a given YouTube URL and language code.

Supported languages:

* ``"fa"`` — Persian (also tries auto-generated Persian when no manual sub)
* ``"en"`` — English (also tries auto-generated English when no manual sub)

The implementation uses :mod:`yt_dlp` (already a project dependency) which
talks directly to YouTube's subtitle tracks — the same source that
``downsub.com`` itself uses — so the user receives an identical SRT file.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

logger = logging.getLogger(__name__)

# ---- languages ----
LANGUAGE_PERSIAN = "fa"
LANGUAGE_ENGLISH = "en"
SUPPORTED_LANGUAGES = (LANGUAGE_PERSIAN, LANGUAGE_ENGLISH)

LANGUAGE_LABELS = {
    LANGUAGE_PERSIAN: "فارسی",
    LANGUAGE_ENGLISH: "English",
}

# Persian auto-sub code on YouTube is "fa" but is sometimes reported as
# "fa-orig" or "fa-IR"; we treat any code starting with "fa" as Persian.
# Likewise "en" covers "en-US", "en-GB", etc.

# ---- URL parsing ----
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_video_id(url: str) -> str:
    """Extract the 11-character YouTube video ID from any YouTube URL form.

    Returns an empty string if the URL is not a recognised YouTube video URL.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return ""
    path = parsed.path or ""
    query = parsed.query or ""

    # youtu.be/{id}
    if host == "youtu.be":
        m = re.match(r"^/([A-Za-z0-9_-]{11})(?:[/?].*)?$", path)
        return m.group(1) if m else ""

    # youtube.com/shorts/{id}
    m = re.match(r"^/shorts/([A-Za-z0-9_-]{11})(?:[/?].*)?$", path)
    if m:
        return m.group(1)

    # youtube.com/watch?v={id}
    if path.rstrip("/") == "/watch":
        m = re.search(r"(?:^|&)v=([A-Za-z0-9_-]{11})(?:&|$)", query)
        return m.group(1) if m else ""

    # youtube.com/embed/{id} or /v/{id} or /live/{id}
    m = re.match(r"^/(?:embed|v|vi|live)/([A-Za-z0-9_-]{11})(?:[/?].*)?$", path)
    if m:
        return m.group(1)

    return ""


def is_youtube_url(url: str) -> bool:
    """Return True if *url* points to youtube.com / youtu.be (any path)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in YOUTUBE_HOSTS


def is_youtube_shorts_url(url: str) -> bool:
    """Return True for ``youtube.com/shorts/{id}`` URLs."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return False
    return bool(re.match(r"^/shorts/", parsed.path or "", re.IGNORECASE))


# ---- exceptions ----
class YouTubeSubtitleError(Exception):
    """Generic subtitle fetch failure (network, yt-dlp, parse, ...)."""


class YouTubeSubtitleNotFound(YouTubeSubtitleError):
    """No subtitle available for the requested language."""


# ---- cache ----
# Keyed by (video_id, language) → (created_at, srt_bytes)
_CACHE: OrderedDict[tuple[str, str], tuple[float, bytes]] = {}
CACHE_TTL_SECONDS = 15 * 60
CACHE_MAX_ITEMS = 256


def _cache_get(key: tuple[str, str]) -> bytes:
    cached = _CACHE.get(key)
    if cached is None:
        return b""
    created, payload = cached
    if time.monotonic() - created >= CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return b""
    _CACHE.move_to_end(key)
    return payload


def _cache_put(key: tuple[str, str], payload: bytes) -> None:
    _CACHE[key] = (time.monotonic(), payload)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX_ITEMS:
        _CACHE.popitem(last=False)


# ---- cookie file resolution ----
def _resolve_cookies_file() -> str | None:
    """Locate the project's cookies.txt if it exists."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "cookies.txt",):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return str(candidate)
    return None


# ---- core synchronous fetcher ----
def _fetch_subtitle_sync(
    youtube_url: str,
    language: str,
    *,
    proxy_url: str | None,
    cookies_file: str | None,
) -> bytes:
    """Synchronous yt-dlp call that downloads subtitles and returns SRT bytes.

    Tries manual subtitles first; falls back to auto-generated subtitles.
    Returns the raw SRT file content as bytes.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise YouTubeSubtitleError(f"Unsupported language: {language!r}")

    video_id = extract_youtube_video_id(youtube_url)
    if not video_id:
        raise YouTubeSubtitleError(f"Could not extract video ID from {youtube_url!r}")

    with tempfile.TemporaryDirectory(prefix="yt_sub_") as tmp_dir:
        out_path = Path(tmp_dir) / f"{video_id}.{language}"
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "ignoreerrors": True,
            "cachedir": False,
            "socket_timeout": 20,
            "retries": 2,
            "extractor_retries": 2,
            # Try manual subs first; auto-subs as a separate call below
            "writesubtitles": True,
            "writeautomaticsub": False,
            "subtitleslangs": [language],
            "subtitlesformat": "srt",
            "outtmpl": str(out_path),
            "convertsubtitlesformat": "srt",
        }
        if proxy_url:
            options["proxy"] = proxy_url
        if cookies_file:
            options["cookiefile"] = cookies_file

        # First attempt: manual subs only
        try:
            with YoutubeDL(options) as ydl:
                ydl.extract_info(youtube_url, download=True)
        except DownloadError as exc:
            logger.info("yt-dlp manual sub fetch failed for %s: %s", video_id, exc)
        except Exception as exc:
            logger.warning("yt-dlp manual sub unexpected error for %s: %s", video_id, exc)

        srt_bytes = _read_srt_file(tmp_dir, video_id, language)
        if srt_bytes:
            return srt_bytes

        # Second attempt: auto-generated subs only
        options["writesubtitles"] = False
        options["writeautomaticsub"] = True
        try:
            with YoutubeDL(options) as ydl:
                ydl.extract_info(youtube_url, download=True)
        except DownloadError as exc:
            logger.info("yt-dlp auto-sub fetch failed for %s: %s", video_id, exc)
        except Exception as exc:
            logger.warning("yt-dlp auto-sub unexpected error for %s: %s", video_id, exc)

        srt_bytes = _read_srt_file(tmp_dir, video_id, language)
        if srt_bytes:
            return srt_bytes

    raise YouTubeSubtitleNotFound(
        f"No {LANGUAGE_LABELS[language]} subtitle available for this video. "
        f"Either the video has no subtitles, or YouTube is currently rate-limiting requests."
    )


def _read_srt_file(directory: str, video_id: str, language: str) -> bytes:
    """Find the .srt file produced by yt-dlp in *directory*.

    yt-dlp may produce files like ``{video_id}.{lang}.srt``, ``{video_id}.{lang}.{format}.srt``,
    or with auto-subs ``{video_id}.{lang}.srt``. We just glob for any .srt that contains
    the language code.
    """
    base = Path(directory)
    # Collect all .srt files in the directory
    srt_files = sorted(base.glob("*.srt"))
    # Prefer ones that contain the language code in their filename
    preferred = [p for p in srt_files if language in p.name]
    for candidate in preferred + srt_files:
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        if data and len(data) > 20:  # a real SRT file is at least a few hundred bytes
            # Quick sanity check: contains a timestamp like "00:00:"
            if b"00:0" in data or b"-->" in data:
                return data
    return b""


# ---- async entry point ----
async def fetch_youtube_subtitle(
    youtube_url: str,
    language: str,
    *,
    proxy_url: str | None = None,
    cookies_file: str | None = None,
    timeout: float = 60.0,
) -> bytes:
    """Asynchronously fetch the SRT subtitle for *youtube_url* in *language*.

    Returns the raw SRT file content as bytes. Raises :class:`YouTubeSubtitleNotFound`
    when no subtitle is available for the requested language, or
    :class:`YouTubeSubtitleError` for any other failure (network, yt-dlp crash,
    invalid URL, ...).

    Results are cached for 15 minutes per (video_id, language) tuple.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise YouTubeSubtitleError(f"Unsupported language: {language!r}")

    video_id = extract_youtube_video_id(youtube_url)
    if not video_id:
        raise YouTubeSubtitleError(f"Could not extract video ID from {youtube_url!r}")

    cache_key = (video_id, language)
    cached = _cache_get(cache_key)
    if cached:
        return cached

    if cookies_file is None:
        cookies_file = _resolve_cookies_file()

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                _fetch_subtitle_sync,
                youtube_url,
                language,
                proxy_url=proxy_url,
                cookies_file=cookies_file,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise YouTubeSubtitleError(
            "Subtitle fetch timed out. YouTube may be slow or rate-limiting requests."
        ) from exc
    except YouTubeSubtitleError:
        raise
    except Exception as exc:
        raise YouTubeSubtitleError(f"Unexpected subtitle fetch failure: {exc}") from exc

    _cache_put(cache_key, result)
    return result
