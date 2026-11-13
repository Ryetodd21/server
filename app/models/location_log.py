import uuid
from datetime import datetime, timezone
from sqlalchemy import String, ForeignKey, Boolean, Float
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LocationLog(Base):
    """Log lokasi per session."""
    __tablename__ = "location_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("students.id", ondelete="CASCADE"))
    timestamp: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    is_on_campus: Mapped[bool] = mapped_column(Boolean, default=False)
    matches_schedule: Mapped[bool] = mapped_column(Boolean, default=False)
    synced: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    # Mobile app fields
    recorded_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=True)  # from mobile app
    campus_id: Mapped[str] = mapped_column(String(36), nullable=True)
    schedule_id: Mapped[str] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=True)
