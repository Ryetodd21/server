"""
Status endpoint — reports server, Redis, MQTT broker, dan user session info.
Digunakan oleh dashboard web untuk menampilkan kondisi service real-time.
"""
from datetime import datetime, timedelta, timezone
import logging
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.database import get_db
from app.models.user import User
from app.routers.auth import get_current_user, get_token_from_request
from app.config import get_settings

# One shared pool for the broker probe. A per-request executor leaked a thread
# (and its shutdown) on every dashboard status poll.
_mqtt_probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mqtt-probe")

router = APIRouter(prefix="/status", tags=["Status"])

settings = get_settings()


@router.get("")
async def get_system_status(
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """
    Get overall system status.
    Returns service health, active users, broker info.
    """
    # Resolve the caller BEFORE probing anything. The landing page and
    # useVersion() hit this endpoint anonymously on every visit just to read
    # `server.version`, and they only ever receive `public_status`. Running the
    # DB counts, Redis ping and MQTT TCP probe first (then discarding them) cost
    # anon callers a full round of I/O for a response they never see.
    current_user = None
    is_staff = False
    if request is not None:
        raw_token = get_token_from_request(request)
        if raw_token:
            try:
                payload = await get_current_user(request)
                is_staff = payload.get("role") in ("admin", "dosen")
                user_id = payload.get("sub")
                if user_id:
                    result = await db.execute(select(User).where(User.id == user_id))
                    user = result.scalar_one_or_none()
                    if user:
                        current_user = {
                            "id": user.id,
                            "username": user.username,
                            "email": user.email,
                            "full_name": user.full_name or "",
                            "role": user.role,
                            "is_active": user.is_active,
                        }
            except Exception:
                pass

    server_status = "running"
    server_time = datetime.now(timezone.utc).isoformat()
    server_version = settings.APP_VERSION
    public_status = {
        "server": {
            "status": server_status,
            "version": server_version,
            "time": server_time,
        },
    }
    if not is_staff:
        return public_status

    # Database status — NEVER echo raw DB errors to clients. The /api/status
    # endpoint is unauthenticated, so a DB error message would be a public
    # reconnaissance oracle. Log internally; return only the health state.
    db_status = "unknown"
    db_response_ms = None
    total_users = 0
    try:
        import time
        start = time.time()
        # This single COUNT also serves as the DB health probe — it used to be
        # issued twice, back to back, for the same value.
        result = await db.execute(select(func.count()).select_from(User))
        total_users = result.scalar() or 0
        db_response_ms = round((time.time() - start) * 1000, 2)
        db_status = "healthy"
    except Exception as e:
        logging.getLogger("monitor").exception("[status] DB probe failed")
        db_status = "unhealthy"

    # Active users: count users with last_login within the last 24 hours
    # (access tokens are stateless JWTs and are NOT stored in the Token table)
    active_users_count = 0
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        active_result = await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.is_active == True)
            .where(User.last_login != None)
            .where(User.last_login >= cutoff)
        )
        active_users_count = active_result.scalar() or 0
    except Exception:
        pass

    # Redis status
    redis_status = "unknown"
    try:
        from app.tasks.worker import job_queue
        if job_queue and hasattr(job_queue, "connection"):
            conn = job_queue.connection
            if conn:
                redis_status = "connected"
        else:
            # Try direct redis ping
            try:
                import redis
                r = redis.Redis(host=settings.REDIS_HOST,
                               port=settings.REDIS_PORT,
                               socket_connect_timeout=2)
                try:
                    if r.ping():
                        redis_status = "connected"
                    else:
                        redis_status = "disconnected"
                finally:
                    # Round 26 audit (P2): close the connection explicitly so
                    # the underlying socket doesn't leak. Without this, every
                    # /api/status call (polled every 30s by the dashboard)
                    # would open a new connection that stays open until GC.
                    try:
                        r.close()
                    except Exception:
                        pass
            except Exception:
                redis_status = "unavailable"
    except Exception:
        redis_status = "unavailable"

    # MQTT broker status — TCP probe (subscriber runs in a separate container;
    # in-process globals from mqtt_subscriber are not reachable across the
    # Docker boundary, so we probe the broker directly. The broker IS shared
    # because both api and subscriber connect to it.)
    mqtt_status = "unknown"
    mqtt_broker = settings.MQTT_BROKER_URL
    mqtt_connected = False
    try:
        def _mqtt_probe():
            # Plain TCP connect on the broker port, matching what this check
            # claims to do. The previous paho handshake called
            # socket.setdefaulttimeout(2), which mutated the timeout of every
            # socket in the process for the rest of its life, and built a fresh
            # ThreadPoolExecutor per request.
            import socket
            host = mqtt_broker.replace("tcp://", "").split(":")[0]
            port = int(mqtt_broker.split(":")[-1]) if ":" in mqtt_broker else 1883
            with socket.create_connection((host, port), timeout=2):
                return True

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_mqtt_probe_pool, _mqtt_probe)
        mqtt_status = "connected"
        mqtt_connected = True
    except Exception:
        mqtt_status = "unavailable"

    return {
        **public_status,
        "database": {
            "status": db_status,
            "response_ms": db_response_ms,
        },
        "redis": {
            "status": redis_status,
        },
        "mqtt_broker": {
            "status": mqtt_status,
            "broker_url": mqtt_broker,
            "connected": mqtt_connected,
        },
        "users": {
            "total": total_users,
            "active": active_users_count,
        },
        "current_user": current_user,
    }


@router.get("/active-users")
async def get_active_users(
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """List recent active users (logged in within last 24h)."""
    if request is not None:
        raw_token = get_token_from_request(request)
        if not raw_token:
            return {"users": [], "count": 0}
        try:
            payload = await get_current_user(request)
            if payload.get("role") not in ("admin", "dosen"):
                return {"users": [], "count": 0, "error": "unauthorized"}
        except Exception:
            return {"users": [], "count": 0, "error": "invalid_token"}
    else:
        return {"users": [], "count": 0}

    # Ambil user list yang last_login dalam 24 jam terakhir
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    result = await db.execute(
        select(User, User.last_login)
        .where(User.is_active == True)
        .where(User.last_login != None)
        .where(User.last_login >= cutoff)
        .order_by(User.last_login.desc() if hasattr(User, "last_login") else User.created_at.desc())
        .limit(50)
    )
    rows = result.all()

    users = []
    for row in rows:
        user = row[0]
        users.append({
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name or "",
            "role": user.role,
            "is_active": user.is_active,
            "last_login": row[1].isoformat() if row[1] else None,
        })

    return {"users": users, "count": len(users)}
