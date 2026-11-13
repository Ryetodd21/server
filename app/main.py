# Sistem Monitoring Distraksi — FastAPI Server
import logging
from contextlib import asynccontextmanager
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.limiter import limiter
from app.config import get_settings
from app.database import (
    init_db,
    run_migrations,
    run_type_migrations,
    run_string_widen_migrations,
    run_relax_migrations,
    run_index_migrations,
    run_unique_constraint_migrations,
    run_mata_kuliah_campus_migration,
    run_geofence_columns_migration,
    run_app_logs_daily_id_migration,
    run_app_logs_schedule_id_migration,
    run_autovacuum_tuning_migrations,
    run_fcm_token_migration,
    cleanup_orphan_prodi,
    run_student_user_unique_migration,
)
from app.routers import auth, campus, schedule, logs, models, reports, students, users, schedule_request, status, ai, seed, config, program_studi, fakultas, jurusan, mata_kuliah, jadwal_mata_kuliah, request_akademik, institution_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monitor")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up Sistem Monitoring Distraksi Server...")
    await init_db()
    await run_migrations()
    await run_type_migrations()
    await run_string_widen_migrations()
    await run_relax_migrations()
    await run_index_migrations()
    # Round 15: bring the academic structure up to spec — guarantees on
    # Jurusan.code uniqueness and cleanup of orphan ProgramStudi rows.
    await run_unique_constraint_migrations()
    await run_student_user_unique_migration()
    await cleanup_orphan_prodi()
    # Round 17-ish: bind mata_kuliah to campuses + UNIQUE on subjects.code.
    await run_mata_kuliah_campus_migration()
    # Round 17-ish: add geofence attendance columns to app_logs.
    await run_geofence_columns_migration()
    # Preserve legacy daily rows, then allow one row per class schedule per day.
    await run_app_logs_schedule_id_migration()
    # Round 24: per-table autovacuum scale factors. Without this, small
    # churn-heavy tables (login_devices, tokens) accumulate dead tuples
    # indefinitely because the default 0.2 scale factor rarely triggers
    # on a 14-row table.
    await run_autovacuum_tuning_migrations()
    # Round 27: users.fcm_token column for FCM device registration.
    await run_fcm_token_migration()
    # Round 27: FCM admin SDK init. No-op if FCM_SERVICE_ACCOUNT_PATH is
    # unset; logs a WARNING at startup so the operator sees the state.
    from app.services.fcm import init_fcm
    init_fcm()
    # Round 29 audit P0 #5: subscribe to the cross-process model cache
    # invalidation channel. The RQ worker publishes here after training
    # success/failure (worker process) so the API process (this one) can
    # drop its in-process `_model_cache` and `app.cache` keys. Without
    # this, the API kept serving the previous model after every nightly
    # retrain until the API container restarted.
    from app.cache_pubsub import start_subscriber
    start_subscriber()
    logger.info("Database tables created/verified")
    yield
    # Shutdown
    logger.info("Shutting down...")
    from app.cache_pubsub import stop_subscriber
    stop_subscriber()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Server Backend — PCA + K-Means Pipeline for Student Distraction Monitoring",
    lifespan=lifespan,
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
allowed_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Cache preflight responses for 1 hour so the browser doesn't re-issue
    # OPTIONS before every POST/PUT/DELETE during a session. Browsers honor
    # this for both same-origin and (with credentials) CORS preflights.
    max_age=3600,
)


@app.middleware("http")
async def reject_cross_site_mutations(request: Request, call_next):
    """Reject cookie-authenticated mutations from untrusted origins."""
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin not in allowed_origins:
        return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
    return await call_next(request)


# ─── MQTT-over-WebSocket proxy (Round 26) ─────────────────────────────────────
#
# Why this exists:
# cloudflared (Cloudflare Tunnel daemon) uses HTTP/2 transport between itself
# and the origin service. mosquitto's WebSocket listener (libwebsockets) only
# speaks HTTP/1.1 — receiving the HTTP/2 connection preface (PRI * HTTP/2.0)
# causes libwebsockets to immediately close the socket with "bad socket
# read/write: Unknown error", and the Cloudflare edge returns 502.
#
# This FastAPI endpoint accepts the HTTP/2 WebSocket upgrade from cloudflared
# (via uvicorn's standard HTTP/1.1 server, which handles the HTTP/2 → HTTP/1.1
# demuxing transparently) and forwards each message to mosquitto:9001 over
# a fresh HTTP/1.1 WebSocket client connection.
#
# Routing in cloudflared: `mqtt.monitorterop.my.id → http://localhost:8000/mqtt`
# (this FastAPI app listens on 8000).
#
# Round 26 (P1) — TLS at the Cloudflare edge terminates the WSS handshake
# before traffic enters the tunnel, so credentials + telemetry never travel
# in cleartext. The proxy is fully inside the trusted Docker network.
mqtt_backend_url = "ws://mqtt:9001/mqtt"  # docker network — plaintext


@app.websocket("/mqtt")
async def mqtt_websocket_proxy(ws: WebSocket):
    """Bidirectional WebSocket proxy: client (cloudflared) <-> mosquitto:9001.

    Forwards every frame both ways with no transformation. Bytes flow as-is.
    The backend URL uses the docker-network hostname `mqtt` so we don't
    have to expose mosquitto:9001 to the host.
    """
    await ws.accept(subprotocol="mqtt" if "mqtt" in ws.scope.get("subprotocols", []) else None)
    backend = None
    try:
        backend = await websockets.connect(
            mqtt_backend_url,
            subprotocols=["mqtt"],
            max_size=2 ** 20,  # 1 MB — same cap as mosquitto's max_packet_size
        )
    except Exception as e:
        logging.getLogger("wss_proxy").error("Failed to connect to mosquitto: %s", e)
        await ws.close(code=1011, reason=f"upstream connect failed: {e}")
        return

    async def client_to_backend() -> None:
        try:
            while True:
                msg = await ws.receive_bytes()
                if len(msg) > 65536:
                    await ws.close(code=1009, reason="Message too large")
                    return
                await backend.send(msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logging.getLogger("wss_proxy").debug("client_to_backend stopped: %s", e)

    async def backend_to_client() -> None:
        try:
            async for msg in backend:
                if not isinstance(msg, bytes):
                    await ws.close(code=1003, reason="MQTT requires binary frames")
                    return
                await ws.send_bytes(msg)
        except Exception as e:
            logging.getLogger("wss_proxy").debug("backend_to_client stopped: %s", e)

    tasks = [asyncio.create_task(client_to_backend()), asyncio.create_task(backend_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if backend is not None:
            try:
                await backend.close()
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass


# Include routers
app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(campus.router, prefix=settings.API_V1_PREFIX)
app.include_router(schedule.router, prefix=settings.API_V1_PREFIX)
app.include_router(logs.router, prefix=settings.API_V1_PREFIX)
app.include_router(models.router, prefix=settings.API_V1_PREFIX)
app.include_router(reports.router, prefix=settings.API_V1_PREFIX)
app.include_router(students.router, prefix=settings.API_V1_PREFIX)
app.include_router(schedule_request.router, prefix=settings.API_V1_PREFIX)
app.include_router(status.router, prefix=settings.API_V1_PREFIX)
app.include_router(users.router, prefix=settings.API_V1_PREFIX)
app.include_router(ai.router, prefix=settings.API_V1_PREFIX)
app.include_router(seed.router, prefix=settings.API_V1_PREFIX)
app.include_router(config.router, prefix=settings.API_V1_PREFIX)
app.include_router(program_studi.router, prefix=settings.API_V1_PREFIX)
app.include_router(fakultas.router, prefix=settings.API_V1_PREFIX)
app.include_router(jurusan.router, prefix=settings.API_V1_PREFIX)
app.include_router(mata_kuliah.router, prefix=settings.API_V1_PREFIX)
app.include_router(jadwal_mata_kuliah.router, prefix=settings.API_V1_PREFIX)
app.include_router(request_akademik.router, prefix=settings.API_V1_PREFIX)
app.include_router(institution_settings.router, prefix=settings.API_V1_PREFIX)


@app.get("/")
async def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}
