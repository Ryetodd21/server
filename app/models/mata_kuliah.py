import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MataKuliah(Base):
    """Master list of courses (mata kuliah) per program studi.

    Each course belongs to one program_studi and optionally one dosen
    (instructor). Schedules (jam mata kuliah) live in a separate table
    so a single course can have multiple schedule slots per week.

    `campus_id` is REQUIRED — Android needs to know which campus a
    mahasiswa should be on during class time. Geofence attendance
    detection (see `app_logs.geofence_status`) is computed against
    this campus's lat/lon/radius.

    Status: is_active lets admins deactivate without losing history.
    """

    __tablename__ = "mata_kuliah"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # SKS (Satuan Kredit Semester) — credit hours. Integer between 1 and 8.
    sks: Mapped[int] = mapped_column(Integer, default=2)
    # Semester when this course is offered (1-8 for S1).
    semester: Mapped[int] = mapped_column(Integer, default=1)
    # FK to program_studi.id. Soft-fail when the parent is hard-deleted.
    program_studi_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("program_studi.id", ondelete="SET NULL"), nullable=True
    )
    # Optional FK to users.id (dosen). SET NULL when the dosen is removed.
    dosen_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Required FK to campuses.id — lokasi mata kuliah. Geofence check
    # saat attendance ingest (logs.py) pakai campus ini.
    campus_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campuses.id", ondelete="RESTRICT"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)
