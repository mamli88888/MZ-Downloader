"""Async HTTP client for a self-hosted cobalt API instance.

Wraps the public cobalt REST API (POST /, GET /, GET /tunnel) and exposes a
small, typed surface that the rest of MZ-Downloader can call without caring
about HTTP details.

Reference: https://github.com/imputnet/cobalt/blob/main/docs/api.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx

logger = logging.getLogger("MZDownloader.cobalt")


class CobaltError(RuntimeError):
    """Raised when cobalt returns an error or the request fails."""


@dataclass(frozen=True)
class CobaltTunnel:
    """A single file tunnel returned by cobalt.

    Cobalt returns either:
      * `tunnel`/`redirect` (single file) -> we wrap it as a 1-element tunnel
      * `picker` (multiple files, e.g. Instagram carousel) -> wrap each item
      * `local-processing` (multi-file, requires ffmpeg mux) -> wrap each tunnel
    """

    url: str
    filename: str
    kind: str  # "video", "audio", "photo", "gif", "document"
    # For local-processing responses, cobalt tells us whether to merge/mux.
    # We carry this forward so the gateway can decide what to do.
    output_type: str | None = None  # "merge", "mute", "audio", "gif", "remux"


@dataclass(frozen=True)
class CobaltResponse:
    """Parsed cobalt API response.

    `status` is one of: tunnel, redirect, picker, local-processing, error.
    For `error`, `error_code` is set and `tunnels` is empty.
    For `picker`, `tunnels` contains one entry per picker item.
    For `tunnel`/`redirect`, `tunnels` has exactly one entry.
    For `local-processing`, `tunnels` has one or more entries that need to be
    combined via ffmpeg (see `merge_type`).
    """

    status: str
    tunnels: tuple[CobaltTunnel, ...]
    error_code: str = ""
    # When status == "local-processing", this is the cobalt-reported type:
    # "merge" (video+audio), "mute" (video only), "audio", "gif", "remux".
    merge_type: str | None = None
    output_filename: str | None = None
    # Original picker (when status == "picker") so we can show thumbnails.
    picker: tuple[dict[str, Any], ...] = ()


ProgressCallback = Callable[[int, int], Awaitable[None]]


class CobaltClient:
    """Async HTTP client for a cobalt API instance.

    The cobalt API is documented at:
    https://github.com/imputnet/cobalt/blob/main/docs/api.md

    The bot only needs three operations:
      * `info()` — GET / to verify the instance is up.
      * `request(payload)` — POST / to submit a download request.
      * `download_to_file(...)` — stream a tunnel URL to disk.
    """

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str | None = None,
        proxy_url: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        if not api_url:
            raise ValueError("api_url is required")
        # Normalize: cobalt expects a trailing slash on API_URL.
        self.api_url = api_url if api_url.endswith("/") else api_url + "/"
        self.api_key = api_key.strip() if api_key else None
        self.proxy_url = proxy_url
        self.timeout = timeout
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            # Cobalt supports Api-Key auth (instance-owner-issued keys).
            self._headers["Authorization"] = f"Api-Key {self.api_key}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def info(self) -> dict[str, Any]:
        """GET / — returns instance info (version, services, git)."""
        try:
            async with self._client() as client:
                response = await client.get(self.api_url, headers=self._headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise CobaltError(f"cobalt info request failed: {exc}") from exc

    async def request(self, payload: Mapping[str, Any]) -> CobaltResponse:
        """POST / — submit a download request and parse the response.

        Note: cobalt returns HTTP 400 with a JSON body for "expected" errors
        (e.g. video unavailable, no matching format). We must NOT treat 4xx as
        a transport failure — we parse the JSON body and let the gateway
        decide what to do with the error code.
        """
        try:
            async with self._client() as client:
                response = await client.post(
                    self.api_url,
                    json=dict(payload),
                    headers=self._headers,
                )
        except httpx.HTTPError as exc:
            raise CobaltError(f"cobalt request transport failed: {exc}") from exc
        # 5xx and connection-level errors are real failures.
        if response.status_code >= 500:
            raise CobaltError(
                f"cobalt returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        # 4xx and 2xx both carry a JSON body we can parse.
        try:
            data = response.json()
        except Exception as exc:
            raise CobaltError(
                f"cobalt returned non-JSON response (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            ) from exc
        return self._parse_response(data)

    async def download_to_file(
        self,
        url: str,
        dest_directory: Path,
        *,
        filename: str | None = None,
        max_size: int = 0,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """Stream a cobalt tunnel URL (or direct media URL) to disk.

        Returns the path to the downloaded file. Raises CobaltError on failure.
        """
        dest_directory = dest_directory.resolve()
        dest_directory.mkdir(parents=True, exist_ok=True)
        if not filename:
            # Derive a filename from the URL path, fallback to a uuid-based name.
            from urllib.parse import urlsplit

            tail = urlsplit(url).path.rsplit("/", 1)[-1]
            filename = tail or "cobalt_download.bin"
        # Sanitize: keep only the basename, strip Windows-illegal chars.
        import re

        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename).name).strip(" .")
        if not filename:
            filename = "cobalt_download.bin"
        dest_path = dest_directory / filename

        timeout = httpx.Timeout(max(self.timeout, 300.0), connect=30.0)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                proxy=self.proxy_url,
            ) as client:
                async with client.stream("GET", url, headers=self._download_headers()) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("content-length") or 0)
                    if max_size > 0 and content_length > max_size:
                        raise CobaltError("Cobalt media exceeds MAX_DOWNLOAD_SIZE_MB")
                    # If cobalt supplied a Content-Disposition filename, prefer it.
                    disposition = response.headers.get("content-disposition", "")
                    if disposition:
                        from email.message import Message as EmailMessage

                        msg = EmailMessage()
                        msg["content-disposition"] = disposition
                        candidate = msg.get_filename()
                        if candidate:
                            cleaned = re.sub(
                                r'[<>:"/\\|?*\x00-\x1f]', "_", Path(candidate).name
                            ).strip(" .")
                            if cleaned:
                                dest_path = dest_directory / cleaned
                    size = 0
                    with dest_path.open("wb") as output:
                        async for chunk in response.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if max_size > 0 and size > max_size:
                                raise CobaltError("Cobalt media exceeds MAX_DOWNLOAD_SIZE_MB")
                            output.write(chunk)
                            if progress_callback is not None:
                                await progress_callback(size, content_length or size)
        except httpx.HTTPError as exc:
            raise CobaltError(f"cobalt download failed: {exc}") from exc
        if not dest_path.is_file() or dest_path.stat().st_size <= 0:
            raise CobaltError("cobalt download produced an empty file")
        return dest_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout, connect=30.0),
            proxy=self.proxy_url,
        )

    def _download_headers(self) -> dict[str, str]:
        # Cobalt tunnel endpoints don't require JSON content-type, but they do
        # require the Authorization header if the instance is protected.
        # For `redirect` responses, the URL points to the source CDN (e.g.
        # scontent.*.cdninstagram.com) which often rejects requests without a
        # realistic User-Agent — so we always send one.
        headers: dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }
        if self.api_key:
            headers["Authorization"] = f"Api-Key {self.api_key}"
        return headers

    @staticmethod
    def _parse_response(data: Mapping[str, Any]) -> CobaltResponse:
        status = str(data.get("status", "")).lower()
        if status == "error":
            error = data.get("error") or {}
            code = str(error.get("code", "") or "error.api.unknown")
            return CobaltResponse(status="error", tunnels=(), error_code=code)
        if status in {"tunnel", "redirect"}:
            url = str(data.get("url", "") or "")
            filename = str(data.get("filename", "") or "cobalt_media")
            kind = _infer_kind_from_filename(filename)
            return CobaltResponse(
                status=status,
                tunnels=(CobaltTunnel(url=url, filename=filename, kind=kind),),
                output_filename=filename,
            )
        if status == "picker":
            picker = data.get("picker") or []
            tunnels: list[CobaltTunnel] = []
            normalized_picker: list[dict[str, Any]] = []
            for index, item in enumerate(picker):
                if not isinstance(item, Mapping):
                    continue
                item_type = str(item.get("type", "") or "photo").lower()
                url = str(item.get("url", "") or "")
                if not url:
                    continue
                # Build a filename; cobalt doesn't supply one for picker items.
                ext = "mp4" if item_type == "video" else ("gif" if item_type == "gif" else "jpg")
                filename = f"cobalt_picker_{index + 1}.{ext}"
                tunnels.append(
                    CobaltTunnel(url=url, filename=filename, kind=item_type)
                )
                normalized_picker.append(
                    {
                        "type": item_type,
                        "url": url,
                        "thumb": str(item.get("thumb", "") or ""),
                        "filename": filename,
                    }
                )
            # Optional slideshow audio (e.g. TikTok image posts).
            audio_url = str(data.get("audio", "") or "")
            audio_filename = str(data.get("audioFilename", "") or "")
            if audio_url:
                tunnels.append(
                    CobaltTunnel(
                        url=audio_url,
                        filename=audio_filename or "cobalt_picker_audio.mp3",
                        kind="audio",
                    )
                )
            return CobaltResponse(
                status="picker",
                tunnels=tuple(tunnels),
                picker=tuple(normalized_picker),
            )
        if status == "local-processing":
            output = data.get("output") or {}
            output_filename = str(output.get("filename", "") or "cobalt_local.mp4")
            output_type = str(data.get("type", "") or "merge").lower()
            tunnel_urls = data.get("tunnel") or []
            tunnels: list[CobaltTunnel] = []
            for index, url in enumerate(tunnel_urls):
                if not url:
                    continue
                tunnels.append(
                    CobaltTunnel(
                        url=str(url),
                        filename=f"cobalt_local_{index + 1}.bin",
                        kind=_infer_kind_from_output_type(output_type, index),
                        output_type=output_type,
                    )
                )
            return CobaltResponse(
                status="local-processing",
                tunnels=tuple(tunnels),
                merge_type=output_type,
                output_filename=output_filename,
            )
        # Unknown status — treat as error.
        return CobaltResponse(
            status="error",
            tunnels=(),
            error_code="error.api.unknown",
        )


def _infer_kind_from_filename(filename: str) -> str:
    """Best-effort media-kind inference from a cobalt-supplied filename."""
    name = filename.lower()
    if name.endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")):
        return "audio"
    if name.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "photo"
    if name.endswith(".gif"):
        return "gif"
    return "video"


def _infer_kind_from_output_type(output_type: str, index: int) -> str:
    """For local-processing responses, guess the kind of each tunnel.

    Cobalt always lists tunnels in a known order depending on `type`:
      * merge: [video, audio]
      * mute: [video]
      * audio: [audio] (+ optional cover)
      * gif: [video]
      * remux: [video or audio]
    """
    output_type = (output_type or "").lower()
    if output_type == "merge":
        return "video" if index == 0 else "audio"
    if output_type == "audio":
        return "audio"
    return "video"
