import uuid
import json
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, literal_column, and_, String, cast, Date
from sqlalchemy.exc import IntegrityError
from app.timezone import WITA
from app.database import get_db
from app.models.app_log import AppLog
from app.models.location_log import LocationLog
from app.models.student import Student
from app.models.user import User
from app.models.mata_kuliah import MataKuliah
from app.models.schedule import Schedule
from app.models.campus import Campus
from app.schemas.log import BatchLogRequest, SyncStatusResponse, AppLogItem, LocationLogItem
from app.routers.auth import get_current_user, get_current_user_id
from app.config import get_settings
from app.limiter import limiter

settings = get_settings()
router = APIRouter(prefix="/logs", tags=["Logs"])


# ─── Geofence attendance helper ──────────────────────────────────────────────
# Used during ingest to compute "did the student actually attend class on
# campus?" The math is plain haversine against the campus's
# lat/lon/radius_meters. We keep this small and pure so it's easy to unit
# test if we ever add pytest.

GEOFENCE_INSIDE = "inside"
GEOFENCE_OUTSIDE = "outside"
GEOFENCE_UNKNOWN = "unknown"  # missing location fix, no schedule match, etc.


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in meters."""
    r = 6_371_000  # Earth radius
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geofence_check(lat: float | None, lon: float | None, campus: Campus | None) -> str:
    """Return GEOFENCE_INSIDE / OUTSIDE / UNKNOWN based on the fix and campus.

    A missing fix OR a missing campus → UNKNOWN. The dashboard surfaces
    UNKNOWN rows separately so admins can see "we don't have location data
    for this student during this class" rather than mistaking it for a
    false negative (outside).
    """
    if lat is None or lon is None or campus is None:
        return GEOFENCE_UNKNOWN
    distance = _haversine_meters(lat, lon, campus.latitude, campus.longitude)
    return GEOFENCE_INSIDE if distance <= campus.radius_meters else GEOFENCE_OUTSIDE


async def _broadcast_new_logs(
    student_id: str,
    log_dates: list,
    loc_keys: list[str],
) -> None:
    """Background task: relay saved/updated log rows to BOTH audiences.

    - Redis `dashboard:logs` (web SSE) — the dashboard `/logs` page subscribes
      here and renders full per-log rows in the live table. Without this
      publish, the page sits at "0 log · 0 buffered" forever even though
      `/api/logs/batch` is committing rows. The Round-17 web SSE client
      expects `{type: "app_log", student_id, log_date, total_screen_time_minutes, ...}`
      per log — see `website/src/app/(dashboard)/logs/page.tsx`.
    - MQTT `dashboard/broadcast` (legacy) — Android `MqttPushClient` listens
      here to surface a generic "log received" notification. Payload is just
      a count, since Android only displays "Live · log received".

    Both writes are best-effort: a failed publish doesn't break the API
    response. The records are already in PostgreSQL.

    Important: this task runs AFTER the request handler returns, so the
    request-scoped `AsyncSession` (from `Depends(get_db)`) is closed by the
    time we get here. We open a fresh session to read the saved rows.

    Round 20: query rows by `(student_id, log_date)` instead of
    `idempotency_key` because the UPSERT path keeps the same row id across
    re-syncs — only the per-day aggregate is what changed. The `log_dates`
    list is the canonical set of dates the batch touched.
    """
    logger = logging.getLogger("monitor.broadcast")
    logger.info(
        "[_broadcast_new_logs] START student=%s log_dates=%d loc_keys=%d",
        student_id, len(log_dates), len(loc_keys),
    )

    # 1) Per-log events to Redis for the web SSE. Open a fresh session —
    #    the request-scoped one is closed by the time BackgroundTasks fires.
    from app.database import async_session
    from sqlalchemy import func as sql_func

    redis_client = _make_redis()
    published_app = 0
    published_loc = 0
    if redis_client is not None:
        try:
            async with async_session() as session:
                if log_dates:
                    # AwareDateTime decorator normalizes to UTC. Querying by
                    # `func.date(log_date)` is portable across SQLite +
                    # PostgreSQL and matches the partial UNIQUE INDEX.
                    # Filter by `source = 'mobile'` so the broadcast only
                    # delivers Android-app events; seed rows (which can
                    # share a (student, date) with a mobile row) are
                    # excluded to avoid duplicate SSE events.
                    app_res = await session.execute(
                        select(AppLog).where(
                            AppLog.student_id == student_id,
                            AppLog.source == "mobile",
                            # Cast to TEXT so PostgreSQL can compare DATE expression with
                            # string parameters in the IN clause without type mismatch.
                            # `func.date()` returns DATE; comparing DATE = VARCHAR fails with
                            # "operator does not exist: date = character varying".
                            cast(sql_func.date(AppLog.log_date), String).in_(
                                [d.isoformat() for d in log_dates]
                            ),
                        )
                    )
                    for row in app_res.scalars():
                        await redis_client.publish(
                            "dashboard:logs",
                            json.dumps(
                                {
                                    "type": "app_log",
                                    "student_id": row.student_id,
                                    "log_date": row.log_date.isoformat() if row.log_date else None,
                                    "total_screen_time_minutes": row.total_screen_time_minutes,
                                    "social_media_time_minutes": row.social_media_time_minutes,
                                    "game_time_minutes": row.game_time_minutes,
                                    "unlock_count": row.unlock_count,
                                    "used_during_class": row.used_during_class,
                                },
                                default=str,
                            ),
                        )
                        published_app += 1
                if loc_keys:
                    loc_res = await session.execute(
                        select(LocationLog).where(LocationLog.idempotency_key.in_(loc_keys))
                    )
                    for row in loc_res.scalars():
                        await redis_client.publish(
                            "dashboard:logs",
                            json.dumps(
                                {
                                    "type": "location_log",
                                    "student_id": row.student_id,
                                    "recorded_at": row.timestamp.isoformat() if row.timestamp else None,
                                    "is_on_campus": row.is_on_campus,
                                    "matches_schedule": row.matches_schedule,
                                    "latitude": row.latitude,
                                    "longitude": row.longitude,
                                },
                                default=str,
                            ),
                        )
                        published_loc += 1
            logger.info(
                "[_broadcast_new_logs] Redis publish OK student=%s app=%d loc=%d",
                student_id, published_app, published_loc,
            )
        except Exception:
            logger.exception(
                "[logs/batch] Redis dashboard:logs broadcast failed for student=%s",
                student_id,
            )
        finally:
            try:
                await redis_client.aclose()
            except Exception:
                pass
    else:
        logger.warning(
            "[_broadcast_new_logs] redis_client is None — publish skipped"
        )

    # 2) Generic count event to MQTT for Android.
    try:
        from app.mqtt_bridge import broadcast_dashboard

        await broadcast_dashboard(
            "log_received",
            {
                "student_id": student_id,
                "app_count": len(log_dates),
                "loc_count": len(loc_keys),
            },
        )
    except Exception:
        logger.exception(
            "[logs/batch] MQTT log_received broadcast failed for student=%s", student_id
        )


def _make_redis():
    if settings.USE_FAKEREDIS:
        import fakeredis.aioredis
        return fakeredis.aioredis.FakeRedis()
    import redis.asyncio as redis_async
    return redis_async.from_url(settings.REDIS_URL)


async def get_student_id(request: Request, db: AsyncSession = Depends(get_db)) -> str:
    """Get student_id from the authenticated user's token.

    Auto-creates a Student row on first batch upload when the user has
    role=mahasiswa but no admin-linked Student profile yet. Without this,
    a freshly-registered mobile user got `404 Student not found` on every
    sync — the dashboard never saw their data, and the Android client
    showed perpetual "syncing" state. Idempotent via UPSERT-style check
    (re-fetch after insert to absorb the rare concurrent-upload race).
    """
    payload = await get_current_user(request)
    user_id = payload.get("sub")

    res = await db.execute(select(Student).where(Student.user_id == user_id))
    student = res.scalar_one_or_none()
    if student:
        return student.id

    # No linked Student — auto-provision one from the User row so the batch
    # upload can succeed. Defaults to an empty NIM-suffix and an
    # informational program string; admin can fill these in later via
    # PUT /api/students/{id}.
    user_res = await db.execute(select(User).where(User.id == user_id))
    user = user_res.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Generate a unique NIM. Username often collides as NIM (e.g.
    # "harymahasiswa"), but the column has a UNIQUE index so we must avoid
    # duplicate-key crashes. Suffix a UUID short tag if the bare username
    # is already taken.
    nim_candidate = (user.username or user.email.split("@")[0])[:20] or f"mhs-{user_id[:8]}"
    exists_res = await db.execute(select(Student.id).where(Student.nim == nim_candidate))
    if exists_res.scalar_one_or_none() is not None:
        nim_candidate = f"{nim_candidate[:14]}-{user_id[:4]}"

    student = Student(
        user_id=user.id,
        nim=nim_candidate,
        full_name=user.full_name or user.username or "Mahasiswa",
        program_studi=user.jurusan or "",
        angkatan=2024,
        semester=1,
        is_active=True,
    )
    db.add(student)
    try:
        await db.commit()
    except Exception:
        # Concurrent upload from the same user created the row between our
        # SELECT and INSERT — re-fetch and return.
        await db.rollback()
        res = await db.execute(select(Student).where(Student.user_id == user_id))
        student = res.scalar_one_or_none()
        if student is None:
            raise HTTPException(status_code=500, detail="Failed to provision Student")
        return student.id

    # Refresh to pick up the generated id (server-side default).
    await db.refresh(student)
    return student.id


@router.post("/batch")
@limiter.limit("30/minute")
async def submit_batch(
    request: Request,  # required by slowapi rate limiter
    data: BatchLogRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    student_id: str = Depends(get_student_id),
):
    # Round 20: dedup is now (student_id, log_date) for mobile-source rows,
    # enforced by the partial UNIQUE INDEX `uq_app_logs_student_date_mobile`.
    # The Android client sends ONE row per day (the day's running total);
    # if a sync accidentally re-sends or replays an old payload, the server
    # UPSERTs the row in place — no more "5 rows for 2026-10-06" entries
    # in the dashboard.
    #
    # Existing rows for the same (student_id, log_date) are UPDATED (not
    # appended) because the incoming value already IS the day total. The
    # Android client sends the cumulative minute/unlock counts since
    # midnight, not deltas, so REPLACE semantics are correct.
    app_keys: list[str] = []
    loc_keys: list[str] = []
    for log_item in data.logs:
        app_keys.append(f"{data.idempotency_key}:app:{log_item.log_date}:{log_item.schedule_id}")
    # Round 29 audit P2 (B2): LocationLog dedup key MUST be per-fix,
    # not per-batch. Previously keyed on `idempotency_key`, which is
    # batch-scoped — a partial-failed retry that gets a new
    # idempotency_key (e.g. server restart) would re-insert the same
    # (student, recorded_at) location. Keyed on (student, recorded_at,
    # ~coords) instead. The coords are rounded to 4 decimals (~11m
    # precision) so minor GPS jitter from the same fix doesn't
    # generate duplicate dedup keys.
    # BatchLogRequest carries no student_id — the caller's identity comes from
    # the auth dependency. Referencing data.student_id raised AttributeError on
    # every batch that contained a location row, so the whole request 500'd.
    for loc_item in data.locations:
        loc_keys.append(
            f"{student_id}:loc:{loc_item.recorded_at}:"
            f"{loc_item.latitude:.4f}:{loc_item.longitude:.4f}"
        )

    # Geofence pre-load: when an app_log is for class-time usage
    # (`used_during_class=true` AND `schedule_id` set), we need to know
    # (a) which campus the student SHOULD be at, and (b) where they
    # actually were during the class. Both lookups are batched so the
    # ingest loop stays O(N).
    schedule_ids = {
        log_item.schedule_id
        for log_item in data.logs
        if log_item.used_during_class and log_item.schedule_id
    }
    schedule_campus: dict[str, Campus] = {}
    if schedule_ids:
        rows = (
            await db.execute(
                select(Schedule.id, Campus).join(
                    MataKuliah, MataKuliah.id == Schedule.subject_id
                ).join(Campus, Campus.id == MataKuliah.campus_id).where(
                    Schedule.id.in_(schedule_ids)
                )
            )
        ).all()
        for sched_id, campus in rows:
            schedule_campus[sched_id] = campus

    # Most recent location per schedule — within this batch only. The
    # Android service emits a location fix on every 45-min probe, and
    # the batch is uploaded at 22:00 WITA, so the in-batch fix IS the
    # most recent one for that day. We index by schedule_id to support
    # multiple schedules in one day.
    #
    # Wave 2 #8: parse recorded_at as datetime before comparing. The schema
    # accepts both naive ("2026-10-01T08:30:00") and aware
    # ("2026-10-01T08:30:00+08:00") ISO strings; lex-compare treats a naive
    # string as a prefix of its aware counterpart, so naive always sorts
    # before aware → wrong "most recent". Normalize naive as WITA (matches
    # Android default) so both sides are tz-aware before compare.
    def _to_aware_iso(s: str) -> datetime:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=WITA) if dt.tzinfo is None else dt

    location_by_schedule: dict[str, tuple[float, float]] = {}
    if schedule_ids and data.locations:
        for loc_item in data.locations:
            if loc_item.schedule_id and loc_item.schedule_id in schedule_ids:
                key = loc_item.schedule_id
                existing = location_by_schedule.get(key)
                new_aware = _to_aware_iso(loc_item.recorded_at)
                if existing is None or new_aware > _to_aware_iso(existing[2]):
                    location_by_schedule[key] = (
                        loc_item.latitude,
                        loc_item.longitude,
                        loc_item.recorded_at,
                    )

    # Round 26 audit: build the list of unique log_dates from the batch.
    # Without this, `if log_dates:` at line ~365 NameErrored on every Android
    # sync — the function defined `app_keys`/`loc_keys` but never built
    # the corresponding date list. Deduped via set so the `IN (...)` clause
    # stays compact even if the client re-sent the same day.
    log_dates: list[datetime.date] = sorted({
        datetime.strptime(item.log_date, "%Y-%m-%d").date()
        for item in data.logs
        if item.log_date
    })

    # Pre-load existing mobile rows for the (student_id, log_date) pairs in
    # this batch — enables one-shot UPDATE for the "same day, new totals"
    # case. Round 20 fix: previously the server treated each row as a fresh
    # INSERT, which accumulated rows every 45 min the service polled.
    #
    # We match by `func.date(log_date) IN (...)` rather than `log_date IN
    # (datetime.combine(date, midnight, tz=UTC))` because legacy rows may
    # have been stored at any time-of-day (WITA midnight, UTC midnight, or
    # the moment the service polled). The `func.date()` cast normalizes
    # the comparison to the date-only key, matching the Android
    # `log_date` format ("YYYY-MM-DD").
    existing_by_key: dict[tuple[datetime.date, str | None], AppLog] = {}
    if log_dates:
        from sqlalchemy import func as sql_func
        existing_res = await db.execute(
            select(AppLog).where(
                AppLog.student_id == student_id,
                AppLog.source == "mobile",
                cast(sql_func.date(AppLog.log_date), String).in_(
                    [d.isoformat() for d in log_dates]
                ),
            )
        )
        for row in existing_res.scalars():
            if row.log_date is not None:
                existing_by_key[(row.log_date.date(), row.schedule_id)] = row

    processed = 0
    synced_client_ids: list[str] = []
    for log_item in data.logs:
        log_date = datetime.strptime(log_item.log_date, "%Y-%m-%d")
        log_date_day = log_date.date()

        # Geofence verdict for this row. Only meaningful for class-time logs.
        campus_id_for_log: str | None = None
        geofence_status: str | None = None
        if log_item.used_during_class and log_item.schedule_id:
            campus = schedule_campus.get(log_item.schedule_id)
            loc = location_by_schedule.get(log_item.schedule_id)
            if campus is not None:
                campus_id_for_log = campus.id
                geofence_status = geofence_check(
                    loc[0] if loc else None,
                    loc[1] if loc else None,
                    campus,
                )

        existing_row = existing_by_key.get((log_date_day, log_item.schedule_id))
        if existing_row is not None:
            # UPSERT in place: replace the day's accumulated values with the
            # new day total from this batch. The incoming value IS the day
            # total (Android's `collectAppUsage` accumulates into a single
            # local row), so REPLACE is correct — no summing.
            existing_row.total_screen_time_minutes = log_item.total_screen_time_minutes
            existing_row.social_media_time_minutes = log_item.social_media_time_minutes
            existing_row.game_time_minutes = log_item.game_time_minutes
            existing_row.other_app_time_minutes = log_item.other_app_time_minutes
            existing_row.unlock_count = log_item.unlock_count
            # used_during_class is OR-collapsed: a class-time interval sets
            # the day flag, a non-class-time interval must NOT clear it
            # (a single non-class collection doesn't mean "not in class
            # today" — only a class-time collection flips it ON).
            existing_row.used_during_class = (
                existing_row.used_during_class or log_item.used_during_class
            )
            existing_row.schedule_id = log_item.schedule_id or existing_row.schedule_id
            existing_row.device_id = data.device_id or existing_row.device_id
            # campus_id and geofence_status are only updated if the current
            # batch has a class-time verdict. A non-class-time batch must
            # NOT null out a previously-recorded class-time geofence
            # verdict. Falls back to existing values when this batch has
            # nothing to say.
            if campus_id_for_log is not None:
                existing_row.campus_id = campus_id_for_log
                existing_row.geofence_status = geofence_status
            # idempotency_key is rotated on every batch — the latest
            # batch's key is the canonical "this row was last touched by"
            # marker. Useful for debugging and for any future query that
            # wants the most recent batch that wrote this row.
            existing_row.idempotency_key = (
                f"{data.idempotency_key}:app:{log_item.log_date}:{log_item.schedule_id}"
            )
            existing_row.is_synced = True
            # Echo back the Android local row id so the client can mark it
            # SYNCED. The id we return is the server-side row's id (stable
            # across re-syncs), which the client tracks by `(student, date)`
            # anyway.
            if log_item.client_id:
                synced_client_ids.append(log_item.client_id)
        else:
            # First batch for this (student, date). Insert a new row.
            # Race condition: two concurrent batches from the same user
            # could both reach this branch (the pre-load didn't see an
            # existing row). The UNIQUE INDEX
            # `uq_app_logs_student_date_mobile` will block the second
            # INSERT with IntegrityError. Return no ACK and retry the whole
            # batch so a rollback never acknowledges discarded rows.
            new_log = AppLog(
                id=str(uuid.uuid4()),
                student_id=student_id,
                log_date=log_date,
                total_screen_time_minutes=log_item.total_screen_time_minutes,
                social_media_time_minutes=log_item.social_media_time_minutes,
                game_time_minutes=log_item.game_time_minutes,
                other_app_time_minutes=log_item.other_app_time_minutes,
                unlock_count=log_item.unlock_count,
                used_during_class=log_item.used_during_class,
                schedule_id=log_item.schedule_id,
                device_id=data.device_id,
                idempotency_key=f"{data.idempotency_key}:app:{log_item.log_date}:{log_item.schedule_id}",
                is_synced=True,
                source="mobile",  # Mark as from mobile app
                campus_id=campus_id_for_log,
                geofence_status=geofence_status,
            )
            try:
                db.add(new_log)
                await db.flush()
            except IntegrityError as exc:
                # shortcut: retry the whole batch on contention; use DB upsert if it becomes frequent.
                await db.rollback()
                raise HTTPException(
                    status_code=409, detail="Konflik sinkronisasi; kirim ulang seluruh batch",
                ) from exc
            existing_by_key[(log_date_day, log_item.schedule_id)] = new_log
            if log_item.client_id:
                synced_client_ids.append(log_item.client_id)
        processed += 1

    # Process location logs (skip duplicates by composite idempotency key).
    # Locations still use the per-batch idempotency_key dedup because each
    # location fix is a discrete event (not a daily aggregate).
    existing_loc: set[str] = set()
    if loc_keys:
        res = await db.execute(
            select(LocationLog.idempotency_key).where(LocationLog.idempotency_key.in_(loc_keys))
        )
        existing_loc = {row[0] for row in res.all()}

    seen_loc = set(existing_loc)
    for loc_item in data.locations:
        # Round 30 audit (P2): was per-batch keyed on data.idempotency_key —
        # a server restart with a new batch UUID would re-insert the same
        # (student, recorded_at) location. The comment block above (line
        # 304-307) already documented per-fix as the intent; the actual
        # code was missed. Use (student, recorded_at, ~coords) so the
        # dedup is stable across batch boundaries.
        loc_key = (
            f"{student_id}:loc:{loc_item.recorded_at}:"
            f"{loc_item.latitude:.4f}:{loc_item.longitude:.4f}"
        )
        if loc_key in seen_loc:
            if loc_item.client_id:
                synced_client_ids.append(loc_item.client_id)
            continue
        recorded_dt = datetime.fromisoformat(loc_item.recorded_at)
        loc = LocationLog(
            id=str(uuid.uuid4()),
            student_id=student_id,
            timestamp=recorded_dt,
            latitude=loc_item.latitude,
            longitude=loc_item.longitude,
            campus_id=loc_item.campus_id,
            is_on_campus=loc_item.is_on_campus,
            matches_schedule=loc_item.matches_schedule,
            schedule_id=loc_item.schedule_id,
            idempotency_key=loc_key,
            synced=True,
        )
        db.add(loc)
        seen_loc.add(loc_key)
        if loc_item.client_id:
            synced_client_ids.append(loc_item.client_id)

    await db.commit()

    # Best-effort broadcast after the durable PostgreSQL write. Runs in the
    # background so the API response doesn't block on Redis/MQTT. The records
    # are already persisted — the push is just a notification, so a dropped
    # publish has no data-integrity consequence.
    if processed > 0:
        background_tasks.add_task(
            _broadcast_new_logs,
            student_id=student_id,
            log_dates=list(log_dates),
            loc_keys=[k for k in loc_keys if k not in existing_loc],
        )
        # Round 28: server-side ML serving. After persisting the new
        # app_log rows, run the trained model against each (student,
        # date) pair so the cluster_results table stays in sync with
        # actual usage. This is what makes /api/reports/cluster-trends
        # and /api/ai/context reflect real-time assignments instead
        # of waiting for the next 23:00 WITA retrain.
        # If no trained model exists yet, the call returns a "no
        # active model" summary — silently ignored.
        if log_dates:
            from app.ml.serving import score_recently_synced
            background_tasks.add_task(
                score_recently_synced,
                student_id=student_id,
                log_dates=list(log_dates),
            )

    return SyncStatusResponse(
        idempotency_key=data.idempotency_key,
        status="processed",
        processed_count=processed,
        synced_client_ids=synced_client_ids,
    )


@router.get("/sync-status")
async def check_sync_status(
    idempotency_key: str,
    db: AsyncSession = Depends(get_db),
    student_id: str = Depends(get_student_id),
):
    result = await db.execute(select(AppLog).where(AppLog.idempotency_key == idempotency_key))
    record = result.scalar_one_or_none()
    if record:
        return SyncStatusResponse(
            idempotency_key=idempotency_key,
            status="processed",
            processed_count=1,
        )
    return SyncStatusResponse(
        idempotency_key=idempotency_key,
        status="pending",
        processed_count=0,
    )


@router.get("/history")
async def get_log_history(
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500, description="Max rows to return (capped at 500)"),
):
    """
    Return recent app_logs + location_logs.
    Admin/dosen: all logs from all students.
    Mahasiswa: only their own logs.
    Includes student full_name and nim for display.
    Used by the web /logs page on mount to populate historical data instead of
    starting empty (SSE only delivers new events, not buffered history).
    """
    from sqlalchemy import select, literal_column, and_

    role = payload.get("role", "")
    is_staff = role in ("admin", "dosen")

    if is_staff:
        # Admin/dosen: fetch mobile-source logs only (exclude seed data).
        # One row per (student_id, DATE(log_date)) via window function dedup.
        student_subq = select(Student.id, Student.full_name, Student.nim).subquery()
        dedup_subq = (
            select(
                AppLog.id,
                func.row_number()
                .over(
                    partition_by=[AppLog.student_id, func.date(AppLog.log_date)],
                    order_by=[AppLog.log_date.desc(), AppLog.id.desc()],
                )
                .label("rn"),
            )
            .where(AppLog.source == "mobile")
            .subquery()
        )
        app_res = await db.execute(
            select(
                AppLog,
                literal_column("'app_log'").label("log_type"),
                student_subq.c.full_name,
                student_subq.c.nim,
            )
            .outerjoin(student_subq, AppLog.student_id == student_subq.c.id)
            .join(
                dedup_subq,
                and_(
                    dedup_subq.c.id == AppLog.id,
                    dedup_subq.c.rn == 1,
                ),
            )
            .where(AppLog.source == "mobile")
            .order_by(AppLog.log_date.desc())
            .limit(limit)
        )
        app_rows = app_res.all()

        loc_res = await db.execute(
            select(
                LocationLog,
                literal_column("'location_log'").label("log_type"),
                student_subq.c.full_name,
                student_subq.c.nim,
            )
            .outerjoin(student_subq, LocationLog.student_id == student_subq.c.id)
            .order_by(LocationLog.timestamp.desc())
            .limit(limit)
        )
        loc_rows = loc_res.all()
    else:
        # Mahasiswa: only their own logs
        user_id = payload.get("sub", "")
        student_res = await db.execute(
            select(Student.id, Student.full_name, Student.nim).where(Student.user_id == user_id)
        )
        student_rows = student_res.all()
        student_ids = [r[0] for r in student_rows]
        student_info = {r[0]: (r[1], r[2]) for r in student_rows}

        if not student_ids:
            return []

        app_res = await db.execute(
            select(AppLog, literal_column("'app_log'").label("log_type"))
            .where(AppLog.student_id.in_(student_ids))
            .where(AppLog.source == "mobile")
            .order_by(AppLog.log_date.desc())
            .limit(limit)
        )
        app_rows = [(row, lt, *student_info.get(row.student_id, (None, None))) for row, lt in app_res.all()]

        loc_res = await db.execute(
            select(LocationLog, literal_column("'location_log'").label("log_type"))
            .where(LocationLog.student_id.in_(student_ids))
            .order_by(LocationLog.timestamp.desc())
            .limit(limit)
        )
        loc_rows = [(row, lt, *student_info.get(row.student_id, (None, None))) for row, lt in loc_res.all()]

    events = []
    for row, _log_type, student_name, nim in app_rows:
        events.append({
            "type": "app_log",
            "student_id": row.student_id,
            "student_name": student_name or "",
            "nim": nim or "",
            "log_date": row.log_date.isoformat() if row.log_date else None,
            "total_screen_time_minutes": row.total_screen_time_minutes,
            "social_media_time_minutes": row.social_media_time_minutes,
            "game_time_minutes": row.game_time_minutes,
            "unlock_count": row.unlock_count,
            "used_during_class": row.used_during_class,
            # Geofence attendance verdict (see run_geofence_columns_migration).
            # Only set when `used_during_class` is true and a campus was resolved.
            # UI renders the badge based on this string.
            "campus_id": row.campus_id,
            "geofence_status": row.geofence_status,
            # Track provenance: "mobile" = from Android, "seed" = from seed script.
            # Filtering by source='mobile' above means all rows are mobile here,
            # but the field is included so the frontend can label the source.
            "source": row.source,
        })
    for row, _log_type, student_name, nim in loc_rows:
        events.append({
            "type": "location_log",
            "student_id": row.student_id,
            "student_name": student_name or "",
            "nim": nim or "",
            "recorded_at": row.timestamp.isoformat() if row.timestamp else None,
            "is_on_campus": row.is_on_campus,
            "matches_schedule": row.matches_schedule,
            "latitude": row.latitude,
            "longitude": row.longitude,
        })

    # Sort by date descending and trim to limit
    def sort_key(e):
        d = e.get("log_date") or e.get("recorded_at") or ""
        return d

    events.sort(key=sort_key, reverse=True)
    return events[:limit]


@router.get("/geofence-summary")
async def geofence_summary(
    request: Request,
    days: int = Query(default=7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """Aggregate geofence attendance for the dashboard's metric card.

    Counts AppLog rows in the last `days` where `used_during_class=true`
    AND `geofence_status` is one of "inside" / "outside" / "unknown". The
    rates are computed against the inside + outside total (excluding
    `unknown`, which means we have no location data and shouldn't penalize
    the student for the dashboard view).

    Returns:
      { inside: int, outside: int, unknown: int, total: int,
        inside_rate: float, days: int }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_date = cutoff.date()  # date for AppLog.log_date comparison (Date column)

    rows = (
        await db.execute(
            select(AppLog.geofence_status, func.count(AppLog.id)).where(
                AppLog.used_during_class.is_(True),
                # Compare as naive date to avoid AwareDateTime.process_bind_param
                # receiving a datetime.date (no .tzinfo) instead of datetime.
                cast(AppLog.log_date, Date) >= cutoff_date,
            ).group_by(AppLog.geofence_status)
        )
    ).all()

    counts = {"inside": 0, "outside": 0, "unknown": 0}
    for status, n in rows:
        if status in counts:
            counts[status] = n
        elif status is None:
            # Legacy rows from before the migration have NULL — bucket
            # them as `unknown` so the dashboard doesn't lose them.
            counts["unknown"] += n

    decided = counts["inside"] + counts["outside"]
    inside_rate = (counts["inside"] / decided) if decided else 0.0
    return {
        "inside": counts["inside"],
        "outside": counts["outside"],
        "unknown": counts["unknown"],
        "total": counts["inside"] + counts["outside"] + counts["unknown"],
        "inside_rate": round(inside_rate, 3),
        "days": days,
    }


# ─── WebSocket live stream ────────────────────────────────────────────────────
#
# Auth: reads the HttpOnly access_token cookie or Authorization header.
#
# Message format: JSON LogStreamEvent per frame.

from fastapi import WebSocket, WebSocketDisconnect  # noqa: E402


@router.get("/stream/ws")
async def logs_stream_ws_info():
    """Info endpoint for the WebSocket path.

    Round 31 (18 Okt 2026) follow-up: cloudflared's HTTP/1.1 transport
    does an origin preflight GET to verify the origin is alive BEFORE
    forwarding the WebSocket upgrade. FastAPI's @router.websocket()
    route only accepts WebSocket upgrades — a GET returns 404. cloudflared
    interprets 4xx as "origin rejected" and returns 502 to the browser,
    even though the WebSocket upgrade would have succeeded.

    Firefox uses HTTP/1.1 for WebSocket upgrades (per WHATWG Fetch),
    so this only affects real browsers — `curl --http2` reaches FastAPI
    directly and returns 426 "WebSocket only" (the correct response).
    Adding a 2xx GET handler satisfies the preflight so cloudflared
    forwards the real upgrade.

    The actual WebSocket authentication (Origin check, HttpOnly cookie
    verification, JWT decode) is unchanged — that all still happens
    in logs_stream_ws() when the browser opens the actual socket.
    This handler does NO auth, by design: it's a no-op for the
    preflight, and unauthenticated GET probes are fine to answer with
    the same info.
    """
    return {
        "endpoint": "stream/ws",
        "protocol": "websocket",
        "note": "Use WebSocket upgrade. Browser will replace this GET with a real WS connection.",
    }


@router.websocket("/stream/ws")
async def logs_stream_ws(websocket: WebSocket):
    """WebSocket real-time log updates via Redis pubsub dashboard:logs."""
    _log = logging.getLogger("monitor")

    # Round 29 audit P2 #4: check Origin header. CORS middleware
    # applies to HTTP requests but NOT to WebSocket upgrade requests,
    # so an attacker page on a different origin could open this
    # socket and exfiltrate the live log stream. Reject any Origin
    # that doesn't match the expected deployment URLs.
    #
    # The `Origin` header is null for native WebSocket clients
    # (Python websockets, wscat, etc.) and for same-origin browser
    # requests in some configurations — both are allowed.
    ALLOWED_ORIGINS = {
        "https://monitorterop.my.id",
        "http://localhost:3000",  # local dev web
        "http://127.0.0.1:3000",
    }
    origin = websocket.headers.get("origin")
    _log.debug(
        "[WS] origin=%s user_agent=%s has_cookie=%s has_auth_header=%s",
        origin,
        websocket.headers.get("user-agent"),
        bool(websocket.cookies.get("access_token")),
        bool(websocket.headers.get("authorization")),
    )
    if origin is not None and origin not in ALLOWED_ORIGINS:
        _log.warning(
            "[WS] rejected WebSocket upgrade from disallowed origin=%s",
            origin,
        )
        # 403 is the closest non-1008 code for "policy violation
        # before accept" — browsers surface this as a connection
        # failure. The websocket is closed without `accept()` so
        # no handshake completes.
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    raw_token = None

    # Auth sources (tried in order of preference):
    # 1. access_token HttpOnly cookie — browser auto-sends on WS upgrade.
    # 2. Authorization header (for non-browser clients like the AI monitor).
    #
    # Note: `?token=` query param fallback was REMOVED in Round 29. JWT in
    # URLs leaked to Cloudflare Tunnel access logs, nginx access logs, and
    # browser history. The browser now sends the HttpOnly `access_token`
    # cookie automatically on the WS upgrade, which the server reads
    # here. Non-browser clients should use the Authorization header.
    raw_token = websocket.cookies.get("access_token")
    if not raw_token:
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            raw_token = auth[7:]

    _log.info(
        "[WS] token_sources: cookies=%s auth_header=%s token_len=%s",
        list(websocket.cookies.keys()),
        "authorization" in websocket.headers,
        len(raw_token) if raw_token else 0,
    )

    if not raw_token:
        _log.warning("[WS] No token found — closing 1008")
        await websocket.close(code=1008, reason="Not authenticated")
        return

    try:
        from app.routers.auth import authenticate_access_token
        payload = await authenticate_access_token(raw_token)
        if payload.get("role") not in {"admin", "dosen"}:
            raise HTTPException(status_code=403, detail="Staff access required")
    except Exception as e:
        _log.warning("[WS] Invalid token: %s", e)
        await websocket.close(code=1008, reason="Invalid token")
        return

    await websocket.accept()
    _log.info("[WS] accepted")
    try:
        await websocket.send_json({"event": "connected", "message": "WebSocket stream active"})
        _log.info("[WS] connected message sent")
    except Exception as e:
        _log.warning("[WS] send_json failed: %s", e)
        await websocket.close(code=1011)
        return

    redis_client = None
    pubsub = None
    try:
        try:
            redis_client = _make_redis()
            pubsub = redis_client.pubsub()
            await pubsub.subscribe("dashboard:logs")
            _log.info("[WS] subscribed to dashboard:logs")
        except Exception:
            logging.getLogger("monitor").exception("[WS] Redis connect/subscribe failed")
            try:
                await websocket.send_json({"event": "error", "message": "Redis unavailable"})
            except Exception:
                pass
            await websocket.close(code=1011)
            return

        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        payload = await authenticate_access_token(raw_token)
                        if payload.get("role") not in {"admin", "dosen"}:
                            raise HTTPException(status_code=403, detail="Staff access required")
                    except HTTPException:
                        await websocket.close(code=1008, reason="Session no longer authorized")
                        return
                    await websocket.send_text(message["data"])
        except WebSocketDisconnect:
            _log.info("[WS] client disconnected")
        except Exception as e:
            _log.warning("[WS] pubsub loop error: %s", e)
    finally:
        _log.info("[WS] cleaning up")
        if pubsub is not None:
            try:
                await pubsub.unsubscribe("dashboard:logs")
            except Exception:
                pass
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
