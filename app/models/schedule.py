import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, ForeignKey, Integer, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Subject(Base):
    """Mata Kuliah (Subject) directory."""
    __tablename__ = "subjects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(50))  # e.g. "IF101"
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)


class Lecturer(Base):
    """Dosen directory."""
    __tablename__ = "lecturers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    nidn: Mapped[str] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("students.id", ondelete="CASCADE"))

    subject_id: Mapped[str] = mapped_column(String(36), ForeignKey("subjects.id"), nullable=True)
    lecturer_id: Mapped[str] = mapped_column(String(36), ForeignKey("lecturers.id"), nullable=True)

    # Free-text fallback (if subject/lecturer not yet in directory)
    subject_name_raw: Mapped[str] = mapped_column(String(200), nullable=True)
    lecturer_name_raw: Mapped[str] = mapped_column(String(200), nullable=True)

    day_of_week: Mapped[int] = mapped_column()  # 0=Senin, 6=Minggu
    start_time: Mapped[str] = mapped_column(String(5))  # "07:30"
    end_time: Mapped[str] = mapped_column(String(5))
    campus_id: Mapped[str] = mapped_column(String(36), ForeignKey("campuses.id"), nullable=True)
    room: Mapped[str] = mapped_column(String(100), nullable=True)

    reminder_h1: Mapped[bool] = mapped_column(default=True)
    reminder_h3: Mapped[bool] = mapped_column(default=True)
    reminder_h1_before: Mapped[bool] = mapped_column(default=True)

    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)

    # Relations
    student: Mapped["Student"] = relationship("Student", back_populates="schedules")
    subject: Mapped["Subject"] = relationship("Subject")
    lecturer: Mapped["Lecturer"] = relationship("Lecturer")
    campus: Mapped["Campus"] = relationship("Campus")
