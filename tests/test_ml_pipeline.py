"""Tests for the ML training gates: minimum dataset size and holdout
(open-set) evaluation before a model is allowed to be marked `completed`.

These run the real `train_pipeline` against an in-memory SQLite database
populated with synthetic app_logs, so they exercise the actual SQL
aggregation + StandardScaler/PCA/KMeans path rather than mocking it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database import Base
from app.ml.pipeline import (
    _check_holdout_gate,
    _cluster_quality_metrics,
    build_feature_dataframe,
    train_pipeline,
)
from app.models.app_log import AppLog
from app.models.student import Student


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _add_students(session: Session, n: int) -> list[str]:
    ids = []
    for i in range(n):
        sid = str(uuid.uuid4())
        session.add(Student(id=sid, nim=f"NIM{i:04d}", full_name=f"Student {i}"))
        ids.append(sid)
    return ids


def _add_log(
    session: Session,
    student_id: str,
    day: datetime,
    *,
    screen: int,
    sosmed: int,
    game: int,
    unlocks: int,
    in_class: bool = False,
) -> None:
    session.add(
        AppLog(
            id=str(uuid.uuid4()),
            student_id=student_id,
            timestamp=day,
            log_date=day,
            total_screen_time_minutes=screen,
            social_media_time_minutes=sosmed,
            game_time_minutes=game,
            unlock_count=unlocks,
            used_during_class=in_class,
            source="seed",
        )
    )


def _seed_varied_dataset(session: Session, students: int = 12, days: int = 4) -> int:
    """Seed a dataset with real spread across the feature space.

    Three behavioural archetypes (low / medium / high distraction) keep the
    clusters separable, so a correct implementation should pass the holdout
    gate. Returns the number of (student, date) rows created.
    """
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    ids = _add_students(session, students)
    rows = 0
    for idx, sid in enumerate(ids):
        archetype = idx % 3
        for d in range(days):
            day = base + timedelta(days=d)
            if archetype == 0:
                screen, sosmed, game, unlocks, in_class = 60, 10, 5, 2, False
            elif archetype == 1:
                screen, sosmed, game, unlocks, in_class = 240, 90, 45, 8, True
            else:
                screen, sosmed, game, unlocks, in_class = 460, 170, 90, 16, True
            # Jitter so points are not identical (identical points break
            # silhouette) while staying inside the archetype's neighbourhood.
            jitter = (idx * 7 + d * 13) % 17
            _add_log(
                session,
                sid,
                day,
                screen=screen + jitter,
                sosmed=sosmed + jitter // 2,
                game=game + jitter // 3,
                unlocks=unlocks + jitter % 4,
                in_class=in_class,
            )
            rows += 1
    session.commit()
    return rows


# ─── Minimum dataset gate ──────────────────────────────────────────────────


def test_train_rejects_dataset_below_minimum(session, monkeypatch):
    """A handful of rows must be refused outright, not silently modelled."""
    monkeypatch.setattr(get_settings(), "ML_MIN_DATASET_SIZE", 30)
    ids = _add_students(session, 3)
    day = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    for sid in ids:
        _add_log(session, sid, day, screen=100, sosmed=20, game=10, unlocks=4)
    session.commit()

    with pytest.raises(ValueError) as exc:
        train_pipeline(session, pca_n=2, k_n=3)
    msg = str(exc.value)
    assert "butuh minimal 30" in msg
    assert "student-date rows" in msg


def test_train_rejects_empty_dataset(session):
    with pytest.raises(ValueError) as exc:
        train_pipeline(session, pca_n=2, k_n=3)
    assert "No log data available" in str(exc.value)


def test_min_dataset_gate_uses_settings(session, monkeypatch):
    """The floor must come from Settings, not a hardcoded constant."""
    monkeypatch.setattr(get_settings(), "ML_MIN_DATASET_SIZE", 1000)
    _seed_varied_dataset(session, students=4, days=2)  # 8 rows
    with pytest.raises(ValueError) as exc:
        train_pipeline(session, pca_n=2, k_n=3)
    assert "butuh minimal 1000" in str(exc.value)


# ─── Holdout evaluation ────────────────────────────────────────────────────


def test_train_returns_holdout_metrics(session, monkeypatch):
    """A healthy dataset must yield train + holdout metrics in the result."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ML_MIN_DATASET_SIZE", 10)
    monkeypatch.setattr(settings, "ML_MIN_HOLDOUT_SILHOUETTE", 0.0)
    _seed_varied_dataset(session, students=12, days=4)

    result = train_pipeline(session, pca_n=2, k_n=3)

    holdout = result["holdout_metrics"]
    train = result["train_metrics"]
    assert holdout["n_samples"] == result["test_size"]
    assert train["n_samples"] == result["train_size"]
    assert holdout["silhouette"] is not None
    assert train["silhouette"] is not None
    # All three internal indices are present and finite.
    assert holdout["davies_bouldin"] is not None
    assert holdout["calinski_harabasz"] is not None
    assert result["holdout_min_silhouette"] == 0.0
    # The persisted training-partition silhouette is still reported.
    assert result["silhouette_score"] is not None


def test_train_fails_when_holdout_silhouette_below_gate(session, monkeypatch):
    """A gate above the achievable score must abort training."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ML_MIN_DATASET_SIZE", 10)
    monkeypatch.setattr(settings, "ML_MIN_HOLDOUT_SILHOUETTE", 0.99)
    _seed_varied_dataset(session, students=12, days=4)

    with pytest.raises(ValueError) as exc:
        train_pipeline(session, pca_n=2, k_n=3)
    msg = str(exc.value)
    assert "gagal evaluasi holdout" in msg
    assert "0.99" in msg


def test_holdout_gate_disabled_when_threshold_none(session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ML_MIN_DATASET_SIZE", 10)
    monkeypatch.setattr(settings, "ML_MIN_HOLDOUT_SILHOUETTE", None)
    _seed_varied_dataset(session, students=12, days=4)

    result = train_pipeline(session, pca_n=2, k_n=3)
    assert result["holdout_min_silhouette"] is None
    assert result["holdout_metrics"]["silhouette"] is not None


# ─── Gate helper semantics ─────────────────────────────────────────────────


def test_gate_passes_when_threshold_none():
    ok, reason = _check_holdout_gate({"silhouette": -0.9}, None)
    assert ok is True and reason == ""


def test_gate_skips_when_no_holdout_split():
    ok, reason = _check_holdout_gate({"silhouette": 0.0}, 0.5, has_holdout=False)
    assert ok is True
    assert "dilewati" in reason


def test_gate_skips_when_silhouette_undefined():
    """A degenerate holdout is not a failure — it is unknown, not bad."""
    ok, reason = _check_holdout_gate({"silhouette": None}, 0.5)
    assert ok is True
    assert "tidak dapat dihitung" in reason


def test_gate_blocks_below_threshold():
    ok, reason = _check_holdout_gate({"silhouette": 0.04}, 0.10)
    assert ok is False
    assert "0.04" in reason and "0.1" in reason


def test_gate_allows_equal_threshold():
    ok, _ = _check_holdout_gate({"silhouette": 0.10}, 0.10)
    assert ok is True


# ─── Metric helper robustness ──────────────────────────────────────────────


def test_metrics_none_for_degenerate_single_cluster():
    import numpy as np

    X = np.array([[0.0, 1.0], [1.0, 0.0], [2.0, 2.0]])
    labels = np.array([0, 0, 0])
    metrics = _cluster_quality_metrics(X, labels, k_n=3)
    assert metrics["n_clusters_present"] == 1
    assert metrics["silhouette"] is None
    assert metrics["davies_bouldin"] is None


def test_metrics_none_when_k_is_one():
    import numpy as np

    X = np.array([[0.0], [1.0], [2.0]])
    labels = np.array([0, 0, 0])
    metrics = _cluster_quality_metrics(X, labels, k_n=1)
    assert metrics["silhouette"] is None


# ─── Feature aggregation parity with serving ───────────────────────────────


def test_feature_dataframe_uses_max_not_sum(session):
    """Multiple same-day rows must not inflate the day total (Round 26 bug)."""
    sid = _add_students(session, 1)[0]
    day = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    # Three rows for the same (student, date), each carrying the day total.
    for screen in (120, 200, 240):
        _add_log(session, sid, day, screen=screen, sosmed=30, game=15, unlocks=6)
    session.commit()

    df = build_feature_dataframe(session)
    assert len(df) == 1
    assert df.iloc[0]["screen_time"] == 240
    assert df.iloc[0]["unlock_count"] == 6


def test_cluster_distribution_covers_full_dataset(session, monkeypatch):
    """cluster_distribution must sum to dataset_size, not just train_size.

    The dashboard reads this dict to show per-cluster totals next to
    `dataset_size`. Counting only `train_labels` made the numbers silently
    disagree (e.g. 38 shown for a 48-row model).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "ML_MIN_DATASET_SIZE", 10)
    monkeypatch.setattr(settings, "ML_MIN_HOLDOUT_SILHOUETTE", 0.0)
    rows = _seed_varied_dataset(session, students=12, days=4)

    result = train_pipeline(session, pca_n=2, k_n=3)

    assert result["dataset_size"] == rows
    assert sum(result["cluster_distribution"].values()) == result["dataset_size"]
    # Every row is also emitted as a clustering result.
    assert len(result["clustering_results"]) == result["dataset_size"]


def test_holdout_split_preserves_all_rows(session, monkeypatch):
    """train_size + test_size must equal dataset_size (no rows lost)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ML_MIN_DATASET_SIZE", 10)
    monkeypatch.setattr(settings, "ML_MIN_HOLDOUT_SILHOUETTE", 0.0)
    rows = _seed_varied_dataset(session, students=12, days=4)

    result = train_pipeline(session, pca_n=2, k_n=3)
    assert result["train_size"] + result["test_size"] == rows


def test_train_and_serve_aggregate_log_date_null_identically(session):
    """Trainer and serving must agree when `log_date` is NULL.

    The trainer derives the day with `coalesce(log_date, timestamp)`; serving
    used to filter on bare `date(log_date)` and silently dropped those rows.
    That made the model score a student on a different feature vector than the
    one it was fitted on. Both paths must now produce the same numbers.
    """
    from app.ml.serving import aggregate_student_features_sync

    sid = _add_students(session, 1)[0]
    day = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    # Row with log_date set.
    _add_log(session, sid, day, screen=100, sosmed=10, game=5, unlocks=3)
    # Same day, log_date NULL (legacy seed.py / MQTT payload without the field).
    session.add(
        AppLog(
            id=str(uuid.uuid4()),
            student_id=sid,
            timestamp=day,
            log_date=None,
            total_screen_time_minutes=300,
            social_media_time_minutes=50,
            game_time_minutes=20,
            unlock_count=9,
            used_during_class=True,
            source="seed",
        )
    )
    session.commit()

    train_df = build_feature_dataframe(session)
    serve_features = aggregate_student_features_sync(session, sid, day.date())

    assert serve_features is not None
    assert train_df.iloc[0]["screen_time"] == 300  # MAX across both rows
    assert serve_features["screen_time"] == 300
    assert train_df.iloc[0]["unlock_count"] == 9
    assert serve_features["unlock_count"] == 9
    assert serve_features["sosmed_time"] == 50
    assert serve_features["game_time"] == 20

