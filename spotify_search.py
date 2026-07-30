"""Spotify search via Browser Automation on Chosic.com."""

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
        """Search Spotify using browser automation on Chosic.com."""
        from bot import browser_navigate, browser_input, browser_console_exec
        
        normalized = normalize_search_query(query)
        results = []
        
        try:
            # 1. Navigate to Chosic
            await browser_navigate(
                brief=f"Searching for '{normalized}' on Chosic",
                url=f"https://www.chosic.com/find-song-by-lyrics/?query={quote(normalized)}",
                intent="informational"
            )
            
            # Wait for Google CSE and Chosic scripts to load
            await asyncio.sleep(5)
            
            # 2. Extract data using browser console
            # We use a script that simulates clicking the Spotify icon and grabbing the ID
            extraction_script = """
            (async () => {
                const items = [];
                const titleElements = document.querySelectorAll(".gsc-thumbnail-inside a.gs-title");
                
                // Limit to 30 results
                const count = Math.min(titleElements.length, 30);
                
                for (let i = 0; i < count; i++) {
                    let fullTitle = titleElements[i].innerText;
                    if (!fullTitle) continue;
                    
                    // Cleanup title
                    let title = fullTitle.split("Lyrics")[0].replace(" | Genius", "").replace(" | Musixmatch", "").trim();
                    
                    // We call Chosic's internal API from the browser context to avoid 401/403
                    const apiUrl = window.location.protocol + "//" + window.location.host + '/api/tools/search?q=' + encodeURIComponent(title) + '&type=track&limit=1';
                    try {
                        const r = await fetch(apiUrl);
                        if (r.ok) {
                            const d = await r.json();
                            if (d.tracks && d.tracks.items && d.tracks.items.length > 0) {
                                const item = d.tracks.items[0];
                                items.push({
                                    track_id: item.id,
                                    title: item.name,
                                    artist: item.artist,
                                    url: "https://open.spotify.com/track/" + item.id,
                                    thumbnail_url: item.image
                                });
                            }
                        }
                    } catch(e) {}
                }
                return JSON.stringify(items);
            })();
            """
            
            res_json = await browser_console_exec(
                brief="Extracting Spotify data from Chosic",
                javascript=extraction_script
            )
            
            if res_json and isinstance(res_json, str):
                try:
                    extracted = json.loads(res_json)
                    for item in extracted:
                        results.append(SpotifySearchResult(**item))
                except Exception as e:
                    logger.error(f"Failed to parse extracted JSON: {e}")

        except Exception as exc:
            logger.error(f"Browser-based search failed: {exc}")
            
        if not results:
            # Last ditch effort: Try a simple regex search in the page source if console failed
            raise SpotifySearchError("متأسفانه نتیجه‌ای پیدا نشد. لطفاً عبارت دیگری را امتحان کنید.")

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


def quote(text: str) -> str:
    from urllib.parse import quote as url_quote
    return url_quote(text)
