"""Google Drive upload integration for large Telegram files."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


logger = logging.getLogger("MZDownloader.google_drive")
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)


class GoogleDriveError(RuntimeError):
    """Raised when a Google Drive upload cannot be completed."""


class GoogleDriveUploader:
    """Upload files to Google Drive using a service-account credential."""

    def __init__(
        self,
        service_account_json: str,
        folder_id: str = "",
    ) -> None:
        try:
            credentials_info = json.loads(service_account_json)
        except json.JSONDecodeError as exc:
            raise GoogleDriveError(
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON must contain valid JSON"
            ) from exc
        if not isinstance(credentials_info, dict):
            raise GoogleDriveError(
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON must contain a JSON object"
            )
        try:
            self.credentials = Credentials.from_service_account_info(
                credentials_info,
                scopes=DRIVE_SCOPES,
            )
        except (KeyError, ValueError) as exc:
            raise GoogleDriveError(
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON is not a valid service-account credential"
            ) from exc
        self.folder_id = folder_id.strip()

    def _service(self) -> Any:
        return build(
            "drive",
            "v3",
            credentials=self.credentials,
            cache_discovery=False,
        )

    async def upload(self, file_path: Path) -> tuple[str, str]:
        """Upload *file_path* and return (download_url, file_id)."""

        def do_upload() -> tuple[str, str]:
            metadata: dict[str, Any] = {"name": file_path.name}
            if self.folder_id:
                metadata["parents"] = [self.folder_id]
            media = MediaFileUpload(
                str(file_path),
                resumable=True,
                chunksize=8 * 1024 * 1024,
            )
            service = self._service()
            created = (
                service.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            file_id = str(created.get("id") or "")
            if not file_id:
                raise GoogleDriveError("Google Drive response did not include a file ID")
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True,
            ).execute()
            return f"https://drive.google.com/uc?export=download&id={file_id}", file_id

        try:
            return await asyncio.to_thread(do_upload)
        except GoogleDriveError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GoogleDriveError(f"Google Drive upload failed: {exc}") from exc

    async def delete(self, file_id: str) -> None:
        """Delete a Drive file after its temporary download window expires."""
        if not file_id:
            return

        def do_delete() -> None:
            self._service().files().delete(
                fileId=file_id,
                supportsAllDrives=True,
            ).execute()

        try:
            await asyncio.to_thread(do_delete)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 404:
                logger.warning("Google Drive DELETE %s failed: %s", file_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google Drive DELETE %s failed: %s", file_id, exc)