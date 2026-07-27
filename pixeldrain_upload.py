"""Pixeldrain upload integration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx


logger = logging.getLogger("MZDownloader.pixeldrain")
PIXELDRAIN_API = "https://pixeldrain.com/api"


class PixeldrainError(RuntimeError):
    pass


class PixeldrainUploader:
    """Upload files to pixeldrain.com."""

    def __init__(self, api_key: str | None = None, proxy_url: str | None = None) -> None:
        self.api_key = api_key
        self.proxy_url = proxy_url

    async def upload(self, file_path: Path) -> str:
        """Upload and return the file ID."""
        auth = None
        if self.api_key:
            auth = ("", self.api_key)

        timeout = httpx.Timeout(900.0, connect=30.0)
        async with httpx.AsyncClient(
            proxy=self.proxy_url,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            with file_path.open("rb") as file_handle:
                response = await client.post(
                    f"{PIXELDRAIN_API}/file",
                    files={"file": (file_path.name, file_handle)},
                    auth=auth,
                )
            
            if response.status_code != 201:
                raise PixeldrainError(f"Pixeldrain API returned {response.status_code}: {response.text}")
            
            data = response.json()
            if not data.get("success"):
                raise PixeldrainError(f"Pixeldrain upload failed: {data}")
                
            return data.get("id")

def build_pixeldrain_worker_url(
    worker_url: str,
    file_id: str,
    access_key: str | None = None,
) -> str:
    """Build the public Cloudflare Worker URL for one Pixeldrain file."""
    base = worker_url.rstrip("/")
    url = f"{base}/pd/{file_id}"
    if access_key:
        from urllib.parse import quote
        return f"{url}?key={quote(access_key, safe='')}"
    return url
