"""Redis-based distributed lock for serializing training runs across processes.

Round 30 audit (P2 #1): the existing `threading.Lock` in this module
only protected against concurrent training within ONE Python process.
Since the RQ worker is a separate container from the FastAPI event
loop, a manual "Train Ulang" click in the dashboard could race the
23:00 WITA cron — both processes would pass the "no active training"
check, both create a new ModelVersion row, and both run the full PCA
+ K-Means on the same dataset. The result was wasted compute and a
corrupt `is_active` flag (the second commit wins, leaving the first's
MV marked active but never finalized).

The fix: use a Redis SETNX-based lock. The first process to acquire
the key holds the lock; concurrent processes either block (with a
short timeout) or fail-fast depending on the caller's choice.

For training, we use a short timeout (5 seconds) and fail-fast. If
the lock is already held, the new caller sees a 409 Conflict and the
admin can retry once the previous run finishes. This matches the
"in-flight training should not duplicate" semantic we want.

Pattern from redis.io/docs/manual/patterns/distributed-locks:
- SET key value NX EX 5  (atomic acquire-with-expiry)
- DEL key (release)
"""
import logging
import os
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("monitor.train_lock_redis")

_LOCK_KEY = "monitorung:train_lock"
# Auto-expire after this many seconds. Defense-in-depth: if a process
# crashes after acquire, the lock releases on its own.
_LOCK_TTL_SEC = 300
# How long to wait when calling acquire_with_timeout.
_DEFAULT_TIMEOUT_SEC = 0.0  # 0 = fail-fast (no wait)


def _redis_client():
    """Lazy Redis client. We import redis here (not at module top) so
    the rest of this module can be imported in dev (USE_FAKEREDIS=true)
    without forcing the redis library path.
    """
    import redis
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(redis_url, decode_responses=True)


def _acquire_sync(client, key: str, ttl: int) -> bool:
    """SET key value NX EX ttl — atomic acquire-with-expiry."""
    return bool(client.set(key, str(int(time.time())), nx=True, ex=ttl))


def _release_sync(client, key: str) -> bool:
    """DEL key — release. Returns True if the key was deleted."""
    try:
        return bool(client.delete(key))
    except Exception as e:
        logger.warning("train_lock: release failed: %s", e)
        return False


class TrainLockBusy(Exception):
    """Raised when another process holds the training lock."""


@contextmanager
def acquire_train_lock(timeout: float = _DEFAULT_TIMEOUT_SEC):
    """Acquire the distributed training lock. Fail-fast by default.

    Usage:
        with acquire_train_lock() as locked:
            if not locked:
                raise TrainLockBusy("another training is in progress")
            # do training work
    """
    client = _redis_client()
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            if _acquire_sync(client, _LOCK_KEY, _LOCK_TTL_SEC):
                acquired = True
                yield True
                break
            if time.monotonic() >= deadline:
                yield False
                break
            time.sleep(0.1)
    finally:
        if acquired:
            _release_sync(client, _LOCK_KEY)


def is_train_lock_held() -> bool:
    """Return True if another process currently holds the training lock.

    Used by the scheduler cron / admin endpoints to detect contention
    before kicking off a heavy training run.
    """
    try:
        client = _redis_client()
        return bool(client.exists(_LOCK_KEY))
    except Exception as e:
        logger.warning("train_lock: exists check failed: %s", e)
        return False
