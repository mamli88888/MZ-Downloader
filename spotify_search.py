"""Spotify search via Deezer and Odesli mapping (Pure Python)."""

from __future__ import annotations

import asyncio
import io
import logging
import json
import re
from urllib.parse import quote
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


@dataclass(frozen=True)
class SpotifySearchResult:
    track_id: str
    title: str
    artist: str
    url: str
    thumbnail_url: str


class SpotifySearchError(RuntimeError):
    """Raised when Spotify search fails."""


def normalize_search_query(query: str) -> str:
    """Normalize user input."""
    value = " ".join((query or "").split())
    if not value:
        raise SpotifySearchError("Search query is empty")
    return value


class SpotifySearchService:
    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url
        self._page_slots = asyncio.Semaphore(MAX_CONCURRENT_PAGE_BUILDS)

    async def search(self, query: str) -> tuple[SpotifySearchResult, ...]:
        """Search Spotify using Deezer API and Odesli mapping."""
        normalized = normalize_search_query(query)
        results = []
        
        async with httpx.AsyncClient(timeout=20.0, proxy=self.proxy_url, follow_redirects=True) as client:
            try:
                # 1. Search Deezer (Reliable, no auth, gives good metadata)
                deezer_res = await client.get(f"https://api.deezer.com/search?q={quote(normalized)}&limit=30")
                if deezer_res.status_code == 200:
                    deezer_data = deezer_res.json().get("data", [])
                    
                    # We process items in parallel to speed up mapping
                    tasks = []
                    for item in deezer_data:
                        tasks.append(self._map_to_spotify(client, item))
                    
                    mapped_results = await asyncio.gather(*tasks)
                    results = [r for r in mapped_results if r is not None]
            except Exception as e:
                logger.error(f"Deezer search failed: {e}")

        if not results:
            # Final fallback: iTunes (if Deezer is down)
            try:
                async with httpx.AsyncClient(timeout=15.0, proxy=self.proxy_url) as client:
                    itunes_res = await client.get(
                        "https://itunes.apple.com/search",
                        params={"term": normalized, "media": "music", "limit": 15}
                    )
                    if itunes_res.status_code == 200:
                        itunes_data = itunes_res.json().get("results", [])
                        for item in itunes_data:
                            # Construct a search URL as a fallback if mapping is too slow
                            results.append(
                                SpotifySearchResult(
                                    track_id=str(item.get("trackId")),
                                    title=item.get("trackName", "Unknown"),
                                    artist=item.get("artistName", "Unknown"),
                                    url=f"https://open.spotify.com/search/{quote(item.get('trackName', '') + ' ' + item.get('artistName', ''))}",
                                    thumbnail_url=item.get("artworkUrl100", "").replace("100x100", "600x600"),
                                )
                            )
            except Exception as e:
                logger.error(f"iTunes fallback failed: {e}")

        if not results:
            raise SpotifySearchError("هیچ نتیجه‌ای پیدا نشد. لطفاً دوباره تلاش کنید.")

        return tuple(results[:MAX_RESULTS])

    async def _map_to_spotify(self, client: httpx.AsyncClient, deezer_item: dict) -> SpotifySearchResult | None:
        """Map a Deezer track to a Spotify track using Odesli or ISRC."""
        try:
            title = deezer_item.get("title", "Unknown")
            artist = deezer_item.get("artist", {}).get("name", "Unknown")
            thumbnail = deezer_item.get("album", {}).get("cover_xl") or deezer_item.get("album", {}).get("cover_medium")
            deezer_url = deezer_item.get("link")
            
            # Use Odesli to find Spotify link
            odesli_res = await client.get(f"https://api.song.link/v1-alpha.1/links?url={quote(deezer_url)}")
            if odesli_res.status_code == 200:
                spotify_data = odesli_res.json().get("linksByPlatform", {}).get("spotify")
                if spotify_data:
                    spotify_url = spotify_data.get("url")
                    track_id_match = re.search(r"track/([a-zA-Z0-9]+)", spotify_url)
                    track_id = track_id_match.group(1) if track_id_match else "unknown"
                    return SpotifySearchResult(
                        track_id=track_id,
                        title=title,
                        artist=artist,
                        url=spotify_url,
                        thumbnail_url=thumbnail
                    )
            
            # Fallback: Just return a search URL if mapping fails
            return SpotifySearchResult(
                track_id=f"search_{deezer_item.get('id')}",
                title=title,
                artist=artist,
                url=f"https://open.spotify.com/search/{quote(title + ' ' + artist)}",
                thumbnail_url=thumbnail
            )
        except Exception:
            return None

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
                "timeout": httpx.Timeout(15.0, connect=10.0),
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
