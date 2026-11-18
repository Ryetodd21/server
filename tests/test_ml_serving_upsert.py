"""Regression: `upsert_cluster_result_sync` must update every column.

A missing `existing.pca_z = ...` assignment in the update branch meant that
re-scoring an existing (student, model, date) row left the 3D scatter Z
coordinate frozen at its first-write value while X and Y moved. The insert
branch always set it, so only the update path drifted — invisible until the
3D plot was compared against a fresh score.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.ml.serving import upsert_cluster_result_sync
from app.models.cluster_result import ClusterResult
from app.models.model_version import ModelVersion
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


class _FakeModel:
    """Minimal stand-in — only the identity fields upsert reads."""

    version_id = "model-1"
    version_number = 1


def _prediction(label: int, name: str, score: float, x: float, y: float, z: float):
    return {
        "cluster_label": label,
        "cluster_name": name,
        "distraction_score": score,
        "pca_x": x,
        "pca_y": y,
        "pca_z": z,
        "features_used": {"screen_time": float(label)},
    }


def _seed(session: Session) -> str:
    sid = str(uuid.uuid4())
    session.add(Student(id=sid, nim=f"NIM{sid[:6]}", full_name="Test Student"))
    session.add(
        ModelVersion(
            id="model-1",
            version_number=1,
            status="completed",
            is_active=True,
        )
    )
    session.commit()
    return sid


def test_update_branch_writes_pca_z(session):
    sid = _seed(session)
    target = date(2026, 3, 1)

    upsert_cluster_result_sync(
        session, _FakeModel(), sid, target, _prediction(0, "Rendah", 0.1, 1.0, 2.0, 3.0)
    )
    session.commit()

    upsert_cluster_result_sync(
        session, _FakeModel(), sid, target, _prediction(1, "Tinggi", 0.9, 9.0, 8.0, 7.0)
    )
    session.commit()

    row = session.query(ClusterResult).one()
    assert row.pca_x == 9.0
    assert row.pca_y == 8.0
    assert row.pca_z == 7.0, "pca_z must follow pca_x/pca_y on re-score"
    assert row.cluster_label == 1
    assert row.cluster_name == "Tinggi"
    assert row.distraction_score == 0.9


def test_upsert_is_idempotent_not_duplicating(session):
    """Re-scoring the same key must update in place, not insert a second row."""
    sid = _seed(session)
    target = date(2026, 3, 1)
    for _ in range(3):
        upsert_cluster_result_sync(
            session, _FakeModel(), sid, target, _prediction(2, "Sedang", 0.5, 0.0, 0.0, 0.0)
        )
        session.commit()
    assert session.query(ClusterResult).count() == 1


def test_insert_branch_sets_pca_z(session):
    sid = _seed(session)
    upsert_cluster_result_sync(
        session,
        _FakeModel(),
        sid,
        date(2026, 3, 2),
        _prediction(1, "Tinggi", 0.8, 4.0, 5.0, 6.0),
    )
    session.commit()
    row = session.query(ClusterResult).one()
    assert (row.pca_x, row.pca_y, row.pca_z) == (4.0, 5.0, 6.0)
