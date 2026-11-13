"""
MQTT Smartphone Stub — simulates student phones sending logs to the MQTT broker.
Jalankan: python -m app.mqtt_client_stub

Opsi:
  --students N   Jumlah student yang di-simulate (default: 10)
  --interval S  Interval kirim dalam detik (default: 5)
  --total N     Total messages per student (default: 10)

Contoh:
  python -m app.mqtt_client_stub --students 20 --interval 3
"""
import argparse
import hashlib
import hmac
import json
import random
import threading
import time

from app.config import get_settings
settings = get_settings()

_mqtt_broker = settings.MQTT_BROKER_URL
_broker_host = _mqtt_broker.replace("tcp://", "").split(":")[0]
_broker_port = int(_mqtt_broker.split(":")[-1]) if ":" in _mqtt_broker else 1883
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.student import Student
from app.models.user import Token

settings = get_settings()
engine_async = create_async_engine(settings.DATABASE_URL, echo=False)
async_session_maker = async_sessionmaker(engine_async, class_=AsyncSession, expire_on_commit=False)


def make_sync_engine():
    return create_engine(settings.DATABASE_SYNC_URL)


def get_student_ids() -> list[str]:
    """Ambil semua student_id dari DB."""
    sync_engine = make_sync_engine()
    with Session(sync_engine) as session:
        rows = session.execute(select(Student.id, Student.nim)).fetchall()
    return [(r.id, r.nim) for r in rows]


def publish_telemetry(client, student_id: str, topic: str, payload: dict):
    """Simulation requires an existing login session; never publish bearer tokens."""
    engine = make_sync_engine()
    try:
        with Session(engine) as session:
            token = session.execute(
                select(Token.token).join(Student, Student.user_id == Token.user_id)
                .where(Student.id == student_id, Token.token_type == "refresh",
                       Token.expires_at > datetime.now(timezone.utc))
                .order_by(Token.created_at.desc()).limit(1)
            ).scalar_one_or_none()
    finally:
        engine.dispose()
    if token is None:
        raise ValueError("MQTT simulation requires a student with an active login session")
    body, sent_at = json.dumps(payload), int(time.time())
    signed = f"monitorung-mqtt-v1\n{topic}\n{sent_at}\n{body}".encode("utf-8")
    signature = hmac.new(token.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    client.publish(topic, json.dumps({
        "payload": body, "sent_at": sent_at, "signature": signature,
    }).encode("utf-8"), qos=1)


def send_app_log(client, student_id: str, student_nim: str):
    now = datetime.now(timezone.utc)
    # Randomize time within last hour
    log_time = now - timedelta(minutes=random.randint(1, 60))

    screen = random.randint(30, 300)
    social = random.randint(0, 180)
    game = random.randint(0, 90)
    other = random.randint(0, 120)

    payload = {
        "log_date": log_time.isoformat(),
        "total_screen_time_minutes": screen,
        "social_media_time_minutes": social,
        "game_time_minutes": game,
        "other_app_time_minutes": other,
        "unlock_count": random.randint(5, 120),
        "used_during_class": random.random() < 0.3,
        "device_id": f"phone-{student_nim}",
        "schedule_id": None,
    }

    topic = f"students/{student_id}/logs"
    publish_telemetry(client, student_id, topic, payload)
    print(f"  📱 [{student_nim}] AppLog: {screen}m screen, {social}m sosmed, {game}m game")


def send_location_log(client, student_id: str, student_nim: str):
    now = datetime.now(timezone.utc)
    log_time = now - timedelta(minutes=random.randint(0, 30))

    campus_lat = 0.5387
    campus_lng = 123.0645

    is_on_campus = random.random() < 0.7

    if is_on_campus:
        lat = campus_lat + random.uniform(-0.003, 0.003)
        lng = campus_lng + random.uniform(-0.003, 0.003)
    else:
        lat = campus_lat + random.uniform(-0.05, 0.05)
        lng = campus_lng + random.uniform(-0.05, 0.05)

    payload = {
        "recorded_at": log_time.isoformat(),
        "latitude": round(lat, 6),
        "longitude": round(lng, 6),
        "campus_id": None,
        "is_on_campus": is_on_campus,
        "matches_schedule": random.random() < 0.6,
    }

    topic = f"students/{student_id}/location"
    publish_telemetry(client, student_id, topic, payload)
    campus_icon = "🏫" if is_on_campus else "🏠"
    print(f"  {campus_icon} [{student_nim}] Location: {'on campus' if is_on_campus else 'off campus'}")


async def simulate_student(client, student_id: str, student_nim: str, interval: int, total: int):
    for i in range(total):
        await send_app_log(client, student_id, student_nim)
        await asyncio.sleep(0.3)  # small delay between logs
        await send_location_log(client, student_id, student_nim)
        if i < total - 1:
            await asyncio.sleep(interval)


def run_simulation(students_count: int, interval: int, total: int):
    import paho.mqtt.client as mqtt
    import threading

    student_records = get_student_ids()
    if not student_records:
        print("❌ No students found in DB. Run seed.py first.")
        return

    if len(student_records) < students_count:
        print(f"⚠️  Only {len(student_records)} students in DB, using all of them")
        students_count = len(student_records)

    chosen = student_records[:students_count]
    print(f"\n🚀 Starting MQTT simulation:")
    print(f"   Students : {len(chosen)}")
    print(f"   Interval : {interval}s")
    print(f"   Messages : {total} per student")
    print(f"   Broker   : {_mqtt_broker}\n")

    connected_event = threading.Event()
    client_ref: list = [None]

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("   ✅ MQTT connection confirmed")
        else:
            print(f"   ❌ Connection failed, rc={rc}")
        connected_event.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    # Wave 4: password auth (matches mosquitto config; required when broker
    # is exposed publicly, e.g. production via Cloudflare Tunnel).
    client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client_ref[0] = client
    client.on_connect = on_connect
    client.connect(_broker_host, _broker_port, keepalive=60)
    client.loop_start()

    # Wait for connection
    connected_event.wait(timeout=5)
    print("✅ Connected to broker\n")

    def send_all_sync():
        for i in range(total):
            for sid, snim in chosen:
                send_app_log(client, sid, snim)
                time.sleep(0.2)
                send_location_log(client, sid, snim)
                time.sleep(0.2)
            if i < total - 1:
                time.sleep(interval)

    # Run in background thread
    t = threading.Thread(target=send_all_sync, daemon=True)
    t.start()
    t.join(timeout=60)
    print("\n✅ All messages sent!")
    client.loop_stop()
    client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="MQTT Smartphone Stub")
    parser.add_argument("--students", type=int, default=10, help="Number of students to simulate")
    parser.add_argument("--interval", type=int, default=5, help="Seconds between sends")
    parser.add_argument("--total", type=int, default=10, help="Total messages per student")
    args = parser.parse_args()
    run_simulation(args.students, args.interval, args.total)


if __name__ == "__main__":
    main()
