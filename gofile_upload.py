"""gofile.io upload integration with multiple account token rotation."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

logger = logging.getLogger("MZDownloader.gofile")

GOFILE_API = "https://api.gofile.io"


class GofileError(RuntimeError):
    pass


class GofileUploader:
    """Upload files to gofile.io with token rotation across multiple accounts."""

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

    async def _get_server(self, client: httpx.AsyncClient) -> str:
        resp = await client.get(f"{GOFILE_API}/servers")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise GofileError(f"Could not fetch gofile.io servers: {data}")
        servers = (data.get("data") or {}).get("servers") or []
        if not servers:
            raise GofileError("gofile.io returned no upload servers")
        return servers[0]["name"]

    async def upload(self, file_path: Path) -> tuple[str, str, str | None]:
        """Upload *file_path* and return (download_url, content_id, token_used)."""
        token = self._next_token()
        timeout = httpx.Timeout(300.0, connect=30.0)
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            proxy=self.proxy_url,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            server = await self._get_server(client)
            with file_path.open("rb") as fh:
                resp = await client.post(
                    f"https://{server}.gofile.io/contents/uploadFile",
                    files={"file": (file_path.name, fh)},
                    headers=headers,
                )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                raise GofileError(f"gofile.io upload failed: {data}")
            info = data.get("data") or {}
            folder_id = info.get("parentFolder") or info.get("folderId") or ""
            content_id = info.get("id") or info.get("fileId") or ""
            if not folder_id or not content_id:
                raise GofileError(f"gofile.io response missing IDs: {info}")
            download_url = f"https://gofile.io/d/{folder_id}"
            return download_url, content_id, token

    async def delete(self, content_id: str, token: str | None) -> None:
        """Delete a content entry from gofile.io (best-effort)."""
        if not content_id:
            return
        try:
            headers: dict[str, str] = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.delete(
                    f"{GOFILE_API}/contents/{content_id}",
                    headers=headers,
                )
                if resp.status_code not in {200, 404}:
                    logger.warning(
                        "gofile.io DELETE %s returned %s", content_id, resp.status_code
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("gofile.io delete failed for %s: %s", content_id, exc)
