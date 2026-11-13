"""Self-check for the daily_scheduler slot logic.

Drives the real loop with a fake clock and fake enqueue, then asserts which
jobs fire per tick. Run against the host source, not the baked image:

    cd /home/terio-op && docker run --rm -i \
        -v $PWD/server/app/schedules/scheduler.py:/app/app/schedules/scheduler.py:ro \
        -e DATABASE_URL=sqlite+aiosqlite:///:memory: \
        -e DATABASE_SYNC_URL=sqlite:///:memory: \
        -e SECRET_KEY=x -e USE_FAKEREDIS=true \
        --entrypoint python monitorung-api:latest - < server/scripts/scheduler_slot_check.py
"""
import sys
from datetime import datetime, timezone

import app.ml.serving as serving
import app.schedules.scheduler as sched

UTC = timezone.utc

ticks = [
    # Host asleep across the whole 14:00-15:00 UTC window, resumes 20:00 UTC.
    (datetime(2026, 10, 29, 20, 0, tzinfo=UTC),
     ["cutoff", "training", "scoring"]),
    (datetime(2026, 10, 29, 20, 1, tzinfo=UTC), []),
    # Cross midnight UTC — the case the old `hour==0 and minute==0` reset missed.
    (datetime(2026, 10, 29, 23, 59, 30, tzinfo=UTC), []),
    (datetime(2026, 10, 30, 0, 0, 30, tzinfo=UTC), []),
    (datetime(2026, 10, 30, 14, 30, tzinfo=UTC), ["cutoff"]),
    (datetime(2026, 10, 30, 15, 5, tzinfo=UTC), ["training", "scoring"]),
    (datetime(2026, 10, 30, 16, 0, tzinfo=UTC), []),
]

names = {"cutoff_data_task": "cutoff", "train_model_task": "training"}
state = {"tick": 0, "fired": []}
results = []


class FakeDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = ticks[min(state["tick"], len(ticks) - 1)][0]
        return value.astimezone(tz) if tz else value


def fake_enqueue(task):
    state["fired"].append(names.get(getattr(task, "__name__", ""), "?"))


def fake_score(_target):
    state["fired"].append("scoring")
    return {"scored": 0, "skipped": 0, "errors": 0}


def fake_sleep(_seconds):
    results.append((ticks[state["tick"]][0], list(state["fired"])))
    state["fired"].clear()
    state["tick"] += 1
    if state["tick"] >= len(ticks):
        raise SystemExit


sched.datetime = FakeDateTime
sched.time.sleep = fake_sleep
sched.job_queue.enqueue = fake_enqueue
serving.score_all_students_for_date = fake_score

try:
    sched.daily_scheduler()
except SystemExit:
    pass

failures = 0
for (when, got), (_, want) in zip(results, ticks):
    if got != want:
        failures += 1
        print(f"FAIL {when:%Y-%m-%d %H:%M:%S}Z got {got} want {want}")

if len(results) != len(ticks):
    failures += 1
    print(f"FAIL ran {len(results)} of {len(ticks)} ticks")

print(f"{'FAIL' if failures else 'PASS'}: {len(ticks)} ticks, {failures} mismatch")
sys.exit(1 if failures else 0)
