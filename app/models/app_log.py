import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, ForeignKey, Integer, Boolean, Text, Float, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AppLog(Base):
    """Log penggunaan aplikasi per session."""
    __tablename__ = "app_logs"

    # One row per mobile student, day, and class schedule. Seed data and other sources
    # are excluded from this constraint so historical seed rows (which model
    # multiple sessions per day for ML training) are unaffected.
    #
    # The migration `run_app_logs_daily_id_migration()` creates this index
    # idempotently on startup — new tables get it via Base.metadata.create_all.
    __table_args__ = (
        Index(
            "uq_app_logs_student_date_schedule_mobile",
            "student_id",
            "log_date",
            "schedule_id",
            unique=True,
            sqlite_where=text("source = 'mobile'"),
            postgresql_where=text("source = 'mobile'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("students.id", ondelete="CASCADE"))
    timestamp: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    app_package: Mapped[str] = mapped_column(String(200), nullable=True, default="")
    app_name: Mapped[str] = mapped_column(String(200), nullable=True, default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    category: Mapped[str] = mapped_column(String(50), default="other")
    is_foreground: Mapped[bool] = mapped_column(Boolean, default=True)
    # Mobile sync fields
    log_date: Mapped[datetime] = mapped_column(AwareDateTime, nullable=True)
    total_screen_time_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    social_media_time_minutes: Mapped[int] = mapped_column(Integer, default=0)
    game_time_minutes: Mapped[int] = mapped_column(Integer, default=0)
    other_app_time_minutes: Mapped[int] = mapped_column(Integer, default=0)
    unlock_count: Mapped[int] = mapped_column(Integer, default=0)
    used_during_class: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_id: Mapped[str] = mapped_column(String(36), nullable=True)
    device_id: Mapped[str] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=True)
    is_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    # Track source: "seed" = data seed, "mobile" = from mobile app
    source: Mapped[str] = mapped_column(String(20), default="seed")
    # Geofence attendance detection (round 17-ish). When `used_during_class`
    # is true and the student sent a location in the same batch, the server
    # resolves which campus the student SHOULD be on (via Schedule → MataKuliah
    # → campus_id) and compares against the actual GPS fix. The result
    # ("inside" / "outside" / "unknown") is stored here for the dashboard
    # to render without recomputing on every read.
    campus_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("campuses.id", ondelete="SET NULL"), nullable=True
    )
    geofence_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
