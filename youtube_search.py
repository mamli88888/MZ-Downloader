"""YouTube search and safe six-thumbnail collage generation."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


logger = logging.getLogger(__name__)

YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_THUMBNAIL_HOSTS = ("ytimg.com", "ggpht.com")
MAX_RESULTS = 30
SEARCH_CANDIDATE_COUNT = 36
RESULTS_PER_PAGE = 6
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024
MAX_CONCURRENT_SEARCHES = 2
MAX_CONCURRENT_PAGE_BUILDS = 3

CANVAS_WIDTH = 1280
CANVAS_PADDING = 24
CARD_GAP = 24
CARD_WIDTH = (CANVAS_WIDTH - (2 * CANVAS_PADDING) - CARD_GAP) // 2
THUMBNAIL_HEIGHT = 338
LABEL_HEIGHT = 64
CARD_HEIGHT = THUMBNAIL_HEIGHT + LABEL_HEIGHT
CANVAS_HEIGHT = (2 * CANVAS_PADDING) + (3 * CARD_HEIGHT) + (2 * CARD_GAP)


class YouTubeSearchError(RuntimeError):
    """Raised when YouTube search cannot produce usable video results."""


@dataclass(frozen=True)
class YouTubeSearchResult:
    video_id: str
    title: str
    url: str
    thumbnail_url: str


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


def parse_search_entries(entries: Sequence[Any], limit: int = MAX_RESULTS) -> tuple[YouTubeSearchResult, ...]:
    """Convert yt-dlp's loose dictionaries into validated, deduplicated results."""
    results: list[YouTubeSearchResult] = []
    seen: set[str] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        video_id = _video_id_from_entry(raw_entry)
        if not video_id or video_id in seen:
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
