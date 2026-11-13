import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RequestAkademik(Base):
    """User-submitted requests for new mata_kuliah or jadwal_mata_kuliah.

    Single table for both request types — `type` distinguishes them. Admins
    see all requests in their management tab and approve/reject with notes.
    Approved requests can be promoted into mata_kuliah rows manually by the
    admin (the system never auto-creates a course from a request).

    Optional `target_id` lets users request edits to an existing row
    (e.g. change room or move time slot) without disrupting history until
    approved.
    """

    __tablename__ = "request_akademik"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # "course" or "schedule_slot" — discriminator
    type: Mapped[str] = mapped_column(String(20))
    # Human-readable request title (course name, slot name, etc.)
    name: Mapped[str] = mapped_column(String(200))
    # Long justification / details
    description: Mapped[str] = mapped_column(Text, default="")
    # Optional payload — JSON-encoded extra fields (sks, semester, day, time)
    payload: Mapped[str] = mapped_column(Text, default="")
    # If this request modifies an existing row, the row's id is stored here.
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requested_by_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # "pending" | "approved" | "rejected"
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reviewed_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    review_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)
