"""Performance primitives: TTL cache, circuit breaker, shared HTTP connection pooling, token-bucket rate limiter."""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

import httpx

__all__ = [
    "TTLCache", "CircuitBreaker", "CLOSED", "OPEN", "HALF_OPEN",
    "get_breaker", "all_breakers", "SharedClientManager", "CLIENTS",
    "pooled_client", "AsyncRateLimiter", "get_limiter",
]

# Circuit breaker states
CLOSED = "closed"      # normal operation, requests flow
OPEN = "open"          # tripped: requests rejected until recovery timeout
HALF_OPEN = "half_open"  # probing: limited calls allowed to test recovery


# ================================ TTLCache ================================
class TTLCache:
    """Asyncio-safe in-memory cache with per-entry TTL and LRU eviction.

    Guarded by an ``asyncio.Lock`` — safe when coroutines interleave on a
    single event loop (not thread-safe across loops/threads by design).
    Expired entries are deleted lazily on ``get``. When ``maxsize`` is
    exceeded, the least-recently-used entry is evicted (OrderedDict
    ``move_to_end`` / ``popitem(last=False)`` semantics).

    Usage::

        cache = TTLCache(maxsize=2048)
        await cache.set("k", payload, ttl=300.0)
        val = await cache.get("k")   # None if missing/expired
        cache.stats()                # {"size", "hits", "misses", "evictions"}
    """

    def __init__(self, maxsize: int = 2048) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    async def get(self, key: str) -> Any | None:
        """Return cached value or None; expired entries are removed lazily."""
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if expires_at <= time.monotonic():
                del self._data[key]  # lazy expiry
                self._misses += 1
                return None
            self._data.move_to_end(key)  # mark recently used
            self._hits += 1
            return value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        """Store ``value`` for ``ttl`` seconds (monotonic clock); evicts the
        oldest entry when the cache is full."""
        async with self._lock:
            self._data[key] = (value, time.monotonic() + ttl)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
                self._evictions += 1

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present (no error when missing)."""
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        """Drop every entry (counters are kept for stats)."""
        async with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        """Cheap sync snapshot: {"size", "hits", "misses", "evictions"}."""
        return {"size": len(self._data), "hits": self._hits, "misses": self._misses, "evictions": self._evictions}


# ============================== CircuitBreaker ==============================
class CircuitBreaker:
    """Minimal sync circuit breaker keyed by logical dependency name.

    Usage::

        br = get_breaker("tikwm")           # create-if-missing singleton
        if not br.allow():
            return "rate limited / breaker open"
        try:
            ...
        except Exception:
            br.record_failure()
        else:
            br.record_success()
    """

    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0,
                 half_open_max_calls: int = 1) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0

    def _maybe_recover(self) -> None:
        """Promote OPEN -> HALF_OPEN once the recovery timeout has elapsed."""
        if self._state == OPEN and time.monotonic() >= self._opened_at + self._recovery_timeout:
            self._state = HALF_OPEN
            self._half_open_calls = 0

    def allow(self) -> bool:
        """Cheap sync gate. closed -> True; open -> True only after the
        recovery timeout (switching to half_open); half_open -> True while
        under ``half_open_max_calls`` concurrent probes."""
        self._maybe_recover()
        if self._state == CLOSED:
            return True
        if self._state == OPEN:
            return False
        # HALF_OPEN: allow a limited number of probe calls
        if self._half_open_calls < self._half_open_max_calls:
            self._half_open_calls += 1
            return True
        return False

    def record_success(self) -> None:
        """A call succeeded: reset failure count and close the breaker."""
        self._failures = 0
        self._state = CLOSED
        self._half_open_calls = 0

    def record_failure(self) -> None:
        """A call failed: on threshold (closed) or any failure (half_open),
        trip the breaker open with a fresh timestamp."""
        self._failures += 1
        if self._state == HALF_OPEN:
            self._trip_open()
        elif self._state == CLOSED and self._failures >= self._failure_threshold:
            self._trip_open()

    def _trip_open(self) -> None:
        self._state = OPEN
        self._opened_at = time.monotonic()
        self._half_open_calls = 0

    def state(self) -> str:
        """Current state string (refreshes OPEN -> HALF_OPEN if due)."""
        self._maybe_recover()
        return self._state

    def info(self) -> dict:
        """Snapshot dict for dashboards/alerts."""
        return {"name": self.name, "state": self.state(), "failures": self._failures,
                "opened_at": self._opened_at, "failure_threshold": self._failure_threshold,
                "recovery_timeout": self._recovery_timeout}


_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    """Return (creating on first use) the named breaker singleton. Keyword
    args (failure_threshold, recovery_timeout, half_open_max_calls) only
    apply at creation time."""
    br = _BREAKERS.get(name)
    if br is None:
        br = CircuitBreaker(name, **kwargs)
        _BREAKERS[name] = br
    return br


def all_breakers() -> dict[str, dict]:
    """Snapshot ``{name: info()}`` for every registered breaker."""
    return {name: br.info() for name, br in _BREAKERS.items()}


# ============================ SharedClientManager ============================
class SharedClientManager:
    """ONE shared ``httpx.AsyncClient`` per logical name so TCP/TLS
    connections are reused across the process instead of per-call clients.

    Usage::

        client = pooled_client("apify", headers=..., proxy=...)
        resp = await client.get(url)   # long-lived; do NOT `async with` it

    NOTE: if a client already exists (and is open) it is returned as-is —
    later ``headers``/``proxy`` arguments are IGNORED. Callers are expected
    to pass the same configuration for a given name on every call. A client
    that was closed (``.is_closed``) is transparently recreated.
    """

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}

    def client(self, name: str, *, headers: dict | None = None,
               proxy: str | None = None) -> httpx.AsyncClient:
        """Get or create the pooled client for ``name`` (recreates closed)."""
        existing = self._clients.get(name)
        if existing is not None and not existing.is_closed:
            return existing
        new_client = httpx.AsyncClient(
            headers={"Accept-Encoding": "gzip, deflate", **(headers or {})},
            follow_redirects=True,
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0),
            proxy=proxy,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0),
        )
        self._clients[name] = new_client
        return new_client

    async def aclose_all(self) -> None:
        """Close every pooled client concurrently; errors are suppressed."""
        if self._clients:
            await asyncio.gather(
                *(c.aclose() for c in self._clients.values()),
                return_exceptions=True,
            )
        self._clients.clear()


CLIENTS = SharedClientManager()
"""Module-level singleton manager."""


def pooled_client(name: str, **kw: Any) -> httpx.AsyncClient:
    """Shortcut for ``CLIENTS.client(name, **kw)``."""
    return CLIENTS.client(name, **kw)


# ============================== AsyncRateLimiter ============================
class AsyncRateLimiter:
    """Token-bucket rate limiter (per platform). ``acquire`` NEVER blocks —
    it returns False immediately when the bucket is empty so callers can
    defer/reject work; use ``wait_acquire`` when polling is acceptable.

    Usage::

        rl = get_limiter("apify", rate_per_minute=30)
        if not await rl.acquire():
            ...  # defer this job
        if rl.over_limit():   # peek only, does not consume
            ...
    """

    def __init__(self, rate_per_minute: float, burst: int | None = None) -> None:
        self._rate_per_sec = rate_per_minute / 60.0
        self._capacity = float(burst if burst is not None else max(1, int(rate_per_minute)))
        self._tokens = self._capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_sec)
            self._last_refill = now

    def over_limit(self) -> bool:
        """Sync peek without consuming: True when fewer than 1 token left."""
        self._refill()
        return self._tokens < 1.0

    async def acquire(self) -> bool:
        """Consume 1 token if available; returns False immediately (non-
        blocking) when the bucket is empty so the caller can defer."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    async def wait_acquire(self, timeout: float = 30.0) -> bool:
        """Poll ``acquire`` every 0.25s until a token frees up or ``timeout``
        seconds elapse; returns True once a token was consumed."""
        deadline = time.monotonic() + timeout
        while True:
            if await self.acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)


_LIMITERS: dict[str, AsyncRateLimiter] = {}


def get_limiter(name: str, rate_per_minute: float, burst: int | None = None) -> AsyncRateLimiter:
    """Return (creating on first use) the named limiter singleton. Args only
    apply at creation time."""
    rl = _LIMITERS.get(name)
    if rl is None:
        rl = AsyncRateLimiter(rate_per_minute, burst=burst)
        _LIMITERS[name] = rl
    return rl
