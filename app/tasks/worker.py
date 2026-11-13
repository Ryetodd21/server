"""
RQ Task Workers — dipanggil oleh Redis Queue.
Jalankan: rq worker monitoring-worker

Dev mode (USE_FAKEREDIS=true): pakai fakeredis, jobs jalan in-process.
"""
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from app.config import get_settings
from app.models.model_version import ModelVersion
from app.models.cluster_result import ClusterResult
from app.models.student import Student

settings = get_settings()


def _make_redis():
    if settings.USE_FAKEREDIS:
        import fakeredis
        return fakeredis.FakeRedis()
    from redis import Redis
    return Redis.from_url(settings.REDIS_URL)


redis_conn = _make_redis()

if settings.USE_FAKEREDIS:
    # In dev: bypass RQ Queue, run tasks directly
    class _DevQueue:
        def enqueue(self, fn, *args, **kwargs):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            return t
    import threading
    job_queue = _DevQueue()
else:
    from rq import Queue
    job_queue = Queue("monitoring", connection=redis_conn)


from app.ml.pipeline import train_pipeline
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def cutoff_data_task() -> dict:
    """
    Task 1: Cut-off data harian pukul 22.00 WITA.
    Tandai semua log hari ini sebagai "ready for training".
    """
    from app.models.app_log import AppLog
    from app.models.location_log import LocationLog

    engine = create_engine(settings.DATABASE_SYNC_URL)
    with Session(engine) as session:
        from datetime import datetime as _dt, timedelta as _td
        from app.timezone import WITA

        # WITA date at midnight = UTC 16:00 previous day
        wita_offset = WITA.utcoffset(None)
        now_utc = _dt.now(timezone.utc)
        wita_midnight_utc = (now_utc - wita_offset).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wita_end_utc = wita_midnight_utc + _td(days=1)

        # log_date: app stores WITA date at midnight UTC — filter to today's WITA range
        log_count = session.query(AppLog).filter(
            AppLog.log_date >= wita_midnight_utc,
            AppLog.log_date < wita_end_utc,
        ).update({"is_synced": True}, synchronize_session=False)

        # timestamp: MQTT stores UTC datetime — filter to UTC today range
        utc_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        loc_count = session.query(LocationLog).filter(
            LocationLog.timestamp >= utc_midnight
        ).update({"synced": True}, synchronize_session=False)

        session.commit()

    return {
        "task": "cutoff",
        "cutoff_time": str(datetime.now(timezone.utc)),
        "app_logs_processed": log_count,
        "location_logs_processed": loc_count,
    }


def train_model_task() -> dict:
    """
    Task 2: Training PCA + K-Means pukul 23.00 WITA.
    Jalankan pipeline, simpan model version, simpan hasil clustering.
    """
    # Round 30 audit (P0 #1): the local imports that used to be here
    # (`from app.models.model_version import ModelVersion` etc.) were
    # LOCAL to `train_model_task` only — they did NOT propagate to
    # module scope. The actual training body is in `_train_model_task_impl`,
    # which is a MODULE-LEVEL function (not nested inside this one), so
    # it could not see the local imports. The function raised
    # `NameError: name 'ModelVersion' is not defined` at line 120 on the
    # first SELECT — which is exactly what the production worker logs
    # show for the 2026-10-09 15:00 training run:
    #   File "/app/app/tasks/worker.py", line 112, in train_model_task
    #     return _train_model_task_impl()
    #   File "/app/app/tasks/worker.py", line 120, in _train_model_task_impl
    #   NameError: name 'ModelVersion' is not defined
    # The local-import pattern was a different manifestation of the
    # same class of bug as the routers/models.py:372 one (Python name
    # binding) — fixed in Round 30 by hoisting the imports to module
    # scope (top of file). Now both `train_model_task` AND
    # `_train_model_task_impl` see them.

    # Round 26 audit (P2): serialize concurrent training runs. The HTTP
    # path in routers/models.py holds an `asyncio.Lock` for the same
    # purpose, but the RQ worker is a separate process and a separate
    # event loop — the asyncio.Lock doesn't apply. A threading.Lock
    # is enough for a single-process worker. Without it, two simultaneous
    # triggers (e.g., a manual click in the UI fires POST /api/models/train
    # while the 23:00 cron is still running) both pass the "no active
    # training" check, both create a new ModelVersion, and both run the
    # full PCA + K-Means on the same dataset. That's wasted compute
    # AND a corrupt `is_active` flag (the second commits last and leaves
    # the first's MV marked active but orphaned).
    # Round 30 audit (P2 #1): the threading.Lock was only effective
    # within THIS process. The RQ worker is a separate container from
    # the FastAPI event loop, so a dashboard "Train Ulang" click in
    # the API process could race this cron. The Redis-backed lock in
    # `_train_lock_redis` is the cross-process guard. We also still
    # hold the threading lock to serialize within the worker itself
    # (the worker has only one process but RQ can also run jobs from
    # other queues).
    from app.tasks._train_lock_redis import acquire_train_lock
    with acquire_train_lock() as got_lock:
        if not got_lock:
            # Another process (likely the API's manual "Train Ulang")
            # is already training. Skip this cron run; the next cron
            # in 24h will catch up.
            logger.warning(
                "train_model_task: lock held by another process, skipping cron"
            )
            return {
                "task": "train",
                "status": "skipped_locked",
                "message": "training already in progress elsewhere",
            }
        from app.tasks._train_lock import train_lock
        with train_lock:
            return _train_model_task_impl()


def _train_model_task_impl() -> dict:

    engine = create_engine(settings.DATABASE_SYNC_URL)
    with Session(engine) as session:
        # Get next version number
        last_version = session.query(ModelVersion).order_by(
            ModelVersion.version_number.desc()
        ).first()
        next_version = (last_version.version_number + 1) if last_version else 1

        # Deactivate previous models
        session.query(ModelVersion).update({"is_active": False})
        session.commit()

        # Create version record (training state). Round 29 audit P2
        # (B3): create with `is_active=True` so reports endpoints
        # always see exactly one active model, even during the
        # training window. If training fails, the except branch
        # (line 246+) re-activates the previous completed MV. This
        # mirrors the HTTP path's pattern in `routers/models.py` —
        # previously the RQ worker left a brief "no active model"
        # gap between deactivation and the new MV's completion.
        mv = ModelVersion(
            id=str(uuid.uuid4()),
            version_number=next_version,
            training_date=datetime.now(timezone.utc),
            status="training",
            started_at=datetime.now(timezone.utc),
            pca_n_components=settings.PCA_N_COMPONENTS,
            kmeans_n_clusters=settings.KMEANS_N_CLUSTERS,
            is_active=True,
        )
        session.add(mv)
        session.commit()

        try:
            # Run pipeline
            result = train_pipeline(
                session,
                pca_n=settings.PCA_N_COMPONENTS,
                k_n=settings.KMEANS_N_CLUSTERS,
            )

            # Update model version
            mv.is_active = True
            mv.status = "completed"
            mv.dataset_size = result["dataset_size"]
            mv.train_size = result["train_size"]
            mv.test_size = result["test_size"]
            mv.pca_n_components = result["pca_n_components"]
            mv.kmeans_n_clusters = result["kmeans_n_clusters"]
            mv.inertia = result["inertia"]
            mv.silhouette_score = result["silhouette_score"]
            mv.cluster_distribution = json.dumps(result["cluster_distribution"])
            mv.cluster_labels = json.dumps(result["cluster_labels"])
            # Round 30 audit (P0 #3): was missing scaler_data. Without it,
            # /api/models/predict returns 409 "artifact rusak (NoneType has no
            # .encode)" because the serving code (app/ml/serving.py:115-127)
            # does `base64.b64decode(mv.scaler_model_data.encode())` and
            # `.encode()` on None raises AttributeError. The HTTP path in
            # routers/models.py:332 has this line — the worker was missing it.
            mv.scaler_model_data = result["scaler_data"]
            mv.pca_model_data = result["pca_data"]
            mv.kmeans_model_data = result["kmeans_data"]
            mv.completed_at = datetime.now(timezone.utc)

            # Round 26 audit (P1): the previous version of this block had
            # only a comment promising "Delete old cluster results for
            # these students/dates" — the actual DELETE statement was
            # never added. Without it, every nightly 23:00 WITA retrain
            # appended a new `cluster_results` row per (student, date)
            # instead of replacing the old one. After N nights, the
            # cluster_results table grew by N× per student, and the
            # reports query (which orders by result_date DESC, takes the
            # first) still returned the most recent — but the older
            # rows accumulated as dead storage and showed up in raw
            # data exports. Drop rows whose (student_id, result_date)
            # matches what we're about to insert.
            student_ids = [r["student_id"] for r in result["clustering_results"]]
            # Round 30 audit (P0 #3): result_date from the pipeline is
            # str(date_val)[:10] (a "YYYY-MM-DD" string), but the
            # ClusterResult.result_date column is AwareDateTime.
            # SQLAlchemy's AwareDateTime.process_bind_param accesses
            # `value.tzinfo` on the value, which raises AttributeError
            # for str — even though the .in_() filter would have been
            # converted to a parameterized SQL string anyway. Convert
            # to datetime objects explicitly so the bind_param succeeds.
            result_dates_dt = sorted({
                datetime.fromisoformat(r["result_date"])
                for r in result["clustering_results"]
            })
            if student_ids and result_dates_dt:
                session.query(ClusterResult).filter(
                    ClusterResult.student_id.in_(student_ids),
                    ClusterResult.result_date.in_(result_dates_dt),
                ).delete(synchronize_session=False)

            session.commit()

            # Save new cluster results
            saved = 0
            for r in result["clustering_results"]:
                student_check = session.query(Student).filter(
                    Student.id == r["student_id"]
                ).first()
                if not student_check:
                    continue

                cr = ClusterResult(
                    id=str(uuid.uuid4()),
                    student_id=r["student_id"],
                    model_version_id=mv.id,
                    result_date=datetime.fromisoformat(r["result_date"]),
                    cluster_id=int(r["cluster_label"]),  # Round 30 audit: was missing — NotNullViolation
                    cluster_label=r["cluster_label"],
                    pca_x=r.get("pca_x"),
                    pca_y=r.get("pca_y"),
                    pca_z=r.get("pca_z"),
                    distraction_score=r["distraction_score"],
                    features=json.dumps(r["features"]) if isinstance(r.get("features"), dict) else r.get("features"),
                )
                session.add(cr)
                saved += 1

            session.commit()

            # Round 29 audit P0 #5: tell the API process(es) to drop
            # their model cache. The RQ worker runs in a separate
            # container from the API, so its in-process cache
            # invalidation can't reach the API's `_model_cache` and
            # `app.cache` keys. The fix publishes a message on the
            # `model:invalidate` Redis channel that the API process
            # subscribes to via `app.cache_pubsub._subscriber_loop()`.
            # Without this, the API kept serving the previous model
            # after every nightly retrain until the API container
            # was restarted.
            try:
                from app.cache_pubsub import publish_model_invalidate
                publish_model_invalidate_sync(mv.id, "training_completed")
            except Exception:
                logging.getLogger("monitor").exception(
                    "[worker] model cache invalidate publish failed "
                    "(model_version=%s)", mv.id,
                )

            # Best-effort MQTT push so dashboard listeners see "new model
            # ready" without waiting for the next poll. RQ runs in a separate
            # container from the api — broker access goes via mqtt_bridge →
            # Redis mqtt:relay → subscriber.
            try:
                import asyncio
                from app.mqtt_bridge import broadcast_dashboard
                asyncio.run(broadcast_dashboard(
                    "training_completed",
                    {
                        "model_version_id": mv.id,
                        "version": next_version,
                        "cluster_count": result["kmeans_n_clusters"],
                        "dataset_size": result["dataset_size"],
                    },
                ))
            except Exception:
                logging.getLogger("monitor").exception(
                    "[worker] training MQTT broadcast failed (model_version=%s)",
                    mv.id,
                )

            return {
                "task": "training",
                "version": next_version,
                "status": "completed",
                "dataset_size": result["dataset_size"],
                "silhouette_score": result["silhouette_score"],
                "cluster_distribution": result["cluster_distribution"],
                "results_saved": saved,
            }

        except Exception as e:
            mv.status = "failed"
            mv.error_message = str(e)
            mv.completed_at = datetime.now(timezone.utc)
            mv.is_active = False
            # Round 26 audit (P0 #6): on training failure, the previous
            # active ModelVersion was already deactivated at line 107 and
            # the new MV defaults to inactive. Without this, NO model is
            # active after a failure — /api/reports/* queries short-circuit
            # on `get_active_model_id()`. Re-activate the most recent
            # completed MV so reports endpoints keep working until the
            # next successful training.
            prev = (
                session.query(ModelVersion)
                .filter(
                    ModelVersion.status == "completed",
                    ModelVersion.id != mv.id,
                )
                .order_by(ModelVersion.version_number.desc())
                .first()
            )
            if prev is not None:
                prev.is_active = True
            session.commit()

            # Round 29 audit P0 #5: even on training failure, the API
            # needs to know the previous model is the one to serve. Publish
            # the re-activated model id (or None if no previous existed) so
            # the API drops any cache entry that pointed at the failed MV.
            try:
                from app.cache_pubsub import publish_model_invalidate
                publish_model_invalidate_sync(
                    prev.id if prev is not None else None,
                    "training_failed",
                )
            except Exception:
                logging.getLogger("monitor").exception(
                    "[worker] model cache invalidate publish failed on failure"
                )

            raise


def publish_model_invalidate_sync(
    model_version_id: Optional[str],
    reason: str,
) -> None:
    """Synchronous wrapper around `publish_model_invalidate`.

    The RQ worker is sync; the cache_pubsub helper is async (because
    it uses `redis.asyncio`). We bridge by running the coroutine in a
    fresh event loop. Best-effort: any exception bubbles up to the
    caller's try/except.
    """
    import asyncio
    from app.cache_pubsub import publish_model_invalidate
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                publish_model_invalidate(model_version_id, reason)
            )
        finally:
            loop.close()
    except Exception:
        # Re-raise so the caller's try/except sees it.
        raise
