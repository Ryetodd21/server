"""Regression: a failed training run must leave a usable active model.

The holdout gate added in this round makes `train_pipeline` raise `ValueError`
as a *normal* outcome, not an exceptional one. `_run_training` deactivates
every ModelVersion up front, so the failure branches must re-activate the most
recent completed model — otherwise `/api/reports/*` and
`/api/models/results/latest` return 404 until the next successful training.

This test drives `_run_training` directly against an in-memory SQLite DB so it
does not need the full HTTP stack (and so it can force a failing pipeline
without seeding a real dataset).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.model_version import ModelVersion
from app.routers import models as models_router
from app.schemas.model import TrainRequest


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "gate_test.db"


def _make_engines(db_path):
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    return sync_engine, async_engine


def test_failed_training_reactivates_previous_model(db_path, monkeypatch):
    sync_engine, async_engine = _make_engines(db_path)
    SessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def scenario():
        async with SessionLocal() as db:
            # Seed a previously completed + active model (v1).
            prev = ModelVersion(
                id=str(uuid.uuid4()),
                version_number=1,
                training_date=datetime.now(timezone.utc),
                status="completed",
                is_active=True,
                pca_n_components=2,
                kmeans_n_clusters=3,
            )
            db.add(prev)
            await db.commit()

            # Force the holdout gate to fail.
            def boom(*args, **kwargs):
                raise ValueError(
                    "Model gagal evaluasi holdout: silhouette pada data uji = 0.01, "
                    "di bawah ambang minimal 0.99."
                )

            monkeypatch.setattr("app.ml.pipeline.train_pipeline", boom)
            # `_run_training` imports train_pipeline lazily inside the function,
            # so patching the module attribute above is what it will resolve.
            monkeypatch.setattr(models_router, "get_sync_engine", lambda: sync_engine)

            with pytest.raises(Exception):
                await models_router._run_training(
                    TrainRequest(pca_n_components=2, kmeans_n_clusters=3), db
                )

            # A failed MV exists and is not active.
            failed = (
                await db.execute(
                    select(ModelVersion).where(ModelVersion.status == "failed")
                )
            ).scalars().all()
            assert len(failed) == 1
            assert failed[0].is_active is False
            assert "holdout" in (failed[0].error_message or "")

            # The previous model must be active again.
            await db.refresh(prev)
            assert prev.is_active is True

            # Exactly one active model overall.
            active = (
                await db.execute(
                    select(ModelVersion).where(ModelVersion.is_active == True)  # noqa: E712
                )
            ).scalars().all()
            assert len(active) == 1
            assert active[0].id == prev.id

    try:
        asyncio.run(scenario())
    finally:
        sync_engine.dispose()


def test_failed_training_without_previous_model_stays_clean(db_path, monkeypatch):
    """First-ever training failing must not crash or create a phantom active model."""
    sync_engine, async_engine = _make_engines(db_path)
    SessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def scenario():
        async with SessionLocal() as db:
            def boom(*args, **kwargs):
                raise ValueError("Data tidak cukup untuk training (2 rows, butuh minimal 30).")

            monkeypatch.setattr("app.ml.pipeline.train_pipeline", boom)
            monkeypatch.setattr(models_router, "get_sync_engine", lambda: sync_engine)

            with pytest.raises(Exception):
                await models_router._run_training(
                    TrainRequest(pca_n_components=2, kmeans_n_clusters=3), db
                )

            active = (
                await db.execute(
                    select(ModelVersion).where(ModelVersion.is_active == True)  # noqa: E712
                )
            ).scalars().all()
            assert active == []
            failed = (
                await db.execute(
                    select(ModelVersion).where(ModelVersion.status == "failed")
                )
            ).scalars().all()
            assert len(failed) == 1

    try:
        asyncio.run(scenario())
    finally:
        sync_engine.dispose()
