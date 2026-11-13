"""
MQTT Subscriber — listens to student logs from the broker,
writes to SQLite DB, and publishes real-time updates to Redis.

Jalankan: python -m app.mqtt_subscriber
(Jalankan di terminal terpisah dari broker dan uvicorn)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.config import get_settings
settings = get_settings()

_mqtt_broker = settings.MQTT_BROKER_URL
_broker_host = _mqtt_broker.replace("tcp://", "").split(":")[0]
_broker_port = int(_mqtt_broker.split(":")[-1]) if ":" in _mqtt_broker else 1883

# Round 28: lightweight throughput counter for the MQTT path. The subscriber
# is the hot ingest for mobile clients (no batching, 1 commit per message
# per the explicit design in Round 25 — too risky to add cross-message
# buffers inside a single-tenant subscriber). Counting every 100 messages
# gives operators a live "msgs/sec" number from the log stream without
# changing the per-message round-trip pattern.
_throughput_counter: int = 0
_throughput_started_ms: int = int(time.time() * 1000)
# Round 29 audit P2 #5: cap the counter's lifetime so msgs/sec stays
# meaningful. After 1 hour, snapshot → log → reset so the next
# msgs/sec calculation uses a 1-hour window. Without this, a long
# uptime (months) makes msgs/sec approach zero (e.g. 10M messages
# in 3,600,000 seconds = 2.78 msg/s — looks broken, actually fine).
_THROUGHPUT_RESET_MS = 60 * 60 * 1000  # 1 hour


def _parse_datetime(value: str | None) -> datetime:
    """Parse ISO-8601 timestamp and return a tz-aware datetime in UTC.

    MQTT payloads from the Android client may carry WITA (UTC+8) offsets like
    '2026-09-18T08:30:00+08:00', or be naive. DB columns are
    `TIMESTAMP WITH TIME ZONE`, so we coerce to UTC and keep tz-aware.

    Naive inputs are assumed UTC — this matches what the Android client emits
    for some fields (e.g. log_date without offset). If that assumption ever
    changes, prefer to add offset on the client side rather than guessing here.
    """
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# Heartbeat file for docker healthcheck — touched every 5s in the main loop.
# Healthcheck verifies the file exists and was modified in the last 60s.
_HEARTBEAT_FILE = Path("/tmp/mqtt_subscriber_heartbeat")

# Module-level handle so local tests / status probes can introspect.
# for tests that import this module in-process.
import paho.mqtt.client as _paho_mqtt  # noqa: E402
mqtt_client: _paho_mqtt.Client | None = None


def is_connected() -> bool:
    return mqtt_client is not None and mqtt_client.is_connected()

import redis.asyncio as redis_async

# Setup path so we can import from app.*
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.models.app_log import AppLog
from app.models.location_log import LocationLog
from app.models.student import Student
from app.models.user import User, Token

settings = get_settings()
logger = logging.getLogger("mqtt_subscriber")


# ─── Async DB session ─────────────────────────────────────────────────────────

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ─── Redis pub/sub ────────────────────────────────────────────────────────────

def make_redis():
    if settings.USE_FAKEREDIS:
        import fakeredis.aioredis
        return fakeredis.aioredis.FakeRedis()
    return redis_async.from_url(settings.REDIS_URL)

redis: redis_async.Redis | None = None


async def publish_to_redis(channel: str, data: dict):
    if redis:
        try:
            await redis.publish(channel, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"Redis publish failed: {e}")


async def publish_to_mqtt(topic: str, payload: dict, qos: int = 0, retain: bool = False):
    """Publish an event to the MQTT broker from inside the subscriber process.

    The subscriber already holds a paho client connected to the broker — we
    piggy-back on that. Paho's `publish()` is non-blocking (queues in the
    network thread), so this stays safe to call from asyncio code.

    `topic` should match a dashboard subscription pattern (e.g.
    `dashboard/broadcast`, `students/{id}/events`).
    """
    global mqtt_client
    if mqtt_client is None:
        logger.debug("publish_to_mqtt: no client yet, dropping event")
    try:
        info = mqtt_client.publish(
            topic,
            json.dumps(payload, default=str),
            qos=qos,
            retain=retain,
        )
        # info.wait_for_publish(timeout=1.0)  # would block; paho is non-blocking
        logger.debug(f"published mqtt: topic={topic} mid={info.mid}")
    except Exception as e:
        logger.warning(f"publish_to_mqtt failed (topic={topic}): {e}")


# ─── Message handlers ──────────────────────────────────────────────────────────

async def authenticate_mqtt_message(topic: str, envelope: dict) -> tuple[str, dict]:
    """Verify possession of a live user session without exposing its token to MQTT."""
    # shortcut: signatures protect ingest; per-user broker ACLs are needed for topic read isolation.
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "students" or parts[2] not in {"logs", "location"}:
        raise ValueError("Invalid telemetry topic")
    if not isinstance(envelope, dict):
        raise ValueError("Signed telemetry required")
    payload, signature, sent_at = (
        envelope.get("payload"), envelope.get("signature"), envelope.get("sent_at"),
    )
    if (not isinstance(payload, str) or len(payload.encode("utf-8")) > 65536
            or not isinstance(signature, str) or len(signature) != 64
            or any(c not in "0123456789abcdef" for c in signature)
            or type(sent_at) is not int or abs(time.time() - sent_at) > 300):
        raise ValueError("Invalid or expired telemetry envelope")
    signed = f"monitorung-mqtt-v1\n{topic}\n{sent_at}\n{payload}".encode("utf-8")
    async with async_session_maker() as session:
        rows = await session.execute(
            select(Student.id, Token.token)
            .join(User, User.id == Student.user_id)
            .join(Token, Token.user_id == User.id)
            .where(
                or_(Student.id == parts[1], User.id == parts[1]),
                Student.is_active == True, User.is_active == True,
                User.role == "mahasiswa", Token.token_type == "refresh",
                Token.expires_at > datetime.now(timezone.utc),
            )
        )
        for student_id, token in rows:
            expected = hmac.new(token.encode("utf-8"), signed, hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected):
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise ValueError("Telemetry payload must be an object")
                return student_id, data
    raise ValueError("Telemetry session not authorized")


async def handle_app_log(topic: str, payload: dict, student_id: str):
    """
    Write an AppLog record to the DB.
    topic: students/{id}/logs
    payload: {
        "log_date": "2026-09-18T07:00:00",
        "total_screen_time_minutes": 180,
        "social_media_time_minutes": 60,
        "game_time_minutes": 30,
        "other_app_time_minutes": 90,
        "unlock_count": 45,
        "used_during_class": false,
        "device_id": "phone-001",
        "schedule_id": null
    }
    """
    log_date_str = payload.get("log_date")
    schedule_id = payload.get("schedule_id")
    snapshot_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    idempotency_key = f"mqtt:{student_id}:app:{snapshot_hash}"
    from app.timezone import WITA
    # Match HTTP's business-date key, not the timestamp of each collection.
    stamp = datetime.fromisoformat(log_date_str) if log_date_str else datetime.now(WITA)
    day = stamp.astimezone(WITA).date() if stamp.tzinfo else stamp.date()
    log_date = datetime.combine(day, datetime.min.time())

    async with async_session_maker() as session:
        for attempt in range(2):
            log = (await session.execute(select(AppLog).where(
                AppLog.student_id == student_id,
                AppLog.source == "mobile",
                AppLog.log_date == log_date,
                AppLog.schedule_id == schedule_id,
            ))).scalar_one_or_none()
            if log is not None and log.idempotency_key == idempotency_key:
                return
            if log is None:
                log = AppLog(id=str(uuid.uuid4()), student_id=student_id,
                             log_date=log_date, source="mobile", schedule_id=schedule_id)
                session.add(log)
            for field in ("total_screen_time_minutes", "social_media_time_minutes",
                          "game_time_minutes", "other_app_time_minutes", "unlock_count"):
                setattr(log, field, int(payload.get(field, 0)))
            log.used_during_class = bool(payload.get("used_during_class", False))
            log.device_id = payload.get("device_id")
            log.idempotency_key = idempotency_key
            log.is_synced = False
            try:
                await session.commit()
                break
            except IntegrityError:
                await session.rollback()
                if attempt:
                    raise

        logger.info(f"AppLog saved: student={student_id}, screen_time={log.total_screen_time_minutes}m")

        # Publish to Redis for dashboard SSE
        await publish_to_redis("dashboard:logs", {
            "type": "app_log",
            "student_id": student_id,
            "log_date": log_date.isoformat(),
            "total_screen_time_minutes": log.total_screen_time_minutes,
            "social_media_time_minutes": log.social_media_time_minutes,
            "game_time_minutes": log.game_time_minutes,
            "unlock_count": log.unlock_count,
            "used_during_class": log.used_during_class,
        })


async def handle_location_log(topic: str, payload: dict, student_id: str):
    """
    Write a LocationLog record to the DB.
    topic: students/{id}/location
    payload: {
        "recorded_at": "2026-09-18T08:30:00",
        "latitude": 0.5391,
        "longitude": 123.0587,
        "campus_id": "...",
        "is_on_campus": true,
        "matches_schedule": true
    }
    """
    recorded_at_str = payload.get("recorded_at")
    idempotency_key = f"mqtt:{student_id}:loc:{recorded_at_str}"

    recorded_at = _parse_datetime(recorded_at_str)

    async with async_session_maker() as session:
        existing = await session.execute(
            select(LocationLog).where(LocationLog.idempotency_key == idempotency_key)
        )
        if existing.scalar_one_or_none():
            logger.debug(f"Duplicate LocationLog skipped: {idempotency_key}")
            return

        log = LocationLog(
            id=str(uuid.uuid4()),
            student_id=student_id,
            timestamp=recorded_at,
            latitude=float(payload.get("latitude", 0)),
            longitude=float(payload.get("longitude", 0)),
            campus_id=payload.get("campus_id"),
            is_on_campus=bool(payload.get("is_on_campus", False)),
            matches_schedule=bool(payload.get("matches_schedule", False)),
            idempotency_key=idempotency_key,
            synced=False,
        )
        session.add(log)
        await session.commit()

        logger.info(f"LocationLog saved: student={student_id}, on_campus={log.is_on_campus}")

        await publish_to_redis("dashboard:logs", {
            "type": "location_log",
            "student_id": student_id,
            "recorded_at": recorded_at.isoformat(),
            "is_on_campus": log.is_on_campus,
            "matches_schedule": log.matches_schedule,
        })


async def start_subscriber():
    global redis
    redis = make_redis()

    # Capture the running event loop so the paho network thread can dispatch
    # asyncio coroutines back to this loop via call_soon_threadsafe.
    _loop = asyncio.get_running_loop()

    subscribed_topics: list[tuple[str, int]] = [
        ("students/+/logs", 1),
        ("students/+/location", 1),
        ("dashboard/broadcast", 0),
    ]

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("✅ Connected to MQTT broker")
            for topic, qos in subscribed_topics:
                client.subscribe(topic, qos)
                logger.info(f"   Subscribed: {topic} (QoS {qos})")
        else:
            logger.error(f"Connection failed, rc={rc}")

    def on_message(client, userdata, msg: _paho_mqtt.MQTTMessage, properties=None):
        # paho's network thread doesn't have a running asyncio loop, so we
        # cannot use asyncio.create_task() here. Instead, schedule the
        # coroutine on the running loop via call_soon_threadsafe (the loop
        # is captured in the enclosing closure via _loop below).
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            _loop.call_soon_threadsafe(
                asyncio.ensure_future, on_message_async(msg.topic, payload)
            )
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from {msg.topic}: {e}")
        except Exception as e:
            logger.error(f"on_message dispatch failed: {e}", exc_info=True)

    async def on_message_async(topic: str, payload: dict):
        global _throughput_counter, _throughput_started_ms
        try:
            parts = topic.split("/")
            if len(parts) != 3 or parts[0] != "students" or parts[2] not in ("logs", "location"):
                return
            log_type = parts[2]
            student_id, payload = await authenticate_mqtt_message(topic, payload)
            if log_type == "logs":
                await handle_app_log(topic, payload, student_id)
            elif log_type == "location":
                await handle_location_log(topic, payload, student_id)
            else:
                logger.warning(f"Unknown log_type: {log_type}")
                return
            _throughput_counter += 1
            if _throughput_counter % 100 == 0:
                elapsed_ms = int(time.time() * 1000) - _throughput_started_ms
                msgs_per_sec = round(_throughput_counter * 1000 / max(elapsed_ms, 1), 2)
                logger.info(
                    f"[MQTT-Sub] throughput: {_throughput_counter} messages "
                    f"in {elapsed_ms // 1000}s ({msgs_per_sec} msg/s)"
                )
            # Round 29 audit P2 #5: reset every hour so msgs/sec stays
            # meaningful regardless of subscriber uptime. The next 100
            # message log line uses the new window. (Reset is also
            # triggered on restart since these are module-level vars.)
            if int(time.time() * 1000) - _throughput_started_ms > _THROUGHPUT_RESET_MS:
                logger.info(
                    f"[MQTT-Sub] throughput window reset "
                    f"({_throughput_counter} messages in last hour)"
                )
                _throughput_counter = 0
                _throughput_started_ms = int(time.time() * 1000)
        except ValueError:
            logger.warning("Rejected invalid or unauthorized MQTT telemetry")
        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)

    client = _paho_mqtt.Client(_paho_mqtt.CallbackAPIVersion.VERSION2,
                               protocol=_paho_mqtt.MQTTv311)
    # Wave 4: password auth required (mosquitto config forbids anonymous).
    # Username + password loaded from env (MQTT_USERNAME, MQTT_PASSWORD) and
    # must match the hash in server/mosquitto/passwords. Rotation: update env,
    # regenerate hash, redeploy subscriber, force-restart mqtt.
    client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    global mqtt_client
    mqtt_client = client

    # Relay bridge: anything published to Redis channel `mqtt:relay` is
    # forwarded to the MQTT broker. The API container doesn't hold a direct
    # MQTT connection (it lives in a separate compose service) — instead it
    # publishes to Redis, and this subscriber (which already has a broker
    # socket) relays the message. Cheap, fire-and-forget, no persistence.
    async def redis_relay_loop():
        if redis is None:
            return
        pubsub = redis.pubsub()
        await pubsub.subscribe("mqtt:relay")
        logger.info("   Subscribed: redis mqtt:relay (relay → broker)")
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                try:
                    msg = json.loads(raw["data"].decode("utf-8") if isinstance(raw["data"], bytes) else raw["data"])
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.warning(f"redis_relay: bad payload: {e}")
                    continue
                topic = msg.get("topic")
                payload = msg.get("payload")
                if not topic or payload is None:
                    continue
                await publish_to_mqtt(topic, payload, qos=int(msg.get("qos", 0)))
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe("mqtt:relay")
            await pubsub.close()

    relay_task = asyncio.create_task(redis_relay_loop())

    try:
        client.connect(_broker_host, _broker_port, keepalive=60)
        logger.info(f"Connecting to MQTT broker at {_broker_host}:{_broker_port}...")
        client.loop_start()
        _HEARTBEAT_FILE.touch()

        # Keep running
        while True:
            _HEARTBEAT_FILE.touch()
            await asyncio.sleep(5)

    except KeyboardInterrupt:
        logger.info("Shutting down subscriber...")
    finally:
        client.loop_stop()
        client.disconnect()
        mqtt_client = None
        if redis:
            await redis.aclose()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [MQTT-Sub] %(levelname)s: %(message)s",
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(signum, frame):
        logger.info("Shutting down subscriber...")
        for t in asyncio.all_tasks(loop):
            t.cancel()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(start_subscriber())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
