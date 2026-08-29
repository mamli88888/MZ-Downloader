"""Reel music recognition via ShazamIO for MZ Downloader.

Replaces the old "forward the reel link to an external music-finder bot"
flow with LOCAL recognition: the (already downloaded or freshly re-
downloaded) reel video is handed to ShazamIO and the song information
(title / artist / album / cover / links) is returned to the caller.

Notes on the ShazamIO runtime behaviour (verified against shazamio==0.8.1):
    * ``Shazam.recognize()`` accepts a path or bytes and, on success, the
      response dict carries a top-level ``track`` payload with everything
      we need (title, subtitle, images, sections, share links, ...).
    * The Rust signature decoder handles plain audio files directly, but
      video containers (MP4/MOV — what reel downloads are) decode more
      reliably when the audio track is first extracted with ffmpeg. So
      video inputs are pre-converted to a temporary 16 kHz mono WAV.
    * ``Shazam.track_about()`` is currently broken upstream (404 HTML), so
      all track details are parsed from the ``recognize()`` payload itself.

Public API:
    - ReelMusicMatch: dataclass describing the recognized song
    - ReelMusicError: raised when recognition cannot run at all
    - ReelMusicService: recognize(media_path) -> ReelMusicMatch | None
    - format_song_caption(match): HTML body for the Telegram song card
"""

from __future__ import annotations

import asyncio
import html
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ShazamIO is an optional runtime dependency (same policy as mz_shazam_search):
# the bot keeps working without it, the reel-music button just degrades.
try:  # pragma: no cover - exercised at import time
    from shazamio import Shazam  # type: ignore
    from shazamio.exceptions import FailedDecodeJson  # type: ignore

    _SHAZAMIO_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    _SHAZAMIO_AVAILABLE = False

    class Shazam:  # type: ignore[no-redef]
        """Local stub used when shazamio is not installed."""

    class FailedDecodeJson(Exception):  # type: ignore[no-redef]
        """Local stub used when shazamio is not installed."""


# --- tunables --------------------------------------------------------------

RECOGNIZE_TIMEOUT_SECONDS = 60.0
FFMPEG_TIMEOUT_SECONDS = 90.0
MAX_CONCURRENT_RECOGNITIONS = 2

COVER_TIMEOUT_SECONDS = 15.0
MAX_COVER_BYTES = 8 * 1024 * 1024

# What Instagram reel downloads look like on disk.
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".3gp"}
VIDEO_MIME_PREFIXES = ("video/",)


# --- exceptions & data -----------------------------------------------------


class ReelMusicError(RuntimeError):
    """Raised when reel music recognition cannot run (not "no match")."""


@dataclass(frozen=True)
class ReelMusicMatch:
    """The song recognized inside a reel video."""

    track_name: str
    artist_name: str
    album_name: str = ""
    release_year: str = ""
    record_label: str = ""
    genre: str = ""
    cover_url: str = ""
    shazam_url: str = ""
    apple_music_url: str = ""
    shazam_track_id: str = ""
    match_offset_seconds: float = 0.0

    @property
    def label(self) -> str:
        """Combined display label, mirrors ShazamSearchResult.label."""
        return f"{self.artist_name} - {self.track_name}"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


# --- payload parsing -------------------------------------------------------


def _apple_music_url_from_track(track: dict) -> str:
    """Extract a plain https Apple Music link from the hub options."""
    hub = track.get("hub")
    options = hub.get("options") if isinstance(hub, dict) else None
    if not isinstance(options, list):
        return ""
    for option in options:
        if not isinstance(option, dict):
            continue
        actions = option.get("actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            uri = str(action.get("uri") or "")
            if "music.apple.com" not in uri:
                continue
            # Shazam hands out intent:// deep links — normalize to https.
            if uri.startswith("intent://"):
                uri = "https://" + uri[len("intent://"):]
            if "?" in uri:
                # strip tracking params (mttnage hub:..., feature=, ...)
                uri = uri.split("?", 1)[0]
            if uri.startswith("https://music.apple.com"):
                return uri
    return ""


def parse_track_payload(payload: Any) -> ReelMusicMatch | None:
    """Parse a ShazamIO ``recognize()`` response into a :class:`ReelMusicMatch`.

    Returns ``None`` when nothing was recognized (an empty match list) —
    that is a normal outcome, not an error.
    """
    if not isinstance(payload, dict):
        raise ReelMusicError("ShazamIO returned an unexpected payload")

    matches = payload.get("matches")
    track = payload.get("track")
    if not isinstance(track, dict) and isinstance(matches, list):
        # Older API shapes sometimes nested the track inside the match.
        for match in matches:
            if isinstance(match, dict) and isinstance(match.get("track"), dict):
                track = match["track"]
                break

    if not isinstance(track, dict) or not matches:
        return None

    title = _clean(track.get("title"))
    artist = _clean(track.get("subtitle"))
    if not title or not artist:
        return None

    first_match = matches[0] if isinstance(matches[0], dict) else {}
    offset = first_match.get("offset")
    try:
        offset_seconds = float(offset) if offset is not None else 0.0
    except (TypeError, ValueError):
        offset_seconds = 0.0

    album = year = release_label = ""
    sections = track.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict) or section.get("type") != "SONG":
                continue
            metadata = section.get("metadata")
            if not isinstance(metadata, list):
                continue
            for meta in metadata:
                if not isinstance(meta, dict):
                    continue
                meta_title = str(meta.get("title") or "").lower()
                meta_text = _clean(meta.get("text"))
                if meta_title == "album" and not album:
                    album = meta_text
                elif meta_title == "released" and not year:
                    year = meta_text
                elif meta_title == "label" and not release_label:
                    release_label = meta_text

    genre = ""
    genres = track.get("genres")
    if isinstance(genres, dict):
        genre = _clean(genres.get("primary"))

    images = track.get("images")
    cover_url = ""
    if isinstance(images, dict):
        cover_url = (
            _clean(images.get("coverarthq"))
            or _clean(images.get("coverart"))
            or _clean(images.get("background"))
        )
    if not cover_url:
        share = track.get("share")
        if isinstance(share, dict):
            cover_url = _clean(share.get("image"))

    share = track.get("share") if isinstance(track.get("share"), dict) else {}
    shazam_url = _clean(track.get("url")) or _clean(share.get("href"))

    track_id = _clean(track.get("key")) or _clean(first_match.get("id"))

    return ReelMusicMatch(
        track_name=title,
        artist_name=artist,
        album_name=album,
        release_year=year,
        record_label=release_label,
        genre=genre,
        cover_url=cover_url,
        shazam_url=shazam_url,
        apple_music_url=_apple_music_url_from_track(track),
        shazam_track_id=track_id,
        match_offset_seconds=offset_seconds,
    )


# --- service ---------------------------------------------------------------


class ReelMusicService:
    """Recognize the song inside a reel video / audio file with ShazamIO."""

    def __init__(self) -> None:
        if _SHAZAMIO_AVAILABLE:
            try:
                self._shazam: Shazam | None = Shazam()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ShazamIO initialization failed: %s", exc)
                self._shazam = None
        else:
            self._shazam = None
            logger.info("ShazamIO not installed; reel music recognition is disabled")
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_RECOGNITIONS)

    # -- audio extraction ---------------------------------------------------

    @staticmethod
    def is_video_file(path: Path, mime_type: str = "") -> bool:
        """True when the downloaded reel is a video container (not pure audio)."""
        if mime_type and mime_type.lower().startswith(VIDEO_MIME_PREFIXES):
            return True
        return path.suffix.lower() in VIDEO_EXTENSIONS

    async def _extract_audio_track(self, video_path: Path) -> Path:
        """Extract a 16 kHz mono WAV via ffmpeg (same pre-processing the
        AHM7 gateway relies on for MP3 conversion, so ffmpeg is guaranteed
        to be present in deployments)."""
        if shutil.which("ffmpeg") is None:
            raise ReelMusicError("ffmpeg is not installed")
        handle = tempfile.NamedTemporaryFile(prefix="reel-music-", suffix=".wav", delete=False)
        output_path = Path(handle.name)
        handle.close()
        command = (
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000",
            "-map", "0:a:0",
            str(output_path),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            output_path.unlink(missing_ok=True)
            raise ReelMusicError(f"ffmpeg could not be started: {exc}") from exc
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            output_path.unlink(missing_ok=True)
            raise ReelMusicError("ffmpeg audio extraction timed out")
        if process.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            tail = detail[-1] if detail else "unknown ffmpeg error"
            if "Output file does not contain any stream" in tail or "does not contain any stream" in tail:
                raise ReelMusicError("the reel has no audio track")
            raise ReelMusicError(f"ffmpeg audio extraction failed: {tail}")
        return output_path

    # -- recognition ---------------------------------------------------------

    async def recognize(
        self,
        media_path: Path,
        mime_type: str = "",
        proxy_url: str | None = None,
    ) -> ReelMusicMatch | None:
        """Recognize the song inside ``media_path``.

        Returns ``None`` when Shazam could not match the audio (normal for
        voice-overs / original sounds). Raises :class:`ReelMusicError` when
        recognition itself could not run.
        """
        if self._shazam is None:
            raise ReelMusicError("ShazamIO is not available")
        if not media_path.is_file() or media_path.stat().st_size == 0:
            raise ReelMusicError("media file is missing or empty")

        extracted: Path | None = None
        try:
            if self.is_video_file(media_path, mime_type):
                extracted = await self._extract_audio_track(media_path)
                target = extracted
            else:
                target = media_path

            # aiohttp only accepts http(s) proxies; socks proxies are already
            # wired into the Telethon/httpx stacks and are skipped here.
            proxy = proxy_url if proxy_url and proxy_url.startswith(("http://", "https://")) else None

            async with self._slots:
                try:
                    payload = await asyncio.wait_for(
                        self._shazam.recognize(str(target), proxy=proxy),
                        timeout=RECOGNIZE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    raise ReelMusicError("Shazam recognition timed out") from exc
                except FailedDecodeJson as exc:
                    raise ReelMusicError(f"Shazam response decode failed: {exc}") from exc
            return parse_track_payload(payload)
        finally:
            if extracted is not None:
                extracted.unlink(missing_ok=True)

    # -- cover art -----------------------------------------------------------

    async def download_cover(self, match: ReelMusicMatch, proxy_url: str | None = None) -> bytes | None:
        """Download the cover art for the song card (None on any failure)."""
        if not match.cover_url:
            return None
        client_kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "timeout": httpx.Timeout(COVER_TIMEOUT_SECONDS, connect=10.0),
            "headers": {"User-Agent": "MZDownloader/1.0"},
        }
        if proxy_url and proxy_url.startswith(("socks", "http")):
            client_kwargs["proxy"] = proxy_url
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(match.cover_url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("image/"):
                    return None
                if len(response.content) > MAX_COVER_BYTES:
                    return None
                return response.content or None
        except (httpx.HTTPError, ValueError):
            return None


# --- caption ---------------------------------------------------------------


def format_song_caption(match: ReelMusicMatch) -> str:
    """HTML body (for ``status_card``) describing the recognized song."""
    lines = [
        f"🎼 <b>{html.escape(match.track_name)}</b>",
        f"👤 خواننده: <b>{html.escape(match.artist_name)}</b>",
    ]
    if match.album_name:
        lines.append(f"💿 آلبوم: {html.escape(match.album_name)}")
    details = " • ".join(
        piece for piece in (match.release_year, match.genre, match.record_label) if piece
    )
    if details:
        lines.append(f"🗂 {html.escape(details)}")
    links = []
    if match.shazam_url:
        links.append(f'<a href="{html.escape(match.shazam_url)}">Shazam</a>')
    if match.apple_music_url:
        links.append(f'<a href="{html.escape(match.apple_music_url)}">Apple Music</a>')
    if links:
        lines.append("🔗 " + " | ".join(links))
    return "\n".join(lines)
