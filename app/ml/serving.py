"""
ML serving — load trained model from DB and run inference for new data.

The training pipeline (`pipeline.py`) saves the fitted scaler, PCA, and
KMeans as base64-pickle blobs in the `model_versions` table. This
module deserializes them and runs `score_*` functions to predict
clusters for students on demand (or in batch).

The typical flow:
1. Admin trains a model (PCA + K-Means). `model_versions` row now has
   the serialized artifacts.
2. Student syncs usage from Android → `POST /api/logs/batch`.
3. `submit_batch` queues a background `score_recently_synced` task.
4. `score_recently_synced` loads the active model (cached in-process),
   aggregates the same feature columns the training pipeline used,
   applies `scaler → PCA → KMeans`, and upserts a `cluster_results`
   row for each (student, date).

This makes the dashboard's `/api/reports/cluster-trends` and the AI
assistant's `/api/ai/context` reflect real-time cluster assignments
instead of waiting for the next 23:00 WITA retrain.

Performance notes:
- Model load: ~50-200ms (3 pickle unpickles, one-time per server
  process, cached in `_model_cache`).
- Per-student score: ~10-30ms (5 NumPy ops + 1 SQL query for the
  aggregate + 1 upsert).
- Batch 100 students: ~1-3s (one big aggregate query + 100 upserts).
  Acceptable as a background task; not run inline on the request.
"""

import base64
import json
import logging
import pickle
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import numpy as np
from sqlalchemy import cast, Float, Integer, case, func as sql_func
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.cluster_result import ClusterResult
from app.models.location_log import LocationLog
from app.models.app_log import AppLog

logger = logging.getLogger("monitor.serving")


# ─── Feature columns MUST match pipeline.train_pipeline() ───────────────────
# If you change this, also update pipeline.py. (Ponytail: keep them
# side-by-side in source so a diff reveals drift.)
FEATURE_COLUMNS = [
    "screen_time",
    "sosmed_time",
    "game_time",
    "unlock_count",
    "campus_duration",
    "schedule_match_rate",
    "used_during_class_int",
]


# ─── Loaded model artifact cache ────────────────────────────────────────────
# In-process dict keyed by model_version_id. Populated on first
# `load_active_model` / `load_model`, invalidated by
# `invalidate_model_cache` (called by the trainer on success/failure).
# Ponytail: no Redis cache, no LRU eviction — a typical deployment has
# one active model at a time, and the model objects are small (~1MB
# pickled). If we ever serve multiple models, switch to LRU.
_model_cache: dict[str, "LoadedModel"] = {}


@dataclass(frozen=True)
class LoadedModel:
    """Deserialized scaler + PCA + KMeans from a `model_versions` row."""

    scaler: StandardScaler
    pca: PCA
    kmeans: KMeans
    feature_columns: list[str]
    # Stored in DB as {"0": "Rendah", "1": "Sedang", "2": "Tinggi"};
    # inverted here to {0: "Rendah", 1: "Sedang", ...}.
    cluster_labels: dict[int, str]
    pca_n_components: int
    kmeans_n_clusters: int
    version_id: str
    version_number: int


# ─── Model loading ──────────────────────────────────────────────────────────

def load_model(db_sync: Session, model_version_id: str) -> LoadedModel:
    """Load (and cache) a ModelVersion by id.

    Raises `ValueError` if the model doesn't exist, isn't completed, or
    the pickled artifacts are corrupt. The trainer calls
    `invalidate_model_cache(id)` on success/failure to keep the cache
    fresh — without that, the next `/api/models/train` would silently
    serve the previous model.
    """
    if model_version_id in _model_cache:
        return _model_cache[model_version_id]

    from app.models.model_version import ModelVersion

    mv = db_sync.get(ModelVersion, model_version_id)
    if mv is None:
        raise ValueError(f"ModelVersion {model_version_id} tidak ditemukan")
    if mv.status != "completed":
        raise ValueError(
            f"ModelVersion {model_version_id} berstatus '{mv.status}', "
            f"bukan 'completed'. Latih ulang lewat POST /api/models/train."
        )

    try:
        scaler = pickle.loads(base64.b64decode(mv.scaler_model_data.encode()))
        pca = pickle.loads(base64.b64decode(mv.pca_model_data.encode()))
        kmeans = pickle.loads(base64.b64decode(mv.kmeans_model_data.encode()))
    except Exception as e:
        raise ValueError(
            f"ModelVersion {model_version_id}: artifact rusak ({type(e).__name__}: {e}). "
            f"Latih ulang untuk generate model baru."
        ) from e

    if not (hasattr(scaler, "transform") and hasattr(pca, "transform") and hasattr(kmeans, "predict")):
        raise ValueError(
            f"ModelVersion {model_version_id}: artifact tidak sesuai format "
            f"(butuh StandardScaler + PCA + KMeans). Latih ulang."
        )

    try:
        feature_columns = json.loads(mv.feature_columns) if mv.feature_columns else FEATURE_COLUMNS
        if not feature_columns:
            feature_columns = FEATURE_COLUMNS
    except Exception:
        feature_columns = FEATURE_COLUMNS

    raw_labels = json.loads(mv.cluster_labels) if mv.cluster_labels else {}
    cluster_labels = {int(k): str(v) for k, v in raw_labels.items()}

    loaded = LoadedModel(
        scaler=scaler,
        pca=pca,
        kmeans=kmeans,
        feature_columns=feature_columns,
        cluster_labels=cluster_labels,
        pca_n_components=mv.pca_n_components,
        kmeans_n_clusters=mv.kmeans_n_clusters,
        version_id=mv.id,
        version_number=mv.version_number,
    )
    _model_cache[model_version_id] = loaded
    logger.info(
        "Loaded model version_id=%s version_number=%s features=%s",
        mv.id,
        mv.version_number,
        feature_columns,
    )
    return loaded


def get_active_model(db_sync: Session) -> LoadedModel:
    """Return the active model, falling back to the most recent completed.

    Trainer sets `is_active=True` on the new model inside the same
    transaction that deactivates the previous one (see routers/models.py
    `_run_training`). So a SELECT WHERE is_active=True should return
    exactly one row in normal operation. The fallback to "most recent
    completed" matches the same auto-activation logic in
    `routers/models.py get_latest_result` — both code paths recover
    gracefully from a manual `is_active` mis-set.
    """
    from app.models.model_version import ModelVersion

    mv = (
        db_sync.query(ModelVersion)
        .filter(ModelVersion.is_active == True)  # noqa: E712
        .first()
    )
    if mv is None:
        mv = (
            db_sync.query(ModelVersion)
            .filter(ModelVersion.status == "completed")
            .order_by(ModelVersion.training_date.desc())
            .first()
        )
    if mv is None:
        raise ValueError(
            "Belum ada model yang dilatih. POST /api/models/train terlebih dahulu."
        )
    return load_model(db_sync, mv.id)


def invalidate_model_cache(model_version_id: Optional[str] = None) -> None:
    """Drop a single model from cache, or all models if id is None.

    Called by the trainer at three points:
    1. On training success (new model_id, plus invalidate old if any).
    2. On training ValueError (no model was created; clear stale cache
       in case an earlier process left entries).
    3. On PATCH /api/models/{id}/activate (the activated model is the
       same one that was loaded earlier, but its identity is now
       "active" — a fresh load is cheap and avoids stale `is_active`
       being baked into any future call).
    """
    if model_version_id is None:
        cleared = len(_model_cache)
        _model_cache.clear()
        if cleared:
            logger.info("Cleared all %d cached models", cleared)
    else:
        if _model_cache.pop(model_version_id, None) is not None:
            logger.info("Invalidated cached model id=%s", model_version_id)


# ─── Feature aggregation (mirrors pipeline.build_feature_dataframe) ────────

def aggregate_student_features_sync(
    db_sync: Session,
    student_id: str,
    target_date: date,
) -> Optional[dict[str, float]]:
    """Aggregate features for one (student, date) — same logic as the
    training pipeline's `pivot` groupby, but for a single row.

    Returns None if no app_log or location_log data exists for the
    (student, date) — the caller should treat this as "not enough
    data to score" rather than zero-defaulting everything.
    """
    return _aggregate_student_features_batch_sync(db_sync, student_id, [target_date]).get(target_date)


def _aggregate_student_features_batch_sync(
    db_sync: Session,
    student_id: str,
    target_dates: list[date],
) -> dict[date, dict[str, float]]:
    """Batch version: aggregate features for one (student, N dates)
    with only ONE app_log query and ONE location_log query, GROUP BY
    date. Replaces the per-(student, date) loop in `score_recently_synced`
    which previously issued 2 × N queries per Android catch-up sync.
    """
    if not target_dates:
        return {}
    target_dates_set = set(target_dates)
    # ── App logs (1 query) ──
    log_rows = (
        db_sync.query(
            sql_func.date(AppLog.log_date).label("log_date"),
            sql_func.max(AppLog.total_screen_time_minutes).label("screen_time"),
            sql_func.max(AppLog.social_media_time_minutes).label("sosmed_time"),
            sql_func.max(AppLog.game_time_minutes).label("game_time"),
            sql_func.max(AppLog.unlock_count).label("unlock_count"),
            sql_func.avg(
                cast(
                    case(
                        (AppLog.used_during_class.is_(None), None),
                        (AppLog.used_during_class == True, 1.0),  # noqa: E712
                        else_=0.0,
                    ),
                    Float,
                )
            ).label("used_during_class_int"),
        )
        .filter(
            AppLog.student_id == student_id,
            sql_func.date(AppLog.log_date).in_(target_dates),
        )
        .group_by(sql_func.date(AppLog.log_date))
        .all()
    )

    # ── Location logs (1 query) ──
    loc_rows = (
        db_sync.query(
            sql_func.date(LocationLog.timestamp).label("log_date"),
            sql_func.sum(cast(LocationLog.is_on_campus, Integer)).label("campus_duration"),
            sql_func.avg(cast(LocationLog.matches_schedule, Integer)).label("schedule_match_rate"),
        )
        .filter(
            LocationLog.student_id == student_id,
            sql_func.date(LocationLog.timestamp).in_(target_dates),
        )
        .group_by(sql_func.date(LocationLog.timestamp))
        .all()
    )

    def _scalar(value: Any, default: float = 0.0) -> float:
        return float(value) if value is not None else default

    by_date: dict[date, dict[str, float]] = {}
    for row in log_rows:
        d = row.log_date
        if isinstance(d, str):
            d = datetime.fromisoformat(d).date()
        if d not in target_dates_set:
            continue
        by_date[d] = {
            "screen_time": _scalar(getattr(row, "screen_time", None)),
            "sosmed_time": _scalar(getattr(row, "sosmed_time", None)),
            "game_time": _scalar(getattr(row, "game_time", None)),
            "unlock_count": _scalar(getattr(row, "unlock_count", None)),
            "used_during_class_int": _scalar(getattr(row, "used_during_class_int", None)),
            "campus_duration": 0.0,
            "schedule_match_rate": 0.0,
        }
    for row in loc_rows:
        d = row.log_date
        if isinstance(d, str):
            d = datetime.fromisoformat(d).date()
        if d not in target_dates_set:
            continue
        if d not in by_date:
            by_date[d] = {
                "screen_time": 0.0, "sosmed_time": 0.0, "game_time": 0.0,
                "unlock_count": 0.0, "used_during_class_int": 0.0,
            }
        by_date[d]["campus_duration"] = _scalar(getattr(row, "campus_duration", None))
        by_date[d]["schedule_match_rate"] = _scalar(getattr(row, "schedule_match_rate", None))
    return by_date


def _aggregate_one_from_batch(
    log_row: Any, loc_row: Any
) -> Optional[dict[str, float]]:
    # ── App logs ──
    # MAX() on the 3 pre-aggregated time columns (Round 26 P1 fix — the
    # previous SUM() inflated seed data 8-20x). Round 20's UNIQUE INDEX
    # ensures one row per (mobile student, date) on the device, so MAX
    # over the small set is correct.
    #
    # Use `sqlalchemy.cast` rather than `sql_func.cast`: the latter
    # returns a fresh Function object that doesn't carry a static
    # cache key in SQLAlchemy 2.1.x, which trips the compiler on the
    # first execute(). Same generated SQL, but the typed `cast`
    # import caches correctly across calls.
    log_row = (
        db_sync.query(
            sql_func.max(AppLog.total_screen_time_minutes).label("screen_time"),
            sql_func.max(AppLog.social_media_time_minutes).label("sosmed_time"),
            sql_func.max(AppLog.game_time_minutes).label("game_time"),
            sql_func.max(AppLog.unlock_count).label("unlock_count"),
            sql_func.avg(
                cast(
                    case(
                        (AppLog.used_during_class.is_(None), None),
                        (AppLog.used_during_class == True, 1.0),  # noqa: E712
                        else_=0.0,
                    ),
                    Float,
                )
            ).label("used_during_class_int"),
        )
        .filter(
            AppLog.student_id == student_id,
            sql_func.date(AppLog.log_date) == target_date,
        )
        .first()
    )

    # ── Location logs ──
    # SUM on is_on_campus (boolean cast → 0/1) gives the count of
    # on-campus location fixes that day. AVG on matches_schedule gives
    # the proportion of fixes that matched a class window.
    loc_row = (
        db_sync.query(
            sql_func.sum(cast(LocationLog.is_on_campus, Integer)).label("campus_duration"),
            sql_func.avg(cast(LocationLog.matches_schedule, Integer)).label("schedule_match_rate"),
        )
        .filter(
            LocationLog.student_id == student_id,
            sql_func.date(LocationLog.timestamp) == target_date,
        )
        .first()
    )

    if log_row is None and loc_row is None:
        return None

    def _scalar(value: Any, default: float = 0.0) -> float:
        return float(value) if value is not None else default

    return {
        "screen_time": _scalar(getattr(log_row, "screen_time", None)),
        "sosmed_time": _scalar(getattr(log_row, "sosmed_time", None)),
        "game_time": _scalar(getattr(log_row, "game_time", None)),
        "unlock_count": _scalar(getattr(log_row, "unlock_count", None)),
        "used_during_class_int": _scalar(
            getattr(log_row, "used_during_class_int", None)
        ),
        "campus_duration": _scalar(
            getattr(loc_row, "campus_duration", None)
        ),
        "schedule_match_rate": _scalar(
            getattr(loc_row, "schedule_match_rate", None)
        ),
    }


def _aggregate_one_from_batch(
    log_row: Any, loc_row: Any
) -> Optional[dict[str, float]]:
    """Shape a single (log_row, loc_row) pair into the feature dict
    the model expects. Returns None if both rows are empty.
    """
    if log_row is None and loc_row is None:
        return None

    def _scalar(value: Any, default: float = 0.0) -> float:
        return float(value) if value is not None else default

    return {
        "screen_time": _scalar(getattr(log_row, "screen_time", None)) if log_row else 0.0,
        "sosmed_time": _scalar(getattr(log_row, "sosmed_time", None)) if log_row else 0.0,
        "game_time": _scalar(getattr(log_row, "game_time", None)) if log_row else 0.0,
        "unlock_count": _scalar(getattr(log_row, "unlock_count", None)) if log_row else 0.0,
        "used_during_class_int": _scalar(
            getattr(log_row, "used_during_class_int", None)
        ) if log_row else 0.0,
        "campus_duration": _scalar(
            getattr(loc_row, "campus_duration", None)
        ) if loc_row else 0.0,
        "schedule_match_rate": _scalar(
            getattr(loc_row, "schedule_match_rate", None)
        ) if loc_row else 0.0,
    }


# ─── Scoring ────────────────────────────────────────────────────────────────

def compute_distraction_score(row: dict[str, float]) -> float:
    """Same formula as the trainer's `compute_distraction_score`.

    Round 29 audit P1 #4: now a thin re-export of the shared
    implementation in `app.ml.score`. The formula is identical to
    `pipeline.py:120` because both delegate to the same function.
    Drift between trainer and serving is now structurally impossible.
    """
    from app.ml.score import compute_distraction_score as _shared
    return _shared(row)


def score_one(features: dict[str, float], model: LoadedModel) -> dict[str, Any]:
    """Run scaler → PCA → KMeans on a single feature row.

    Returns a dict the API can serialize directly:
    {
        cluster_label, cluster_name, distraction_score,
        pca_x, pca_y, pca_z, features_used
    }

    Round 29 audit P0 #6: if `features` is None (no data for the
    (student, date) pair), the caller should treat this as a skip
    rather than running the model on an all-zeros input. The previous
    behavior ran the model on `features or {}` (all-zeros), which
    landed in some random cluster via -mean/scale projection and
    returned `distraction_score=0.0` — a contradictory result the
    dashboard couldn't disambiguate from a legitimate "Sangat Rendah"
    classification. Callers should branch on `features is None`
    before calling `score_one`.
    """
    if features is None:
        # Sentinel: caller should treat as skip, NOT call score_one.
        raise ValueError(
            "score_one called with features=None — caller should treat "
            "no-data as a skip instead of running the model on zeros"
        )
    row = np.array([[features.get(col, 0.0) for col in model.feature_columns]], dtype=float)
    scaled = model.scaler.transform(row)
    # KMeans was fit on PCA-transformed data (see pipeline.train_pipeline),
    # not on the raw 7-feature scaled matrix. Apply PCA first, then
    # KMeans — same order as the trainer's prediction path.
    pca_2d = model.pca.transform(scaled)  # shape (1, pca_n_components)
    cluster = int(model.kmeans.predict(pca_2d)[0])
    pca_flat = pca_2d[0].tolist()
    cluster_name = model.cluster_labels.get(cluster, f"Cluster {cluster}")
    score = compute_distraction_score(features)
    return {
        "cluster_label": cluster,
        "cluster_name": cluster_name,
        "distraction_score": score,
        "pca_x": float(pca_flat[0]) if len(pca_flat) > 0 else None,
        "pca_y": float(pca_flat[1]) if len(pca_flat) > 1 else None,
        "pca_z": float(pca_flat[2]) if len(pca_flat) > 2 else None,
        "features_used": features,
    }


# ─── ClusterResult upsert ──────────────────────────────────────────────────

def upsert_cluster_result_sync(
    db_sync: Session,
    model: LoadedModel,
    student_id: str,
    target_date: date,
    prediction: dict[str, Any],
) -> ClusterResult:
    """Insert or update a ClusterResult row for (student_id, model_id, date).

    The unique key is `(student_id, model_version_id, result_date)` —
    one row per (student, model, date) so re-scoring the same day is
    idempotent and re-training with a new model creates a new set of
    rows without colliding on the old ones.
    """
    if isinstance(target_date, date) and not isinstance(target_date, datetime):
        target_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    else:
        target_dt = target_date
    existing = (
        db_sync.query(ClusterResult)
        .filter(
            ClusterResult.student_id == student_id,
            ClusterResult.model_version_id == model.version_id,
            ClusterResult.result_date == target_dt,
        )
        .first()
    )

    if existing is not None:
        existing.cluster_id = prediction["cluster_label"]
        existing.cluster_label = prediction["cluster_label"]
        existing.cluster_name = prediction["cluster_name"]
        existing.pca_x = prediction["pca_x"]
        existing.pca_y = prediction["pca_y"]
        existing.distraction_score = prediction["distraction_score"]
        existing.features = json.dumps(prediction["features_used"])
        existing.computed_at = datetime.now(timezone.utc)
        # Round 30 audit (P2): don't commit per-row. The caller (batch predict
        # or score_recently_synced) commits at the end. Saves 500 round-trips
        # for predict_students_batch with 500 students.
        logger.debug(
            "Updated ClusterResult student=%s date=%s cluster=%s",
            student_id,
            target_date,
            prediction["cluster_label"],
        )
        return existing

    # result_date column is AwareDateTime (DateTime(timezone=True)) — needs
    # a tz-aware datetime, not a plain date. Convert at midnight UTC
    # to match the trainer's storage convention.
    if isinstance(target_date, date) and not isinstance(target_date, datetime):
        result_date_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    else:
        result_date_dt = target_date
    cr = ClusterResult(
        id=str(uuid.uuid4()),
        student_id=student_id,
        model_version_id=model.version_id,
        cluster_id=prediction["cluster_label"],
        cluster_label=prediction["cluster_label"],
        cluster_name=prediction["cluster_name"],
        result_date=result_date_dt,
        pca_x=prediction["pca_x"],
        pca_y=prediction["pca_y"],
        pca_z=prediction["pca_z"],
        features=json.dumps(prediction["features_used"]),
        distraction_score=prediction["distraction_score"],
        computed_at=datetime.now(timezone.utc),
    )
    db_sync.add(cr)
    # Round 30 audit (P2): don't commit per-row — caller commits at end.
    logger.info(
        "Inserted ClusterResult student=%s date=%s cluster=%s score=%s",
        student_id,
        target_date,
        prediction["cluster_label"],
        prediction["distraction_score"],
    )
    return cr


# ─── High-level scorers ─────────────────────────────────────────────────────

def score_student_for_date(
    db_sync: Session,
    student_id: str,
    target_date: date,
) -> Optional[dict[str, Any]]:
    """Score one student on one date. Loads the active model, runs
    inference, upserts ClusterResult, returns the prediction dict.

    Returns None if the student has no data for that date.
    """
    model = get_active_model(db_sync)
    features = aggregate_student_features_sync(db_sync, student_id, target_date)
    if features is None:
        return None
    prediction = score_one(features, model)
    upsert_cluster_result_sync(db_sync, model, student_id, target_date, prediction)
    return prediction


def score_student_for_dates(
    db_sync: Session,
    student_id: str,
    target_dates: list[date],
) -> list[dict[str, Any]]:
    """Score one student across multiple dates. Used by
    `score_recently_synced` in `routers/logs.py` after a sync from
    Android (one row per affected date).
    """
    if not target_dates:
        return []
    try:
        model = get_active_model(db_sync)
    except ValueError as e:
        logger.warning("score_student_for_dates: no model available: %s", e)
        return []
    # Round 30 audit (P2): batch the per-date aggregations into 2
    # queries total (1 for app_log GROUP BY date, 1 for location_log).
    # Was: 2 × len(target_dates) queries (e.g. 14 for a 7-day sync).
    features_by_date = _aggregate_student_features_batch_sync(
        db_sync, student_id, target_dates
    )
    out = []
    for d in target_dates:
        try:
            features = features_by_date.get(d)
            if features is None:
                continue
            prediction = score_one(features, model)
            upsert_cluster_result_sync(db_sync, model, student_id, d, prediction)
            out.append(prediction)
        except Exception as e:
            logger.exception(
                "score_student_for_dates: failed for student=%s date=%s: %s",
                student_id,
                d,
                e,
            )
    return out


def score_recently_synced(
    student_id: str,
    log_dates: list[date],
) -> dict[str, int]:
    """Background task: score the (student, dates) that were just
    synced from Android. Called from `routers/logs.py submit_batch`
    via `background_tasks.add_task` so the API response isn't blocked
    on inference.

    Returns a summary dict for logging:
        {"scored": N, "skipped_no_data": M, "errors": K}
    """
    from app.database import get_sync_session

    scored = 0
    skipped = 0
    errors = 0
    try:
        with get_sync_session()() as db_sync:
            # Round 29 audit P2 (B4): load the active model ONCE before
            # the loop, not per-iteration. Previously called
            # `get_active_model(db_sync)` inside the loop — model is
            # cached in `_model_cache` so it's cheap, but every call
            # still does a SELECT for `is_active=True` and goes
            # through the cache key check. For a 7-day Android
            # catch-up sync, that's 7 redundant queries.
            try:
                model = get_active_model(db_sync)
            except ValueError as e:
                logger.warning("score_recently_synced: no model: %s", e)
                return {"scored": 0, "skipped_no_data": 0, "errors": 1}
            for d in log_dates:
                try:
                    features = aggregate_student_features_sync(
                        db_sync, student_id, d
                    )
                    if features is None:
                        skipped += 1
                        continue
                    prediction = score_one(features, model)
                    upsert_cluster_result_sync(
                        db_sync, model, student_id, d, prediction
                    )
                    scored += 1
                except Exception as e:
                    errors += 1
                    logger.exception(
                        "score_recently_synced: error for student=%s date=%s: %s",
                        student_id,
                        d,
                        e,
                    )
    except Exception as e:
        logger.exception("score_recently_synced: session error: %s", e)
    logger.info(
        "score_recently_synced student=%s dates=%d scored=%d skipped=%d errors=%d",
        student_id,
        len(log_dates),
        scored,
        skipped,
        errors,
    )
    return {"scored": scored, "skipped_no_data": skipped, "errors": errors}


def score_all_students_for_date(target_date: date) -> dict[str, int]:
    """Nightly cron: re-score every student with a log on the given
    date. Used by the scheduler (added in this round) to catch up
    on students whose sync was missed (e.g. server restart between
    Android sync and auto-score).
    """
    from app.database import get_sync_session
    from app.models.app_log import AppLog
    from sqlalchemy import distinct

    with get_sync_session()() as db_sync:
        try:
            model = get_active_model(db_sync)
        except ValueError as e:
            logger.warning("score_all_students_for_date: no model: %s", e)
            return {"scored": 0, "skipped": 0, "errors": 1}

        student_rows = db_sync.query(distinct(AppLog.student_id)).filter(
            sql_func.date(AppLog.log_date) == target_date
        ).all()
        student_ids = [r[0] for r in student_rows]

        if not student_ids:
            return {"scored": 0, "skipped": 0, "errors": 0}

        # Round 30 audit (P2): batch the per-student aggregations into
        # 2 queries total (1 for app_log GROUP BY student, 1 for
        # location_log GROUP BY student). Was: 2 × len(student_ids)
        # queries (e.g. 200 for 100 students).
        log_rows = (
            db_sync.query(
                AppLog.student_id.label("sid"),
                sql_func.max(AppLog.total_screen_time_minutes).label("screen_time"),
                sql_func.max(AppLog.social_media_time_minutes).label("sosmed_time"),
                sql_func.max(AppLog.game_time_minutes).label("game_time"),
                sql_func.max(AppLog.unlock_count).label("unlock_count"),
                sql_func.avg(
                    cast(
                        case(
                            (AppLog.used_during_class.is_(None), None),
                            (AppLog.used_during_class == True, 1.0),  # noqa: E712
                            else_=0.0,
                        ),
                        Float,
                    )
                ).label("used_during_class_int"),
            )
            .filter(
                AppLog.student_id.in_(student_ids),
                sql_func.date(AppLog.log_date) == target_date,
            )
            .group_by(AppLog.student_id)
            .all()
        )
        loc_rows = (
            db_sync.query(
                LocationLog.student_id.label("sid"),
                sql_func.sum(cast(LocationLog.is_on_campus, Integer)).label("campus_duration"),
                sql_func.avg(cast(LocationLog.matches_schedule, Integer)).label("schedule_match_rate"),
            )
            .filter(
                LocationLog.student_id.in_(student_ids),
                sql_func.date(LocationLog.timestamp) == target_date,
            )
            .group_by(LocationLog.student_id)
            .all()
        )
        log_by_sid = {r.sid: r for r in log_rows}
        loc_by_sid = {r.sid: r for r in loc_rows}

        scored = 0
        skipped = 0
        errors = 0
        for sid in student_ids:
            try:
                features = _aggregate_one_from_batch(log_by_sid.get(sid), loc_by_sid.get(sid))
                if features is None:
                    skipped += 1
                    continue
                prediction = score_one(features, model)
                upsert_cluster_result_sync(db_sync, model, sid, target_date, prediction)
                scored += 1
            except Exception as e:
                errors += 1
                logger.exception(
                    "score_all_students_for_date: student=%s error: %s", sid, e
                )
    logger.info(
        "score_all_students_for_date=%s candidates=%d scored=%d skipped=%d errors=%d",
        target_date,
        len(student_ids),
        scored,
        skipped,
        errors,
    )
    return {"scored": scored, "skipped": skipped, "errors": errors}
