import uuid
from datetime import datetime, timezone
from sqlalchemy import String, ForeignKey, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Student(Base):
    """Profil mahasiswa."""
    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Nullable: a Student record can exist without a User account (legacy import,
    # graduated-but-still-tracked, etc). When present, the user can log in to the
    # mobile app. The POST /api/students endpoint also supports auto-creating a
    # User row via create_user_account=true so a single admin/dosen call can
    # register both halves of the relationship.
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    nim: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(150))
    program_studi: Mapped[str] = mapped_column(String(100), default="")
    angkatan: Mapped[int] = mapped_column(default=2024)
    semester: Mapped[int] = mapped_column(default=1)
    advisor_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, default=None)
    phone: Mapped[str] = mapped_column(String(30), default="")
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)

    # Relations — annotation matches the non-null declared on User.student (inverse).
    # The FK column itself is nullable; SQLAlchemy 2.0 doesn't require the annotation
    # to be Optional[str] for a nullable FK — the column-level nullable=True suffices.
    user: Mapped["User"] = relationship("User", back_populates="student", foreign_keys=[user_id])
    advisor: Mapped["User"] = relationship("User", foreign_keys=[advisor_id])
    schedules: Mapped[list["Schedule"]] = relationship("Schedule", back_populates="student")
