"""
Redis-to-MQTT relay client (lightweight).

The MQTT broker lives behind the docker-compose `mqtt` service, and the only
process that holds a direct connection to it is the `subscriber` container.
Anything else that wants to publish an event (the api container, rq worker,
scheduler) speaks Redis instead — this module publishes to the
`mqtt:relay` channel, and `mqtt_subscriber.start_subscriber()` consumes
that channel and forwards the message onto the broker.

Design constraint: keep this small. The retry loop is best-effort — if
Redis is briefly unreachable, the message is dropped and logged. There is
no durability because every site that calls this already persisted its
own row in PostgreSQL first; the broadcast is a push notification, not
a record of fact.

Used at:
    - `routers/logs.py:batch` after `await db.commit()` for AppLog insert
    - `routers/logs.py:batch` after `await db.commit()` for LocationLog insert
    - `tasks/worker.py` after training completes (model version flipped active)
    - `routers/users.py` admin user mutations (broadcast `user_event`)
"""
import json
import logging
from typing import Any

import redis.asyncio as redis_async

from app.config import get_settings

logger = logging.getLogger("mqtt_bridge")
settings = get_settings()

_redis: redis_async.Redis | None = None


def _get_redis() -> redis_async.Redis | None:
    """Lazy singleton. Returns None when USE_FAKEREDIS is set so dev
    environments without a real Redis don't blow up on import."""
    global _redis
    if settings.USE_FAKEREDIS:
        import fakeredis.aioredis
        if _redis is None:
            _redis = fakeredis.aioredis.FakeRedis()
        return _redis
    if _redis is None:
        _redis = redis_async.from_url(settings.REDIS_URL)
    return _redis


async def publish(topic: str, payload: dict[str, Any], qos: int = 0) -> None:
    """Forward a message to the MQTT broker via Redis.

    `topic` is the MQTT topic (e.g. `dashboard/broadcast`, `students/abc/events`).
    The subscriber picks up the JSON envelope `{topic, payload, qos}` from
    Redis and publishes it on the broker.
    """
    r = _get_redis()
    if r is None:
        logger.debug(f"mqtt_bridge: no redis client, dropping topic={topic}")
        return
    try:
        envelope = json.dumps({"topic": topic, "payload": payload, "qos": qos})
        await r.publish("mqtt:relay", envelope)
    except Exception as e:
        # Best-effort: the persistent record is already in PostgreSQL. The
        # notification is fire-and-forget; do not break the calling request.
        logger.warning(f"mqtt_bridge.publish({topic}) dropped: {e}")


async def broadcast_dashboard(event: str, data: dict[str, Any]) -> None:
    """Convenience: publish a `dashboard/broadcast` event. Android clients
    subscribe to this topic for real-time UI updates (new log received,
    training completed, etc)."""
    await publish(
        "dashboard/broadcast",
        {"event": event, **data},
    )
