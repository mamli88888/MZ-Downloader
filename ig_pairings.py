"""Pairing codes and IG↔TG user mapping for the Instagram DM bridge.

A Telegram user obtains a 6-character pairing code via the `/ig` bot command,
then sends that code to the configured Instagram page's DM. When the IG bridge
sees the code in a DM, it links the sender's Instagram user ID to that Telegram
user ID. From then on, any Instagram URL the user sends to the IG DM is
forwarded to the bot for downloading, and the result is sent back to them in
Telegram.

Persistence:
    - Pending pairing codes: in-memory only (lost on restart, but the user can
      simply generate a new one with `/ig`).
    - Confirmed IG↔TG mappings: persisted to `ig_pairings.json` so they survive
      bot restarts.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

# Pairing codes use an unambiguous alphabet (no 0/O, 1/I, etc.)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
CODE_TTL_SECONDS = 24 * 3600  # unclaimed codes expire after 24h

# Files
_PAIRINGS_FILE = Path(__file__).resolve().parent / "ig_pairings.json"

# In-memory state (guarded by a lock so the poller thread and the bot thread
# can both touch it safely)
_lock = threading.RLock()

# code -> {"tg_user_id": int, "created_at": float, "claimed": bool,
#          "ig_user_id"?: int, "ig_username"?: str, "claimed_at"?: float}
_PENDING_CODES: dict[str, dict] = {}

# Reverse lookup: tg_user_id -> currently-active code (so /ig can return the
# same code if the user calls it twice within the TTL)
_USER_CODES: dict[int, str] = {}

# ig_user_id -> tg_user_id  (persisted)
_IG_TO_TG: dict[int, int] = {}

# tg_user_id -> ig_user_id  (in-memory mirror of _IG_TO_TG, for unpair-by-tg)
_TG_TO_IG: dict[int, int] = {}


def _load_from_disk() -> None:
    """Load the persisted IG↔TG mapping (NOT pending codes)."""
    global _IG_TO_TG, _TG_TO_IG
    if not _PAIRINGS_FILE.exists():
        return
    try:
        data = json.loads(_PAIRINGS_FILE.read_text(encoding="utf-8"))
        mapping = data.get("ig_to_tg", {})
        with _lock:
            _IG_TO_TG = {int(k): int(v) for k, v in mapping.items()}
            _TG_TO_IG = {int(v): int(k) for k, v in _IG_TO_TG.items()}
    except Exception:
        # Corrupt file — start fresh
        _IG_TO_TG = {}
        _TG_TO_IG = {}


def _save_to_disk() -> None:
    """Persist the IG↔TG mapping."""
    try:
        with _lock:
            snapshot = {str(k): v for k, v in _IG_TO_TG.items()}
        _PAIRINGS_FILE.write_text(
            json.dumps({"ig_to_tg": snapshot, "version": 1}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# Load on import
_load_from_disk()


def _new_code() -> str:
    """Generate a unique 6-character pairing code."""
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in _PENDING_CODES:
            return code


def generate_code(tg_user_id: int) -> str:
    """Return an unexpired pairing code for the given Telegram user.

    If the user already has an unexpired, unclaimed code, returns the same one
    (so repeated `/ig` commands don't generate multiple codes).
    """
    with _lock:
        existing = _USER_CODES.get(tg_user_id)
        if existing and existing in _PENDING_CODES:
            p = _PENDING_CODES[existing]
            if not p.get("claimed") and time.time() - p["created_at"] < CODE_TTL_SECONDS:
                return existing

        code = _new_code()
        _PENDING_CODES[code] = {
            "tg_user_id": tg_user_id,
            "created_at": time.time(),
            "claimed": False,
        }
        _USER_CODES[tg_user_id] = code
        return code


def claim_code(code: str, ig_user_id: int, ig_username: str = "") -> Optional[int]:
    """Claim a pairing code on behalf of an Instagram user.

    Returns the linked Telegram user ID on success, or None if the code is
    invalid/expired/already-claimed.
    """
    if not code:
        return None
    code = code.upper().strip()
    if len(code) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in code):
        return None

    with _lock:
        p = _PENDING_CODES.get(code)
        if not p:
            return None
        if p.get("claimed"):
            return None
        if time.time() - p["created_at"] > CODE_TTL_SECONDS:
            # Expired — clean up
            _PENDING_CODES.pop(code, None)
            _USER_CODES.pop(p["tg_user_id"], None)
            return None

        # If this IG user is already paired with a different TG user, unpair first
        existing_tg = _IG_TO_TG.get(ig_user_id)
        if existing_tg is not None and existing_tg != p["tg_user_id"]:
            _TG_TO_IG.pop(existing_tg, None)

        # If this TG user was previously paired with a different IG account, unpair that
        existing_ig = _TG_TO_IG.get(p["tg_user_id"])
        if existing_ig is not None and existing_ig != ig_user_id:
            _IG_TO_TG.pop(existing_ig, None)

        p["claimed"] = True
        p["ig_user_id"] = ig_user_id
        p["ig_username"] = ig_username
        p["claimed_at"] = time.time()

        tg_user_id = p["tg_user_id"]
        _IG_TO_TG[ig_user_id] = tg_user_id
        _TG_TO_IG[tg_user_id] = ig_user_id
        _save_to_disk()

        # Clean up the code so it can't be reused
        _PENDING_CODES.pop(code, None)
        _USER_CODES.pop(tg_user_id, None)

        return tg_user_id


def get_tg_user_id(ig_user_id: int) -> Optional[int]:
    """Look up the Telegram user ID linked to an Instagram user ID."""
    with _lock:
        return _IG_TO_TG.get(ig_user_id)


def get_ig_user_id(tg_user_id: int) -> Optional[int]:
    """Look up the Instagram user ID linked to a Telegram user ID."""
    with _lock:
        return _TG_TO_IG.get(tg_user_id)


def is_paired_ig(ig_user_id: int) -> bool:
    return get_tg_user_id(ig_user_id) is not None


def is_paired_tg(tg_user_id: int) -> bool:
    return get_ig_user_id(tg_user_id) is not None


def unpair_by_tg(tg_user_id: int) -> bool:
    """Remove the IG pairing for a Telegram user. Returns True if a pairing was removed."""
    with _lock:
        ig_id = _TG_TO_IG.pop(tg_user_id, None)
        if ig_id is None:
            return False
        _IG_TO_TG.pop(ig_id, None)
        _save_to_disk()
        return True


def unpair_by_ig(ig_user_id: int) -> bool:
    """Remove the IG pairing for an Instagram user. Returns True if a pairing was removed."""
    with _lock:
        tg_id = _IG_TO_TG.pop(ig_user_id, None)
        if tg_id is None:
            return False
        _TG_TO_IG.pop(tg_id, None)
        _save_to_disk()
        return True


def cleanup_expired() -> int:
    """Remove expired unclaimed codes. Returns the number removed."""
    now = time.time()
    with _lock:
        expired = [
            c for c, p in _PENDING_CODES.items()
            if not p.get("claimed") and now - p["created_at"] > CODE_TTL_SECONDS
        ]
        for c in expired:
            p = _PENDING_CODES.pop(c, None)
            if p:
                _USER_CODES.pop(p["tg_user_id"], None)
        return len(expired)


def pairings_count() -> int:
    """Number of active IG↔TG pairings."""
    with _lock:
        return len(_IG_TO_TG)


def pending_codes_count() -> int:
    """Number of unclaimed pending codes."""
    with _lock:
        return sum(1 for p in _PENDING_CODES.values() if not p.get("claimed"))
