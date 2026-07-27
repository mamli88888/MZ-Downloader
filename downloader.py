from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from email.message import Message as EmailMessage
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlsplit

import httpx
from telethon import events
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from routing import Platform, detect_platform


logger = logging.getLogger("MZDownloader.core")
ProgressCallback = Callable[[int, int], Awaitable[None]]


class MediaKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    REJECTED = "rejected"
    NONE = "none"


class PoolUnavailable(RuntimeError):
    pass


class InvalidDownload(RuntimeError):
    pass


class DrDownloaderError(RuntimeError):
    pass


class DownloadTooLarge(InvalidDownload):
    pass


@dataclass(frozen=True)
class QualityOption:
    label: str
    row: int
    column: int
    fingerprint: str
    expected_kind: MediaKind | None
    expected_height: int | None = None
    expected_bitrate_kbps: int | None = None
    action: str = "media"


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    kind: MediaKind
    source_message_id: int
    mime_type: str
    size: int
    duration: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class GatewayResult:
    status: str
    bot_username: str
    reason: str = ""
    media: tuple[DownloadedMedia, ...] = ()
    options: tuple[QualityOption, ...] = ()
    menu_message_id: int | None = None
    request_message_id: int | None = None
    correlation: str = ""
    preview: DownloadedMedia | None = None
    text: str = ""


@dataclass
class AccountWorker:
    name: str
    phone: str
    client: Any
    lease_id: str | None = None

    @property
    def busy(self) -> bool:
        return self.lease_id is not None


@dataclass(frozen=True)
class WorkerLease:
    worker: AccountWorker
    lease_id: str


class AccountPool:
    """Cancellation-safe worker pool with ownership-checked leases."""

    def __init__(self) -> None:
        self.workers: list[AccountWorker] = []
        self._available: asyncio.Queue[AccountWorker] = asyncio.Queue()
        self._waiting = 0

    def add_worker(self, worker: AccountWorker) -> None:
        if any(existing.name == worker.name for existing in self.workers):
            raise ValueError(f"Duplicate worker name: {worker.name}")
        self.workers.append(worker)
        self._available.put_nowait(worker)

    async def acquire(self) -> WorkerLease:
        if not self.workers:
            raise PoolUnavailable("No Telegram accounts are connected")
        self._waiting += 1
        try:
            worker = await self._available.get()
        finally:
            self._waiting -= 1
        lease_id = uuid.uuid4().hex
        if worker.lease_id is not None:
            raise RuntimeError("Worker pool returned an already leased worker")
        worker.lease_id = lease_id
        return WorkerLease(worker=worker, lease_id=lease_id)

    def release(self, lease: WorkerLease) -> bool:
        worker = lease.worker
        if worker.lease_id != lease.lease_id:
            logger.warning("Ignored release for stale lease %s on %s", lease.lease_id, worker.name)
            return False
        worker.lease_id = None
        self._available.put_nowait(worker)
        return True

    @property
    def queue_length(self) -> int:
        return self._waiting

    @property
    def busy_count(self) -> int:
        return sum(worker.busy for worker in self.workers)

    @property
    def total(self) -> int:
        return len(self.workers)


class CooldownRegistry:
    def __init__(self, duration: float) -> None:
        self.duration = duration
        self._until: dict[tuple[str, str], float] = {}

    @staticmethod
    def _key(worker_name: str, bot_username: str) -> tuple[str, str]:
        return worker_name, bot_username.lower().lstrip("@")

    def mark_timeout(self, worker_name: str, bot_username: str) -> None:
        self._until[self._key(worker_name, bot_username)] = time.monotonic() + self.duration

    def clear(self, worker_name: str, bot_username: str) -> None:
        self._until.pop(self._key(worker_name, bot_username), None)

    def remaining(self, worker_name: str, bot_username: str) -> float:
        key = self._key(worker_name, bot_username)
        remaining = self._until.get(key, 0.0) - time.monotonic()
        if remaining <= 0:
            self._until.pop(key, None)
            return 0.0
        return remaining

    def active_count(self) -> int:
        for worker_name, bot_username in list(self._until):
            self.remaining(worker_name, bot_username)
        return len(self._until)


QUALITY_PATTERN = re.compile(
    r"(?:\b\d{3,4}p(?:\d{2,3})?\b|\b[248]k\b|\b(?:uhd|qhd|fhd|full\s*hd|hd|sd)\b|"
    r"\b(?:mp3|m4a|aac|opus|ogg|audio|video)\b|\b\d{2,3}\s*kbps\b)",
    re.IGNORECASE,
)
RESOLUTION_PATTERN = re.compile(r"\b(\d{3,4})p(?:\d{2,3})?\b", re.IGNORECASE)
BITRATE_PATTERN = re.compile(r"\b(\d{2,3})\s*kbps\b", re.IGNORECASE)
AUDIO_PATTERN = re.compile(
    r"\b(?:mp3|m4a|aac|opus|ogg|audio|music|voice|\d{2,3}\s*kbps)\b",
    re.IGNORECASE,
)
DENIED_BUTTON_TEXT = {
    "back",
    "cancel",
    "refresh metadata",
    "partial download by timing",
    "share",
}
DENIED_BUTTON_MARKERS = (
    "download again",
    "extract audio",
    "edit video",
    "edit audio",
    "change format",
    "refresh metadata",
)
CAPTION_BUTTON_PATTERN = re.compile(r"\b(?:download\s+)?caption\b", re.IGNORECASE)
ERROR_MARKERS = (
    "invalid url",
    "unsupported",
    "not supported",
    "cannot download",
    "can't download",
    "couldn't download",
    "private account",
    "private video",
    "not found",
    "rate limit",
    "try again later",
    "join the channel",
    "must join",
    "error occurred",
)
AD_GATE_MARKERS = (
    "short ad",
    "watch ad",
    "view ad",
    "see an ad",
    "تبلیغ را ببین",
    "مشاهده تبلیغ",
)
PROGRESS_MARKERS = (
    "processing",
    "downloading",
    "preparing",
    "please wait",
    "fetching",
    "wait a moment",
)
PROMOTION_MARKERS = (
    "sponsored",
    "advertisement",
    "join our channel",
    "follow our channel",
)
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}
DR_ALBUM_DOWNLOAD_ALL_MARKERS = ("download all", "دانلود همه")
EXTERNAL_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRUSTED_MEDIA_HOSTS = {"pictube.app"}


def _button_payload(button: Any) -> bytes:
    payload = getattr(button, "data", None)
    raw_button = getattr(button, "button", None)
    if payload is None and raw_button is not None:
        payload = getattr(raw_button, "data", None)
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    if isinstance(payload, bytes):
        return payload
    return b""


def _button_url(button: Any) -> str:
    url = getattr(button, "url", None)
    raw_button = getattr(button, "button", None)
    if url is None and raw_button is not None:
        url = getattr(raw_button, "url", None)
    return str(url or "")


def button_fingerprint(button: Any, row: int, column: int) -> str:
    text = str(getattr(button, "text", "") or "").strip()
    payload = _button_payload(button)
    material = payload if payload else f"{row}:{column}:{text}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def extract_quality_options(message: Any) -> tuple[QualityOption, ...]:
    rows = getattr(message, "buttons", None) or ()
    options: list[QualityOption] = []
    for row_index, row in enumerate(rows):
        for column_index, button in enumerate(row):
            label = str(getattr(button, "text", "") or "").strip()
            lowered_label = label.lower()
            if not label or lowered_label in DENIED_BUTTON_TEXT:
                continue
            if _button_url(button) or any(marker in lowered_label for marker in DENIED_BUTTON_MARKERS):
                continue
            if CAPTION_BUTTON_PATTERN.search(label):
                options.append(
                    QualityOption(
                        label=label,
                        row=row_index,
                        column=column_index,
                        fingerprint=button_fingerprint(button, row_index, column_index),
                        expected_kind=None,
                        action="caption",
                    )
                )
                continue
            if not QUALITY_PATTERN.search(label) and "شناسایی موسیقی" not in label:
                continue
            expected = MediaKind.AUDIO if AUDIO_PATTERN.search(label) else MediaKind.VIDEO
            resolution_match = RESOLUTION_PATTERN.search(label)
            bitrate_match = BITRATE_PATTERN.search(label)
            height = int(resolution_match.group(1)) if resolution_match else None
            if height is None:
                lowered = label.lower()
                height = 4320 if re.search(r"\b8k\b", lowered) else height
                height = 2160 if height is None and re.search(r"\b4k\b", lowered) else height
                height = 1440 if height is None and re.search(r"\b2k\b", lowered) else height
            options.append(
                QualityOption(
                    label=label,
                    row=row_index,
                    column=column_index,
                    fingerprint=button_fingerprint(button, row_index, column_index),
                    expected_kind=expected,
                    expected_height=height,
                    expected_bitrate_kbps=int(bitrate_match.group(1)) if bitrate_match else None,
                )
            )
    return tuple(options)


def message_text(message: Any) -> str:
    return str(
        getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or getattr(message, "message", None)
        or ""
    )


def message_mime_type(message: Any) -> str:
    document = getattr(message, "document", None)
    if document is None:
        document = getattr(getattr(message, "media", None), "document", None)
    return str(getattr(document, "mime_type", "") or "").lower()


def message_file_name(message: Any) -> str:
    file_obj = getattr(message, "file", None)
    return str(getattr(file_obj, "name", "") or "")


def message_file_size(message: Any) -> int:
    file_obj = getattr(message, "file", None)
    size = getattr(file_obj, "size", None)
    if size is None:
        document = getattr(message, "document", None)
        size = getattr(document, "size", 0)
    try:
        return int(size or 0)
    except (TypeError, ValueError):
        return 0


def message_media_kind(message: Any) -> MediaKind:
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return MediaKind.PHOTO
    if not isinstance(media, MessageMediaDocument):
        return MediaKind.NONE

    document = getattr(message, "document", None) or getattr(media, "document", None)
    attributes = getattr(document, "attributes", None) or ()
    attribute_names = {type(attribute).__name__.lower() for attribute in attributes}
    if any("sticker" in name for name in attribute_names):
        return MediaKind.REJECTED

    mime = message_mime_type(message)
    extension = Path(message_file_name(message)).suffix.lower()
    if mime.startswith("image/") and mime != "image/gif":
        return MediaKind.PHOTO
    if mime.startswith("video/") or extension in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if mime.startswith("audio/") or extension in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    if mime in {"text/html", "application/xhtml+xml"}:
        return MediaKind.REJECTED
    if mime == "image/gif" or any("animated" in name for name in attribute_names):
        return MediaKind.VIDEO
    return MediaKind.DOCUMENT


def message_reply_to_id(message: Any) -> int | None:
    reply_id = getattr(message, "reply_to_msg_id", None)
    if reply_id is None:
        reply_id = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    try:
        return int(reply_id) if reply_id is not None else None
    except (TypeError, ValueError):
        return None


def extract_trusted_external_url(text: str) -> str | None:
    for match in EXTERNAL_URL_PATTERN.finditer(text or ""):
        candidate = match.group(0).rstrip(".,!?;:)]}")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower().strip(".")
        if parsed.scheme == "https" and any(
            host == trusted or host.endswith(f".{trusted}")
            for trusted in TRUSTED_MEDIA_HOSTS
        ):
            return candidate
    return None


def expected_kind_for_url(url: str) -> MediaKind | None:
    platform = detect_platform(url)
    if platform in {Platform.SPOTIFY, Platform.SOUNDCLOUD}:
        return MediaKind.AUDIO
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.lower()
    extension = Path(path).suffix.lower()
    if extension in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if extension in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    if extension in {".jpg", ".jpeg", ".png", ".webp", ".heic"}:
        return MediaKind.PHOTO
    if host in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"}:
        return MediaKind.VIDEO
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return MediaKind.VIDEO
    if host in {"vimeo.com", "player.vimeo.com"}:
        return MediaKind.VIDEO
    if (host == "instagram.com" or host.endswith(".instagram.com")) and (
        path.startswith("/reel/") or path.startswith("/reels/") or path.startswith("/tv/")
    ):
        return MediaKind.VIDEO
    return None


def is_correlated_message(
    message: Any,
    *,
    after_id: int,
    reply_targets: Iterable[int],
    is_edit: bool = False,
    allowed_edit_ids: Iterable[int] = (),
) -> bool:
    if getattr(message, "out", False):
        return False
    try:
        message_id = int(getattr(message, "id", 0) or 0)
    except (TypeError, ValueError):
        return False
    allowed_edits = set(allowed_edit_ids)
    if message_id <= after_id and not (is_edit and message_id in allowed_edits):
        return False
    targets = set(reply_targets)
    reply_id = message_reply_to_id(message)
    if reply_id is not None and targets and reply_id not in targets:
        return False
    return True


def _message_signature(message: Any, is_edit: bool) -> tuple[Any, ...]:
    button_labels = tuple(
        str(getattr(button, "text", "") or "")
        for row in (getattr(message, "buttons", None) or ())
        for button in row
    )
    edit_date = getattr(message, "edit_date", None)
    return (
        getattr(message, "id", None),
        bool(is_edit),
        str(edit_date or ""),
        message_text(message),
        button_labels,
        message_media_kind(message),
    )


@dataclass(frozen=True)
class StreamItem:
    message: Any
    is_edit: bool
    received_at: float


class BotEventStream:
    """Captures new and edited bot messages before an action is triggered."""

    def __init__(self, client: Any, bot_username: str) -> None:
        self.client = client
        self.bot_username = bot_username
        self.queue: asyncio.Queue[StreamItem] = asyncio.Queue()
        self._seen: set[tuple[Any, ...]] = set()
        self._new_event = events.NewMessage(from_users=bot_username)
        self._edit_event = events.MessageEdited(from_users=bot_username)

    async def _on_new(self, event: Any) -> None:
        await self._put(getattr(event, "message", None), False)

    async def _on_edit(self, event: Any) -> None:
        await self._put(getattr(event, "message", None), True)

    async def _put(self, message: Any, is_edit: bool) -> None:
        if message is None:
            return
        signature = _message_signature(message, is_edit)
        if signature in self._seen:
            return
        self._seen.add(signature)
        await self.queue.put(StreamItem(message, is_edit, time.monotonic()))

    async def __aenter__(self) -> "BotEventStream":
        self.client.add_event_handler(self._on_new, self._new_event)
        self.client.add_event_handler(self._on_edit, self._edit_event)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.client.remove_event_handler(self._on_new, self._new_event)
        self.client.remove_event_handler(self._on_edit, self._edit_event)

    async def get(self, timeout: float) -> StreamItem | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=max(timeout, 0.001))
        except asyncio.TimeoutError:
            return None


@dataclass(frozen=True)
class ResponseDecision:
    status: str
    messages: tuple[Any, ...] = ()
    options: tuple[QualityOption, ...] = ()
    menu_message_id: int | None = None
    reason: str = ""
    correlation: str = ""
    preview_messages: tuple[Any, ...] = ()
    text: str = ""
    external_url: str = ""


def _contains_marker(text: str, markers: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _kind_matches(message: Any, kind: MediaKind, expected: MediaKind | None) -> bool:
    if expected is None:
        return kind not in {MediaKind.NONE, MediaKind.REJECTED}
    if kind == expected:
        return True
    if kind != MediaKind.DOCUMENT:
        return False
    extension = Path(message_file_name(message)).suffix.lower()
    if expected == MediaKind.VIDEO:
        return extension in VIDEO_EXTENSIONS
    if expected == MediaKind.AUDIO:
        return extension in AUDIO_EXTENSIONS
    return False


def _quality_matches(message: Any, option: QualityOption) -> bool:
    if option.action == "caption":
        return message_media_kind(message) not in {MediaKind.NONE, MediaKind.REJECTED}
    kind = message_media_kind(message)
    if not _kind_matches(message, kind, option.expected_kind):
        return False
    if option.expected_height is None:
        return True
    width, height, _ = _document_dimensions(message)
    dimensions = [value for value in (width, height) if value and value > 0]
    if dimensions:
        actual = min(dimensions)
        tolerance = max(8, int(option.expected_height * 0.03))
        return abs(actual - option.expected_height) <= tolerance
    evidence = f"{message_file_name(message)} {message_text(message)}".lower()
    if option.expected_height == 2160:
        return bool(re.search(r"\b(?:2160p|4k)\b", evidence))
    if option.expected_height == 4320:
        return bool(re.search(r"\b(?:4320p|8k)\b", evidence))
    if option.expected_height == 1440:
        return bool(re.search(r"\b(?:1440p|2k)\b", evidence))
    return bool(re.search(rf"\b{option.expected_height}p\b", evidence))


async def await_response_decision(
    stream: BotEventStream,
    *,
    after_id: int,
    reply_targets: Iterable[int],
    timeout: float,
    preview_grace: float,
    album_window: float,
    expected_kind: MediaKind | None = None,
    expected_option: QualityOption | None = None,
    allowed_edit_ids: Iterable[int] = (),
) -> ResponseDecision:
    started = time.monotonic()
    deadline = started + timeout
    candidate_deadline: float | None = None
    button_deadline: float | None = None
    candidates: dict[int, Any] = {}
    candidate_group_id: int | None = None
    candidate_text = ""
    correlation = "sequence"
    targets = set(reply_targets)

    while True:
        now = time.monotonic()
        if candidates and candidate_deadline is not None and now >= candidate_deadline:
            ordered = tuple(candidates[key] for key in sorted(candidates))
            return ResponseDecision(
                status="media",
                messages=ordered,
                correlation=correlation,
                text=candidate_text,
            )
        if button_deadline is not None and now >= button_deadline:
            return ResponseDecision(status="error", reason="unsupported_buttons")
        if now >= deadline:
            return ResponseDecision(status="timeout", reason="timeout")

        next_deadline = deadline
        if candidate_deadline is not None:
            next_deadline = min(next_deadline, candidate_deadline)
        if button_deadline is not None:
            next_deadline = min(next_deadline, button_deadline)
        item = await stream.get(next_deadline - now)
        if item is None:
            continue
        message = item.message
        # CRITICAL FIX for @Musicfindmhdbot:
        # If we just clicked a button, we must be extremely aggressive in picking up the NEXT message.
        # We ignore all correlation checks (reply_to, etc.) because some bots send the result 
        # as a completely new un-replied message.
        if expected_option is not None:
            # Ignore edits entirely
            if item.is_edit:
                continue
            # Ignore messages without media (ads/text)
            if kind == MediaKind.NONE:
                continue
        else:
            # Normal correlation for non-button requests
            if not is_correlated_message(
                message,
                after_id=after_id,
                reply_targets=targets,
                is_edit=item.is_edit,
                allowed_edit_ids=allowed_edit_ids,
            ):
                continue
        if message_reply_to_id(message) in targets:
            correlation = "reply"

        text = message_text(message)
        kind = message_media_kind(message)
        message_id = int(getattr(message, "id", 0) or 0)
        targets.add(message_id)
        button_text = " ".join(
            str(getattr(button, "text", "") or "")
            for row in (getattr(message, "buttons", None) or ())
            for button in row
        )
        if _contains_marker(f"{text} {button_text}", AD_GATE_MARKERS):
            return ResponseDecision(status="error", reason="ad_required")

        # During the initial request, a photo with buttons is a preview/menu.
        # After a quality click, media wins over post-processing buttons such as
        # "Extract audio" or "Edit video".
        if expected_option is None:
            options = extract_quality_options(message)
            if options:
                previews = dict(candidates)
                if kind == MediaKind.PHOTO:
                    previews[message_id] = message
                return ResponseDecision(
                    status="menu",
                    options=options,
                    menu_message_id=message_id,
                    correlation=correlation,
                    preview_messages=tuple(previews[key] for key in sorted(previews)),
                    text=text.strip(),
                )

        if kind == MediaKind.NONE:
            external_url = extract_trusted_external_url(text)
            if (
                external_url
                and expected_option is not None
                and expected_option.action == "media"
            ):
                return ResponseDecision(
                    status="external_url",
                    external_url=external_url,
                    correlation=correlation,
                )
            if _contains_marker(text, ERROR_MARKERS):
                return ResponseDecision(status="error", reason="service_rejected")
            
            # If we clicked a button, ignore text-only messages (likely ads or progress)
            # unless we specifically asked for a caption.
            if expected_option is not None and expected_option.action != "caption":
                continue

            if (
                expected_option is not None
                and expected_option.action == "caption"
                and text.strip()
                and not _contains_marker(text, PROGRESS_MARKERS)
            ):
                return ResponseDecision(
                    status="text",
                    text=text.strip(),
                    correlation=correlation,
                )
            if getattr(message, "buttons", None):
                button_deadline = button_deadline or min(deadline, time.monotonic() + 20.0)
            continue
        if kind == MediaKind.REJECTED or _contains_marker(text, PROMOTION_MARKERS):
            continue
        if expected_option is not None:
            if not _quality_matches(message, expected_option):
                continue
        elif not _kind_matches(message, kind, expected_kind):
            continue

        if text.strip() and not _contains_marker(text, PROMOTION_MARKERS):
            candidate_text = candidate_text or text.strip()

        group_id = getattr(message, "grouped_id", None)
        if group_id is not None:
            if candidate_group_id is None:
                # A real Telegram album can be preceded by a standalone preview.
                # Once the album starts, keep only members of that album.
                candidates.clear()
                candidate_group_id = int(group_id)
            if int(group_id) != candidate_group_id:
                continue
            candidates[message_id] = message
            candidate_deadline = time.monotonic() + album_window
            continue

        if kind == MediaKind.PHOTO:
            # A photo may be the real result or a preview for a later quality menu/file.
            if expected_kind is not None and expected_option is None:
                continue
            if candidate_group_id is not None:
                continue
            # Some download bots send carousel photos as individual messages
            # without grouped_id. Accumulate every correlated photo until the
            # response has been quiet for the collection window.
            candidates[message_id] = message
            candidate_deadline = time.monotonic() + max(preview_grace, album_window)
            continue

        # If we are waiting for a specific button click result, 
        # we want to capture the absolute latest message that matches.
        if expected_option is not None:
            candidates[message_id] = message
            candidate_deadline = time.monotonic() + album_window
            continue

        return ResponseDecision(
            status="media",
            messages=(message,),
            correlation=correlation,
            text=candidate_text,
        )


def create_attempt_directory(download_root: Path, request_id: str, attempt: str) -> Path:
    safe_request = "".join(char for char in request_id if char.isalnum() or char in {"-", "_"})
    safe_attempt = "".join(char for char in attempt if char.isalnum() or char in {"-", "_"})
    if not safe_request or not safe_attempt:
        raise ValueError("Request and attempt identifiers must not be empty")
    directory = download_root.resolve() / safe_request / safe_attempt
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def cleanup_request_directory(path: Path, download_root: Path) -> None:
    try:
        resolved = path.resolve()
        root = download_root.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError("Refusing to clean a path outside the download root")
        if resolved.exists():
            shutil.rmtree(resolved)
        parent = resolved.parent
        if parent != root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.warning("Cleanup failed for %s: %s", path, exc)


def _validate_download_path(path_value: str | Path | None, directory: Path) -> Path:
    if not path_value:
        raise InvalidDownload("Downloader returned no local file")
    path = Path(path_value).resolve()
    root = directory.resolve()
    if not path.is_relative_to(root):
        raise InvalidDownload("Downloaded path escaped the request directory")
    if not path.is_file() or path.stat().st_size <= 0:
        raise InvalidDownload("Downloaded file is missing or empty")
    return path


def _document_dimensions(message: Any) -> tuple[int | None, int | None, int | None]:
    document = getattr(message, "document", None)
    for attribute in getattr(document, "attributes", None) or ():
        width = getattr(attribute, "w", None)
        height = getattr(attribute, "h", None)
        duration = getattr(attribute, "duration", None)
        if width is not None or height is not None or duration is not None:
            return (
                int(width) if width is not None else None,
                int(height) if height is not None else None,
                int(duration) if duration is not None else None,
            )
    return None, None, None


async def download_messages(
    messages: Iterable[Any],
    directory: Path,
    max_download_size: int,
    progress_callback: ProgressCallback | None = None,
) -> tuple[DownloadedMedia, ...]:
    downloaded: list[DownloadedMedia] = []
    seen_ids: set[int] = set()
    total_expected = 0
    ordered_messages = sorted(messages, key=lambda item: int(getattr(item, "id", 0) or 0))
    expected_total = sum(message_file_size(message) for message in ordered_messages)
    completed_size = 0
    for message in ordered_messages:
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id in seen_ids:
            continue
        seen_ids.add(message_id)
        kind = message_media_kind(message)
        if kind in {MediaKind.NONE, MediaKind.REJECTED}:
            raise InvalidDownload("Response contained unsupported media")
        expected_size = message_file_size(message)
        total_expected += expected_size
        if max_download_size > 0 and (expected_size > max_download_size or total_expected > max_download_size):
            raise DownloadTooLarge("Source media exceeds MAX_DOWNLOAD_SIZE_MB")
        async def on_progress(current: int, total: int) -> None:
            if progress_callback is not None:
                overall_total = expected_total or completed_size + int(total or 0)
                await progress_callback(completed_size + int(current or 0), overall_total)

        path = _validate_download_path(
            await message.download_media(file=str(directory), progress_callback=on_progress),
            directory,
        )
        actual_size = path.stat().st_size
        if max_download_size > 0 and actual_size > max_download_size:
            raise DownloadTooLarge("Downloaded media exceeds MAX_DOWNLOAD_SIZE_MB")
        width, height, duration = _document_dimensions(message)
        downloaded.append(
            DownloadedMedia(
                path=path,
                kind=kind,
                source_message_id=message_id,
                mime_type=message_mime_type(message),
                size=actual_size,
                duration=duration,
                width=width,
                height=height,
            )
        )
        completed_size += actual_size
    if not downloaded:
        raise InvalidDownload("No valid media was downloaded")
    return tuple(downloaded)


def _safe_external_filename(headers: httpx.Headers, url: str, kind: MediaKind) -> str:
    disposition = EmailMessage()
    disposition["content-disposition"] = headers.get("content-disposition", "")
    filename = disposition.get_filename() or Path(urlsplit(url).path).name
    if not filename:
        filename = "download.mp4" if kind == MediaKind.VIDEO else "download.mp3"
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename).name).strip(" .")
    if filename and not Path(filename).suffix:
        suffix = ".mp4" if kind == MediaKind.VIDEO else ".mp3" if kind == MediaKind.AUDIO else ".bin"
        filename += suffix
    return filename or f"download-{uuid.uuid4().hex[:8]}.bin"


async def download_trusted_external_media(
    url: str,
    directory: Path,
    max_download_size: int,
    expected_kind: MediaKind | None,
    proxy_url: str | None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[DownloadedMedia, ...]:
    if extract_trusted_external_url(url) != url:
        raise InvalidDownload("External media URL is not trusted")
    timeout = httpx.Timeout(120.0, connect=30.0)
    attempts = [proxy_url]
    if proxy_url:
        attempts.append(None)
    last_error: Exception | None = None
    for attempt_proxy in attempts:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                proxy=attempt_proxy,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    final_url = str(response.url)
                    if extract_trusted_external_url(final_url) != final_url:
                        raise InvalidDownload("External media redirected to an untrusted host")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        raise InvalidDownload("External media returned HTML")
                    content_length = int(response.headers.get("content-length") or 0)
                    if max_download_size > 0 and content_length > max_download_size:
                        raise DownloadTooLarge("External media exceeds MAX_DOWNLOAD_SIZE_MB")
                    if content_type.startswith("video/"):
                        kind = MediaKind.VIDEO
                    elif content_type.startswith("audio/"):
                        kind = MediaKind.AUDIO
                    elif expected_kind in {MediaKind.VIDEO, MediaKind.AUDIO}:
                        kind = expected_kind
                    else:
                        kind = MediaKind.DOCUMENT
                    filename = _safe_external_filename(response.headers, final_url, kind)
                    path = directory.resolve() / filename
                    size = 0
                    with path.open("wb") as output:
                        async for chunk in response.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if max_download_size > 0 and size > max_download_size:
                                raise DownloadTooLarge("External media exceeds MAX_DOWNLOAD_SIZE_MB")
                            output.write(chunk)
                            if progress_callback is not None:
                                await progress_callback(size, content_length or size)
            break
        except (httpx.ConnectError, httpx.ProxyError, httpx.ConnectTimeout) as exc:
            last_error = exc
            if attempt_proxy is None:
                raise
            logger.info("Trusted media proxy failed; retrying directly: %s", exc)
    else:
        if last_error:
            raise last_error
        raise InvalidDownload("External media download failed")
    path = _validate_download_path(path, directory)
    return (
        DownloadedMedia(
            path=path,
            kind=kind,
            source_message_id=0,
            mime_type=content_type,
            size=path.stat().st_size,
        ),
    )


class DownloaderGateway:
    def __init__(
        self,
        *,
        wait_timeout: float,
        preview_grace: float,
        album_window: float,
        max_download_size: int,
        cooldowns: CooldownRegistry,
        http_proxy_url: str | None = None,
    ) -> None:
        self.wait_timeout = wait_timeout
        self.preview_grace = preview_grace
        self.album_window = album_window
        self.max_download_size = max_download_size
        self.cooldowns = cooldowns
        self.http_proxy_url = http_proxy_url

    @staticmethod
    async def _latest_message_id(client: Any, bot_username: str) -> int:
        latest = await client.get_messages(bot_username, limit=1)
        if isinstance(latest, (list, tuple)):
            latest = latest[0] if latest else None
        return int(getattr(latest, "id", 0) or 0)

    async def request(
        self,
        *,
        client: Any,
        worker_name: str,
        bot_username: str,
        url: str,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
        expected_kind_override: MediaKind | None = None,
    ) -> GatewayResult:
        if self.cooldowns.remaining(worker_name, bot_username) > 0:
            return GatewayResult(status="error", bot_username=bot_username, reason="cooldown")
        try:
            async with BotEventStream(client, bot_username) as stream:
                baseline = await self._latest_message_id(client, bot_username)
                sent = await client.send_message(bot_username, url)
                sent_id = int(getattr(sent, "id", 0) or 0)
                effective_kind = (
                    expected_kind_override
                    if expected_kind_override is not None
                    else expected_kind_for_url(url)
                )
                decision = await await_response_decision(
                    stream,
                    after_id=max(baseline, sent_id),
                    reply_targets={sent_id},
                    timeout=self.wait_timeout,
                    preview_grace=self.preview_grace,
                    album_window=self.album_window,
                    expected_kind=effective_kind,
                )
            if decision.status == "timeout":
                self.cooldowns.mark_timeout(worker_name, bot_username)
                return GatewayResult(
                    status="error",
                    bot_username=bot_username,
                    reason="timeout",
                    request_message_id=sent_id,
                )
            if decision.status == "error":
                if decision.reason != "service_rejected":
                    self.cooldowns.mark_timeout(worker_name, bot_username)
                return GatewayResult(
                    status="error",
                    bot_username=bot_username,
                    reason=decision.reason,
                    request_message_id=sent_id,
                )
            if decision.status == "menu":
                preview = None
                if decision.preview_messages:
                    try:
                        downloaded_preview = await download_messages(
                            (decision.preview_messages[0],),
                            attempt_directory,
                            self.max_download_size,
                            progress_callback,
                        )
                        preview = downloaded_preview[0]
                    except InvalidDownload as exc:
                        logger.info("Preview from @%s was skipped: %s", bot_username, exc)
                self.cooldowns.clear(worker_name, bot_username)
                return GatewayResult(
                    status="needs_selection",
                    bot_username=bot_username,
                    options=decision.options,
                    menu_message_id=decision.menu_message_id,
                    request_message_id=sent_id,
                    correlation=decision.correlation,
                    preview=preview,
                    text=decision.text,
                )
            media = await download_messages(
                decision.messages,
                attempt_directory,
                self.max_download_size,
                progress_callback,
            )
            self.cooldowns.clear(worker_name, bot_username)
            return GatewayResult(
                status="ready",
                bot_username=bot_username,
                media=media,
                request_message_id=sent_id,
                correlation=decision.correlation,
                text=decision.text,
            )
        except DownloadTooLarge:
            return GatewayResult(status="error", bot_username=bot_username, reason="too_large")
        except InvalidDownload as exc:
            self.cooldowns.mark_timeout(worker_name, bot_username)
            logger.warning("Invalid output from @%s: %s", bot_username, exc)
            return GatewayResult(status="error", bot_username=bot_username, reason="invalid_output")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.cooldowns.mark_timeout(worker_name, bot_username)
            logger.exception("Downloader request failed for @%s: %s", bot_username, exc)
            return GatewayResult(status="error", bot_username=bot_username, reason="service_error")

    async def select(
        self,
        *,
        client: Any,
        worker_name: str,
        bot_username: str,
        request_message_id: int,
        menu_message_id: int,
        option: QualityOption,
        attempt_directory: Path,
        progress_callback: ProgressCallback | None = None,
        expected_kind_override: MediaKind | None = None,
    ) -> GatewayResult:
        if self.cooldowns.remaining(worker_name, bot_username) > 0:
            return GatewayResult(status="error", bot_username=bot_username, reason="cooldown")
        try:
            menu = await client.get_messages(bot_username, ids=menu_message_id)
            if not menu or not getattr(menu, "buttons", None):
                return GatewayResult(status="error", bot_username=bot_username, reason="menu_missing")
            current_options = extract_quality_options(menu)
            current = next(
                (
                    item
                    for item in current_options
                    if item.row == option.row
                    and item.column == option.column
                    and item.fingerprint == option.fingerprint
                ),
                None,
            )
            if current is None:
                return GatewayResult(status="error", bot_username=bot_username, reason="menu_changed")

            # Click the button
            await menu.click(option.row, option.column)
            
            # Special logic for @Musicfindmhdbot: Wait a bit and check the last 3 messages
            # as requested by the user to avoid complex correlation issues.
            await asyncio.sleep(5.0) # Give the bot some time to respond/edit
            
            # Fetch the last 3 messages in the chat
            last_messages = await client.get_messages(bot_username, limit=3)
            
            target_message = None
            for msg in last_messages:
                kind = message_media_kind(msg)
                # We want anything that is NOT a video, NOT a photo (unless it's the only thing), 
                # and NOT empty text. Primarily we want Audio or Document (m4a/mp3).
                if kind in {MediaKind.AUDIO, MediaKind.DOCUMENT}:
                    target_message = msg
                    break
            
            if not target_message:
                # Fallback to the original decision logic if the "last 3" didn't find a clear audio file
                async with BotEventStream(client, bot_username) as stream:
                    decision = await await_response_decision(
                        stream,
                        after_id=menu_message_id,
                        reply_targets={request_message_id, menu_message_id},
                        timeout=10.0, # Shorter timeout for fallback
                        preview_grace=self.preview_grace,
                        album_window=self.album_window,
                        expected_kind=expected_kind_override or option.expected_kind,
                        expected_option=option,
                        allowed_edit_ids={menu_message_id},
                    )
                if decision.status != "media":
                    self.cooldowns.mark_timeout(worker_name, bot_username)
                    return GatewayResult(status="error", bot_username=bot_username, reason="last_3_failed")
                target_messages = decision.messages
            else:
                target_messages = (target_message,)

            media = await download_messages(
                target_messages,
                attempt_directory,
                self.max_download_size,
                progress_callback,
            )
            self.cooldowns.clear(worker_name, bot_username)
            return GatewayResult(
                status="ready",
                bot_username=bot_username,
                media=media,
                request_message_id=request_message_id,
                menu_message_id=menu_message_id,
                correlation=decision.correlation,
            )
        except DownloadTooLarge:
            return GatewayResult(status="error", bot_username=bot_username, reason="too_large")
        except InvalidDownload as exc:
            self.cooldowns.mark_timeout(worker_name, bot_username)
            logger.warning("Invalid selected output from @%s: %s", bot_username, exc)
            return GatewayResult(status="error", bot_username=bot_username, reason="invalid_output")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.cooldowns.mark_timeout(worker_name, bot_username)
            logger.exception("Downloader selection failed for @%s: %s", bot_username, exc)
            return GatewayResult(status="error", bot_username=bot_username, reason="service_error")


# ---------------------------------------------------------------------------
# Dr_downloader_bot album/playlist flow
# ---------------------------------------------------------------------------

async def request_dr_downloader_album(
    client: Any,
    bot_username: str,
    url: str,
    directory: Path,
    *,
    wait_timeout: float = 90.0,
    track_timeout: float = 45.0,
    max_download_size: int = 0,
) -> tuple[DownloadedMedia, ...]:
    """Download a Spotify album/playlist via Dr_downloader_bot.

    Flow
    ----
    1. Send the URL.  The bot replies with a GIF whose inline keyboard lists
       every track (track-name buttons) plus a final "📥 دانلود همه آهنگ‌ها |
       Download All" button.
    2. Count all buttons except the last one → total expected tracks.
    3. Click the Download All button.
    4. Collect audio messages until the count reaches *total* or *track_timeout*
       elapses without a new audio arriving.
    5. Download all collected messages to *directory* and return DownloadedMedia.

    Raises DrDownloaderError on any protocol mismatch or timeout.
    """
    async with BotEventStream(client, bot_username) as stream:
        # ── baseline ──────────────────────────────────────────────────────────
        latest = await client.get_messages(bot_username, limit=1)
        if isinstance(latest, (list, tuple)):
            latest = latest[0] if latest else None
        baseline = int(getattr(latest, "id", 0) or 0)

        sent = await client.send_message(bot_username, url)
        sent_id = int(getattr(sent, "id", 0) or 0)
        after_id = max(baseline, sent_id)

        # ── Step 1: wait for the menu message (GIF + track buttons) ──────────
        menu_message: Any = None
        menu_deadline = time.monotonic() + wait_timeout
        while time.monotonic() < menu_deadline:
            item = await stream.get(menu_deadline - time.monotonic())
            if item is None:
                break
            msg = item.message
            if getattr(msg, "out", False):
                continue
            msg_id = int(getattr(msg, "id", 0) or 0)
            if msg_id <= after_id:
                continue
            rows = getattr(msg, "buttons", None) or []
            flat = [btn for row in rows for btn in row]
            if flat:
                menu_message = msg
                break

        if menu_message is None:
            raise DrDownloaderError(
                "Dr_downloader_bot did not send a track-list menu within the timeout"
            )

        # ── Step 2: count tracks and locate Download All button ───────────────
        rows = getattr(menu_message, "buttons", None) or []
        flat_buttons = [btn for row in rows for btn in row]
        if len(flat_buttons) < 2:
            raise DrDownloaderError(
                f"Expected at least 2 buttons (tracks + Download All), got {len(flat_buttons)}"
            )

        last_btn_text = str(getattr(flat_buttons[-1], "text", "") or "").lower()
        if not any(m in last_btn_text for m in DR_ALBUM_DOWNLOAD_ALL_MARKERS):
            raise DrDownloaderError(
                f"Last button does not look like Download All: {last_btn_text!r}"
            )

        total_tracks = len(flat_buttons) - 1
        # Row / column of the last button in the keyboard
        last_row_idx = len(rows) - 1
        last_col_idx = len(rows[last_row_idx]) - 1

        # ── Step 3: click Download All ────────────────────────────────────────
        click_baseline = int(getattr(menu_message, "id", 0) or 0)
        try:
            await menu_message.click(last_row_idx, last_col_idx)
        except Exception as exc:
            raise DrDownloaderError(f"Could not click Download All button: {exc}") from exc

        # ── Step 4: collect audio messages ────────────────────────────────────
        received: list[Any] = []
        next_track_deadline = time.monotonic() + track_timeout
        overall_deadline = time.monotonic() + total_tracks * track_timeout + wait_timeout

        while len(received) < total_tracks:
            now = time.monotonic()
            remaining = min(next_track_deadline - now, overall_deadline - now)
            if remaining <= 0:
                break
            item = await stream.get(remaining)
            if item is None:
                break
            msg = item.message
            if getattr(msg, "out", False):
                continue
            msg_id = int(getattr(msg, "id", 0) or 0)
            if msg_id <= click_baseline:
                continue
            kind = message_media_kind(msg)
            if kind == MediaKind.AUDIO:
                received.append(msg)
                next_track_deadline = time.monotonic() + track_timeout
                continue
            if kind == MediaKind.DOCUMENT:
                ext = Path(message_file_name(msg)).suffix.lower()
                if ext in AUDIO_EXTENSIONS:
                    received.append(msg)
                    next_track_deadline = time.monotonic() + track_timeout

        if not received:
            raise DrDownloaderError("No audio tracks received from Dr_downloader_bot")

        logger.info(
            "Dr_downloader_bot: received %d/%d tracks for %s",
            len(received),
            total_tracks,
            url,
        )

        # ── Step 5: download to disk ──────────────────────────────────────────
        return await download_messages(received, directory, max_download_size, None)
