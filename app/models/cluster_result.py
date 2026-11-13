import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, ForeignKey, Integer, Float, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ClusterResult(Base):
    """Hasil clustering per student per hari."""
    __tablename__ = "cluster_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("students.id", ondelete="CASCADE"))
    model_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("model_versions.id", ondelete="CASCADE"))
    cluster_id: Mapped[int] = mapped_column(Integer)
    cluster_label: Mapped[int] = mapped_column(Integer, default=0)
    cluster_name: Mapped[str] = mapped_column(String(100), default="")
    result_date: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    pca_x: Mapped[float] = mapped_column(Float, nullable=True)
    pca_y: Mapped[float] = mapped_column(Float, nullable=True)
    pca_z: Mapped[float] = mapped_column(Float, nullable=True)
    features: Mapped[str] = mapped_column(Text, nullable=True)
    distraction_score: Mapped[float] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
