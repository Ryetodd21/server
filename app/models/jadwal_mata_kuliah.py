import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Integer, ForeignKey, Time
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JadwalMataKuliah(Base):
    """Course schedule slots (jam mata kuliah).

    One mata_kuliah can have multiple slots per week — Monday 08:00-09:40,
    Wednesday 13:00-14:40, etc. The Android client polls this table to
    check whether the user should be in class at the current time.

    Day convention: 0=Minggu, 1=Senin, ..., 6=Sabtu (matches Java's
    Calendar.DAY_OF_WEEK).
    """

    __tablename__ = "jadwal_mata_kuliah"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    mata_kuliah_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mata_kuliah.id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer)  # 0-6
    start_time: Mapped[datetime] = mapped_column(Time)   # WITA local
    end_time: Mapped[datetime] = mapped_column(Time)
    room: Mapped[str] = mapped_column(String(50), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)
