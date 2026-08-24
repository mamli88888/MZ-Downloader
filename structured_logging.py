"""Structured (JSON) logging for MZ-Downloader.

Call ``setup_structured_logging()`` once at bot startup AFTER
``logging.basicConfig``. When env ``LOG_FORMAT`` == "json" the root handlers'
formatters are replaced with ``JsonFormatter``; any other value is a no-op
(human-readable format is kept). Contextual fields can be attached per
task via ``log_context(...)`` / ``clear_context()``.
"""
from __future__ import annotations

import json
import logging
import os
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("mz_log_context", default={})


def log_context(**fields: Any) -> None:
    """Merge ``fields`` into the context-local logging context (ContextVar,
    so asyncio-task safe). ``JsonFormatter`` merges these into every record."""
    merged = dict(_CONTEXT.get())
    merged.update(fields)
    _CONTEXT.set(merged)


def clear_context() -> None:
    """Reset the context-local logging fields to empty."""
    _CONTEXT.set({})


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter: ts (ISO8601 w/ tz), level, logger, msg;
    plus exc/func/line when present, and any ``log_context`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        if getattr(record, "lineno", 0):
            payload["line"] = record.lineno
        if getattr(record, "funcName", None):
            payload["func"] = record.funcName
        payload.update(_CONTEXT.get())
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_structured_logging(format_env: str | None = None) -> None:
    """Switch root handlers to JSON when LOG_FORMAT == "json".

    ``format_env`` (when provided) overrides the ``LOG_FORMAT`` env var.
    Anything other than "json" keeps the existing human format (no-op)."""
    chosen = format_env if format_env is not None else os.environ.get("LOG_FORMAT", "")
    if str(chosen).strip().lower() != "json":
        return
    formatter = JsonFormatter()
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
