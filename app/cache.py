"""In-process TTL cache — Ponytail-level minimum for hot read paths.

Used for active-model lookups that are called on every dashboard load but only
change at training time (typically daily). Redis is intentionally NOT used here
because the cached value is tiny (one row) and the TTL is seconds — the
round-trip to Redis costs more than recomputing from a hot dict.

API:
    cache_get(key, ttl_seconds) → cached value or None
    cache_set(key, value) → store with timestamp
    cache_invalidate(key) → drop a key (used after training flips is_active)

Ponytail: no async, no locks, no TTL background sweeper. Reads are O(1) dict
lookups; the TTL is checked against time.monotonic(). Stale entries are dropped
on next read, not eagerly.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Any

# Single global dict — small, hot, never grows unbounded (callers set with
# explicit short TTLs and explicit invalidate calls).
_store: dict[str, tuple[float, Any]] = {}
_lock = Lock()


def cache_get(key: str, ttl_seconds: float) -> Any | None:
    """Return the cached value if it's still fresh, else None."""
    entry = _store.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        # Stale — drop and force recompute. Lock keeps the delete atomic vs.
        # a concurrent cache_set from another request.
        with _lock:
            _store.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any, ttl_seconds: float) -> None:
    """Store value under key with a fixed TTL."""
    with _lock:
        _store[key] = (time.monotonic() + ttl_seconds, value)


def cache_invalidate(key: str) -> None:
    """Drop a key — call after writes that change the cached state."""
    with _lock:
        _store.pop(key, None)
