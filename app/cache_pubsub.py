"""
Cross-process cache invalidation via Redis pub/sub.

The RQ worker and the API run in separate containers/processes — the
worker's in-process cache invalidations can't reach the API's caches
(and vice versa). To keep `_CACHE_KEY_MODEL_ID` /
`_CACHE_KEY_MODEL_LABELS` (in `app/cache.py`) and the serving
`_model_cache` (in `app/ml/serving.py`) in sync across processes, the
worker publishes a message on the `model:invalidate` Redis channel
after training success/failure, and the API subscribes on startup.

Why Redis pub/sub and not the in-process `cache` module?
- The `cache` module is per-process (in-memory dict). The worker
  invalidating its own copy is useless — the API has its own copy.
- Redis pub/sub is a one-way fire-and-forget channel that all
  subscribed processes receive, which is exactly what we need for
  cache invalidation.
- The channel is also used by `routers/models.py` if the HTTP path
  ever needs to nudge other processes (currently it doesn't — the
  HTTP path runs in the API process, so its in-process invalidation
  is sufficient).

This module deliberately stays tiny: a publisher helper for the
worker and a long-lived subscriber that runs as an asyncio task in
the API process lifespan.
"""
import asyncio
import json
import logging
from typing import Optional

import redis.asyncio as redis_async

from app.config import get_settings

logger = logging.getLogger("monitor.cache_pubsub")
settings = get_settings()

# Redis channel name. Keep it short — every publish/subscribe goes
# through Redis with this as the key prefix.
CHANNEL_MODEL_INVALIDATE = "model:invalidate"

_redis: Optional[redis_async.Redis] = None
_subscriber_task: Optional[asyncio.Task] = None


def _get_redis() -> Optional[redis_async.Redis]:
    """Lazy singleton Redis client.

    Returns None when USE_FAKEREDIS is set so dev environments without
    a real Redis don't blow up on import. (Dev uses fakeredis for
    RQ but pub/sub across processes is a no-op in that mode — the
    worker and API are the same process, so in-process invalidation
    already covers it.)
    """
    global _redis
    if settings.USE_FAKEREDIS:
        return None
    if _redis is None:
        _redis = redis_async.from_url(settings.REDIS_URL)
    return _redis


async def publish_model_invalidate(
    model_version_id: Optional[str] = None,
    reason: str = "unknown",
) -> None:
    """Tell every subscribed API process to drop its model cache.

    `model_version_id` is the freshly-trained (or freshly-activated)
    model. Subscribers drop:
    - `_CACHE_KEY_MODEL_ID` and `_CACHE_KEY_MODEL_LABELS` in
      `app/cache.py` (60s TTL — invalidation short-circuits the wait)
    - The serving module's `_model_cache` entry for this id (or all
      entries if id is None)

    Best-effort. The persistent record is already in PostgreSQL —
    the broadcast is a hint, not a source of truth. If Redis is
    briefly unreachable, the cache will eventually self-correct on
    the next 60s TTL expiry, so a missed publish is acceptable.
    """
    r = _get_redis()
    if r is None:
        logger.debug("cache_pubsub: no redis client, dropping model invalidate")
        return
    try:
        msg = json.dumps({
            "model_version_id": model_version_id,
            "reason": reason,
        })
        await r.publish(CHANNEL_MODEL_INVALIDATE, msg)
    except Exception as e:
        logger.warning(
            f"cache_pubsub.publish_model_invalidate(id={model_version_id}, "
            f"reason={reason}) dropped: {e}"
        )


async def _on_model_invalidate(message: dict) -> None:
    """Subscriber callback: drop the local caches for the given model.

    Imported lazily to avoid a circular import between
    `app.cache_pubsub` and `app.cache` / `app.ml.serving`.
    """
    from app.cache import cache_invalidate
    from app.ml.serving import invalidate_model_cache

    model_version_id = message.get("model_version_id")
    reason = message.get("reason", "unknown")
    logger.info(
        f"cache_pubsub: invalidating model cache "
        f"(id={model_version_id}, reason={reason})"
    )
    # app.cache.py keys
    cache_invalidate("reports:active_model_id")
    cache_invalidate("reports:active_model_labels")
    cache_invalidate("reports:summary")
    # app.ml.serving.py — drop a single model by id, or all if id is None
    invalidate_model_cache(model_version_id)


async def _subscriber_loop() -> None:
    """Long-lived subscriber to the model invalidate channel.

    Exits only when the process is shutting down (cancelled by the
    lifespan context manager). Reconnects on transient Redis errors
    with a short backoff.
    """
    backoff = 1.0
    while True:
        r = _get_redis()
        if r is None:
            # Dev mode — no real Redis. Sleep so we don't busy-loop,
            # but the channel is irrelevant in-process anyway.
            await asyncio.sleep(60)
            continue
        try:
            pubsub = r.pubsub()
            await pubsub.subscribe(CHANNEL_MODEL_INVALIDATE)
            logger.info(
                f"cache_pubsub: subscribed to '{CHANNEL_MODEL_INVALIDATE}'"
            )
            backoff = 1.0  # reset after successful subscribe
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                raw = msg.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    payload = json.loads(raw)
                except Exception:
                    logger.warning(
                        f"cache_pubsub: malformed payload on "
                        f"{CHANNEL_MODEL_INVALIDATE}: {raw!r}"
                    )
                    continue
                await _on_model_invalidate(payload)
        except asyncio.CancelledError:
            logger.info("cache_pubsub: subscriber cancelled, exiting")
            return
        except Exception as e:
            logger.warning(
                f"cache_pubsub: subscriber error, reconnecting in "
                f"{backoff}s: {e}"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def start_subscriber() -> None:
    """Spawn the subscriber as a background task in the current loop.

    Called from `app/main.py` lifespan on startup. No-op in dev
    (USE_FAKEREDIS) because the API and worker are the same process
    and in-process invalidation already works.
    """
    global _subscriber_task
    if _subscriber_task is not None:
        return  # already started
    if settings.USE_FAKEREDIS:
        logger.debug("cache_pubsub: dev mode, skipping subscriber")
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop yet (startup edge case). Caller will retry.
        logger.warning("cache_pubsub: no running loop, will retry on first publish")
        return
    _subscriber_task = loop.create_task(_subscriber_loop())


def stop_subscriber() -> None:
    """Cancel the subscriber task. Called from lifespan shutdown."""
    global _subscriber_task
    if _subscriber_task is None:
        return
    _subscriber_task.cancel()
    _subscriber_task = None
