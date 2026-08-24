"""Additive SQLite store for MZ-Downloader new features.

This module is a NEW, purely additive persistence layer. The legacy
``users.json`` flat list (users_db.py) is NOT touched, read, or modified in
any way -- all state here lives in a separate SQLite database file.

Design:
    - One shared ``sqlite3`` connection, ``check_same_thread=False``,
      ``isolation_level=None`` (autocommit), WAL journal mode.
    - The connection is guarded by a single ``threading.Lock``; every
      statement runs inside that lock.
    - Every public coroutine offloads its sync DB work via
      ``asyncio.to_thread`` so the event loop is never blocked.
    - Migrations: SQL files in ``migrations/`` applied in version order,
      tracked with ``PRAGMA user_version`` (e.g. ``0001_*.sql`` -> version 1).
    - Failure safety: if the store was never initialized (DB unavailable,
      bot partial startup, ...) every public call logs a warning and returns
      a safe no-op value (``None`` / ``0`` / ``[]`` / ``False`` / ``{}``)
      so the bot never crashes on storage problems.

Only the Python standard library is used. This module does NOT import
bot.py or config.py and is standalone-importable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "init_store",
    "close_store",
    "store_path",
    # token status
    "upsert_token",
    "mark_token_result",
    "token_statuses",
    # alerts
    "create_alert",
    "set_alert_sent",
    "due_reminders",
    "bump_alert_reminder",
    "ack_alerts",
    "ack_alerts_for_admin",
    "open_alerts",
    # bookmarks
    "add_bookmark",
    "list_bookmarks",
    "count_bookmarks",
    "delete_bookmark",
    "get_bookmark",
    # stats
    "record_download_event",
    "user_stats",
    "platform_leaders",
    "active_users",
    # dedupe
    "dedupe_lookup",
    "dedupe_save",
    "dedupe_hit",
    "dedupe_prune",
    "dedupe_fingerprint",
    # autoshare
    "add_autoshare_target",
    "remove_autoshare_target",
    "list_autoshare_targets",
    # scheduler
    "add_scheduled_job",
    "due_jobs",
    "update_job_run",
    "set_job_active",
    "delete_job",
    "list_jobs",
    # size audit
    "record_size_mismatch",
    # ai cache
    "ai_cache_get",
    "ai_cache_set",
    "ai_cache_prune",
    # maintenance
    "prune_all",
]

_LOG = logging.getLogger("MZDownloader.store")

_DEFAULT_DB_PATH = "/home/z/MZ-Downloader/mz_data.db"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_DAY_SECONDS = 86400.0


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------


class _State:
    """Holds the shared connection, its guard lock and the resolved path."""

    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None
        self.path: Path | None = None
        self.lock = threading.Lock()
        self.applied_migrations: list[int] = []

    @property
    def ready(self) -> bool:
        return self.conn is not None


_STATE = _State()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    """Current epoch seconds (used for every REAL timestamp column)."""
    return time.time()


def _utc_day_key(ts: float) -> str:
    """Epoch seconds -> 'YYYY-MM-DD' in UTC (matches date(x,'unixepoch'))."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Convert all fetched rows (sqlite3.Row) to plain dicts."""
    return [dict(row) for row in cursor.fetchall()]


def _one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    """Convert a single fetched row to a dict (or None)."""
    row = cursor.fetchone()
    return dict(row) if row is not None else None


async def _run(
    fn: Callable[..., Any],
    /,
    *args: Any,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    """Offload a sync DB helper to a worker thread.

    If the store is not initialized, logs a warning and returns ``default``
    so callers get a safe no-op result instead of an exception.
    """
    if not _STATE.ready:
        _LOG.warning("store not initialized (call await init_store()); no-op for %r", fn.__name__)
        return default
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Init / migrations / close
# ---------------------------------------------------------------------------


def _resolve_db_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    env = os.environ.get("MZ_DB_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(_DEFAULT_DB_PATH)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations from migrations/ tracked via user_version."""
    if not _MIGRATIONS_DIR.is_dir():
        _LOG.warning("migrations directory not found: %s", _MIGRATIONS_DIR)
        return
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    pending: list[tuple[int, Path]] = []
    for sql_file in _MIGRATIONS_DIR.glob("*.sql"):
        prefix = sql_file.name.split("_", 1)[0]
        if not prefix.isdigit():
            _LOG.warning("skipping migration with unparseable version: %s", sql_file.name)
            continue
        version = int(prefix)
        if version > current:
            pending.append((version, sql_file))
    for version, sql_file in sorted(pending):
        sql = sql_file.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version:d}")
        _STATE.applied_migrations.append(version)
        _LOG.info("applied migration %s (user_version=%d)", sql_file.name, version)
    if not pending:
        _LOG.info("store schema up to date (user_version=%d)", current)


def _sync_init_store(path: str | Path | None = None) -> None:
    db_path = _resolve_db_path(path)
    with _STATE.lock:
        if _STATE.conn is not None:
            if _STATE.path == db_path:
                return  # already initialized at this path
            _LOG.info("re-initializing store at new path: %s", db_path)
            _close_conn_locked()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _STATE.conn = conn
        _STATE.path = db_path
        try:
            _apply_migrations(conn)
        except sqlite3.Error:
            _LOG.exception("migration failure for %s", db_path)
            _close_conn_locked()
            raise
        _LOG.info("store initialized at %s (user_version=%d)", db_path, conn.execute("PRAGMA user_version").fetchone()[0])


def _close_conn_locked() -> None:
    """Close and reset the shared connection. Caller must hold _STATE.lock."""
    conn, _STATE.conn = _STATE.conn, None
    _STATE.path = None
    _STATE.applied_migrations = []
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            _LOG.exception("error closing store connection")


def _sync_close_store() -> None:
    with _STATE.lock:
        _close_conn_locked()
    _LOG.info("store closed")


async def init_store(path: str | Path | None = None) -> None:
    """Open the SQLite store and apply pending migrations.

    ``path`` overrides the ``MZ_DB_PATH`` env var, which overrides the
    default ``/home/z/MZ-Downloader/mz_data.db``.
    """
    await asyncio.to_thread(_sync_init_store, path)


async def close_store() -> None:
    """Close the shared connection (idempotent)."""
    await asyncio.to_thread(_sync_close_store)


def store_path() -> Path:
    """Resolved DB path. Sync; raises RuntimeError if uninitialized."""
    if _STATE.path is None:
        raise RuntimeError("store not initialized; call await init_store() first")
    return _STATE.path


# ---------------------------------------------------------------------------
# Token status
# ---------------------------------------------------------------------------


def _impl_upsert_token(token_hash: str, token_label: str, owner_email: str) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "INSERT OR IGNORE INTO apify_token_status"
            " (token_hash, token_label, owner_email, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (token_hash, token_label, owner_email, _now()),
        )


async def upsert_token(token_hash: str, token_label: str = "", owner_email: str = "") -> None:
    """Register a token (INSERT OR IGNORE) without touching its status."""
    await _run(_impl_upsert_token, token_hash, token_label, owner_email)


def _impl_mark_token_result(
    token_hash: str,
    ok: bool,
    error_type: str,
    error_message: str,
    owner_email: str,
    token_label: str,
) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    now = _now()
    with _STATE.lock:
        conn.execute(
            "INSERT OR IGNORE INTO apify_token_status (token_hash, updated_at) VALUES (?, ?)",
            (token_hash, now),
        )
        if owner_email or token_label:
            conn.execute(
                "UPDATE apify_token_status SET"
                " token_label = CASE WHEN ? != '' THEN ? ELSE token_label END,"
                " owner_email = CASE WHEN ? != '' THEN ? ELSE owner_email END"
                " WHERE token_hash = ?",
                (token_label, token_label, owner_email, owner_email, token_hash),
            )
        if ok:
            conn.execute(
                "UPDATE apify_token_status SET status = 'active', fail_count = 0,"
                " last_success_at = ?, updated_at = ? WHERE token_hash = ?",
                (now, now, token_hash),
            )
        else:
            row = conn.execute(
                "SELECT fail_count FROM apify_token_status WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            fail_count = (int(row["fail_count"]) if row is not None else 0) + 1
            status = "suspect" if fail_count < 3 else "broken"
            conn.execute(
                "UPDATE apify_token_status SET fail_count = ?, status = ?,"
                " last_error = ?, last_error_type = ?, last_error_at = ?, updated_at = ?"
                " WHERE token_hash = ?",
                (fail_count, status, error_message, error_type, now, now, token_hash),
            )


async def mark_token_result(
    token_hash: str,
    ok: bool,
    error_type: str = "",
    error_message: str = "",
    owner_email: str = "",
    token_label: str = "",
) -> None:
    """Record a success/failure for a token and derive its status.

    ok  -> status='active', fail_count=0, last_success_at=now.
    bad -> fail_count+=1; 'suspect' until it reaches 3, then 'broken'.
    """
    await _run(
        _impl_mark_token_result,
        token_hash, ok, error_type, error_message, owner_email, token_label,
    )


def _impl_token_statuses() -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM apify_token_status"
            " ORDER BY CASE status WHEN 'broken' THEN 0 WHEN 'suspect' THEN 1 ELSE 2 END,"
            " updated_at DESC"
        )
        return _rows(cur)


async def token_statuses() -> list[dict[str, Any]]:
    """All token rows: broken first, then suspect, then active (updated_at desc)."""
    return await _run(_impl_token_statuses, default=[])


# ---------------------------------------------------------------------------
# Token alerts
# ---------------------------------------------------------------------------


def _impl_create_alert(
    token_hash: str, owner_email: str, platform: str, error_type: str, error_message: str
) -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "INSERT INTO token_alerts"
            " (token_hash, owner_email, platform, error_type, error_message, first_seen_at, done)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (token_hash, owner_email, platform, error_type, error_message, _now()),
        )
        return int(cur.lastrowid or 0)


async def create_alert(
    token_hash: str,
    owner_email: str,
    platform: str,
    error_type: str,
    error_message: str,
) -> int:
    """Insert a new open alert; returns its row id."""
    return await _run(
        _impl_create_alert,
        token_hash, owner_email, platform, error_type, error_message,
        default=0,
    )


def _impl_set_alert_sent(alert_id: int, sent_at: float, next_reminder_at: float) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "UPDATE token_alerts SET sent_at = ?, next_reminder_at = ? WHERE id = ?",
            (sent_at, next_reminder_at, alert_id),
        )


async def set_alert_sent(alert_id: int, sent_at: float, next_reminder_at: float) -> None:
    """Mark an alert as notified and schedule its first reminder."""
    await _run(_impl_set_alert_sent, alert_id, sent_at, next_reminder_at)


def _impl_due_reminders(now: float) -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM token_alerts WHERE done = 0 AND acked_at IS NULL"
            " AND next_reminder_at IS NOT NULL AND next_reminder_at <= ?"
            " AND reminders_sent < 5 ORDER BY next_reminder_at ASC"
            ,
            (now,),
        )
        return _rows(cur)


async def due_reminders(now: float) -> list[dict[str, Any]]:
    """Open, unacked alerts whose next reminder is due (< 5 reminders sent)."""
    return await _run(_impl_due_reminders, now, default=[])


def _impl_bump_alert_reminder(alert_id: int, next_reminder_at: float) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        row = conn.execute(
            "SELECT reminders_sent FROM token_alerts WHERE id = ?", (alert_id,)
        ).fetchone()
        if row is None:
            return
        reminders_sent = int(row["reminders_sent"]) + 1
        done = 1 if reminders_sent >= 5 else 0
        conn.execute(
            "UPDATE token_alerts SET reminders_sent = ?, next_reminder_at = ?, done = ?"
            " WHERE id = ?",
            (reminders_sent, next_reminder_at, done, alert_id),
        )


async def bump_alert_reminder(alert_id: int, next_reminder_at: float) -> None:
    """reminders_sent += 1; when it reaches 5 the alert is closed (done=1)."""
    await _run(_impl_bump_alert_reminder, alert_id, next_reminder_at)


def _impl_ack_alerts(alert_ids: list[int]) -> int:
    conn = _STATE.conn
    if conn is None or not alert_ids:
        return 0
    with _STATE.lock:
        marks = ",".join("?" for _ in alert_ids)
        cur = conn.execute(
            f"UPDATE token_alerts SET acked_at = ?, done = 1 WHERE id IN ({marks})",  # noqa: S608
            (_now(), *alert_ids),
        )
        return int(cur.rowcount or 0)


async def ack_alerts(alert_ids: list[int]) -> int:
    """Ack the given alerts (done=1); returns count acknowledged."""
    if not alert_ids:
        return 0
    return await _run(_impl_ack_alerts, list(alert_ids), default=0)


def _impl_ack_alerts_for_admin() -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "UPDATE token_alerts SET acked_at = ?, done = 1 WHERE done = 0", (_now(),)
        )
        return int(cur.rowcount or 0)


async def ack_alerts_for_admin() -> int:
    """Ack ALL open alerts; returns the count acknowledged."""
    return await _run(_impl_ack_alerts_for_admin, default=0)


def _impl_open_alerts() -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM token_alerts WHERE done = 0 ORDER BY first_seen_at DESC"
        )
        return _rows(cur)


async def open_alerts() -> list[dict[str, Any]]:
    """All open (done=0) alerts, newest first."""
    return await _run(_impl_open_alerts, default=[])


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------


def _impl_add_bookmark(
    user_id: int,
    url: str,
    platform: str,
    title: str,
    file_id: str,
    media_kind: str,
    size_bytes: int,
) -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "INSERT INTO bookmarks"
            " (user_id, url, platform, title, file_id, media_kind, size_bytes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, url, platform, title, file_id, media_kind, size_bytes, _now()),
        )
        return int(cur.lastrowid or 0)


async def add_bookmark(
    user_id: int,
    url: str,
    platform: str = "",
    title: str = "",
    file_id: str = "",
    media_kind: str = "",
    size_bytes: int = 0,
) -> int:
    """Save a bookmark for a user; returns its row id."""
    return await _run(
        _impl_add_bookmark,
        user_id, url, platform, title, file_id, media_kind, size_bytes,
        default=0,
    )


def _impl_list_bookmarks(user_id: int, limit: int, offset: int) -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM bookmarks WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        return _rows(cur)


async def list_bookmarks(user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    return await _run(_impl_list_bookmarks, user_id, limit, offset, default=[])


def _impl_count_bookmarks(user_id: int) -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM bookmarks WHERE user_id = ?", (user_id,)
        )
        return int(cur.fetchone()["n"])


async def count_bookmarks(user_id: int) -> int:
    return await _run(_impl_count_bookmarks, user_id, default=0)


def _impl_delete_bookmark(bookmark_id: int, user_id: int) -> bool:
    conn = _STATE.conn
    if conn is None:
        return False
    with _STATE.lock:
        cur = conn.execute(
            "DELETE FROM bookmarks WHERE id = ? AND user_id = ?", (bookmark_id, user_id)
        )
        return bool(cur.rowcount)


async def delete_bookmark(bookmark_id: int, user_id: int) -> bool:
    return await _run(_impl_delete_bookmark, bookmark_id, user_id, default=False)


def _impl_get_bookmark(bookmark_id: int, user_id: int) -> dict[str, Any] | None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM bookmarks WHERE id = ? AND user_id = ?", (bookmark_id, user_id)
        )
        return _one(cur)


async def get_bookmark(bookmark_id: int, user_id: int) -> dict[str, Any] | None:
    return await _run(_impl_get_bookmark, bookmark_id, user_id, default=None)


# ---------------------------------------------------------------------------
# Download stats
# ---------------------------------------------------------------------------


def _impl_record_download_event(
    user_id: int, platform: str, media_kind: str, size_bytes: int, request_id: str
) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "INSERT INTO user_download_events"
            " (user_id, platform, media_kind, size_bytes, request_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, platform, media_kind, size_bytes, request_id, _now()),
        )


async def record_download_event(
    user_id: int,
    platform: str,
    media_kind: str,
    size_bytes: int,
    request_id: str = "",
) -> None:
    """Fire-and-forget event for per-user / per-platform statistics."""
    await _run(_impl_record_download_event, user_id, platform, media_kind, size_bytes, request_id)


def _impl_user_stats(user_id: int, days: int) -> dict[str, Any]:
    conn = _STATE.conn
    if conn is None:
        return {}
    now = _now()
    cutoff = now - days * _DAY_SECONDS
    with _STATE.lock:
        total_row = conn.execute(
            "SELECT COUNT(*) AS total_downloads, COALESCE(SUM(size_bytes), 0) AS total_bytes"
            " FROM user_download_events WHERE user_id = ? AND created_at >= ?",
            (user_id, cutoff),
        ).fetchone()
        platform_rows = _rows(
            conn.execute(
                "SELECT platform, COUNT(*) AS downloads, COALESCE(SUM(size_bytes), 0) AS bytes"
                " FROM user_download_events WHERE user_id = ? AND created_at >= ?"
                " GROUP BY platform ORDER BY downloads DESC, platform ASC LIMIT 6",
                (user_id, cutoff),
            )
        )
        active_days_row = conn.execute(
            "SELECT COUNT(DISTINCT date(created_at, 'unixepoch')) AS active_days"
            " FROM user_download_events WHERE user_id = ? AND created_at >= ?",
            (user_id, cutoff),
        ).fetchone()
        daily_rows = _rows(
            conn.execute(
                "SELECT date(created_at, 'unixepoch') AS day, COUNT(*) AS downloads,"
                " COALESCE(SUM(size_bytes), 0) AS bytes"
                " FROM user_download_events WHERE user_id = ? AND created_at >= ?"
                " GROUP BY day",
                (user_id, now - 14 * _DAY_SECONDS),
            )
        )
    # Gap-fill the last 14 UTC days with zeros.
    by_day = {row["day"]: row for row in daily_rows}
    daily: list[dict[str, Any]] = []
    for back in range(13, -1, -1):
        key = _utc_day_key(now - back * _DAY_SECONDS)
        row = by_day.get(key)
        daily.append(
            {
                "day": key,
                "downloads": int(row["downloads"]) if row else 0,
                "bytes": int(row["bytes"]) if row else 0,
            }
        )
    return {
        "total_downloads": int(total_row["total_downloads"]),
        "total_bytes": int(total_row["total_bytes"]),
        "platforms": platform_rows,
        "daily": daily,
        "active_days": int(active_days_row["active_days"]),
    }


async def user_stats(user_id: int, days: int = 30) -> dict[str, Any]:
    """Aggregated stats for a user: totals, top-6 platforms, 14 gap-filled days."""
    return await _run(_impl_user_stats, user_id, days, default={})


def _impl_platform_leaders(days: int) -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT platform, COUNT(*) AS downloads, COALESCE(SUM(size_bytes), 0) AS bytes"
            " FROM user_download_events WHERE created_at >= ?"
            " GROUP BY platform ORDER BY downloads DESC, platform ASC LIMIT 5",
            (_now() - days * _DAY_SECONDS,),
        )
        return _rows(cur)


async def platform_leaders(days: int = 30) -> list[dict[str, Any]]:
    """Top 5 platforms globally by downloads within the window."""
    return await _run(_impl_platform_leaders, days, default=[])


def _impl_active_users(days: int) -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM user_download_events WHERE created_at >= ?",
            (_now() - days * _DAY_SECONDS,),
        )
        return int(cur.fetchone()["n"])


async def active_users(days: int = 30) -> int:
    """Distinct users with at least one download in the window."""
    return await _run(_impl_active_users, days, default=0)


# ---------------------------------------------------------------------------
# Media dedupe
# ---------------------------------------------------------------------------


def dedupe_fingerprint(source_url: str, quality: str = "") -> str:
    """Sync helper: stable fingerprint = sha256('<url>|<quality>')."""
    payload = f"{source_url}|{quality.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _impl_dedupe_lookup(fingerprint: str) -> dict[str, Any] | None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM media_dedupe WHERE fingerprint = ?", (fingerprint,)
        )
        return _one(cur)


async def dedupe_lookup(fingerprint: str) -> dict[str, Any] | None:
    return await _run(_impl_dedupe_lookup, fingerprint, default=None)


def _impl_dedupe_save(
    fingerprint: str,
    source_url: str,
    quality: str,
    file_id: str,
    mime_type: str,
    size_bytes: int,
) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    now = _now()
    with _STATE.lock:
        conn.execute(
            "INSERT OR REPLACE INTO media_dedupe"
            " (fingerprint, source_url, quality, file_id, mime_type, size_bytes,"
            "  created_at, last_hit_at, hits)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (fingerprint, source_url, quality, file_id, mime_type, size_bytes, now, now),
        )


async def dedupe_save(
    fingerprint: str,
    source_url: str,
    quality: str,
    file_id: str,
    mime_type: str,
    size_bytes: int,
) -> None:
    """Remember a fingerprint -> uploaded file mapping (dedupe cache)."""
    await _run(_impl_dedupe_save, fingerprint, source_url, quality, file_id, mime_type, size_bytes)


def _impl_dedupe_hit(fingerprint: str) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "UPDATE media_dedupe SET hits = hits + 1, last_hit_at = ? WHERE fingerprint = ?",
            (_now(), fingerprint),
        )


async def dedupe_hit(fingerprint: str) -> None:
    """Register a cache hit for an existing fingerprint."""
    await _run(_impl_dedupe_hit, fingerprint)


def _impl_dedupe_prune(max_age_seconds: float) -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "DELETE FROM media_dedupe WHERE last_hit_at < ?",
            (_now() - max_age_seconds,),
        )
        return int(cur.rowcount or 0)


async def dedupe_prune(max_age_seconds: float) -> int:
    """Drop fingerprints idle for longer than max_age_seconds; returns count."""
    return await _run(_impl_dedupe_prune, max_age_seconds, default=0)


# ---------------------------------------------------------------------------
# Autoshare targets
# ---------------------------------------------------------------------------


def _impl_add_autoshare_target(user_id: int, chat_id: int, title: str) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "INSERT OR REPLACE INTO autoshare_targets (user_id, chat_id, title, added_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, chat_id, title, _now()),
        )


async def add_autoshare_target(user_id: int, chat_id: int, title: str = "") -> None:
    await _run(_impl_add_autoshare_target, user_id, chat_id, title)


def _impl_remove_autoshare_target(user_id: int, chat_id: int) -> bool:
    conn = _STATE.conn
    if conn is None:
        return False
    with _STATE.lock:
        cur = conn.execute(
            "DELETE FROM autoshare_targets WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        return bool(cur.rowcount)


async def remove_autoshare_target(user_id: int, chat_id: int) -> bool:
    return await _run(_impl_remove_autoshare_target, user_id, chat_id, default=False)


def _impl_list_autoshare_targets(user_id: int) -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM autoshare_targets WHERE user_id = ?"
            " ORDER BY added_at ASC, chat_id ASC",
            (user_id,),
        )
        return _rows(cur)


async def list_autoshare_targets(user_id: int) -> list[dict[str, Any]]:
    return await _run(_impl_list_autoshare_targets, user_id, default=[])


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


def _impl_add_scheduled_job(
    user_id: int,
    chat_id: int,
    url: str,
    platform: str,
    interval_minutes: int,
    next_run_at: float,
) -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "INSERT INTO scheduled_jobs"
            " (user_id, chat_id, url, platform, interval_minutes, next_run_at, active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (user_id, chat_id, url, platform, interval_minutes, next_run_at, _now()),
        )
        return int(cur.lastrowid or 0)


async def add_scheduled_job(
    user_id: int,
    chat_id: int,
    url: str,
    platform: str,
    interval_minutes: int,
    next_run_at: float,
) -> int:
    """Create a recurring download job; returns its row id."""
    return await _run(
        _impl_add_scheduled_job,
        user_id, chat_id, url, platform, interval_minutes, next_run_at,
        default=0,
    )


def _impl_due_jobs(now: float) -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM scheduled_jobs WHERE active = 1 AND next_run_at <= ?"
            " ORDER BY next_run_at ASC LIMIT 5",
            (now,),
        )
        return _rows(cur)


async def due_jobs(now: float) -> list[dict[str, Any]]:
    """Up to 5 active jobs whose next_run_at has passed."""
    return await _run(_impl_due_jobs, now, default=[])


def _impl_update_job_run(job_id: int, next_run_at: float, last_status: str) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "UPDATE scheduled_jobs SET next_run_at = ?, last_status = ?, last_run_at = ?"
            " WHERE id = ?",
            (next_run_at, last_status, _now(), job_id),
        )


async def update_job_run(job_id: int, next_run_at: float, last_status: str = "") -> None:
    await _run(_impl_update_job_run, job_id, next_run_at, last_status)


def _impl_set_job_active(job_id: int, active: bool) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "UPDATE scheduled_jobs SET active = ? WHERE id = ?",
            (1 if active else 0, job_id),
        )


async def set_job_active(job_id: int, active: bool) -> None:
    await _run(_impl_set_job_active, job_id, active)


def _impl_delete_job(job_id: int, user_id: int) -> bool:
    conn = _STATE.conn
    if conn is None:
        return False
    with _STATE.lock:
        cur = conn.execute(
            "DELETE FROM scheduled_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
        )
        return bool(cur.rowcount)


async def delete_job(job_id: int, user_id: int) -> bool:
    return await _run(_impl_delete_job, job_id, user_id, default=False)


def _impl_list_jobs(user_id: int) -> list[dict[str, Any]]:
    conn = _STATE.conn
    if conn is None:
        return []
    with _STATE.lock:
        cur = conn.execute(
            "SELECT * FROM scheduled_jobs WHERE user_id = ? ORDER BY next_run_at ASC",
            (user_id,),
        )
        return _rows(cur)


async def list_jobs(user_id: int) -> list[dict[str, Any]]:
    return await _run(_impl_list_jobs, user_id, default=[])


# ---------------------------------------------------------------------------
# Size audit
# ---------------------------------------------------------------------------


def _impl_record_size_mismatch(
    request_id: str,
    url: str,
    quality: str,
    expected_bytes: int | None,
    actual_bytes: int | None,
) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "INSERT INTO size_audit_log"
            " (request_id, url, quality, expected_bytes, actual_bytes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, url, quality, expected_bytes, actual_bytes, _now()),
        )


async def record_size_mismatch(
    request_id: str,
    url: str,
    quality: str,
    expected_bytes: int | None,
    actual_bytes: int | None,
) -> None:
    """Log an expected-vs-actual size mismatch for later auditing."""
    await _run(_impl_record_size_mismatch, request_id, url, quality, expected_bytes, actual_bytes)


# ---------------------------------------------------------------------------
# AI cache
# ---------------------------------------------------------------------------


def _impl_ai_cache_get(cache_key: str) -> dict[str, Any] | None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        cur = conn.execute("SELECT * FROM ai_cache WHERE cache_key = ?", (cache_key,))
        return _one(cur)


async def ai_cache_get(cache_key: str) -> dict[str, Any] | None:
    return await _run(_impl_ai_cache_get, cache_key, default=None)


def _impl_ai_cache_set(cache_key: str, kind: str, result: str, provider: str) -> None:
    conn = _STATE.conn
    if conn is None:
        return None
    with _STATE.lock:
        conn.execute(
            "INSERT OR REPLACE INTO ai_cache (cache_key, kind, result, provider, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (cache_key, kind, result, provider, _now()),
        )


async def ai_cache_set(cache_key: str, kind: str, result: str, provider: str = "") -> None:
    await _run(_impl_ai_cache_set, cache_key, kind, result, provider)


def _impl_ai_cache_prune(max_age_seconds: float) -> int:
    conn = _STATE.conn
    if conn is None:
        return 0
    with _STATE.lock:
        cur = conn.execute(
            "DELETE FROM ai_cache WHERE created_at < ?",
            (_now() - max_age_seconds,),
        )
        return int(cur.rowcount or 0)


async def ai_cache_prune(max_age_seconds: float) -> int:
    """Drop cached AI results older than max_age_seconds; returns count."""
    return await _run(_impl_ai_cache_prune, max_age_seconds, default=0)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


async def prune_all() -> dict[str, Any]:
    """Periodic maintenance: prune dedupe + AI cache at a 7-day TTL."""
    dedupe_removed = await dedupe_prune(7 * _DAY_SECONDS)
    ai_cache_removed = await ai_cache_prune(7 * _DAY_SECONDS)
    return {
        "dedupe": int(dedupe_removed),
        "ai_cache": int(ai_cache_removed),
        "pruned_at": _now(),
    }
