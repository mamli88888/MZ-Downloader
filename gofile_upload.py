"""Gofile upload integration with token rotation and delayed cleanup."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


logger = logging.getLogger("MZDownloader.gofile")
GOFILE_API = "https://api.gofile.io"
GOFILE_UPLOAD_API = "https://upload.gofile.io"


class GofileError(RuntimeError):
    pass


def _first_value(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return ""


class GofileUploader:
    """Upload files to gofile.io with token rotation across accounts."""

    def __init__(self, tokens: list[str], proxy_url: str | None = None) -> None:
        self.tokens = list(tokens)
        self._index = 0
        self.proxy_url = proxy_url

    def _next_token(self) -> str | None:
        if not self.tokens:
            return None
        token = self.tokens[self._index % len(self.tokens)]
        self._index += 1
        return token

    @staticmethod
    def _response_data(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") not in {None, "ok"}:
            raise GofileError(f"Gofile API returned an error: {payload}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GofileError(f"Gofile API response has no data: {payload}")
        return data

    async def upload(
        self, file_path: Path
    ) -> tuple[str, str, str, str | None]:
        """Upload and return (download URL, file ID, folder ID, token used)."""
        token = self._next_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        timeout = httpx.Timeout(900.0, connect=30.0)
        async with httpx.AsyncClient(
            proxy=self.proxy_url,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            with file_path.open("rb") as file_handle:
                response = await client.post(
                    f"{GOFILE_UPLOAD_API}/uploadfile",
                    files={"file": (file_path.name, file_handle)},
                    headers=headers,
                )
            response.raise_for_status()
            data = self._response_data(response.json())
            content_id = _first_value(data, "fileId", "id", "contentId")
            folder_id = _first_value(data, "parentFolder", "folderId")
            if not content_id:
                raise GofileError(f"Gofile response missing file ID: {data}")
            if not folder_id:
                raise GofileError(f"Gofile response missing folder ID: {data}")
            return f"https://gofile.io/d/{quote(folder_id)}", content_id, folder_id, token

    async def delete(self, content_id: str, token: str | None) -> None:
        """Delete a Gofile content entry (best effort)."""
        if not content_id:
            return
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy_url,
                timeout=httpx.Timeout(30.0, connect=10.0),
            ) as client:
                response = await client.request(
                    "DELETE",
                    f"{GOFILE_API}/contents",
                    json={"contentsId": content_id},
                    headers=headers,
                )
                if response.status_code not in {200, 204, 404}:
                    logger.warning(
                        "Gofile DELETE %s returned %s",
                        content_id,
                        response.status_code,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gofile delete failed for %s: %s", content_id, exc)


def build_worker_url(
    worker_url: str,
    folder_id: str,
    content_id: str,
    access_key: str,
) -> str:
    """Build the public Cloudflare Worker URL for one Gofile file."""
    base = worker_url.rstrip("/")
    url = (
        f"{base}/download/{quote(folder_id, safe='')}"
        f"/{quote(content_id, safe='')}"
    )
    if access_key:
        return f"{url}?key={quote(access_key, safe='')}"
    return url