"""Precise media size accounting for MZ-Downloader (stdlib only).

Displayed sizes must come from HTTP ``Content-Length`` or yt-dlp stream
metadata; if the gap to the actual byte count exceeds
``SIZE_TOLERANCE_BYTES`` (5 MB) the real size is re-measured and persisted
via :class:`SizeAudit`'s pluggable callback.  HLS/DASH sizes are estimated
as bitrate x duration x ``HLS_ACCURACY_FACTOR``.  Human-readable sizes use
two decimals with binary B/KB/MB/GB/TB units.  Never imports bot.py /
config.py / downloader.py.
"""

from __future__ import annotations

import logging
import math
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

__all__ = ["SIZE_TOLERANCE_BYTES", "HLS_ACCURACY_FACTOR", "SizeExpectation",
           "SizeAudit", "fmt_size_exact", "content_length_from_headers",
           "estimate_stream_size", "metadata_size", "normalize_size",
           "is_hls_or_dash", "size_hint_for_quality"]  # noqa: E501

logger = logging.getLogger("MZDownloader.media_size")

# Allowed |displayed - actual| gap before the real size is re-measured.
SIZE_TOLERANCE_BYTES = 5 * 1024 * 1024
# Container-overhead correction applied to HLS/DASH bitrate estimates.
HLS_ACCURACY_FACTOR = 0.95

_UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")
_UNIT_BYTES: dict[str, int] = {u: 1024 ** i for i, u in enumerate(_UNITS)}
_SIZE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)?\s*$", re.I)

# Rough video size (MB per minute) by height, for quality menu hints.
_VIDEO_MB_PER_MIN: dict[int, float] = {144: 2.0, 240: 3.5, 360: 6.0, 480: 10.0,
                                       720: 19.0, 1080: 36.0, 1440: 65.0, 2160: 130.0}


def _to_float(value: Any) -> float | None:
    """Coerce to a finite float; ``None`` for missing/bool/garbage input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> int | None:
    """Coerce to a strictly positive int, else ``None``."""
    number = _to_float(value)
    return int(number) if number is not None and number > 0 else None


def fmt_size_exact(size_bytes: float | int | None) -> str:
    """Render bytes with exactly two decimals, binary 1024 units
    (5242880 -> "5.00 MB", 1536 -> "1.50 KB"); ``None``/non-numeric/
    negative values render as an em dash "—"."""
    value = _to_float(size_bytes)
    if value is None or value < 0:
        return "—"
    for unit in _UNITS[:-1]:
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def content_length_from_headers(headers: Mapping[str, str] | None) -> int | None:
    """Case-insensitive ``Content-Length``; ``None`` if absent/invalid/<= 0."""
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if str(key).strip().lower() != "content-length":
            continue
        try:
            length = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return length if length > 0 else None
    return None


def estimate_stream_size(bitrate_kbps: float | None,
                         duration_seconds: float | None) -> int | None:
    """HLS/DASH estimate ``bitrate_kbps * 1000 / 8 * duration * 0.95``;
    ``None`` unless both inputs are finite and > 0."""
    bitrate = _to_float(bitrate_kbps)
    duration = _to_float(duration_seconds)
    if bitrate is None or bitrate <= 0 or duration is None or duration <= 0:
        return None
    return int(bitrate * 1000 / 8 * duration * HLS_ACCURACY_FACTOR)


def metadata_size(info: Mapping[str, Any] | None) -> int | None:
    """Best size from a yt-dlp-style info dict: top-level ``filesize``,
    then ``filesize_approx``, then the max over ``formats`` entries'
    ``filesize``/``filesize_approx``; ``None`` if nothing usable."""
    if not isinstance(info, Mapping):
        return None
    for key in ("filesize", "filesize_approx"):
        size = _positive_int(info.get(key))
        if size is not None:
            return size
    best: int | None = None
    formats = info.get("formats") or []
    if isinstance(formats, (list, tuple)):
        for entry in formats:
            if not isinstance(entry, Mapping):
                continue
            for key in ("filesize", "filesize_approx"):
                size = _positive_int(entry.get(key))
                if size is not None and (best is None or size > best):
                    best = size
    return best


def normalize_size(value: Any) -> int | None:
    """Normalize to int bytes, or ``None`` when unparseable/negative;
    accepts int/float bytes or strings with an optional unit ("512",
    "12 MB", "1.2GB" — case-insensitive, binary 1024)."""
    if isinstance(value, str):
        match = _SIZE_RE.match(value)
        if match is None:
            return None
        unit = (match.group(2) or "b").upper()
        result = int(float(match.group(1)) * _UNIT_BYTES[unit])
        return result if result >= 0 else None
    number = _to_float(value)
    if number is None or number < 0:
        return None
    return int(number)


def is_hls_or_dash(url_or_mime: str | None) -> bool:
    """True for ``.m3u8``/``.mpd`` suffixes or "mpegurl"/"dash+xml" content."""
    if not isinstance(url_or_mime, str):
        return False
    text = url_or_mime.strip().lower()
    if not text:
        return False
    path = re.split(r"[?#]", text, maxsplit=1)[0]
    return path.endswith((".m3u8", ".mpd")) or "mpegurl" in text or "dash+xml" in text


@dataclass
class SizeExpectation:
    """The size shown to the user, and the signal it came from."""

    expected_bytes: int | None
    source: str = "unknown"  # "content-length" | "metadata" | "estimate" | "unknown"


class SizeAudit:
    """Track expected vs. actual sizes per ``(url, quality)`` key.

    ``finalize`` returns True while within ``tolerance``; on a larger
    deviation it awaits ``on_mismatch(url, quality, expected, actual)`` so
    the caller can persist the real measured size.  Bounded memory: past
    2000 entries the oldest (insertion-ordered) are dropped."""

    MAX_TRACKED = 2000

    def __init__(
        self,
        on_mismatch: Callable[[str, str, int | None, int], Awaitable[None]] | None = None,
        tolerance: int = SIZE_TOLERANCE_BYTES,
    ) -> None:
        self._on_mismatch = on_mismatch
        self._tolerance = tolerance
        self._expectations: OrderedDict[tuple[str, str], SizeExpectation] = OrderedDict()

    def set_expected(self, url: str, quality: str, expected: int | None, source: str) -> None:
        """Record the expectation for ``(url, quality)`` (LRU refresh)."""
        key = (url, quality)
        if key in self._expectations:
            self._expectations.move_to_end(key)
        self._expectations[key] = SizeExpectation(expected, source)
        while len(self._expectations) > self.MAX_TRACKED:
            dropped = self._expectations.popitem(last=False)
            logger.debug("dropped oldest size expectation: %r", dropped)

    async def finalize(self, url: str, quality: str, actual_bytes: int) -> bool:
        """Pop the expectation and compare with the measured byte count.

        True when within tolerance (or nothing/None was recorded); False on
        mismatch, after awaiting ``on_mismatch`` (errors logged, suppressed).
        """
        expectation = self._expectations.pop((url, quality), None)
        if expectation is None or expectation.expected_bytes is None:
            return True
        if abs(expectation.expected_bytes - actual_bytes) <= self._tolerance:
            return True
        logger.warning(
            "size mismatch for %s [%s]: expected %d, actual %d (source=%s)",
            url, quality, expectation.expected_bytes, actual_bytes, expectation.source,
        )
        if self._on_mismatch is not None:
            try:
                await self._on_mismatch(
                    url, quality, expectation.expected_bytes, actual_bytes
                )
            except Exception:
                logger.warning(
                    "on_mismatch callback failed for %s [%s]",
                    url, quality, exc_info=True,
                )
        return False

    def pending(self) -> int:
        """Number of expectations awaiting finalization."""
        return len(self._expectations)


def size_hint_for_quality(kind: str, height_or_bitrate: int | None,
                          duration_seconds: float | None = None) -> str:
    """Short size hint for a quality menu button (Persian fallback).

    - audio + bitrate kbps -> "≈0.9 MB/min" (1 decimal); with duration, the
      total estimate (bitrate x duration x 0.95) via ``fmt_size_exact``.
    - video + height -> nearest table entry, e.g. "≈19MB/min"; with
      duration, the total estimate via ``fmt_size_exact``.
    - unknown -> "حجم تقریبی".
    """
    fallback = "حجم تقریبی"
    amount = _to_float(height_or_bitrate)
    duration = _to_float(duration_seconds)
    if amount is not None and amount <= 0:
        amount = None
    if duration is not None and duration <= 0:
        duration = None
    if amount is None:
        return fallback
    kind_norm = (kind or "").strip().lower()
    if kind_norm.startswith("audio"):
        if duration is not None:
            total = estimate_stream_size(amount, duration)
            return f"≈{fmt_size_exact(total)}" if total is not None else fallback
        return f"≈{amount * 1000 / 8 * 60 / (1024 * 1024):.1f} MB/min"
    if kind_norm.startswith("video"):
        height = min(_VIDEO_MB_PER_MIN, key=lambda h: abs(h - amount))
        per_minute_mb = _VIDEO_MB_PER_MIN[height]
        if duration is not None:
            total_bytes = int(per_minute_mb * (duration / 60.0) * 1024 * 1024)
            return f"≈{fmt_size_exact(total_bytes)}"
        return f"≈{per_minute_mb:g}MB/min"
    return fallback
