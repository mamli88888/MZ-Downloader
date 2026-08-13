"""YouTube search and safe six-thumbnail collage generation."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


logger = logging.getLogger(__name__)

YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_THUMBNAIL_HOSTS = ("ytimg.com", "ggpht.com")
MAX_RESULTS = 30
SEARCH_CANDIDATE_COUNT = 50
RESULTS_PER_PAGE = 6
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024
MAX_CONCURRENT_SEARCHES = 2
MAX_CONCURRENT_PAGE_BUILDS = 3
MAX_CONCURRENT_FORMAT_LOOKUPS = 2
FORMAT_LOOKUP_TIMEOUT_SECONDS = 15.0

# ---- downsub.com integration (used as a fallback for format-size lookup) ----
# When yt-dlp is blocked by YouTube's bot detection, we ask downsub.com for the
# video duration and estimate per-quality file sizes using YouTube's typical
# per-resolution bitrates.
_DOWNSUB_KEY = b"zthxw34cdp6wfyxmpad38v52t3hsz6c5"
_DOWNSUB_API = "https://get.downsub.com/"
_DOWNSUB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Approximate bitrates (bits/sec) YouTube uses for each resolution. Source:
# YouTube's adaptive streaming manifests; these are conservative midpoints.
_QUALITY_BITRATES_BPS: dict[int, int] = {
    144: 100_000,
    240: 300_000,
    360: 800_000,
    480: 1_500_000,
    720: 2_500_000,
    1080: 4_500_000,
    1440: 9_000_000,
    2160: 20_000_000,
}


def _evp_bytes_to_key(
    password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16
) -> tuple[bytes, bytes]:
    d = b""
    prev = b""
    while len(d) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        d += prev
    return d[:key_len], d[key_len : key_len + iv_len]


def _crypto_js_encrypt(plaintext: str, key: bytes) -> str:
    salt = os.urandom(8)
    dk, iv = _evp_bytes_to_key(key, salt)
    ct = AES.new(dk, AES.MODE_CBC, iv).encrypt(pad(plaintext.encode("utf-8"), 16))
    return json.dumps(
        {"ct": base64.b64encode(ct).decode(), "iv": iv.hex(), "s": salt.hex()},
        separators=(",", ":"),
    )


def _b64url(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode().replace("+", "-").replace("/", "_").rstrip("=")


def _downsub_encode(data: str, key: bytes | None = None) -> str:
    if key is None:
        key = _DOWNSUB_KEY
    return _b64url(_crypto_js_encrypt(json.dumps(data, separators=(",", ":")), key))


def _parse_duration_to_seconds(text: str) -> int:
    """Parse ``"HH:MM:SS"`` or ``"MM:SS"`` into total seconds."""
    if not text:
        return 0
    parts = text.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0


def _downsub_fetch_duration(url: str, *, proxy_url: str | None) -> int:
    """Ask downsub.com for the duration of *url* in seconds (0 on failure)."""
    payload = {
        "url": url,
        "data": _downsub_encode(_downsub_encode(url), url.encode("utf-8")),
    }
    headers = {
        "User-Agent": _DOWNSUB_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://downsub.com",
        "Referer": "https://downsub.com/",
    }
    with httpx.Client(
        http2=True, timeout=15.0, headers=headers, proxy=proxy_url
    ) as client:
        r = client.post(_DOWNSUB_API, json=payload)
        if r.status_code != 200:
            return 0
        try:
            body = r.json()
        except Exception:
            return 0
    return _parse_duration_to_seconds(body.get("duration", "") if isinstance(body, dict) else "")

CANVAS_WIDTH = 1280
CANVAS_PADDING = 24
CARD_GAP = 24
CARD_WIDTH = (CANVAS_WIDTH - (2 * CANVAS_PADDING) - CARD_GAP) // 2
THUMBNAIL_HEIGHT = 338
LABEL_HEIGHT = 64
CARD_HEIGHT = THUMBNAIL_HEIGHT + LABEL_HEIGHT
CANVAS_HEIGHT = (2 * CANVAS_PADDING) + (3 * CARD_HEIGHT) + (2 * CARD_GAP)

LIVE_STATUSES = {"is_live", "is_upcoming", "was_live", "post_live"}
NON_VIDEO_ENTRY_TYPES = {"channel", "playlist", "multi_video"}
LIVE_TITLE_PATTERN = re.compile(
    r"(?:^|[\s[(|])(?:🔴\s*)?(?:live(?:\s+(?:now|stream))?|livestream)(?=$|\s*[:|\])\-])"
    r"|پخش\s*زنده|لایو\s*(?:زنده|الان)",
    re.IGNORECASE,
)
MUSIC_TITLE_PATTERN = re.compile(
    r"\bofficial\s+(?:audio|music\s+video|song)\b"
    r"|\bmusic\s+video\b|\blyric(?:s|\s+video)?\b|\baudio\s+only\b"
    r"|\bfull\s+album\b|\bsoundtrack\b|\bkaraoke\b|\bnightcore\b"
    r"|\bslowed(?:\s*(?:&|and)\s*reverb)?\b"
    r"|(?:^|[\s[(\-])(?:آهنگ|ترانه|موزیک(?:\s*ویدیو)?|ریمیکس|کارائوکه)(?:$|[\s)\]\-])",
    re.IGNORECASE,
)


class YouTubeSearchError(RuntimeError):
    """Raised when YouTube search cannot produce usable video results."""


@dataclass(frozen=True)
class YouTubeSearchResult:
    video_id: str
    title: str
    url: str
    thumbnail_url: str


@dataclass(frozen=True)
class YouTubeFormatSize:
    """A single yt-dlp format entry, kept only when it reports a file size."""

    height: int | None
    abr_kbps: int | None
    is_audio_only: bool
    is_muxed: bool
    size: int


def estimate_youtube_size(
    formats: Sequence[YouTubeFormatSize],
    *,
    is_audio: bool,
    target_height: int | None = None,
    target_bitrate_kbps: int | None = None,
) -> int | None:
    """Approximate the final file size for a requested quality.

    Returns None when no matching format with a known size is available;
    callers must treat that as "unknown" rather than guessing a number.
    """
    if not formats:
        return None
    if is_audio:
        audio_formats = [item for item in formats if item.is_audio_only]
        if not audio_formats:
            return None
        if target_bitrate_kbps:
            best_audio = min(
                audio_formats, key=lambda item: abs((item.abr_kbps or 0) - target_bitrate_kbps)
            )
        else:
            best_audio = max(audio_formats, key=lambda item: item.abr_kbps or 0)
        return best_audio.size

    video_formats = [item for item in formats if not item.is_audio_only and item.height]
    if not video_formats:
        return None
    if target_height:
        best_video = min(video_formats, key=lambda item: abs((item.height or 0) - target_height))
    else:
        best_video = max(video_formats, key=lambda item: item.height or 0)
    if best_video.is_muxed:
        return best_video.size
    # Adaptive formats (typical for 720p+) ship video-only; add the best
    # available audio track so the estimate matches a normal combined file.
    audio_only = [item for item in formats if item.is_audio_only]
    audio_bonus = max((item.size for item in audio_only), default=0)
    return best_video.size + audio_bonus


def normalize_search_query(query: str) -> str:
    """Normalize user input and reject values that are unsafe or impractical."""
    value = " ".join((query or "").split())
    if not value:
        raise YouTubeSearchError("Search query is empty")
    if len(value) > 200:
        raise YouTubeSearchError("Search query is too long")
    return value


def _video_id_from_entry(entry: dict[str, Any]) -> str:
    candidates = (entry.get("id"), entry.get("url"), entry.get("webpage_url"))
    for candidate in candidates:
        value = str(candidate or "").strip()
        if YOUTUBE_VIDEO_ID.fullmatch(value):
            return value
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if (parsed.hostname or "").lower().removeprefix("www.") == "youtu.be":
            video_id = parsed.path.strip("/").split("/", 1)[0]
            if YOUTUBE_VIDEO_ID.fullmatch(video_id):
                return video_id
        if (parsed.hostname or "").lower().removeprefix("www.").endswith("youtube.com"):
            match = re.search(r"(?:^|[?&])v=([A-Za-z0-9_-]{11})(?:&|$)", parsed.query)
            if match:
                return match.group(1)
    return ""


def _best_thumbnail(entry: dict[str, Any], video_id: str) -> str:
    thumbnails = entry.get("thumbnails") or ()
    choices: list[tuple[int, str]] = []
    if isinstance(thumbnails, list):
        for thumbnail in thumbnails:
            if not isinstance(thumbnail, dict):
                continue
            url = str(thumbnail.get("url") or "")
            try:
                width = max(0, int(thumbnail.get("width") or 0))
            except (TypeError, ValueError):
                width = 0
            if _thumbnail_url_is_allowed(url):
                choices.append((width, url))
    direct = str(entry.get("thumbnail") or "")
    if _thumbnail_url_is_allowed(direct):
        choices.append((0, direct))
    if choices:
        return max(choices, key=lambda choice: choice[0])[1]
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _metadata_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _safe_timestamp(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def is_regular_video_result(entry: dict[str, Any]) -> bool:
    """Keep ordinary long videos and Shorts; reject live and music/audio results."""
    entry_type = str(entry.get("_type") or "").strip().lower()
    if entry_type in NON_VIDEO_ENTRY_TYPES:
        return False
    extractor = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if "playlist" in extractor or "channel" in extractor:
        return False

    live_status = str(entry.get("live_status") or "").strip().lower()
    if live_status in LIVE_STATUSES:
        return False
    if any(_metadata_flag(entry.get(field)) for field in ("is_live", "is_upcoming", "was_live")):
        return False
    release_at = max(
        _safe_timestamp(entry.get("release_timestamp")),
        _safe_timestamp(entry.get("release_date")),
    )
    if release_at > time.time() + 5 * 60:
        return False

    title = " ".join(str(entry.get("title") or "").split())
    if LIVE_TITLE_PATTERN.search(title):
        return False

    vcodec = str(entry.get("vcodec") or "").strip().lower()
    format_note = str(entry.get("format_note") or entry.get("resolution") or "").strip().lower()
    if vcodec == "none" or "audio only" in format_note:
        return False
    if any(str(entry.get(field) or "").strip() for field in ("track", "artist", "album")):
        return False
    categories = entry.get("categories") or ()
    if isinstance(categories, str):
        categories = (categories,)
    if any(str(category).strip().lower() in {"music", "موسیقی"} for category in categories):
        return False
    channel = str(entry.get("channel") or entry.get("uploader") or "").strip().lower()
    if channel.endswith(" - topic") or channel.endswith(" – topic"):
        return False
    return not MUSIC_TITLE_PATTERN.search(title)


def parse_search_entries(entries: Sequence[Any], limit: int = MAX_RESULTS) -> tuple[YouTubeSearchResult, ...]:
    """Convert yt-dlp's loose dictionaries into validated, deduplicated results."""
    results: list[YouTubeSearchResult] = []
    seen: set[str] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        video_id = _video_id_from_entry(raw_entry)
        if not video_id or video_id in seen or not is_regular_video_result(raw_entry):
            continue
        seen.add(video_id)
        title = " ".join(str(raw_entry.get("title") or "ویدیوی یوتیوب").split())[:300]
        results.append(
            YouTubeSearchResult(
                video_id=video_id,
                title=title,
                url=f"https://www.youtube.com/watch?v={video_id}",
                thumbnail_url=_best_thumbnail(raw_entry, video_id),
            )
        )
        if len(results) >= limit:
            break
    return tuple(results)


def _thumbnail_url_is_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().strip(".")
    return parsed.scheme == "https" and any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_THUMBNAIL_HOSTS)


class YouTubeSearchService:
    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url
        self._search_slots = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
        self._page_slots = asyncio.Semaphore(MAX_CONCURRENT_PAGE_BUILDS)
        self._format_slots = asyncio.Semaphore(MAX_CONCURRENT_FORMAT_LOOKUPS)

    def _search_sync(self, query: str) -> tuple[YouTubeSearchResult, ...]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
            "cachedir": False,
            "socket_timeout": 20,
            "retries": 2,
            "extractor_retries": 2,
            # Ask for a few spare entries because unavailable/private videos may
            # be omitted by the extractor; the validated output is still capped at 30.
            "playlistend": SEARCH_CANDIDATE_COUNT,
        }
        if self.proxy_url:
            options["proxy"] = self.proxy_url
        try:
            with YoutubeDL(options) as ydl:
                payload = ydl.extract_info(f"ytsearch{SEARCH_CANDIDATE_COUNT}:{query}", download=False)
        except DownloadError as exc:
            raise YouTubeSearchError("YouTube search failed") from exc
        except Exception as exc:
            raise YouTubeSearchError("Unexpected YouTube search failure") from exc
        entries = payload.get("entries") if isinstance(payload, dict) else None
        results = parse_search_entries(entries or ())
        if not results:
            raise YouTubeSearchError("No YouTube videos were found")
        return results

    async def search(self, query: str) -> tuple[YouTubeSearchResult, ...]:
        normalized = normalize_search_query(query)
        try:
            await asyncio.wait_for(self._search_slots.acquire(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            raise YouTubeSearchError("YouTube search is busy") from exc

        search_task = asyncio.create_task(asyncio.to_thread(self._search_sync, normalized))
        release_immediately = True
        try:
            return await asyncio.wait_for(asyncio.shield(search_task), timeout=45.0)
        except asyncio.TimeoutError as exc:
            if not search_task.done():
                release_immediately = False
                search_task.add_done_callback(self._release_search_slot)
            raise YouTubeSearchError("YouTube search timed out") from exc
        except asyncio.CancelledError:
            if not search_task.done():
                release_immediately = False
                search_task.add_done_callback(self._release_search_slot)
            raise
        finally:
            if release_immediately:
                self._search_slots.release()

    def _release_search_slot(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()
        self._search_slots.release()

    def _format_sizes_sync(self, url: str) -> tuple[YouTubeFormatSize, ...]:
        # Try yt-dlp first (works on networks where YouTube's bot detection
        # isn't triggered). On failure, fall back to a duration-based estimate
        # derived from the downsub.com API.
        yt_dlp_results = self._format_sizes_via_yt_dlp(url)
        if yt_dlp_results:
            return yt_dlp_results
        return self._format_sizes_via_downsub(url)

    def _format_sizes_via_yt_dlp(self, url: str) -> tuple[YouTubeFormatSize, ...]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "cachedir": False,
            "socket_timeout": 12,
            "retries": 1,
            "extractor_retries": 1,
        }
        if self.proxy_url:
            options["proxy"] = self.proxy_url
        # Suppress yt-dlp's noisy stderr output (it prints "Sign in to
        # confirm you're not a bot" even with quiet=True). We redirect
        # stderr to /dev/null during the call; on any failure we silently
        # fall back to the downsub.com duration-based estimate.
        import os as _os
        import sys as _sys
        try:
            devnull_fd = _os.open(_os.devnull, _os.O_WRONLY)
            saved_stderr_fd = _os.dup(_sys.stderr.fileno())
            _os.dup2(devnull_fd, _sys.stderr.fileno())
            _os.close(devnull_fd)
        except Exception:
            saved_stderr_fd = None
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            # Any failure (including YouTube's "Sign in to confirm you're not
            # a bot" challenge) means we should silently fall back.
            return ()
        finally:
            if saved_stderr_fd is not None:
                try:
                    _os.dup2(saved_stderr_fd, _sys.stderr.fileno())
                    _os.close(saved_stderr_fd)
                except Exception:
                    pass
        formats = info.get("formats") if isinstance(info, dict) else None
        results: list[YouTubeFormatSize] = []
        for entry in formats or ():
            if not isinstance(entry, dict):
                continue
            size = entry.get("filesize") or entry.get("filesize_approx")
            if not size:
                continue
            vcodec = str(entry.get("vcodec") or "none").lower()
            acodec = str(entry.get("acodec") or "none").lower()
            has_video = vcodec not in ("none", "")
            has_audio = acodec not in ("none", "")
            if not has_video and not has_audio:
                continue
            height_value = entry.get("height")
            bitrate_value = entry.get("abr") or entry.get("tbr")
            try:
                size_int = int(size)
            except (TypeError, ValueError):
                continue
            results.append(
                YouTubeFormatSize(
                    height=int(height_value) if height_value else None,
                    abr_kbps=int(bitrate_value) if bitrate_value else None,
                    is_audio_only=has_audio and not has_video,
                    is_muxed=has_audio and has_video,
                    size=size_int,
                )
            )
        return tuple(results)

    def _format_sizes_via_downsub(self, url: str) -> tuple[YouTubeFormatSize, ...]:
        """Fallback: derive format sizes from the video duration via downsub.com.

        When yt-dlp is blocked by YouTube's bot detection, we ask downsub.com
        for the video duration (its backend can fetch metadata that we can't)
        and then estimate per-quality file sizes using YouTube's typical
        per-resolution bitrates.
        """
        try:
            duration_seconds = _downsub_fetch_duration(url, proxy_url=self.proxy_url)
        except Exception as exc:
            logger.info("downsub duration lookup failed for %s: %s", url, exc)
            return ()
        if not duration_seconds or duration_seconds < 1:
            return ()
        results: list[YouTubeFormatSize] = []
        # Per-quality typical bitrates (bits per second). These are the
        # average values YouTube uses for adaptive streaming; actual sizes
        # may vary ±30% depending on content complexity.
        for height, bitrate_bps in _QUALITY_BITRATES_BPS.items():
            estimated_size = int(duration_seconds * bitrate_bps / 8)
            results.append(
                YouTubeFormatSize(
                    height=height,
                    abr_kbps=None,
                    is_audio_only=False,
                    is_muxed=False,
                    size=estimated_size,
                )
            )
        # Audio-only estimate (128 kbps AAC)
        audio_size = int(duration_seconds * 128_000 / 8)
        results.append(
            YouTubeFormatSize(
                height=None,
                abr_kbps=128,
                is_audio_only=True,
                is_muxed=False,
                size=audio_size,
            )
        )
        return tuple(results)

    async def format_sizes(self, url: str) -> tuple[YouTubeFormatSize, ...]:
        """Best-effort approximate format sizes for a YouTube URL.

        Never raises: on busy slots, timeout, or any lookup failure this
        returns an empty tuple so callers can simply skip showing sizes.
        """
        try:
            await asyncio.wait_for(self._format_slots.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            return ()
        lookup_task = asyncio.create_task(asyncio.to_thread(self._format_sizes_sync, url))
        release_immediately = True
        try:
            return await asyncio.wait_for(asyncio.shield(lookup_task), timeout=FORMAT_LOOKUP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            if not lookup_task.done():
                release_immediately = False
                lookup_task.add_done_callback(self._release_format_slot)
            raise
        except Exception as exc:
            logger.info("YouTube format size lookup skipped for %s: %s", url, exc)
            if not lookup_task.done():
                release_immediately = False
                lookup_task.add_done_callback(self._release_format_slot)
            return ()
        finally:
            if release_immediately:
                self._format_slots.release()

    def _release_format_slot(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()
        self._format_slots.release()

    async def _download_thumbnail(self, client: httpx.AsyncClient, result: YouTubeSearchResult) -> bytes | None:
        urls = tuple(
            dict.fromkeys(
                (
                    result.thumbnail_url,
                    f"https://i.ytimg.com/vi/{result.video_id}/hqdefault.jpg",
                )
            )
        )
        for url in urls:
            if not _thumbnail_url_is_allowed(url):
                continue
            try:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    if not _thumbnail_url_is_allowed(str(response.url)):
                        continue
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type.startswith("image/"):
                        continue
                    declared_size = int(response.headers.get("content-length") or 0)
                    if declared_size > MAX_THUMBNAIL_BYTES:
                        continue
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > MAX_THUMBNAIL_BYTES:
                            payload.clear()
                            break
                    if payload:
                        return bytes(payload)
            except (httpx.HTTPError, ValueError):
                continue
        return None

    async def build_page_image(self, results: Sequence[YouTubeSearchResult], page: int) -> bytes:
        page_count = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        if page < 0 or page >= page_count:
            raise YouTubeSearchError("Search page is out of range")
        start = page * RESULTS_PER_PAGE
        page_results = tuple(results[start:start + RESULTS_PER_PAGE])
        if not page_results:
            raise YouTubeSearchError("Search page is empty")
        try:
            await asyncio.wait_for(self._page_slots.acquire(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            raise YouTubeSearchError("Search pages are busy") from exc
        try:
            client_options: dict[str, Any] = {
                "follow_redirects": True,
                "timeout": httpx.Timeout(12.0, connect=8.0),
                "headers": {"User-Agent": "MZDownloader/1.0"},
            }
            if self.proxy_url:
                client_options["proxy"] = self.proxy_url
            async with httpx.AsyncClient(**client_options) as client:
                thumbnails = await asyncio.gather(
                    *(self._download_thumbnail(client, result) for result in page_results)
                )
            return await asyncio.to_thread(render_collage, thumbnails, start + 1)
        finally:
            self._page_slots.release()


def _load_label_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", 42)
    except OSError:
        return ImageFont.load_default(size=42)


def _placeholder_thumbnail() -> Image.Image:
    image = Image.new("RGB", (CARD_WIDTH, THUMBNAIL_HEIGHT), "#20242c")
    draw = ImageDraw.Draw(image)
    center_x, center_y = CARD_WIDTH // 2, THUMBNAIL_HEIGHT // 2
    draw.polygon(
        (
            (center_x - 32, center_y - 45),
            (center_x - 32, center_y + 45),
            (center_x + 48, center_y),
        ),
        fill="#ff0033",
    )
    return image


def _decode_thumbnail(payload: bytes | None) -> Image.Image:
    if not payload:
        return _placeholder_thumbnail()
    try:
        with Image.open(io.BytesIO(payload)) as source:
            source.load()
            converted = source.convert("RGB")
        return ImageOps.fit(
            converted,
            (CARD_WIDTH, THUMBNAIL_HEIGHT),
            method=Image.Resampling.LANCZOS,
        )
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return _placeholder_thumbnail()


def render_collage(thumbnails: Sequence[bytes | None], first_number: int) -> bytes:
    """Render up to six thumbnails in a 2x3 JPEG, numbered below each card."""
    if not thumbnails or len(thumbnails) > RESULTS_PER_PAGE:
        raise YouTubeSearchError("A collage needs between one and six thumbnails")
    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), "#111318")
    draw = ImageDraw.Draw(canvas)
    font = _load_label_font()
    for offset, payload in enumerate(thumbnails):
        row, column = divmod(offset, 2)
        left = CANVAS_PADDING + column * (CARD_WIDTH + CARD_GAP)
        top = CANVAS_PADDING + row * (CARD_HEIGHT + CARD_GAP)
        thumbnail = _decode_thumbnail(payload)
        canvas.paste(thumbnail, (left, top))
        label_top = top + THUMBNAIL_HEIGHT
        draw.rectangle(
            (left, label_top, left + CARD_WIDTH, label_top + LABEL_HEIGHT),
            fill="#f4f5f7",
        )
        label = f"#{first_number + offset}"
        bounds = draw.textbbox((0, 0), label, font=font)
        label_width = bounds[2] - bounds[0]
        label_height = bounds[3] - bounds[1]
        draw.text(
            (
                left + (CARD_WIDTH - label_width) / 2,
                label_top + (LABEL_HEIGHT - label_height) / 2 - bounds[1],
            ),
            label,
            fill="#111318",
            font=font,
        )
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    return output.getvalue()
