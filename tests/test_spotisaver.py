from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

from spotisaver import SpotisaverAlbumDownloader, SpotisaverError, spotify_album_id


class SpotisaverTests(unittest.IsolatedAsyncioTestCase):
    def test_album_url_validation(self) -> None:
        self.assertEqual(
            spotify_album_id("https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy?si=x"),
            "4aawyAB9vmqN3uQ7FjRGTy",
        )
        for invalid in (
            "http://open.spotify.com/album/abc1234567",
            "https://open.spotify.com/track/4aawyAB9vmqN3uQ7FjRGTy",
            "https://evil.example/album/4aawyAB9vmqN3uQ7FjRGTy",
        ):
            with self.assertRaises(SpotisaverError):
                spotify_album_id(invalid)

    async def test_album_tracks_are_downloaded_and_zipped(self) -> None:
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.url.path.endswith("album-downloader"):
                return httpx.Response(200, text='const user_ip = "127.0.0.1";', request=request)
            if request.url.path.endswith("get_signature.php"):
                return httpx.Response(200, json={"success": True, "token": "token", "exp": 9999999999}, request=request)
            if request.url.path.endswith("get_playlist.php"):
                return httpx.Response(
                    200,
                    json={
                        "playlist_info": {"name": "Test Album", "total_tracks": 2},
                        "tracks": [
                            {"id": "one", "name": "First", "duration_ms": 1000, "artists": ["A"]},
                            {"id": "two", "name": "Second", "duration_ms": 1000, "artists": ["B"]},
                        ],
                    },
                    request=request,
                )
            if request.url.path.endswith("download_track.php"):
                body = json.loads(request.content)
                name = body["track"]["name"]
                return httpx.Response(
                    200,
                    headers={"content-type": "audio/mpeg", "content-disposition": f'attachment; filename="{name}.mp3"'},
                    content=(name + "-audio").encode(),
                    request=request,
                )
            return httpx.Response(404, request=request)

        progress: list[int] = []

        async def on_progress(percent: int, detail: str) -> None:
            progress.append(percent)

        with tempfile.TemporaryDirectory() as temp:
            result = await SpotisaverAlbumDownloader(
                transport=httpx.MockTransport(handler)
            ).download_album(
                "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy",
                Path(temp),
                progress=on_progress,
            )
            self.assertEqual(result.downloaded_tracks, 2)
            self.assertTrue(result.path.is_file())
            with zipfile.ZipFile(result.path) as archive:
                self.assertEqual(sorted(archive.namelist()), ["First.mp3", "Second.mp3"])
            self.assertEqual(progress[-1], 100)
            self.assertEqual(calls.count("POST /api/download_track.php"), 2)
