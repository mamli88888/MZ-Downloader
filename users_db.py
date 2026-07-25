"""Persistent storage for users who have started the bot."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from config import PROJECT_DIR

logger = logging.getLogger("MZDownloader.users_db")

_DB_PATH: Path = PROJECT_DIR / "users.json"
_user_ids: set[int] = set()
_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    if _DB_PATH.exists():
        try:
            data = json.loads(_DB_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _user_ids.update(int(uid) for uid in data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load users DB: %s", exc)
    _loaded = True


def _save() -> None:
    try:
        _DB_PATH.write_text(
            json.dumps(sorted(_user_ids), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not save users DB: %s", exc)


def register(user_id: int) -> None:
    """Add a user ID to the persistent store (no-op if already present)."""
    _ensure_loaded()
    if user_id not in _user_ids:
        _user_ids.add(user_id)
        _save()


def all_user_ids() -> list[int]:
    """Return a snapshot list of all stored user IDs."""
    _ensure_loaded()
    return list(_user_ids)


def count() -> int:
    """Return the number of stored users."""
    _ensure_loaded()
    return len(_user_ids)
