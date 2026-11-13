#!/usr/bin/env python3
"""Quick smoke test of running server."""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"

def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]

# 1. health
print("GET /health:", call("GET", "/health"))

# 2. login admin
code, body = call("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
admin_token = body["access_token"]
print("POST /api/auth/login (admin):", code, "token len:", len(admin_token))

# 3. login student
code, body = call("POST", "/api/auth/login", {"username": "411423005", "password": "mahasiswa123"})
stu_token = body["access_token"]
print("POST /api/auth/login (student):", code, "token len:", len(stu_token))

# 4. campuses
code, body = call("GET", "/api/campuses")
print(f"GET /api/campuses: {code}, {len(body)} records")

# 5. logs batch (1st)
code, body = call("POST", "/api/logs/batch", {
    "idempotency_key": "smoke-test-1",
    "device_id": "smoke-device",
    "logs": [{
        "log_date": "2026-09-17",
        "total_screen_time_minutes": 200,
        "social_media_time_minutes": 120,
        "game_time_minutes": 40,
        "other_app_time_minutes": 40,
        "unlock_count": 60,
        "used_during_class": True,
    }],
    "locations": [],
}, token=stu_token)
print("POST /api/logs/batch (1st):", code, body)

# 6. logs batch (duplicate - should be idempotent)
code, body = call("POST", "/api/logs/batch", {
    "idempotency_key": "smoke-test-1",
    "device_id": "smoke-device",
    "logs": [{
        "log_date": "2026-09-17",
        "total_screen_time_minutes": 200,
        "social_media_time_minutes": 120,
        "game_time_minutes": 40,
        "other_app_time_minutes": 40,
        "unlock_count": 60,
        "used_during_class": True,
    }],
    "locations": [],
}, token=stu_token)
print("POST /api/logs/batch (2nd):", code, body, "(should skip duplicate)")

# 7. model config
code, body = call("GET", "/api/models/config", token=admin_token)
print("GET /api/models/config:", code, body)

# 8. models history
code, body = call("GET", "/api/models/history", token=admin_token)
print("GET /api/models/history:", code, len(body), "records")

# 9. schedules directory
code, body = call("GET", "/api/schedules/directory", token=admin_token)
print("GET /api/schedules/directory:", code, len(body), "records")

print("\n✅ Smoke test complete")
