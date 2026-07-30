"""Spotify search and collage generation."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from dataclasses import dataclass
from typing import Any, Sequence

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

MAX_RESULTS = 30
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


class SpotifySearchError(RuntimeError):
    """Raised when Spotify search fails."""


@dataclass(frozen=True)
class SpotifySearchResult:
    track_id: str
    title: str
    artist: str
    url: str
    thumbnail_url: str


def normalize_search_query(query: str) -> str:
    """Normalize user input."""
    value = " ".join((query or "").split())
    if not value:
        raise SpotifySearchError("Search query is empty")
    if len(value) > 200:
        raise SpotifySearchError("Search query is too long")
    return value


class SpotifySearchService:
    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url
        self._search_slots = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
        self._page_slots = asyncio.Semaphore(MAX_CONCURRENT_PAGE_BUILDS)

    async def search(self, query: str) -> tuple[SpotifySearchResult, ...]:
        """Search Spotify for tracks. 
        Note: This is a placeholder implementation. To get real results, 
        you can use a public Spotify API or a scraper.
        """
        normalized = normalize_search_query(query)
        
        # Placeholder results for demonstration. 
        # Replace this logic with actual Spotify API calls if available.
        results = []
        for i in range(MAX_RESULTS):
            results.append(
                SpotifySearchResult(
                    track_id=f"track_{i}",
                    title=f"آهنگ {i+1} - {normalized}",
                    artist="Spotify Artist",
                    url=f"https://open.spotify.com/track/4cOdK2wGqyBM7YV90ncpB7", # Example track
                    thumbnail_url="https://i.scdn.co/image/ab67616d0000b273760980456e3006a8830b776a" # Spotify placeholder
                )
            )
        return tuple(results)

    async def _download_thumbnail(self, client: httpx.AsyncClient, result: SpotifySearchResult) -> bytes | None:
        try:
            async with client.stream("GET", result.thumbnail_url) as response:
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_THUMBNAIL_BYTES:
                        payload.clear()
                        break
                if payload:
                    return bytes(payload)
        except Exception:
            return None
        return None

    async def build_page_image(self, results: Sequence[SpotifySearchResult], page: int) -> bytes:
        page_count = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        if page < 0 or page >= page_count:
            raise SpotifySearchError("Search page is out of range")
        start = page * RESULTS_PER_PAGE
        page_results = tuple(results[start:start + RESULTS_PER_PAGE])
        
        async with self._page_slots:
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


def _load_label_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", 42)
    except OSError:
        return ImageFont.load_default(size=42)


def _placeholder_thumbnail() -> Image.Image:
    image = Image.new("RGB", (CARD_WIDTH, THUMBNAIL_HEIGHT), "#1db954") # Spotify Green
    draw = ImageDraw.Draw(image)
    center_x, center_y = CARD_WIDTH // 2, THUMBNAIL_HEIGHT // 2
    draw.ellipse((center_x - 30, center_y - 30, center_x + 30, center_y + 30), outline="white", width=4)
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
    except Exception:
        return _placeholder_thumbnail()


def render_collage(thumbnails: Sequence[bytes | None], first_number: int) -> bytes:
    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), "#121212") # Dark theme
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
            fill="#1db954", # Spotify Green
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
            fill="white",
            font=font,
        )
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    return output.getvalue()
