import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, ForeignKey, Integer, Float, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ModelVersion(Base):
    """Versioned ML model (PCA + K-Means)."""
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    version_number: Mapped[int] = mapped_column(Integer, unique=True)
    training_date: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    started_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=True)
    dataset_size: Mapped[int] = mapped_column(Integer, default=0)
    train_size: Mapped[int] = mapped_column(Integer, default=0)
    test_size: Mapped[int] = mapped_column(Integer, default=0)
    pca_n_components: Mapped[int] = mapped_column(Integer, default=3)
    kmeans_n_clusters: Mapped[int] = mapped_column(Integer, default=4)
    inertia: Mapped[float] = mapped_column(Float, nullable=True)
    silhouette_score: Mapped[float] = mapped_column(Float, nullable=True)
    cluster_distribution: Mapped[str] = mapped_column(Text, nullable=True)  # JSON string
    cluster_labels: Mapped[str] = mapped_column(Text, nullable=True)  # JSON string
    pca_model_data: Mapped[str] = mapped_column(Text, nullable=True)
    kmeans_model_data: Mapped[str] = mapped_column(Text, nullable=True)
    scaler_model_data: Mapped[str] = mapped_column(Text, nullable=True)
    feature_columns: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="training")
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
