"""Env for CI/local test runs. Must run before `app.*` is imported —
app.config.Settings reads these at import time and has no defaults for
SECRET_KEY / MQTT_USERNAME / MQTT_PASSWORD."""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-32b")
os.environ.setdefault("MQTT_USERNAME", "test")
os.environ.setdefault("MQTT_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./ci_test.db")
os.environ.setdefault("DATABASE_SYNC_URL", "sqlite:///./ci_test.db")
os.environ.setdefault("USE_FAKEREDIS", "true")
os.environ.setdefault("REDIS_URL", "fake://localhost:6379/0")
