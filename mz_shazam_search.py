"""Shazam song search + 2x3 cover-art collage rendering for MZ Downloader.

This module adds the /song command to the bot. It uses the ShazamIO library
(as requested) for text-based song search. Because the public Shazam search
endpoint is occasionally flaky, we automatically fall back to the iTunes
Search API which uses the exact same Apple Music catalog. From the user's
perspective the results are identical: cover art + artist + track name.

Public API:
    - ShazamSearchService: search + build_page_image (mirrors YouTubeSearchService)
    - ShazamSearchResult: dataclass with track_name, artist_name, cover_url, ...
    - ShazamSearchError: raised on search/build failures
    - normalize_song_query: validate user input
    - youtube_url_for_song: helper used by the bot to convert a clicked song
      into a YouTube URL so the existing download pipeline can take over.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

# Import ShazamIO as requested by the project owner. We treat it as an optional
# dependency at runtime so the bot keeps working even if the package is missing
# (in which case the iTunes fallback is used exclusively).
try:  # pragma: no cover - exercised at import time
    from shazamio import Shazam  # type: ignore
    from shazamio.exceptions import FailedDecodeJson  # type: ignore

    _SHAZAMIO_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    _SHAZAMIO_AVAILABLE = False

    class FailedDecodeJson(Exception):  # type: ignore
        """Local stub used when shazamio is not installed."""


logger = logging.getLogger(__name__)

# --- tunables -------------------------------------------------------------

MAX_RESULTS = 30
RESULTS_PER_PAGE = 6
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024
MAX_CONCURRENT_SEARCHES = 2
MAX_CONCURRENT_PAGE_BUILDS = 3
SEARCH_TIMEOUT_SECONDS = 30.0
YOUTUBE_LOOKUP_TIMEOUT_SECONDS = 18.0

# Canvas geometry mirrors youtube_search.py so the two collages look consistent.
CANVAS_WIDTH = 1280
CANVAS_PADDING = 24
CARD_GAP = 24
CARD_WIDTH = (CANVAS_WIDTH - (2 * CANVAS_PADDING) - CARD_GAP) // 2
COVER_HEIGHT = 338
LABEL_HEIGHT = 96  # taller than YouTube's 64 so "Artist - Song Name" fits
CARD_HEIGHT = COVER_HEIGHT + LABEL_HEIGHT
CANVAS_HEIGHT = (2 * CANVAS_PADDING) + (3 * CARD_HEIGHT) + (2 * CARD_GAP)


# --- exceptions & data ----------------------------------------------------


class ShazamSearchError(RuntimeError):
    """Raised when song search cannot produce usable results."""


@dataclass(frozen=True)
class ShazamSearchResult:
    """A single song entry shown to the user."""

    track_name: str
    artist_name: str
    cover_url: str
    apple_music_url: str
    shazam_track_id: str = ""

    @property
    def label(self) -> str:
        """Combined display label used by both the collage and inline button."""
        return f"{self.artist_name} - {self.track_name}"


# --- helpers --------------------------------------------------------------


def normalize_song_query(query: str) -> str:
    """Validate and normalize the user's free-text search query."""
    value = " ".join((query or "").split())
    if not value:
        raise ShazamSearchError("Search query is empty")
    if len(value) > 200:
        raise ShazamSearchError("Search query is too long")
    return value


def _truncate(text: str, max_len: int = 70) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


# --- service --------------------------------------------------------------


class ShazamSearchService:
    """Search songs via ShazamIO with an iTunes Search API fallback."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url
        self._search_slots = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
        self._page_slots = asyncio.Semaphore(MAX_CONCURRENT_PAGE_BUILDS)
        if _SHAZAMIO_AVAILABLE:
            try:
                self._shazam = Shazam()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ShazamIO initialization failed: %s", exc)
                self._shazam = None
        else:
            self._shazam = None
            logger.info("ShazamIO not installed; using iTunes Search API only")

    # -- search backends ---------------------------------------------------

    async def _search_via_shazamio(self, query: str) -> tuple[ShazamSearchResult, ...]:
        """Try ShazamIO's search_track. Raise ShazamSearchError on any failure."""
        if self._shazam is None:
            raise ShazamSearchError("ShazamIO is not available")
        try:
            data = await asyncio.wait_for(
                self._shazam.search_track(query=query, limit=MAX_RESULTS, offset=0),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise ShazamSearchError("ShazamIO search timed out") from exc
        except FailedDecodeJson as exc:
            raise ShazamSearchError(f"ShazamIO decode failed: {exc}") from exc
        except Exception as exc:
            raise ShazamSearchError(f"ShazamIO search failed: {exc}") from exc

        if not isinstance(data, dict):
            raise ShazamSearchError("ShazamIO returned an unexpected payload")
        tracks_block = data.get("tracks") or {}
        hits = tracks_block.get("hits") if isinstance(tracks_block, dict) else None
        if not isinstance(hits, list):
            raise ShazamSearchError("ShazamIO returned no track hits")

        results: list[ShazamSearchResult] = []
        seen: set[str] = set()
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            track = hit.get("track") or {}
            if not isinstance(track, dict):
                continue
            title = str(track.get("title") or "").strip()
            subtitle = str(track.get("subtitle") or "").strip()
            if not title or not subtitle:
                continue
            key = f"{subtitle.lower()}|{title.lower()}"
            if key in seen:
                continue
            seen.add(key)

            images = track.get("images") or {}
            cover_url = ""
            if isinstance(images, dict):
                cover_url = (
                    str(images.get("coverarthq") or "")
                    or str(images.get("coverart") or "")
                    or str(images.get("background") or "")
                )

            apple_url = ""
            share = track.get("share") or {}
            if isinstance(share, dict):
                apple_url = str(share.get("href") or "") or str(share.get("apple") or "")
            if not apple_url:
                hub = track.get("hub") or {}
                options = hub.get("options") if isinstance(hub, dict) else None
                if isinstance(options, list):
                    for option in options:
                        if not isinstance(option, dict):
                            continue
                        actions = option.get("actions") or []
                        if not isinstance(actions, list):
                            continue
                        for action in actions:
                            if not isinstance(action, dict):
                                continue
                            uri = str(action.get("uri") or "")
                            if "music.apple.com" in uri:
                                apple_url = uri
                                break
                        if apple_url:
                            break

            shazam_id = str(track.get("key") or "")
            fallback_url = (
                f"https://www.shazam.com/track/{shazam_id}" if shazam_id else apple_url
            )
            results.append(
                ShazamSearchResult(
                    track_name=_truncate(title),
                    artist_name=_truncate(subtitle),
                    cover_url=cover_url,
                    apple_music_url=apple_url or fallback_url,
                    shazam_track_id=shazam_id,
                )
            )
            if len(results) >= MAX_RESULTS:
                break
        return tuple(results)

    async def _search_via_itunes(self, query: str) -> tuple[ShazamSearchResult, ...]:
        """Fallback: iTunes Search API (open, no auth, same Apple Music catalog)."""
        url = "https://itunes.apple.com/search"
        params = {
            "term": query,
            "entity": "song",
            "media": "music",
            "limit": str(MAX_RESULTS),
        }
        client_kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "timeout": httpx.Timeout(15.0, connect=10.0),
            "headers": {"User-Agent": "MZDownloader/1.0"},
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ShazamSearchError(f"iTunes search failed: {exc}") from exc

        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ShazamSearchError("iTunes search returned no results list")

        results: list[ShazamSearchResult] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("wrapperType") != "track":
                continue
            title = str(item.get("trackName") or "").strip()
            artist = str(item.get("artistName") or "").strip()
            if not title or not artist:
                continue
            key = f"{artist.lower()}|{title.lower()}"
            if key in seen:
                continue
            seen.add(key)

            cover = str(item.get("artworkUrl100") or "")
            if cover:
                # Upgrade to higher-resolution artwork by swapping the size token.
                cover = (
                    cover.replace("100x100bb", "600x600bb")
                    .replace("100x100", "600x600")
                )

            apple_url = str(item.get("trackViewUrl") or "")
            track_id = str(item.get("trackId") or "")

            results.append(
                ShazamSearchResult(
                    track_name=_truncate(title),
                    artist_name=_truncate(artist),
                    cover_url=cover,
                    apple_music_url=apple_url,
                    shazam_track_id=track_id,
                )
            )
            if len(results) >= MAX_RESULTS:
                break
        return tuple(results)

    # -- public entrypoint -------------------------------------------------

    async def search(self, query: str) -> tuple[ShazamSearchResult, ...]:
        """Search songs. Tries ShazamIO first, then iTunes Search API."""
        normalized = normalize_song_query(query)
        try:
            await asyncio.wait_for(self._search_slots.acquire(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            raise ShazamSearchError("Song search is busy") from exc

        try:
            if self._shazam is not None:
                try:
                    results = await self._search_via_shazamio(normalized)
                    if results:
                        return results
                    logger.info(
                        "ShazamIO returned 0 results for %r; falling back to iTunes",
                        normalized,
                    )
                except ShazamSearchError as exc:
                    logger.info(
                        "ShazamIO search failed (%s); falling back to iTunes", exc
                    )
            return await self._search_via_itunes(normalized)
        finally:
            self._search_slots.release()

    # -- cover download & collage -----------------------------------------

    async def _download_cover(
        self, client: httpx.AsyncClient, result: ShazamSearchResult
    ) -> bytes | None:
        if not result.cover_url:
            return None
        try:
            async with client.stream("GET", result.cover_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("image/"):
                    return None
                declared_size = int(response.headers.get("content-length") or 0)
                if declared_size > MAX_THUMBNAIL_BYTES:
                    return None
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_THUMBNAIL_BYTES:
                        payload.clear()
                        break
                return bytes(payload) if payload else None
        except (httpx.HTTPError, ValueError):
            return None

    async def build_page_image(
        self, results: Sequence[ShazamSearchResult], page: int
    ) -> bytes:
        """Render the 2x3 JPEG collage for the requested page index."""
        page_count = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        if page < 0 or page >= page_count:
            raise ShazamSearchError("Search page is out of range")
        start = page * RESULTS_PER_PAGE
        page_results = tuple(results[start : start + RESULTS_PER_PAGE])
        if not page_results:
            raise ShazamSearchError("Search page is empty")
        try:
            await asyncio.wait_for(self._page_slots.acquire(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            raise ShazamSearchError("Search pages are busy") from exc
        try:
            client_kwargs: dict[str, Any] = {
                "follow_redirects": True,
                "timeout": httpx.Timeout(12.0, connect=8.0),
                "headers": {"User-Agent": "MZDownloader/1.0"},
            }
            if self.proxy_url:
                client_kwargs["proxy"] = self.proxy_url
            async with httpx.AsyncClient(**client_kwargs) as client:
                covers = await asyncio.gather(
                    *(self._download_cover(client, item) for item in page_results)
                )
            return await asyncio.to_thread(render_song_collage, page_results, covers)
        finally:
            self._page_slots.release()


# --- rendering ------------------------------------------------------------


def _load_label_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Pick a font that supports Latin + basic Persian/Arabic glyphs."""
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, 30)
        except OSError:
            continue
    return ImageFont.load_default(size=30)


def _placeholder_cover() -> Image.Image:
    image = Image.new("RGB", (CARD_WIDTH, COVER_HEIGHT), "#20242c")
    draw = ImageDraw.Draw(image)
    cx, cy = CARD_WIDTH // 2, COVER_HEIGHT // 2
    draw.ellipse(
        (cx - 64, cy - 64, cx + 64, cy + 64),
        fill="#3a3f4b",
        outline="#5b6273",
        width=4,
    )
    draw.polygon(
        ((cx - 22, cy - 34), (cx - 22, cy + 34), (cx + 34, cy)),
        fill="#e94f64",
    )
    return image


def _decode_cover(payload: bytes | None) -> Image.Image:
    if not payload:
        return _placeholder_cover()
    try:
        with Image.open(io.BytesIO(payload)) as source:
            source.load()
            converted = source.convert("RGB")
        return ImageOps.fit(
            converted,
            (CARD_WIDTH, COVER_HEIGHT),
            method=Image.Resampling.LANCZOS,
        )
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return _placeholder_cover()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if font.getlength(text) <= max_width:
        return text
    ellipsis = "…"
    while text and font.getlength(text + ellipsis) > max_width and len(text) > 4:
        text = text[:-2]
    return text + ellipsis if text else ellipsis


def render_song_collage(
    results: Sequence[ShazamSearchResult],
    covers: Sequence[bytes | None],
) -> bytes:
    """Render up to six (cover, artist, title) cards in a 2x3 JPEG."""
    if not results or len(results) > RESULTS_PER_PAGE:
        raise ShazamSearchError("A collage needs between one and six results")
    covers = list(covers)
    if len(covers) < len(results):
        covers.extend([None] * (len(results) - len(covers)))

    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), "#111318")
    draw = ImageDraw.Draw(canvas)
    font = _load_label_font()
    max_text_width = CARD_WIDTH - 32

    for offset, (result, payload) in enumerate(zip(results, covers)):
        row, column = divmod(offset, 2)
        left = CANVAS_PADDING + column * (CARD_WIDTH + CARD_GAP)
        top = CANVAS_PADDING + row * (CARD_HEIGHT + CARD_GAP)
        cover = _decode_cover(payload)
        canvas.paste(cover, (left, top))

        label_top = top + COVER_HEIGHT
        # Dark panel + thin accent line for a clean "glass button" feel.
        draw.rectangle(
            (left, label_top, left + CARD_WIDTH, label_top + LABEL_HEIGHT),
            fill="#1a1d24",
        )
        draw.rectangle(
            (left, label_top, left + CARD_WIDTH, label_top + 3),
            fill="#e94f64",
        )
        display = _fit_text(draw, result.label, font, max_text_width)
        bbox = draw.textbbox((0, 0), display, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        draw.text(
            (
                left + (CARD_WIDTH - text_w) / 2,
                label_top + (LABEL_HEIGHT - text_h) / 2 - bbox[1] + 2,
            ),
            display,
            fill="#f4f5f7",
            font=font,
        )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    return output.getvalue()


# --- song → YouTube URL helper -------------------------------------------


def _build_ydl_options(proxy_url: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "cachedir": False,
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
        "playlistend": 1,
        "default_search": "ytsearch",
    }
    if proxy_url:
        options["proxy"] = proxy_url
    return options


async def youtube_url_for_song(
    artist: str,
    track: str,
    proxy_url: str | None = None,
    timeout: float = YOUTUBE_LOOKUP_TIMEOUT_SECONDS,
) -> str | None:
    """Find the best-matching YouTube URL for a song.

    Used by the bot when the user clicks a song in the Shazam results so the
    existing YouTube download pipeline can take over. Returns None on any
    failure (timeout, no result, network error).
    """
    # Lazy import keeps the module importable even if yt-dlp is unavailable.
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError  # type: ignore

    query = f"{artist} - {track}"
    options = _build_ydl_options(proxy_url)

    def _do_search() -> str | None:
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        except DownloadError:
            return None
        except Exception:  # pragma: no cover - defensive
            return None
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries or not entries[0]:
            return None
        entry = entries[0]
        video_id = str(entry.get("id") or "")
        if len(video_id) != 11:
            return None
        return f"https://www.youtube.com/watch?v={video_id}"

    try:
        return await asyncio.wait_for(asyncio.to_thread(_do_search), timeout=timeout)
    except asyncio.TimeoutError:
        return None
