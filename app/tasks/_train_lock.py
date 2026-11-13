"""Module-level lock for serializing training runs.

Round 26 (P2) audit: the worker process is separate from the FastAPI
event loop, so the `asyncio.Lock` in routers/models.py does not protect
against concurrent runs that span both. Without this threading lock,
two simultaneous triggers (e.g., a manual "Train Ulang" click while
the 23:00 cron is still running) would both pass the "no active
training" check, both create a new ModelVersion, and both run the
full PCA + K-Means on the same dataset.

The lock is process-local. If you ever scale the worker to multiple
replicas, replace this with a Redis-based lock (or a Postgres advisory
lock) — the acquire path would be the same but you'd avoid two
replicas each running the same training.
"""
import threading

train_lock = threading.Lock()
