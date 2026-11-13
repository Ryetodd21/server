import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Float, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Campus(Base):
    __tablename__ = "campuses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100))  # "Kampus 1 UNG", "Kampus 2 UNG", etc.
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    radius_meters: Mapped[float] = mapped_column(Float, default=200.0)  # detection radius
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)
