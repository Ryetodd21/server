"""Regression gate for the app wiring: startup, health, and the JWT
token-type contract enforced in routers/auth.py."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers.auth import create_access_token, create_refresh_token, decode_token


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "healthy"}


def test_access_token_roundtrip():
    token = create_access_token({"sub": "u1", "role": "dosen"})
    assert decode_token(token)["sub"] == "u1"


def test_refresh_token_rejected_as_access():
    # A 7-day refresh token must not be usable as a 30-minute session.
    with pytest.raises(HTTPException) as exc:
        decode_token(create_refresh_token({"sub": "u1"}))
    assert exc.value.status_code == 401


def test_refresh_token_accepted_as_refresh():
    assert decode_token(create_refresh_token({"sub": "u1"}), "refresh")["type"] == "refresh"


def test_garbage_token_rejected():
    with pytest.raises(HTTPException) as exc:
        decode_token("not-a-jwt")
    assert exc.value.status_code == 401


def test_protected_endpoint_requires_auth(client):
    assert client.get("/api/users/me").status_code == 401
