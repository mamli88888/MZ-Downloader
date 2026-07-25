"""Persistent storage for users who have started the bot.

Persistence strategy (Railway-friendly, no external DB needed):
  1. On startup, user IDs are seeded from the KNOWN_USERS environment variable
     (comma-separated integers).  Set this in Railway Variables after collecting IDs.
  2. New registrations are also written to users.json so they survive in-process
     restarts/reloads, but users.json is ephemeral on Railway (cleared on redeploy).
  3. After adding users with /adduser the admin should copy the printed list to
     the KNOWN_USERS Railway Variable so the IDs survive the next redeploy.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from config import PROJECT_DIR

logger = logging.getLogger("MZDownloader.users_db")

_DB_PATH: Path = PROJECT_DIR / "users.json"
_user_ids: set[int] = set()
_loaded = False


def _load_from_env() -> None:
    """Seed user IDs from the KNOWN_USERS environment variable."""
    raw = os.getenv("KNOWN_USERS", "").strip()
    if not raw:
        return
    added = 0
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            _user_ids.add(int(part))
            added += 1
    if added:
        logger.info("Seeded %d user(s) from KNOWN_USERS env var", added)


def _load_from_file() -> None:
    """Load user IDs from the local JSON file (ephemeral on Railway)."""
    if not _DB_PATH.exists():
        return
    try:
        data = json.loads(_DB_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            before = len(_user_ids)
            _user_ids.update(int(uid) for uid in data)
            added = len(_user_ids) - before
            if added:
                logger.info("Loaded %d additional user(s) from users.json", added)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load users.json: %s", exc)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _load_from_env()
    _load_from_file()
    _loaded = True


def _save() -> None:
    """Write current user IDs to the local JSON file."""
    try:
        _DB_PATH.write_text(
            json.dumps(sorted(_user_ids), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not save users.json: %s", exc)


def register(user_id: int) -> bool:
    """Add a user ID to the store. Returns True if it was newly added."""
    _ensure_loaded()
    if user_id in _user_ids:
        return False
    _user_ids.add(user_id)
    _save()
    return True


def all_user_ids() -> list[int]:
    """Return a snapshot list of all stored user IDs."""
    _ensure_loaded()
    return list(_user_ids)


def all_user_ids_str() -> str:
    """Return all IDs as a comma-separated string (for KNOWN_USERS env var)."""
    _ensure_loaded()
    return ",".join(str(uid) for uid in sorted(_user_ids))


def count() -> int:
    """Return the number of stored users."""
    _ensure_loaded()
    return len(_user_ids)
