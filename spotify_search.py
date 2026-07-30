"""Spotify search via Chosic API with robust fallback."""

from __future__ import annotations

import asyncio
import io
import logging
import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

MAX_RESULTS = 30
RESULTS_PER_PAGE = 6
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024
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
        self._page_slots = asyncio.Semaphore(MAX_CONCURRENT_PAGE_BUILDS)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://www.chosic.com/find-song-by-lyrics/",
            "X-Requested-With": "XMLHttpRequest",
        }

    async def search(self, query: str) -> tuple[SpotifySearchResult, ...]:
        """Search Spotify using Chosic API or iTunes fallback."""
        normalized = normalize_search_query(query)
        results = []

        # Try Chosic API first (as requested by user)
        try:
            async with httpx.AsyncClient(timeout=10.0, proxy=self.proxy_url, follow_redirects=True) as client:
                # We use the search endpoint that returns up to 30 results
                response = await client.get(
                    f"https://www.chosic.com/api/tools/search?q={httpx.utils.quote(normalized)}&type=track&limit=30",
                    headers=self.headers
                )
                if response.status_code == 200:
                    data = response.json()
                    if "tracks" in data and "items" in data["tracks"]:
                        for item in data["tracks"]["items"]:
                            results.append(
                                SpotifySearchResult(
                                    track_id=item["id"],
                                    title=item["name"],
                                    artist=item["artist"],
                                    url=f"https://open.spotify.com/track/{item['id']}",
                                    thumbnail_url=item.get("image", ""),
                                )
                            )
        except Exception as exc:
            logger.warning("Chosic API failed, falling back to iTunes: %s", exc)

        # Fallback to iTunes API if Chosic failed or returned no results
        if not results:
            try:
                async with httpx.AsyncClient(timeout=10.0, proxy=self.proxy_url) as client:
                    response = await client.get(
                        "https://itunes.apple.com/search",
                        params={"term": normalized, "media": "music", "limit": 30}
                    )
                    if response.status_code == 200:
                        data = response.json()
                        for item in data.get("results", []):
                            # Construct a "best effort" Spotify search link or use a mapper
                            # For the sake of the bot, we'll use the title/artist to construct a search URL
                            # that the bot's download logic can potentially handle or just show.
                            # But we really want a track ID.
                            # Since we don't have a Spotify ID from iTunes, we'll mark it for later.
                            results.append(
                                SpotifySearchResult(
                                    track_id=item.get("trackId", ""),
                                    title=item.get("trackName", "Unknown"),
                                    artist=item.get("artistName", "Unknown"),
                                    url=f"https://open.spotify.com/search/{httpx.utils.quote(item.get('trackName', '') + ' ' + item.get('artistName', ''))}",
                                    thumbnail_url=item.get("artworkUrl100", "").replace("100x100", "600x600"),
                                )
                            )
            except Exception as exc:
                logger.error("iTunes fallback failed: %s", exc)

        if not results:
            raise SpotifySearchError("هیچ نتیجه‌ای پیدا نشد.")

        return tuple(results[:MAX_RESULTS])

    async def _download_thumbnail(self, client: httpx.AsyncClient, result: SpotifySearchResult) -> bytes | None:
        if not result.thumbnail_url:
            return None
        try:
            async with client.stream("GET", result.thumbnail_url) as response:
                if response.status_code != 200:
                    return None
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
