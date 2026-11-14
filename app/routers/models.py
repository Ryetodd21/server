from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from datetime import date, datetime, timezone
import uuid
import json
import logging
import asyncio

from pydantic import ValidationError

from app.database import get_db, get_sync_engine
from app.models.model_version import ModelVersion
from app.models.cluster_result import ClusterResult
from app.models.student import Student
from app.models.user import User
from app.schemas.model import (
    ModelVersionResponse,
    LatestResultResponse,
    ModelConfigUpdate,
    TrainRequest,
    TrainResponse,
    ClusterPointResponse,
    PredictRequest,
    PredictResponse,
    BatchPredictRequest,
    BatchPredictResponse,
)
from app.routers.auth import get_current_user
from app.cache import cache_invalidate
from app.ml.serving import (
    get_active_model,
    load_model,
    invalidate_model_cache,
    score_student_for_date,
)

logger = logging.getLogger("monitor")

router = APIRouter(prefix="/models", tags=["Models"])

# Cache keys shared with routers/reports.py — both modules read the active
# model id + labels, so training must invalidate both to avoid stale reports.
_CACHE_KEY_MODEL_ID = "reports:active_model_id"
_CACHE_KEY_MODEL_LABELS = "reports:active_model_labels"

# In-process lock serializes POST /api/models/train. Two concurrent admin
# retrains used to race on ModelVersion.version_number (UNIQUE) — second
# request raised IntegrityError → 500. The lock also closes the window
# where no active model exists between deactivation and INSERT, since the
# new MV is created with is_active=True.
_train_lock = asyncio.Lock()


def _parse_json(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _to_response(mv: ModelVersion) -> ModelVersionResponse:
    return ModelVersionResponse(
        id=mv.id,
        version_number=mv.version_number,
        training_date=mv.training_date,
        dataset_size=mv.dataset_size,
        is_active=mv.is_active,
        status=mv.status,
        pca_n_components=mv.pca_n_components,
        kmeans_n_clusters=mv.kmeans_n_clusters,
        silhouette_score=mv.silhouette_score,
        inertia=mv.inertia,
        train_size=mv.train_size,
        test_size=mv.test_size,
        cluster_distribution=_parse_json(mv.cluster_distribution, {}),
        cluster_labels=_parse_json(mv.cluster_labels, {}),
        # Round 24: include the failure reason so the dashboard's
        # training-failed banner can show why the run was skipped.
        error_message=mv.error_message,
    )


async def require_admin(request: Request) -> dict:
    payload = await get_current_user(request)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return payload


async def require_admin_or_dosen(request: Request) -> dict:
    """Staff-only auth helper — admin / Dosen can read; for endpoints that
    surface aggregate / device / settings data to staff roles.

    Local helper (not shared) per the project's RBAC pattern — shared
    helpers across routers risk circular imports. See
    `routers/students.py` and `routers/reports.py` for the same pattern.
    """
    payload = await get_current_user(request)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(
            status_code=403,
            detail="Akses ditolak. Hanya untuk admin/dosen.",
        )
    return payload


@router.get("/history", response_model=list[ModelVersionResponse])
async def get_model_history(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
):
    result = await db.execute(
        select(ModelVersion).order_by(ModelVersion.version_number.desc())
    )
    versions = result.scalars().all()
    responses = [_to_response(v) for v in versions]
    if "academic_scope" in db.sync_session.info:
        from app.academic_scope import scoped_distribution
        for response in responses:
            response.cluster_distribution = await scoped_distribution(db, response.id)
    return responses


@router.get("/config")
async def get_model_config(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    result = await db.execute(
        select(ModelVersion).where(ModelVersion.is_active == True).limit(1)
    )
    active = result.scalar_one_or_none()
    if active:
        return {
            "pca_n_components": active.pca_n_components,
            "kmeans_n_clusters": active.kmeans_n_clusters,
        }
    return {"pca_n_components": 3, "kmeans_n_clusters": 4}


@router.put("/config")
async def update_model_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Store config — applied to next training."""
    raw_body = await request.body()
    try:
        parsed = json.loads(raw_body) if raw_body else {}
    except Exception as ex:
        logger.exception("[models/config PUT] JSON parse error")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        data = ModelConfigUpdate(**parsed)
    except ValidationError as ex:
        # Structured Pydantic errors — same shape as FastAPI's default
        # RequestValidationError envelope so clients can parse it.
        logger.error(f"[models/config PUT] Pydantic validation error: {ex.errors()}")
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "errors": ex.errors()},
        )
    except Exception as ex:
        logger.exception("[models/config PUT] Unexpected validation error")
        raise HTTPException(status_code=422, detail="Validation failed")

    result = await db.execute(
        select(ModelVersion).where(ModelVersion.is_active == True).limit(1)
    )
    active = result.scalar_one_or_none()
    if active:
        # Round 29 audit P1 #6: validate that cluster_labels keys are
        # 0..kmeans_n_clusters-1 before persisting. The serving module
        # falls back to "Cluster {id}" when an unknown cluster is
        # predicted — that hides the model/config mismatch and leaks
        # raw cluster ids into the dashboard. Catching the mismatch
        # at config-write time surfaces the error to the admin before
        # the bad config is used by the next /predict call.
        if data.cluster_labels is not None:
            if isinstance(data.cluster_labels, dict):
                try:
                    label_keys = {int(k) for k in data.cluster_labels.keys()}
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=422,
                        detail="cluster_labels keys must be integers 0..kmeans_n_clusters-1",
                    )
                expected = set(range(data.kmeans_n_clusters))
                if label_keys != expected:
                    missing = expected - label_keys
                    extra = label_keys - expected
                    detail_parts = []
                    if missing:
                        detail_parts.append(f"missing: {sorted(missing)}")
                    if extra:
                        detail_parts.append(f"unexpected: {sorted(extra)}")
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"cluster_labels must have exactly "
                            f"{data.kmeans_n_clusters} keys (0..{data.kmeans_n_clusters-1}); "
                            + ", ".join(detail_parts)
                        ),
                    )

        update_vals = {"pca_n_components": data.pca_n_components, "kmeans_n_clusters": data.kmeans_n_clusters}
        if data.cluster_labels is not None:
            update_vals["cluster_labels"] = json.dumps(data.cluster_labels)
        await db.execute(
            update(ModelVersion)
            .where(ModelVersion.id == active.id)
            .values(**update_vals)
        )
    await db.commit()

    # Round 29 audit P0 #4: invalidate the in-process caches so the next
    # /api/reports/* and /api/models/predict call picks up the new
    # cluster_labels immediately. The DB was updated, but the cache keys
    # `reports:active_model_id` (60s TTL) and `_model_cache[id].cluster_labels`
    # (no TTL) still held the old values. Without this invalidation, the
    # dashboard would keep showing the OLD labels for up to 60s + ∞.
    cache_invalidate(_CACHE_KEY_MODEL_ID)
    cache_invalidate(_CACHE_KEY_MODEL_LABELS)
    if active is not None:
        invalidate_model_cache(active.id)

    return {"message": "Config updated", "config": data.model_dump()}


@router.post("/train", response_model=TrainResponse)
async def trigger_training(
    data: TrainRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """
    Trigger training. Pipeline runs synchronously and returns when complete.

    Serialized via `_train_lock` so two concurrent retrains can't both pass
    the version-number read and race the INSERT. The lock also closes the
    gap where no active model exists: the new MV is created with
    is_active=True (deactivating the previous active model in the same
    transaction), so `/api/reports/*` and `/api/models/results/latest`
    always see exactly one active model.
    """
    # Cross-process serialization is done via the Redis-backed lock in
    # `app.tasks._train_lock_redis` — the `_train_lock = asyncio.Lock()`
    # below only serializes within THIS Python process (the API). The
    # RQ worker runs in a separate container, so a dashboard "Train
    # Ulang" click can race the 23:00 cron job. The Redis lock fixes
    # that. We keep both: the in-process lock as a fast-path, the
    # Redis lock as the cross-process guard.
    from app.tasks._train_lock_redis import acquire_train_lock, TrainLockBusy
    try:
        with acquire_train_lock() as got_lock:
            if not got_lock:
                # Another process is already training. Don't duplicate.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Training is already in progress in another process. "
                        "Wait for it to complete, then retry."
                    ),
                )
            async with _train_lock:
                return await _run_training(data, db)
    except TrainLockBusy as e:
        raise HTTPException(status_code=409, detail=str(e))


async def _run_training(data: TrainRequest, db: AsyncSession) -> TrainResponse:
    # Get next version number
    last_version_result = await db.execute(
        select(ModelVersion.version_number)
        .order_by(ModelVersion.version_number.desc())
        .limit(1)
    )
    last_version_num = last_version_result.scalar_one_or_none()
    next_version = (last_version_num + 1) if last_version_num else 1

    # Deactivate previous models
    await db.execute(update(ModelVersion).values(is_active=False))

    # Create version record (training state). is_active=True so there's no
    # window where /api/reports/* sees zero active models. If training
    # fails, the failure branch sets is_active=False.
    mv = ModelVersion(
        id=str(uuid.uuid4()),
        version_number=next_version,
        training_date=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        status="training",
        is_active=True,
        pca_n_components=data.pca_n_components,
        kmeans_n_clusters=data.kmeans_n_clusters,
    )
    db.add(mv)

    try:
        await db.commit()
    except IntegrityError:
        # Race: another training finished and bumped version_number while
        # we were computing next_version. Re-read and retry once.
        await db.rollback()
        logger.warning(
            "[models/train] version_number=%d collision, retrying",
            next_version,
        )
        retried = await db.execute(
            select(ModelVersion.version_number)
            .order_by(ModelVersion.version_number.desc())
            .limit(1)
        )
        last_version_num = retried.scalar_one_or_none()
        next_version = (last_version_num + 1) if last_version_num else 1
        mv.version_number = next_version
        await db.execute(update(ModelVersion).values(is_active=False))
        db.add(mv)
        await db.commit()
    await db.refresh(mv)

    try:
        from app.ml.pipeline import train_pipeline
        from sqlalchemy.orm import Session as SyncSession

        engine = get_sync_engine()
        with SyncSession(engine) as session:
            result = train_pipeline(
                session,
                pca_n=data.pca_n_components,
                k_n=data.kmeans_n_clusters,
            )

        # Update model version with results
        await db.execute(
            update(ModelVersion)
            .where(ModelVersion.id == mv.id)
            .values(
                status="completed",
                dataset_size=result["dataset_size"],
                train_size=result["train_size"],
                test_size=result["test_size"],
                pca_n_components=result["pca_n_components"],
                kmeans_n_clusters=result["kmeans_n_clusters"],
                inertia=result["inertia"],
                silhouette_score=result["silhouette_score"],
                cluster_distribution=json.dumps(result["cluster_distribution"]),
                cluster_labels=json.dumps(result["cluster_labels"]),
                pca_model_data=result["pca_data"],
                kmeans_model_data=result["kmeans_data"],
                scaler_model_data=result["scaler_data"],
                feature_columns=json.dumps(result["feature_columns"]),
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        await db.refresh(mv)

        # Store individual cluster results
        for res in result.get("clustering_results", []):
            date_val = res["result_date"]
            if isinstance(date_val, str):
                date_obj = datetime.fromisoformat(date_val)
            else:
                date_obj = date_val
            cr = ClusterResult(
                id=str(uuid.uuid4()),
                student_id=res["student_id"],
                model_version_id=mv.id,
                result_date=date_obj,
                cluster_id=int(res["cluster_label"]),
                cluster_label=int(res["cluster_label"]),
                pca_x=res.get("pca_x"),
                pca_y=res.get("pca_y"),
                pca_z=res.get("pca_z"),
                distraction_score=res.get("distraction_score"),
                features=json.dumps(res.get("features", {})),
            )
            db.add(cr)
        await db.commit()

        # Training flipped is_active to this new MV — clear reports-side caches
        # so dashboards see the new active model on the next hit.
        cache_invalidate(_CACHE_KEY_MODEL_ID)
        cache_invalidate(_CACHE_KEY_MODEL_LABELS)
        # Round 28: also invalidate the in-process serving cache so the
        # next predict call loads the new model instead of the previous.
        # Pass the OLD model id (if any) — clearing by id is cheaper than
        # clearing the whole cache.
        # NOTE: ModelVersion is imported at module scope (line 15) — do NOT
        # re-import locally here, that turns ModelVersion into a function-local
        # binding and every earlier reference (line 255, 263, 268, 291, 298,
        # 317) raises UnboundLocalError on first run. This was the silent bug
        # that produced v24 (committed with is_active=True, no artifacts).
        try:
            prev = await db.execute(
                select(ModelVersion)
                .where(ModelVersion.is_active == False)  # noqa: E712
                .order_by(ModelVersion.training_date.desc())
                .limit(1)
            )
            prev_mv = prev.scalar_one_or_none()
            if prev_mv is not None:
                invalidate_model_cache(prev_mv.id)
        except Exception:
            pass
        # Also clear the new one to force a fresh load on next call.
        invalidate_model_cache(mv.id)

        return TrainResponse(
            message=f"Training completed successfully. Model v{next_version} is now active.",
            version_number=next_version,
        )

    except ValueError as e:
        error_detail = str(e)
        try:
            # Mark this MV as failed AND not active, so reports fall back to
            # the auto-activate-fallback (most recent completed model).
            await db.execute(
                update(ModelVersion)
                .where(ModelVersion.id == mv.id)
                .values(
                    is_active=False,
                    status="failed",
                    error_message=error_detail,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
        except Exception as inner:
            # Cleanup within cleanup — log and continue; don't mask the
            # original error that will propagate to the caller.
            logger.warning(f"[models/train] cleanup failed: {inner}")
        # Invalidate active-model caches — training may have toggled
        # is_active even on failure (the new MV was created with is_active=True
        # before the failure branch flipped it back to False).
        cache_invalidate(_CACHE_KEY_MODEL_ID)
        cache_invalidate(_CACHE_KEY_MODEL_LABELS)
        # Round 28: clear all serving cache too (training may have
        # replaced the model, the cached artifacts are stale).
        invalidate_model_cache()
        logger.error(f"[models/train] Training failed: {error_detail}")
        raise HTTPException(status_code=400, detail="Training gagal")

    except Exception as e:
        error_detail = f"{type(e).__name__}: {str(e)}"
        try:
            await db.execute(
                update(ModelVersion)
                .where(ModelVersion.id == mv.id)
                .values(
                    is_active=False,
                    status="failed",
                    error_message=error_detail,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
        except Exception as inner:
            logger.warning(f"[models/train] cleanup failed (generic branch): {inner}")
        cache_invalidate(_CACHE_KEY_MODEL_ID)
        cache_invalidate(_CACHE_KEY_MODEL_LABELS)
        # Round 28: clear all serving cache too.
        invalidate_model_cache()
        logger.error(f"[models/train] Training failed: {error_detail}")
        raise HTTPException(status_code=500, detail="Training failed")


@router.get("/results/latest", response_model=LatestResultResponse)
async def get_latest_result(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
):
    user_id = payload.get("sub")
    role = payload.get("role")

    if role == "mahasiswa":
        student_result = await db.execute(select(Student).where(Student.user_id == user_id))
        student = student_result.scalar_one_or_none()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        student_id = student.id
    else:
        student_id = None

    active_model = (await db.execute(
        select(ModelVersion).where(ModelVersion.is_active == True)  # noqa: E712
    )).scalar_one_or_none()
    if not active_model:
        # Round 29 audit P1: auto-activate must be a separate, explicit
        # operation, not a side effect of a GET. The previous code did
        # `update(is_active=False) for all + update(is_active=True) for latest`
        # during a read endpoint, which broke GET semantics (caching, retries)
        # and raced when two admins loaded /dashboard simultaneously — both
        # saw no active model, both tried to activate, only one wins, the
        # other might serve stale state.
        # New policy: GET is read-only. If no active model exists, return 404
        # and let an explicit POST endpoint (or the next training run) set
        # is_active=True. Training always sets is_active=True (line 268 +
        # worker Round 28), so this 404 should be rare — it only fires after
        # a manual deactivation or a failed training that left zero active.
        raise HTTPException(
            status_code=404,
            detail="No active model. Run POST /api/models/train to set one.",
        )

    query = (
        select(ClusterResult)
        .where(ClusterResult.model_version_id == active_model.id)
        .order_by(ClusterResult.result_date.desc())
    )
    if student_id:
        query = query.where(ClusterResult.student_id == student_id)

    latest = (await db.execute(query)).scalars().first()
    if not latest:
        raise HTTPException(status_code=404, detail="No result found")

    cluster_name = None
    labels = _parse_json(active_model.cluster_labels, {})
    if labels:
        cluster_name = labels.get(str(latest.cluster_label))

    return LatestResultResponse(
        student_id=latest.student_id,
        result_date=latest.result_date,
        cluster_label=latest.cluster_label,
        distraction_score=latest.distraction_score,
        cluster_name=cluster_name,
    )


@router.get("/results/points", response_model=list[ClusterPointResponse])
async def get_cluster_points(
    limit: int = Query(default=500, le=2000),
    payload: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return PCA scatter plot points. Admin/dosen only."""
    role = payload.get("role")
    if role not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Hanya admin/dosen boleh melihat data ini")

    active = (await db.execute(
        select(ModelVersion).where(ModelVersion.is_active == True)
    )).scalar_one_or_none()
    if not active:
        raise HTTPException(status_code=404, detail="No active model")

    results = await db.execute(
        select(
            ClusterResult.student_id,
            ClusterResult.pca_x,
            ClusterResult.pca_y,
            ClusterResult.cluster_label,
            ClusterResult.distraction_score,
            Student.full_name,
            Student.nim,
        )
        .join(Student, ClusterResult.student_id == Student.id)
        .where(ClusterResult.model_version_id == active.id)
        .limit(limit)
    )
    rows = results.all()

    return [
        ClusterPointResponse(
            student_id=r.student_id,
            full_name=r.full_name,
            nim=r.nim,
            pca_x=r.pca_x or 0.0,
            pca_y=r.pca_y or 0.0,
            cluster_label=r.cluster_label,
            distraction_score=r.distraction_score or 0.0,
        )
        for r in rows
        if r.pca_x is not None
    ]


@router.patch("/{model_id}/activate", status_code=204)
async def activate_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Set a specific model version as the active model.

    Deactivates all other models and activates the target one.
    Invalidates the active-model caches so reports reflect the change immediately.
    """
    # Verify target exists
    target = await db.execute(
        select(ModelVersion).where(ModelVersion.id == model_id)
    )
    target = target.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Model tidak ditemukan")
    if target.status != "completed":
        raise HTTPException(status_code=400, detail="Hanya model dengan status completed yang bisa diaktifkan")

    # Deactivate all
    await db.execute(update(ModelVersion).values(is_active=False))
    # Activate target
    target.is_active = True
    await db.commit()

    # Invalidate caches so /reports/* sees the new active model immediately
    cache_invalidate(_CACHE_KEY_MODEL_ID)
    cache_invalidate(_CACHE_KEY_MODEL_LABELS)
    cache_invalidate("reports:summary")
    # Round 28: drop the serving cache so the next predict call hits
    # the freshly-activated model. Cheaper than dropping all entries
    # by id — we know the model that just got activated.
    invalidate_model_cache(target.id)
    return None


# ─── Server-side ML serving (Round 28) ─────────────────────────────────────
#
# Endpoints that load the trained model from `model_versions` and run
# inference on student data. The model is loaded once and cached in
# `app/ml/serving.py` (per-worker dict). The trainer invalidates the
# cache on every successful/failed retrain so the next /predict call
# picks up the new artifacts.
#
# Why both a sync endpoint and a background auto-score:
# - Sync `/predict` lets admin/dosen check the model's verdict on a
#   specific (student, date) for debugging or "what-if" exploration.
# - Background auto-score (in `routers/logs.py submit_batch`) keeps
#   `cluster_results` table up to date without admin intervention, so
#   `/api/reports/cluster-trends` and `/api/ai/context` reflect
#   real-time cluster assignments.
#
# The serving module is also exposed directly to the cron scheduler
# for nightly batch scoring of every student (catches up on students
# whose sync was missed between training and auto-score).

def _resolve_target_date(target_date: date | None) -> date:
    """Default to today in WITA when no date is given.

    Pin to WITA because the rest of the system uses WITA as the
    canonical "today" boundary — UTC at 17:00-23:59 is already the
    next day in WITA. Falling back to UTC.date() would give admins
    on the East side of WITA a 7-hour stale window.
    """
    if target_date is not None:
        return target_date
    from app.timezone import WITA
    return datetime.now(WITA).date()


@router.post("/predict", response_model=PredictResponse)
async def predict_student(
    data: PredictRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
):
    """Score a single student on a single date using the active model.

    Uses `app/ml/serving.py` (sync entry point) — must run on the
    worker thread (the trained artifacts are pure pickle). The
    /api/logs/batch path uses the same code via a background task
    so the API response is not blocked on inference.
    """
    from app.database import get_sync_engine
    from sqlalchemy.orm import Session as SyncSession

    if not (await db.execute(select(Student.id).where(Student.id == data.student_id))).scalar_one_or_none():
        raise HTTPException(404, "Mahasiswa tidak ditemukan")
    target = _resolve_target_date(data.target_date)
    engine = get_sync_engine()
    with SyncSession(engine) as s:
        try:
            model = get_active_model(s)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        features = __import__(
            "app.ml.serving", fromlist=["aggregate_student_features_sync"]
        ).aggregate_student_features_sync(s, data.student_id, target)

        # Round 29 audit P0 #6: when the (student, date) pair has no
        # data, do NOT score all-zeros — that lands in some random
        # cluster via -mean/scale projection and returns a phantom
        # classification. Return a clear "skipped" signal instead
        # so the dashboard can render "Belum ada data untuk tanggal
        # ini" instead of misleading numbers.
        from app.ml.serving import score_one, upsert_cluster_result_sync
        if features is None:
            return PredictResponse(
                student_id=data.student_id,
                target_date=target,
                cluster_label=-1,
                cluster_name="Tidak ada data",
                distraction_score=0.0,
                pca_x=None,
                pca_y=None,
                pca_z=None,
                features_used={},
                model_version_number=model.version_number,
                skipped=True,
                skipped_reason="no_data",
            )

        prediction = score_one(features, model)
        upsert_cluster_result_sync(s, model, data.student_id, target, prediction)
        s.commit()

    return PredictResponse(
        student_id=data.student_id,
        target_date=target,
        cluster_label=prediction["cluster_label"],
        cluster_name=prediction["cluster_name"],
        distraction_score=prediction["distraction_score"],
        pca_x=prediction["pca_x"],
        pca_y=prediction["pca_y"],
        pca_z=prediction["pca_z"],
        features_used=prediction["features_used"],
        model_version_number=model.version_number,
    )


@router.post("/predict/batch", response_model=BatchPredictResponse)
async def predict_students_batch(
    data: BatchPredictRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
):
    """Score up to 500 students in one call. Useful for cron pre-warm
    and AI-assistant's "predict everyone's cluster for yesterday"
    prompt. Each student is scored independently so one bad row
    doesn't abort the whole batch.
    """
    from app.database import get_sync_engine
    from sqlalchemy.orm import Session as SyncSession
    from app.ml.serving import (
        aggregate_student_features_sync,
        score_one,
        upsert_cluster_result_sync,
    )

    visible = set((await db.execute(select(Student.id).where(Student.id.in_(data.student_ids)))).scalars())
    if set(data.student_ids) - visible:
        raise HTTPException(404, "Mahasiswa tidak ditemukan")
    target = _resolve_target_date(data.target_date)
    results: list[PredictResponse] = []
    skipped: list[dict] = []

    engine = get_sync_engine()
    with SyncSession(engine) as s:
        try:
            model = get_active_model(s)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        for sid in data.student_ids:
            try:
                features = aggregate_student_features_sync(s, sid, target)
                # Round 29 audit P0 #6: no-data → skipped, not phantom
                # cluster. The skipped list surfaces a clear "no data
                # for this student on this date" signal to the caller.
                if features is None:
                    skipped.append({
                        "student_id": sid,
                        "reason": "no_data",
                    })
                    continue
                prediction = score_one(features, model)
                upsert_cluster_result_sync(s, model, sid, target, prediction)
                results.append(
                    PredictResponse(
                        student_id=sid,
                        target_date=target,
                        cluster_label=prediction["cluster_label"],
                        cluster_name=prediction["cluster_name"],
                        distraction_score=prediction["distraction_score"],
                        pca_x=prediction["pca_x"],
                        pca_y=prediction["pca_y"],
                        pca_z=prediction["pca_z"],
                        features_used=prediction["features_used"],
                        model_version_number=model.version_number,
                    )
                )
            except Exception as e:
                logger.exception("predict/batch: error for student=%s: %s", sid, e)
                skipped.append({"student_id": sid, "reason": str(e)})
        s.commit()

    return BatchPredictResponse(
        target_date=target,
        model_version_number=model.version_number,
        results=results,
        skipped=skipped,
    )
