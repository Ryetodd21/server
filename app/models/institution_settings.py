import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InstitutionSettings(Base):
    """
    Singleton table — only ever one row (id is fixed, not auto-generated).
    Stores institution-wide profile info used across all users.
    """
    __tablename__ = "institution_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Display name shown in header/landing, e.g. "Universitas Negeri Gorontalo"
    university_name: Mapped[str] = mapped_column(String(200), default="")
    # Faculty/department affiliation of the admin managing this instance
    faculty: Mapped[str] = mapped_column(String(200), default="")
    # Study program, e.g. "S1 Pendidikan Teknik Informatika"
    program: Mapped[str] = mapped_column(String(200), default="")
    # Campus timezone string, e.g. "WITA (UTC+8)"
    time_zone: Mapped[str] = mapped_column(String(80), default="WITA (UTC+8)")
    # Base64 data URL for the institution logo
    logo_data_url: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)
