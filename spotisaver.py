from __future__ import annotations

import asyncio
import base64
import json
import re
import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx


BASE_URL = "https://spotisaver.net"
ALBUM_PATTERN = re.compile(r"^/album/([A-Za-z0-9]{10,32})/?$")
ProgressCallback = Callable[[int, str], Awaitable[None]]


class SpotisaverError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpotifyAlbumResult:
    path: Path
    album_name: str
    downloaded_tracks: int
    total_tracks: int
    failed_tracks: tuple[str, ...]


def _b64_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _safe_name(value: str, fallback: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip(" .")
    return clean[:160] or fallback


def spotify_album_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SpotisaverError("Invalid Spotify album URL") from exc
    host = (parsed.hostname or "").lower().strip(".")
    if parsed.scheme != "https" or host not in {"open.spotify.com", "spotify.com", "www.spotify.com"}:
        raise SpotisaverError("Only public open.spotify.com album links are supported")
    match = ALBUM_PATTERN.fullmatch(parsed.path)
    if not match:
        raise SpotisaverError("Spotify link is not an album")
    return match.group(1)


async def _notify(callback: ProgressCallback | None, percent: int, detail: str) -> None:
    if callback is not None:
        await callback(max(0, min(100, int(percent))), detail)


def _zip_and_remove(files: list[Path], destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for file_path in files:
            archive.write(file_path, arcname=file_path.name)
            file_path.unlink(missing_ok=True)


class SpotisaverAlbumDownloader:
    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.proxy_url = proxy_url
        self.transport = transport
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _ajax_headers(self) -> dict[str, str]:
        return {
            **self.headers,
            "Accept": "application/json",
            "Referer": f"{BASE_URL}/en/album-downloader",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
        }

    async def _signature(
        self,
        client: httpx.AsyncClient,
        action: str,
        context: dict[str, object],
    ) -> dict[str, str]:
        response = await client.get(
            f"{BASE_URL}/api/get_signature.php",
            params={"action": action, "ctx": _b64_json(context)},
            headers=self._ajax_headers(),
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success") or not payload.get("token") or not payload.get("exp"):
            raise SpotisaverError("Spotisaver signature request failed")
        return {"token": str(payload["token"]), "exp": str(payload["exp"])}

    async def _download_track(
        self,
        client: httpx.AsyncClient,
        track: dict[str, object],
        directory: Path,
        index: int,
        user_ip: str,
    ) -> Path:
        context: dict[str, object] = {"lang": "en"}
        for key in ("id", "name"):
            value = str(track.get(key) or "").strip()
            if value:
                context[key] = value
        try:
            duration = int(track.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration >= 0:
            context["duration_ms"] = str(duration)
        signature = await self._signature(client, "download_track", context)
        sig = _b64_json(signature)
        headers = {**self._ajax_headers(), "Content-Type": "application/json"}
        payload = {
            "track": track,
            "download_dir": "downloads",
            "filename_tag": "SPOTISAVER",
            "user_ip": user_ip,
            "is_premium": False,
            "lang": "en",
        }
        async with client.stream(
            "POST",
            f"{BASE_URL}/api/download_track.php",
            params={"sig": sig},
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code >= 400:
                raw = (await response.aread())[:2048].decode("utf-8", errors="replace")
                raise SpotisaverError(f"Track download failed ({response.status_code}): {raw[:200]}")
            content_type = response.headers.get("content-type", "").lower()
            if "json" in content_type or "html" in content_type:
                raw = (await response.aread())[:2048].decode("utf-8", errors="replace")
                raise SpotisaverError(f"Track was not returned as audio: {raw[:200]}")
            disposition = EmailMessage()
            disposition["content-disposition"] = response.headers.get("content-disposition", "")
            suggested = disposition.get_filename() or f"{index:03d} - {track.get('name') or 'track'}.mp3"
            filename = _safe_name(Path(suggested).name, f"track-{index:03d}.mp3")
            if not Path(filename).suffix:
                filename += ".mp3"
            destination = directory / filename
            suffix_index = 1
            while destination.exists():
                destination = directory / f"{Path(filename).stem}-{suffix_index}{Path(filename).suffix}"
                suffix_index += 1
            size = 0
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes(256 * 1024):
                    size += len(chunk)
                    output.write(chunk)
            if size <= 0:
                destination.unlink(missing_ok=True)
                raise SpotisaverError("Spotisaver returned an empty track")
            return destination

    async def download_album(
        self,
        url: str,
        directory: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> SpotifyAlbumResult:
        album_id = spotify_album_id(url)
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        timeout = httpx.Timeout(300.0, connect=20.0)
        async with httpx.AsyncClient(
            headers=self.headers,
            proxy=self.proxy_url,
            transport=self.transport,
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            await _notify(progress, 2, "گرفتن اطلاعات آلبوم")
            page = await client.get(f"{BASE_URL}/en/album-downloader")
            page.raise_for_status()
            match = re.search(r'const\s+user_ip\s*=\s*["\']([^"\']*)', page.text)
            user_ip = match.group(1) if match else ""
            context = {"id": album_id, "type": "album", "lang": "en"}
            signature = await self._signature(client, "get_playlist", context)
            headers = {
                **self._ajax_headers(),
                "X-PT": signature["token"],
                "X-PE": signature["exp"],
            }
            response = await client.get(
                f"{BASE_URL}/api/get_playlist.php",
                params=context,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise SpotisaverError(str(data["error"]))
            tracks = data.get("tracks") or []
            if not isinstance(tracks, list) or not tracks:
                raise SpotisaverError("Spotisaver returned no album tracks")
            if len(tracks) > 100:
                raise SpotisaverError("Spotisaver free album limit is 100 tracks")
            info = data.get("playlist_info") if isinstance(data.get("playlist_info"), dict) else {}
            album_name = _safe_name(str(info.get("name") or "Spotify Album"), "Spotify Album")
            downloaded: list[Path] = []
            failed: list[str] = []
            total = len(tracks)
            for index, track in enumerate(tracks, start=1):
                if not isinstance(track, dict):
                    continue
                name = str(track.get("name") or f"Track {index}")
                await _notify(progress, 5 + int((index - 1) / total * 80), f"ترک {index}/{total}: {name}")
                try:
                    downloaded.append(await self._download_track(client, track, directory, index, user_ip))
                except (httpx.HTTPError, SpotisaverError) as exc:
                    failed.append(f"{name}: {exc}")
            if not downloaded:
                raise SpotisaverError("No album track could be downloaded")
            await _notify(progress, 90, "ساخت فایل ZIP")
            zip_path = directory / f"{album_name}.zip"
            await asyncio.to_thread(_zip_and_remove, downloaded, zip_path)
            await _notify(progress, 100, "آلبوم آماده است")
            return SpotifyAlbumResult(zip_path, album_name, len(downloaded), total, tuple(failed))
