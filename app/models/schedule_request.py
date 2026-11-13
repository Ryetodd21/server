import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, ForeignKey, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ScheduleRequest(Base):
    """
    Request jadwal oleh mahasiswa.
    request_type: "create" | "edit" | "delete" | "lecturer_fix"
    status: "pending" | "approved" | "rejected"
    """
    __tablename__ = "schedule_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    requester_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("students.id"), nullable=True)

    # For "edit" / "delete" — references existing schedule
    schedule_id: Mapped[str] = mapped_column(String(36), ForeignKey("schedules.id"), nullable=True)

    # For "create" — full schedule data embedded
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=True)
    start_time: Mapped[str] = mapped_column(String(5), nullable=True)
    end_time: Mapped[str] = mapped_column(String(5), nullable=True)
    room: Mapped[str] = mapped_column(String(100), nullable=True)
    campus_id: Mapped[str] = mapped_column(String(36), ForeignKey("campuses.id"), nullable=True)
    subject_id: Mapped[str] = mapped_column(String(36), ForeignKey("subjects.id"), nullable=True)
    lecturer_id: Mapped[str] = mapped_column(String(36), ForeignKey("lecturers.id"), nullable=True)
    subject_name_raw: Mapped[str] = mapped_column(String(200), nullable=True)
    lecturer_name_raw: Mapped[str] = mapped_column(String(200), nullable=True)

    request_type: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default="pending")
    admin_notes: Mapped[str] = mapped_column(Text, nullable=True)
    processed_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)

    # Relations
    student: Mapped["Student"] = relationship("Student", foreign_keys=[student_id])
    subject: Mapped["Subject"] = relationship("Subject", foreign_keys=[subject_id])
    lecturer: Mapped["Lecturer"] = relationship("Lecturer", foreign_keys=[lecturer_id])
    campus: Mapped["Campus"] = relationship("Campus", foreign_keys=[campus_id])
