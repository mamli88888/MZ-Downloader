"""Apify-backed download gateway for public YouTube and Instagram URLs.

The gateway is isolated from the existing Telegram-bot and self-hosted paths.
It first returns a normal ``needs_selection`` quality menu and only starts an
Actor after the user chooses an option. If Apify is unavailable, a selected
Actor fails, or every configured token is exhausted, callers receive an error
result and can continue through the repository's existing fallback chain.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

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

logger = logging.getLogger("MZDownloader.apify")

APIFY_PROVIDER = "apify"
APIFY_API_BASE = "https://api.apify.com/v2"
YOUTUBE_ACTOR_ID = "streamers/youtube-video-downloader"
INSTAGRAM_ACTOR_ID = "apify/instagram-scraper"

# The Streamers YouTube Actor documents these output resolutions. The final
# audio option uses the same Actor with preferredFormat=mp3.
YOUTUBE_QUALITIES: tuple[tuple[str, int], ...] = (
    ("144p", 144),
    ("240p", 240),
    ("360p", 360),
    ("480p", 480),
    ("720p", 720),
    ("1080p", 1080),
    ("1440p", 1440),
    ("2160p (4K)", 2160),
)

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ProcessingCallback = Callable[[int, str, str], Awaitable[None]]
MENU_PREVIEW_MAX_BYTES = 4 * 1024 * 1024
_OG_META_RE = re.compile(
    r'<meta\s+(?:property|name)=["\'](?P<key>[^"\']+)["\']\s+content=["\'](?P<value>[^"\']*)["\']',
    re.IGNORECASE,
)

_TOKEN_FAILURE_TERMS = (
    "billing",
    "charge",
    "credit",
    "limit",
    "payment",
    "quota",
    "rate limit",
    "token",
    "unauthorized",
)


class ApifyError(InvalidDownload):
    """An Apify API or Actor error with enough detail for token failover."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", Path(name).name).strip(" .")
    return cleaned or "media"


def _actor_reference(actor_id: str) -> str:
    """Convert Store-style ``owner/actor`` IDs to the API's named ID form."""
    return actor_id.replace("/", "~", 1)


def _fingerprint(payload: dict[str, Any]) -> str:
    return "apify:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint.startswith("apify:"):
        return None
    try:
        value = json.loads(fingerprint[len("apify:"):])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _instagram_results_type(url: str) -> str:
    path = urlsplit(url).path.lower()
    if "/reel/" in path or "/reels/" in path:
        return "reels"
    return "posts"


def _as_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if urlsplit(candidate).scheme in {"http", "https"} else None


def _youtube_video_id(url: str) -> str | None:
    """Extract the short public ID needed for YouTube's thumbnail CDN."""
    parsed = urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host.endswith("youtube.com"):
        candidate = parse_qs(parsed.query).get("v", [""])[0]
        if not candidate and parsed.path.startswith("/shorts/"):
            candidate = parsed.path.split("/shorts/", 1)[1].split("/", 1)[0]
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", candidate or "") else None


def _extract_og_meta(page: str, key: str) -> str | None:
    target = key.lower()
    for match in _OG_META_RE.finditer(page):
        if match.group("key").lower() == target:
            return html.unescape(match.group("value").strip()) or None
    return None


def option_size_hint(option: QualityOption) -> str:
    """Return a transparent per-minute size estimate for menu button labels."""
    payload = _decode_fingerprint(option.fingerprint) or {}
    if payload.get("platform") == "instagram" and payload.get("kind") == "video":
        return "حجم اصلی"
    if option.expected_kind == MediaKind.AUDIO:
        return "≈1.4MB/min"
    by_height = {
        144: 2.0,
        240: 3.5,
        360: 6.0,
        480: 10.0,
        720: 19.0,
        1080: 36.0,
        1440: 65.0,
        2160: 130.0,
    }
    amount = by_height.get(option.expected_height or 0)
    if amount is None:
        return "حجم تقریبی"
    rendered = str(int(amount)) if amount.is_integer() else f"{amount:.1f}"
    return f"≈{rendered}MB/min"


def _guess_suffix(url: str, content_type: str | None, kind: MediaKind) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix and 1 < len(suffix) <= 8:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    if kind == MediaKind.VIDEO:
        return ".mp4"
    if kind == MediaKind.PHOTO:
        return ".jpg"
    if kind == MediaKind.AUDIO:
        return ".mp3"
    return ".bin"


def _mime_for(kind: MediaKind, suffix: str, content_type: str | None) -> str:
    if content_type:
        return content_type.split(";", 1)[0].strip() or "application/octet-stream"
    guessed, _ = mimetypes.guess_type(f"media{suffix}")
    if guessed:
        return guessed
    if kind == MediaKind.VIDEO:
        return "video/mp4"
    if kind == MediaKind.PHOTO:
        return "image/jpeg"
    if kind == MediaKind.AUDIO:
        return "audio/mpeg"
    return "application/octet-stream"


def _instagram_media_specs(item: Any) -> list[tuple[str, MediaKind]]:
    """Extract video/image assets, including carousel child posts, recursively."""
    collected: list[tuple[str, MediaKind]] = []
    seen: set[str] = set()

    def add(candidate: Any, kind: MediaKind) -> None:
        url = _as_http_url(candidate)
        if url and url not in seen:
            seen.add(url)
            collected.append((url, kind))

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        add(value.get("videoUrl"), MediaKind.VIDEO)
        # Photos are a fallback for the current post only. Carousel children
        # are always visited, whether or not the parent happens to be a video.
        if not _as_http_url(value.get("videoUrl")):
            add(value.get("displayUrl"), MediaKind.PHOTO)
            images = value.get("images")
            if isinstance(images, list):
                for image in images:
                    if isinstance(image, dict):
                        add(image.get("url") or image.get("displayUrl"), MediaKind.PHOTO)
                    else:
                        add(image, MediaKind.PHOTO)
        children = value.get("childPosts")
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(item)
    return collected


class ApifyGateway:
    """Run selected Apify Actors and rotate tokens only on token-side failures."""

    def __init__(
        self,
        *,
        tokens: tuple[str, ...],
        run_timeout: float = 360.0,
        poll_interval: float = 3.0,
        token_cooldown: float = 600.0,
        max_download_size: int = 0,
        api_base: str = APIFY_API_BASE,
    ) -> None:
        self.tokens = tuple(dict.fromkeys(token.strip() for token in tokens if token.strip()))
        self.run_timeout = run_timeout
        self.poll_interval = poll_interval
        self.token_cooldown = token_cooldown
        self.max_download_size = max_download_size
        self.api_base = api_base.rstrip("/")
        self._next_token_index = 0
        self._token_cooldown_until: dict[int, float] = {}
        self._token_lock = asyncio.Lock()

    async def request(
        self,
        *,
        url: str,
        platform: Platform,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> GatewayResult:
        """Return a quality menu and best-effort preview without spending credit."""
        del progress_callback  # Kept for the common gateway contract.
        if not self.tokens:
            return GatewayResult(status="error", bot_username=APIFY_PROVIDER, reason="apify_unconfigured")
        if platform == Platform.YOUTUBE:
            options = self._youtube_options()
        elif platform == Platform.INSTAGRAM:
            options = self._instagram_options()
        else:
            return GatewayResult(status="error", bot_username=APIFY_PROVIDER, reason="apify_unsupported")
        preview, caption = await self._menu_assets(url, platform, attempt_directory)
        return GatewayResult(
            status="needs_selection",
            bot_username=APIFY_PROVIDER,
            options=options,
            preview=preview,
            text=caption,
        )

    async def _menu_assets(
        self,
        url: str,
        platform: Platform,
        attempt_directory: Path,
    ) -> tuple[DownloadedMedia | None, str]:
        """Fetch an actual source thumbnail before showing the quality card.

        This is intentionally best-effort and independent of Actor execution so
        no Apify credit is used merely to open a menu.
        """
        title: str | None = None
        thumbnail_url: str | None = None
        try:
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8",
                },
                follow_redirects=True,
                timeout=httpx.Timeout(connect=5.0, read=8.0, write=8.0, pool=5.0),
            ) as client:
                if platform == Platform.YOUTUBE:
                    video_id = _youtube_video_id(url)
                    if video_id:
                        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    try:
                        oembed = await client.get(
                            "https://www.youtube.com/oembed",
                            params={"url": url, "format": "json"},
                        )
                        if oembed.status_code == 200 and isinstance(oembed.json(), dict):
                            title_value = oembed.json().get("title")
                            title = str(title_value).strip() if title_value else None
                    except Exception:
                        pass
                else:
                    page = await client.get(url)
                    if page.status_code == 200:
                        page_text = page.text[:1_500_000]
                        thumbnail_url = _as_http_url(_extract_og_meta(page_text, "og:image"))
                        title = _extract_og_meta(page_text, "og:title")
                preview = await self._download_menu_preview(client, thumbnail_url, attempt_directory)
        except Exception as exc:
            logger.debug("Apify menu thumbnail fetch failed for %s: %s", url, exc)
            preview = None
        platform_label = "🎬 YouTube" if platform == Platform.YOUTUBE else "📸 Instagram"
        caption_lines = [f"<b>{html.escape(title)}</b>" if title else platform_label]
        if title:
            caption_lines.append(platform_label)
        caption_lines.append("کیفیت یا صدا را از دکمه‌ها انتخاب کن.")
        return preview, "\n".join(caption_lines)

    async def _download_menu_preview(
        self,
        client: httpx.AsyncClient,
        thumbnail_url: str | None,
        attempt_directory: Path,
    ) -> DownloadedMedia | None:
        if not thumbnail_url:
            return None
        response = await client.get(thumbnail_url)
        if response.status_code != 200:
            return None
        content_type = response.headers.get("content-type", "").lower()
        if not content_type.startswith("image/"):
            return None
        if len(response.content) == 0 or len(response.content) > MENU_PREVIEW_MAX_BYTES:
            return None
        extension = ".png" if "png" in content_type else ".jpg"
        attempt_directory.mkdir(parents=True, exist_ok=True)
        destination = attempt_directory / f"_apify_preview{extension}"
        destination.write_bytes(response.content)
        return DownloadedMedia(
            path=destination,
            kind=MediaKind.PHOTO,
            source_message_id=0,
            mime_type=content_type.split(";", 1)[0],
            size=destination.stat().st_size,
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
        """Start the chosen Actor option and download its generated media."""
        payload = _decode_fingerprint(option.fingerprint)
        if payload is None or payload.get("platform") != platform.value:
            return GatewayResult(status="error", bot_username=APIFY_PROVIDER, reason="invalid_fingerprint")
        if not self.tokens:
            return GatewayResult(status="error", bot_username=APIFY_PROVIDER, reason="apify_unconfigured")

        try:
            actor_id, actor_input, expected_kind, convert_instagram_audio = self._actor_request(
                url, platform, payload
            )
            if processing_callback is not None:
                await processing_callback(16, "☁️ درخواست به Apify ارسال شد…", "Actor در حال شروع است")
            result = await self._run_with_failover(actor_id, actor_input, processing_callback)
            if processing_callback is not None:
                await processing_callback(58, "📦 فایل آماده شد…", "دارم لینک خروجی را می‌گیرم")
            media_specs = self._media_specs(platform, result["items"], expected_kind)
            if convert_instagram_audio:
                media_specs = [spec for spec in media_specs if spec[1] == MediaKind.VIDEO]
            if not media_specs:
                return GatewayResult(
                    status="error",
                    bot_username=APIFY_PROVIDER,
                    reason="apify_no_media_url",
                )

            media = await self._download_with_tokenless_client(
                media_specs,
                attempt_directory,
                "youtube" if platform == Platform.YOUTUBE else "instagram",
                progress_callback,
            )
            if convert_instagram_audio:
                if processing_callback is not None:
                    await processing_callback(72, "🎵 دارم MP3 می‌سازم…", "استخراج صدای ویدیو")
                media = await self._convert_instagram_videos_to_mp3(media, attempt_directory)
                if processing_callback is not None:
                    await processing_callback(74, "🎵 MP3 آماده شد…", "ارسال به تلگرام")
            if not media:
                return GatewayResult(
                    status="error",
                    bot_username=APIFY_PROVIDER,
                    reason="apify_no_downloaded_file",
                )
            return GatewayResult(status="ready", bot_username=APIFY_PROVIDER, media=tuple(media))
        except DownloadTooLarge:
            return GatewayResult(status="error", bot_username=APIFY_PROVIDER, reason="too_large")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Apify selected download failed for %s: %s", url, exc)
            return GatewayResult(status="error", bot_username=APIFY_PROVIDER, reason="apify_error")

    @staticmethod
    def _youtube_options() -> tuple[QualityOption, ...]:
        options: list[QualityOption] = []
        for index, (label, height) in enumerate(YOUTUBE_QUALITIES):
            options.append(
                QualityOption(
                    label=label,
                    row=index // 2,
                    column=index % 2,
                    fingerprint=_fingerprint({"platform": "youtube", "kind": "video", "quality": f"{height}p"}),
                    expected_kind=MediaKind.VIDEO,
                    expected_height=height,
                )
            )
        options.append(
            QualityOption(
                label="فقط صدا (MP3)",
                row=len(options) // 2,
                column=len(options) % 2,
                fingerprint=_fingerprint({"platform": "youtube", "kind": "audio"}),
                expected_kind=MediaKind.AUDIO,
                expected_bitrate_kbps=192,
            )
        )
        return tuple(options)

    @staticmethod
    def _instagram_options() -> tuple[QualityOption, ...]:
        return (
            QualityOption(
                label="ویدیو (کیفیت اصلی)",
                row=0,
                column=0,
                fingerprint=_fingerprint({"platform": "instagram", "kind": "video"}),
                expected_kind=MediaKind.VIDEO,
                expected_height=1080,
            ),
            QualityOption(
                label="فقط صدا (MP3)",
                row=0,
                column=1,
                fingerprint=_fingerprint({"platform": "instagram", "kind": "audio"}),
                expected_kind=MediaKind.AUDIO,
                expected_bitrate_kbps=192,
            ),
        )

    def _actor_request(
        self,
        url: str,
        platform: Platform,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any], MediaKind, bool]:
        kind = payload.get("kind")
        if platform == Platform.YOUTUBE:
            if kind == "video":
                quality = payload.get("quality")
                if not isinstance(quality, str) or quality not in {f"{height}p" for _, height in YOUTUBE_QUALITIES}:
                    raise InvalidDownload("invalid YouTube quality")
                return (
                    YOUTUBE_ACTOR_ID,
                    {
                        "videos": [{"url": url}],
                        "storeInKVStore": True,
                        "preferredQuality": quality,
                        "preferredFormat": "mp4",
                    },
                    MediaKind.VIDEO,
                    False,
                )
            if kind == "audio":
                return (
                    YOUTUBE_ACTOR_ID,
                    {
                        "videos": [{"url": url}],
                        "storeInKVStore": True,
                        "preferredFormat": "mp3",
                    },
                    MediaKind.AUDIO,
                    False,
                )
            raise InvalidDownload("invalid YouTube selection")

        if platform == Platform.INSTAGRAM:
            if kind not in {"video", "audio"}:
                raise InvalidDownload("invalid Instagram selection")
            return (
                INSTAGRAM_ACTOR_ID,
                {
                    "directUrls": [url],
                    "resultsType": _instagram_results_type(url),
                    "resultsLimit": 1,
                },
                MediaKind.VIDEO,
                kind == "audio",
            )
        raise InvalidDownload("unsupported Apify platform")

    async def _run_with_failover(
        self,
        actor_id: str,
        actor_input: dict[str, Any],
        processing_callback: ProcessingCallback | None = None,
    ) -> dict[str, Any]:
        last_error: ApifyError | None = None
        for token_index in await self._token_candidates():
            try:
                async with httpx.AsyncClient(
                    headers={
                        "Authorization": f"Bearer {self.tokens[token_index]}",
                        "Accept": "application/json",
                    },
                    follow_redirects=True,
                    timeout=httpx.Timeout(connect=15.0, read=45.0, write=30.0, pool=15.0),
                ) as client:
                    run = await self._run_actor(client, actor_id, actor_input, processing_callback)
                    items = await self._dataset_items(client, run)
                await self._mark_token_success(token_index)
                return {"run": run, "items": items}
            except ApifyError as exc:
                # The user explicitly expects a fast fallback on any Actor/API
                # error. Most quota and billing failures are detected by status
                # code; retrying a transient Actor/proxy failure on the next
                # account is also useful in practice. Cool down this token, then
                # try the next one immediately.
                last_error = exc
                await self._mark_token_unavailable(token_index)
                logger.warning(
                    "Apify token #%d is temporarily skipped after an Actor/API error (HTTP %s)",
                    token_index + 1,
                    exc.status_code or "n/a",
                )
        if last_error is not None:
            raise last_error
        raise ApifyError("No Apify token is available")

    async def _token_candidates(self) -> tuple[int, ...]:
        """Round-robin healthy tokens first; cooled tokens only if none are healthy."""
        async with self._token_lock:
            count = len(self.tokens)
            if count == 0:
                return ()
            start = self._next_token_index % count
            self._next_token_index = (start + 1) % count
            ordered = tuple((start + offset) % count for offset in range(count))
            now = time.monotonic()
            healthy = tuple(index for index in ordered if self._token_cooldown_until.get(index, 0.0) <= now)
            return healthy or ordered

    async def _mark_token_unavailable(self, token_index: int) -> None:
        async with self._token_lock:
            self._token_cooldown_until[token_index] = time.monotonic() + self.token_cooldown

    async def _mark_token_success(self, token_index: int) -> None:
        async with self._token_lock:
            self._token_cooldown_until.pop(token_index, None)

    @staticmethod
    def _is_token_failure(exc: ApifyError) -> bool:
        if exc.status_code in {401, 402, 403, 429}:
            return True
        message = str(exc).lower()
        return any(term in message for term in _TOKEN_FAILURE_TERMS)

    async def _run_actor(
        self,
        client: httpx.AsyncClient,
        actor_id: str,
        actor_input: dict[str, Any],
        processing_callback: ProcessingCallback | None = None,
    ) -> dict[str, Any]:
        response = await client.post(
            f"{self.api_base}/acts/{_actor_reference(actor_id)}/runs",
            json=actor_input,
        )
        if response.status_code >= 400:
            raise ApifyError(self._error_message(response), status_code=response.status_code)
        payload = response.json()
        run = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(run, dict) or not run.get("id"):
            raise ApifyError("Apify start response did not contain a run ID")

        started_at = time.monotonic()
        deadline = started_at + self.run_timeout
        poll_count = 0
        if processing_callback is not None:
            await processing_callback(20, "⚙️ Actor در حال آماده‌سازی است…", "وضعیت: READY")
        while run.get("status") in {"READY", "RUNNING", "ABORTING", "TIMING-OUT"}:
            if time.monotonic() >= deadline:
                raise ApifyError("Apify Actor run timed out")
            await asyncio.sleep(self.poll_interval)
            poll = await client.get(f"{self.api_base}/actor-runs/{run['id']}")
            if poll.status_code >= 400:
                raise ApifyError(self._error_message(poll), status_code=poll.status_code)
            payload = poll.json()
            next_run = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(next_run, dict):
                raise ApifyError("Apify run polling response was invalid")
            run = next_run
            poll_count += 1
            if processing_callback is not None:
                elapsed = max(0.0, time.monotonic() - started_at)
                # Apify exposes reliable run states but not a byte-level
                # extraction percentage. The heartbeat advances on each real
                # polling cycle and reserves the final range for file transfer.
                phase_percent = min(56, 22 + poll_count * 2)
                await processing_callback(
                    phase_percent,
                    "⚙️ Apify در حال پردازش است…",
                    f"وضعیت: {run.get('status', 'RUNNING')} • {int(elapsed)} ثانیه گذشته",
                )

        if run.get("status") != "SUCCEEDED":
            message = str(run.get("statusMessage") or run.get("status") or "unknown status")
            raise ApifyError(f"Apify Actor did not succeed: {message}")
        return run

    async def _dataset_items(self, client: httpx.AsyncClient, run: dict[str, Any]) -> list[dict[str, Any]]:
        dataset_id = run.get("defaultDatasetId")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ApifyError("Apify run did not provide a default dataset")
        response = await client.get(
            f"{self.api_base}/datasets/{dataset_id}/items",
            params={"clean": "1", "format": "json", "limit": "20"},
        )
        if response.status_code >= 400:
            raise ApifyError(self._error_message(response), status_code=response.status_code)
        payload = response.json()
        if not isinstance(payload, list):
            raise ApifyError("Apify dataset response was not a list")
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"Apify HTTP {response.status_code}"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message:
                    return message
        return f"Apify HTTP {response.status_code}"

    @staticmethod
    def _media_specs(
        platform: Platform,
        items: Iterable[dict[str, Any]],
        expected_kind: MediaKind,
    ) -> list[tuple[str, MediaKind]]:
        if platform == Platform.YOUTUBE:
            return [
                (url, expected_kind)
                for item in items
                if (url := _as_http_url(item.get("downloadedFileUrl")))
            ]
        media: list[tuple[str, MediaKind]] = []
        for item in items:
            media.extend(_instagram_media_specs(item))
        return media

    async def _download_with_tokenless_client(
        self,
        media_specs: list[tuple[str, MediaKind]],
        attempt_directory: Path,
        filename_stem: str,
        progress_callback: ProgressCallback | None,
    ) -> list[DownloadedMedia]:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0),
        ) as client:
            downloaded: list[DownloadedMedia] = []
            attempt_directory.mkdir(parents=True, exist_ok=True)
            for index, (media_url, kind) in enumerate(media_specs, start=1):
                downloaded.append(
                    await self._download_one(
                        client,
                        media_url,
                        kind,
                        attempt_directory,
                        f"{filename_stem}_{index}",
                        progress_callback,
                    )
                )
        return downloaded

    async def _download_one(
        self,
        client: httpx.AsyncClient,
        media_url: str,
        kind: MediaKind,
        attempt_directory: Path,
        filename_stem: str,
        progress_callback: ProgressCallback | None,
    ) -> DownloadedMedia:
        total_bytes = 0
        content_length = 0
        content_type: str | None = None
        destination: Path | None = None
        async with client.stream("GET", media_url) as response:
            if response.status_code != 200:
                raise InvalidDownload(f"Apify media URL returned HTTP {response.status_code}")
            content_type = response.headers.get("content-type")
            try:
                content_length = int(response.headers.get("content-length") or 0)
            except ValueError:
                content_length = 0
            if self.max_download_size > 0 and content_length > self.max_download_size:
                raise DownloadTooLarge("Output exceeds MAX_DOWNLOAD_SIZE_MB")
            suffix = _guess_suffix(media_url, content_type, kind)
            destination = attempt_directory / f"{_sanitize_filename(filename_stem)}{suffix}"
            with destination.open("wb") as file_handle:
                async for chunk in response.aiter_bytes():
                    file_handle.write(chunk)
                    total_bytes += len(chunk)
                    if self.max_download_size > 0 and total_bytes > self.max_download_size:
                        raise DownloadTooLarge("Output exceeds MAX_DOWNLOAD_SIZE_MB")
                    if progress_callback is not None:
                        with_context_total = content_length or total_bytes
                        try:
                            await progress_callback(total_bytes, with_context_total)
                        except Exception:
                            pass
        if destination is None or total_bytes == 0:
            raise InvalidDownload("Apify media URL returned an empty file")
        if progress_callback is not None:
            try:
                await progress_callback(total_bytes, content_length or total_bytes)
            except Exception:
                pass
        return DownloadedMedia(
            path=destination,
            kind=kind,
            source_message_id=0,
            mime_type=_mime_for(kind, destination.suffix, content_type),
            size=total_bytes,
        )

    async def _convert_instagram_videos_to_mp3(
        self,
        media: list[DownloadedMedia],
        attempt_directory: Path,
    ) -> list[DownloadedMedia]:
        audio: list[DownloadedMedia] = []
        for index, item in enumerate(media, start=1):
            if item.kind != MediaKind.VIDEO:
                continue
            output = attempt_directory / f"instagram_audio_{index}.mp3"
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(item.path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await process.wait() != 0 or not output.exists() or output.stat().st_size == 0:
                raise InvalidDownload("ffmpeg could not extract Instagram audio")
            try:
                item.path.unlink()
            except OSError:
                pass
            audio.append(
                DownloadedMedia(
                    path=output,
                    kind=MediaKind.AUDIO,
                    source_message_id=0,
                    mime_type="audio/mpeg",
                    size=output.stat().st_size,
                )
            )
        if not audio:
            raise InvalidDownload("Instagram post did not contain a video for audio extraction")
        return audio


def apify_health_check(gateway: ApifyGateway | None) -> str:
    if gateway is None:
        return "disabled"
    return f"enabled ({len(gateway.tokens)} token(s))"
