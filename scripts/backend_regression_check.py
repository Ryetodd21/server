"""Run: python -m scripts.backend_regression_check (no live services or .env).

Optional MONITORUNG_CHECK_DATABASE_URL must point to a disposable database named
monitorung_regression. The script creates fixtures, never starts the API lifespan.
"""
import asyncio
import hashlib
import hmac
import importlib
import inspect
import json
import os
import pkgutil
import time
from datetime import datetime, timedelta, timezone
from app.timezone import WITA
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import false, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.websockets import WebSocket

import app.config as config

database_url = os.environ.get("MONITORUNG_CHECK_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
assert database_url == "sqlite+aiosqlite:///:memory:" or make_url(database_url).database == "monitorung_regression"
settings = config.Settings(
    _env_file=None, DATABASE_URL=database_url, DATABASE_SYNC_URL="sqlite:///:memory:",
    SECRET_KEY="regression-only-not-a-real-key", MQTT_USERNAME="test", MQTT_PASSWORD="test",
    USE_FAKEREDIS=True, BCRYPT_ROUNDS=4,
)
config.get_settings = lambda: settings

import app.models

for module in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module(f"app.models.{module.name}")

from app.database import Base, engine
from app.models.app_log import AppLog
from app.models.location_log import LocationLog
from app.models.student import Student
from app.models.user import Token, User
from app.routers import auth, logs, users
from app.schemas.auth import PasswordChange, UserUpdate
from app.schemas.log import BatchLogRequest
from app import main, mqtt_subscriber

sessions = async_sessionmaker(engine, expire_on_commit=False)
request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})
submit_batch = inspect.unwrap(logs.submit_batch)


async def rejects(awaitable, code=None):
    try:
        await awaitable
    except (HTTPException, ValueError) as exc:
        if code is not None:
            assert isinstance(exc, HTTPException) and exc.status_code == code
    else:
        raise AssertionError("Unauthorized or invalid operation succeeded")


async def add_token(user_id, value):
    async with sessions() as db:
        db.add(Token(user_id=user_id, token=value, token_type="refresh",
                     expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        await db.commit()


async def check_refresh_request():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(auth.router)

    async def test_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[auth.get_db] = test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test.invalid") as client:
        with patch.object(auth.limiter, "enabled", False):
            for body in (None, {}):
                result = await client.post("/auth/refresh", json=body)
                assert result.status_code == 401, result.text
            token = auth.create_refresh_token({"sub": "user-one"})
            await add_token("user-one", token)
            for cookie in (True, False):
                client.cookies.clear()
                if cookie:
                    client.cookies.set(auth.REFRESH_COOKIE, token)
                result = await client.post("/auth/refresh", **({} if cookie else {"json": {"refresh_token": token}}))
                assert result.status_code == 200, result.text
                assert auth.decode_token(result.json()["access_token"])["sub"] == "user-one"
                assert "HttpOnly" in result.headers["set-cookie"]
                token = result.json()["refresh_token"]
            client.cookies.clear()
            result = await client.post("/auth/refresh", json={"refresh_token": "invalid"})
            assert result.status_code == 401, result.text
    async with sessions() as db:
        await db.delete((await db.execute(select(Token).where(Token.token == token))).scalar_one())
        await db.commit()
    print("PASS refresh cookie without body, mobile JSON body, missing/invalid sessions")


async def check_passwords_and_authorization():
    await add_token("user-one", "old-session")
    async with sessions() as db:
        change = inspect.unwrap(users.change_my_password)
        await rejects(change(request, PasswordChange(current_password="wrong", new_password="new-password"), db, "user-one"), 400)
        assert (await db.execute(select(Token))).scalars().all()
        await change(request, PasswordChange(current_password="old-password", new_password="new-password"), db, "user-one")
        assert not (await db.execute(select(Token))).scalars().all()
        user = await db.get(User, "user-one", populate_existing=True)
        assert auth.verify_password("new-password", user.hashed_password)
    await add_token("user-one", "admin-reset-session")
    async with sessions() as db:
        await users.update_user("user-one", UserUpdate(password="admin-password"), db, {"role": "admin"})
        assert not (await db.execute(select(Token))).scalars().all()
        user = await db.get(User, "user-one", populate_existing=True)
        assert auth.verify_password("admin-password", user.hashed_password)

    token = auth.create_access_token({"sub": "user-one", "role": "admin"})
    payload = await auth.authenticate_access_token(token)
    assert payload["role"] == "mahasiswa", "Stale admin claim was trusted"
    token_request = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
    await rejects(users.require_admin(token_request), 403)
    async with sessions() as db:
        await db.execute(update(User).where(User.id == "user-one").values(is_active=False))
        await db.commit()
    await rejects(auth.get_current_user(token_request), 401)
    async with sessions() as db:
        await db.execute(update(User).where(User.id == "user-one").values(is_active=True))
        await db.commit()
    await rejects(auth.authenticate_access_token(auth.create_access_token({"sub": "missing"})), 401)
    await rejects(auth.authenticate_access_token(auth.create_refresh_token({"sub": "user-one"})), 401)
    print("PASS password changes, session deletion, inactive accounts, current roles")


class HiddenWinnerSession(AsyncSession):
    """Simulate a concurrent insert committed after the preload query."""
    hide_once = True

    async def execute(self, statement, *args, **kwargs):
        if self.hide_once and "FROM app_logs" in str(statement):
            self.hide_once = False
            statement = statement.where(false())
        return await super().execute(statement, *args, **kwargs)


async def check_batch():
    async with sessions() as db:
        db.add(AppLog(id="winner", student_id="student-one", log_date=datetime(2026, 11, 13),
                      schedule_id="class-two", source="mobile", total_screen_time_minutes=1))
        await db.commit()
    batch = BatchLogRequest(device_id="test", idempotency_key="batch-conflict", logs=[
        dict(client_id="first", log_date="2026-11-12", schedule_id="class-one", total_screen_time_minutes=10),
        dict(client_id="second", log_date="2026-11-13", schedule_id="class-two", total_screen_time_minutes=20),
    ])
    async with HiddenWinnerSession(engine, expire_on_commit=False) as db:
        await rejects(submit_batch(request, batch, BackgroundTasks(), db, "student-one"), 409)
    async with sessions() as db:
        rows = (await db.execute(select(AppLog))).scalars().all()
        assert len(rows) == 1 and rows[0].total_screen_time_minutes == 1
        result = await submit_batch(request, batch, BackgroundTasks(), db, "student-one")
        assert result.synced_client_ids == ["first", "second"]
        rows = (await db.execute(select(AppLog).order_by(AppLog.log_date))).scalars().all()
        assert [r.total_screen_time_minutes for r in rows] == [10, 20]

        location = dict(client_id="loc-first", recorded_at="2026-11-13T01:00:00+00:00", latitude=0.5, longitude=123.0)
        locations = BatchLogRequest(device_id="test", idempotency_key="locations", logs=[],
                                    locations=[location, dict(location, client_id="loc-second")])
        for attempt in range(2):
            locations.idempotency_key = f"location-attempt-{attempt}"
            result = await submit_batch(request, locations, BackgroundTasks(), db, "student-one")
            assert result.synced_client_ids == ["loc-first", "loc-second"]
            assert len((await db.execute(select(LocationLog))).scalars().all()) == 1
    print("PASS batch conflict rollback/retry and duplicate location ACKs")


async def check_two_classes():
    batch = BatchLogRequest(device_id="test", idempotency_key="two-classes", logs=[
        dict(client_id="usage:1", log_date="2026-11-14", schedule_id="class-one", total_screen_time_minutes=10),
        dict(client_id="usage:2", log_date="2026-11-14", schedule_id="class-two", total_screen_time_minutes=20),
    ])
    async with sessions() as db:
        result = await submit_batch(request, batch, BackgroundTasks(), db, "student-one")
        assert result.synced_client_ids == ["usage:1", "usage:2"]
    with patch.object(mqtt_subscriber, "async_session_maker", sessions), \
         patch.object(mqtt_subscriber, "publish_to_redis"):
        for schedule, minutes, stamp in [
            ("class-one", 15, "2026-11-14"), ("class-two", 25, "2026-11-14"),
            ("class-two", 25, "2026-11-14"), ("class-one", 15, "2026-11-14T22:00:00"),
            ("class-two", 25, "2026-11-14T14:00:00+00:00"),
        ]:
            await mqtt_subscriber.handle_app_log("students/student-one/logs", {
                "log_date": stamp, "schedule_id": schedule,
                "total_screen_time_minutes": minutes, "used_during_class": True,
            }, "student-one")
    async with sessions() as db:
        rows = (await db.execute(select(AppLog).where(AppLog.log_date == datetime(2026, 11, 14)))).scalars().all()
        assert {r.schedule_id: r.total_screen_time_minutes for r in rows} == {"class-one": 15, "class-two": 25}
        assert len(rows) == 2, "Repeated MQTT snapshots must update, not append"
    print("PASS two classes remain separate across HTTP, MQTT, and replay")


def envelope(topic, payload, token, sent_at=None):
    sent_at = int(time.time()) if sent_at is None else sent_at
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    signed = f"monitorung-mqtt-v1\n{topic}\n{sent_at}\n{body}".encode()
    return {"payload": body, "sent_at": sent_at,
            "signature": hmac.new(token.encode(), signed, hashlib.sha256).hexdigest()}


async def check_mqtt_identity():
    await add_token("user-one", "test-session-secret")
    topic, payload = "students/user-one/logs", {"total_screen_time_minutes": 10}
    signed = envelope(topic, payload, "test-session-secret")
    with patch.object(mqtt_subscriber, "async_session_maker", sessions):
        result = await mqtt_subscriber.authenticate_mqtt_message(topic, signed)
        assert result == ("student-one", payload), "Account ID must resolve to Student.id"
        other_topic = "students/student-one/logs"
        assert (await mqtt_subscriber.authenticate_mqtt_message(other_topic, envelope(other_topic, payload, "test-session-secret")))[0] == "student-one"
        for bad_topic, bad_message in [
            (topic, payload), (topic, []), (topic, dict(signed, payload='{"total_screen_time_minutes":999}')),
            ("students/user-two/logs", signed), ("students/user-one/location", signed),
            (topic, envelope(topic, payload, "wrong-session")),
            (topic, envelope(topic, payload, "test-session-secret", int(time.time()) - 301)),
            (topic, envelope(topic, payload, "test-session-secret", int(time.time()) + 301)),
            (topic, envelope(topic, [], "test-session-secret")),
        ]:
            await rejects(mqtt_subscriber.authenticate_mqtt_message(bad_topic, bad_message))
        for values in ({"is_active": False}, {"is_active": True, "role": "dosen"}):
            async with sessions() as db:
                await db.execute(update(User).where(User.id == "user-one").values(**values))
                await db.commit()
            await rejects(mqtt_subscriber.authenticate_mqtt_message(topic, signed))
        async with sessions() as db:
            await db.execute(update(User).where(User.id == "user-one").values(role="mahasiswa"))
            await db.execute(update(Token).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await db.commit()
        await rejects(mqtt_subscriber.authenticate_mqtt_message(topic, signed))
    print("PASS MQTT identity, topic/payload binding, expiry, inactive accounts and roles")


async def check_proxy():
    for mode in ("client-close", "broker-close", "oversize"):
        incoming, replies = asyncio.Queue(), asyncio.Queue()
        packet, response = b"\x10\x00\x80\xff", b"\x20\x02\x00\xff"
        sent, received, closed = [], [], []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "bytes": b"x" * 65537 if mode == "oversize" else packet})

        class Backend:
            async def send(self, message):
                received.append(message)
                await replies.put(None if mode == "broker-close" else response)

            def __aiter__(self):
                return self

            async def __anext__(self):
                reply = await replies.get()
                if reply is None:
                    raise StopAsyncIteration
                return reply

            async def close(self):
                closed.append(True)

        async def connect(*args, **kwargs):
            return Backend()

        async def send(message):
            sent.append(message)
            if message["type"] == "websocket.send":
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        socket = WebSocket({"type": "websocket", "subprotocols": ["mqtt"]}, incoming.get, send)
        with patch.object(main.websockets, "connect", connect):
            await asyncio.wait_for(main.mqtt_websocket_proxy(socket), timeout=2)
        assert sent[0]["subprotocol"] == "mqtt" and closed
        if mode == "oversize":
            assert not received and any(m.get("code") == 1009 for m in sent)
        else:
            assert received == [packet]
            if mode == "client-close":
                assert any(m.get("bytes") == response for m in sent)
    print("PASS MQTT binary frames, subprotocol, frame limit and disconnect cleanup")


async def check_log_stream():
    token = auth.create_access_token({"sub": "user-one", "role": "admin"})
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    def socket():
        return WebSocket({"type": "websocket", "headers": [
            (b"authorization", f"Bearer {token}".encode()),
        ]}, receive, send)

    await logs.logs_stream_ws(socket())
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": "Invalid token"}]
    sent.clear()
    async with sessions() as db:
        await db.execute(update(User).where(User.id == "user-one").values(role="dosen"))
        await db.commit()

    class PubSub:
        async def subscribe(self, channel):
            pass

        async def unsubscribe(self, channel):
            pass

        async def listen(self):
            yield {"type": "message", "data": "allowed-event"}
            async with sessions() as db:
                await db.execute(update(User).where(User.id == "user-one").values(role="mahasiswa"))
                await db.commit()
            yield {"type": "message", "data": "must-not-leak"}

    class Redis:
        def pubsub(self):
            return PubSub()

        async def aclose(self):
            pass

    with patch.object(logs, "_make_redis", Redis):
        await logs.logs_stream_ws(socket())
    assert any(m.get("text") == "allowed-event" for m in sent)
    assert not any(m.get("text") == "must-not-leak" for m in sent)
    assert sent[-1]["type"] == "websocket.close" and sent[-1]["code"] == 1008
    print("PASS staff-only log stream and role revocation on existing connections")


async def check_public_registration_and_report_scores():
    from app.schemas.auth import UserCreate
    from app.routers import reports
    from app.models.model_version import ModelVersion
    from app.models.cluster_result import ClusterResult
    register = inspect.unwrap(users.register_user)
    async with sessions() as db:
        for role in ("mahasiswa", "dosen"):
            result = await register(request, UserCreate(username=f"pending-{role}",
                email=f"pending-{role}@example.invalid", password="TestPassword123!", role=role, is_active=True), db)
            assert result.role == role and result.is_active is False
            token = auth.create_access_token({"sub": result.id, "role": role})
            await rejects(auth.authenticate_access_token(token), 401)
            if role == "dosen":
                await users.update_user(result.id, UserUpdate(is_active=True), db, {"role": "admin"})
                assert (await auth.authenticate_access_token(token))["role"] == "dosen"
        await rejects(register(request, UserCreate(username="forbidden-admin",
            email="forbidden-admin@example.invalid", password="TestPassword123!", role="admin"), db), 422)
        today = datetime.now(WITA).date()
        db.add(ModelVersion(id="score-model", version_number=991, status="completed", is_active=True))
        await db.flush()
        for index, score in enumerate((0.1, 0.7, None)):
            db.add(ClusterResult(id=f"score-{index}", student_id="student-one", model_version_id="score-model",
                cluster_id=index, cluster_label=index, distraction_score=score,
                result_date=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)))
        await db.commit()
        labels = {"0": "Tinggi", "1": "Rendah", "2": "Kelompok khusus", "3": "Lainnya"}
        with patch.object(reports, "get_active_model_id", return_value="score-model"), \
             patch.object(reports, "_get_active_model_labels", return_value=labels):
            report = await reports.get_report(request, "daily", today, today, None, db, {})
            assert report["avg_distraction_score"] == 0.4
            filtered = await reports.get_report(request, "daily", today, today, 0, db, {})
            assert filtered["avg_distraction_score"] == 0.1 and filtered["cluster_filter_label"] == "Tinggi"
            empty = await reports.get_report(request, "daily", today, today, 3, db, {})
            assert empty["avg_distraction_score"] is None
            trends = await reports.get_cluster_trends(request, "daily", 1, db, {})
            row = trends["trends"][0]
            assert row["avg_distraction_score"] == 0.4
            assert row["cluster_counts"] == {"Tinggi": 1, "Rendah": 1, "Kelompok khusus": 1}
    print("PASS pending student/lecturer registration, admin rejection, approval, stored scores and dynamic labels")


async def check_scheduler():
    import app.schedules.scheduler as scheduler
    import app.ml.serving as serving
    calls, scores = [], []
    tick = 0

    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 11, 13, 15, tick, tzinfo=timezone.utc).astimezone(tz)

    def enqueue(task):
        calls.append((tick, task.__name__))
        if tick == 0:
            raise ConnectionError("simulated Redis outage")

    def score(target):
        scores.append(tick)
        return {"scored": 0, "skipped": 0, "errors": 0}

    def sleep(seconds):
        nonlocal tick
        tick += 1
        if tick == 3:
            raise StopIteration

    with patch.object(scheduler, "datetime", Clock), patch.object(scheduler.time, "sleep", sleep), \
         patch.object(scheduler.job_queue, "enqueue", enqueue), \
         patch.object(scheduler, "_HEARTBEAT_FILE", SimpleNamespace(touch=lambda: None)), \
         patch.object(serving, "score_all_students_for_date", score):
        try:
            scheduler.daily_scheduler()
        except StopIteration:
            pass
    assert calls == [(0, "cutoff_data_task"), (0, "train_model_task"), (1, "cutoff_data_task"), (1, "train_model_task")]
    assert scores == [1], "Failed enqueue must not trigger scoring"
    print("PASS scheduler retries failed enqueues without duplicating successful ones")


async def run_checks():
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        for suffix in ("one", "two"):
            db.add(User(id=f"user-{suffix}", username=f"test-{suffix}", email=f"{suffix}@example.invalid",
                        hashed_password=auth.get_password_hash("old-password"), role="mahasiswa", is_active=True))
        await db.flush()
        for suffix in ("one", "two"):
            db.add(Student(id=f"student-{suffix}", user_id=f"user-{suffix}", nim=f"test-{suffix}", full_name="Test User"))
        await db.commit()
    try:
        await check_refresh_request()
        await check_passwords_and_authorization()
        await check_batch()
        await check_two_classes()
        await check_mqtt_identity()
        await check_proxy()
        await check_log_stream()
        await check_scheduler()
        await check_public_registration_and_report_scores()
    finally:
        await engine.dispose()
        await mqtt_subscriber.engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_checks())
