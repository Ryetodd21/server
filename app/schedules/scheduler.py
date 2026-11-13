"""
Cron Scheduler — menjadwalkan cut-off data (22:00 WITA) dan training (23:00 WITA).
Menggunakan cron-like scheduling dengan threading (ringan, tanpa systemd overhead).

Cara pakai:
    python -m app.schedules.scheduler
(jalankan di background terpisah dari uvicorn)

Log output:
    stdout/stderr — di-capture oleh nohup run-server.sh
    File handler opsional: SCHEDULER_LOG_FILE=/path/to/scheduler.log
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.tasks.worker import job_queue, cutoff_data_task, train_model_task

settings = get_settings()

# ─── Logging setup ──────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# File handler opsional via SCHEDULER_LOG_FILE env var
_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
_log_file = os.environ.get("SCHEDULER_LOG_FILE")
if _log_file:
    try:
        _handlers.append(logging.FileHandler(_log_file))
    except OSError as e:
        # File mungkin tidak writable — log warning ke stderr, lanjut stdout saja
        print(f"[scheduler] WARN: cannot open log file {_log_file}: {e}", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATEFMT,
    handlers=_handlers,
    force=True,
)

logger = logging.getLogger("scheduler")

# Heartbeat file for docker healthcheck — touched every loop iteration.
_HEARTBEAT_FILE = Path("/tmp/scheduler_heartbeat")

CUTOFF_HOUR_UTC = settings.CUTOFF_HOUR - 8  # WITA = UTC+8
TRAINING_HOUR_UTC = settings.TRAINING_HOUR - 8


def daily_scheduler():
    fired_date = None
    cutoff_fired = False
    training_fired = False
    score_fired = False

    while True:
        _HEARTBEAT_FILE.touch()
        now = datetime.now(timezone.utc)

        # Reset on UTC date change. Keyed on the date, not on hour==0 && minute==0:
        # a 60s loop can drift past 00:00 and would then skip the reset for the
        # entire day, leaving every *_fired flag True until the container restarts.
        if now.date() != fired_date:
            fired_date = now.date()
            cutoff_fired = training_fired = score_fired = False

        # Slots use >= so a slot missed while the host was asleep/off runs on the
        # next tick instead of being skipped until tomorrow.
        training_was_fired = training_fired

        # Cut-off: 22:00 WITA = 14:00 UTC
        if now.hour >= CUTOFF_HOUR_UTC and not cutoff_fired:
            logger.info("Cut-off triggered (WITA %02d:00)", settings.CUTOFF_HOUR)
            try:
                job_queue.enqueue(cutoff_data_task)
                cutoff_fired = True
                logger.info("Cut-off job queued")
            except Exception as e:
                logger.exception("Cut-off enqueue gagal: %s", e)

        # Training: 23:00 WITA = 15:00 UTC
        if now.hour >= TRAINING_HOUR_UTC and not training_fired:
            logger.info("Training triggered (WITA %02d:00)", settings.TRAINING_HOUR)
            try:
                job_queue.enqueue(train_model_task)
                training_fired = True
                logger.info("Training job queued")
            except Exception as e:
                logger.exception("Training enqueue gagal: %s", e)

        # Round 28: post-training cluster scoring. After the trainer
        # commits the new model + cluster_results, re-score every
        # student that has a log on the same day. Catches up on
        # students whose Android sync was missed between training and
        # the next auto-score (e.g. server restart). Runs 30 minutes
        # after training so the trainer's commit is durable.
        # 23:30 WITA = 15:30 UTC
        if training_fired and not training_was_fired and not score_fired:
            logger.info("Post-training cluster scoring triggered (WITA 23:30)")
            try:
                from app.ml.serving import score_all_students_for_date
                from app.timezone import WITA
                target = datetime.now(WITA).date()
                # Runs in the scheduler thread (cheap — pure CPU + 1 query
                # per student, ~10-30ms each).
                summary = score_all_students_for_date(target)
                logger.info(
                    "Nightly scoring for %s: scored=%d skipped=%d errors=%d",
                    target, summary["scored"], summary["skipped"], summary["errors"],
                )
            except Exception as e:
                logger.exception("Nightly scoring gagal: %s", e)
            score_fired = True

        time.sleep(60)  # check every minute


def start_scheduler():
    thread = threading.Thread(target=daily_scheduler, daemon=True)
    thread.start()
    logger.info("Daily scheduler started (WITA timezone)")
    logger.info("  Cut-off:  %02d:00 WITA = %02d:00 UTC", settings.CUTOFF_HOUR, CUTOFF_HOUR_UTC)
    logger.info("  Training: %02d:00 WITA = %02d:00 UTC", settings.TRAINING_HOUR, TRAINING_HOUR_UTC)
    logger.info("  Scoring:  23:30 WITA = 15:30 UTC (post-train batch)")
    return thread


if __name__ == "__main__":
    # Dipanggil oleh `python -m app.schedules.scheduler` dari run-server.sh
    logger.info("Scheduler starting (PID %d)", os.getpid())
    start_scheduler()
    # Block supaya process tidak exit (daemon thread ikut mati kalau parent keluar)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Scheduler shutting down (SIGINT)")
        sys.exit(0)
