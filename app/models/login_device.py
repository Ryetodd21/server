import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LoginDevice(Base):
    """One row per (user, device_id) — the device_id is a deterministic hash
    the client computes from UA + screen + locale. Same physical browser
    always produces the same device_id, so dedupe is automatic: on every
    login the row is updated in place (last_seen, is_current=True) instead
    of inserted again.

    `is_current` flips true on the most recent login and false on all
    siblings — so the UI can show "(Perangkat ini)" next to the row
    matching the request that hit GET /api/auth/devices.
    """

    __tablename__ = "login_devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 of (UA + screen + locale), truncated to 32 chars
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    # Parsed friendly name e.g. "Chrome di Windows"
    device_name: Mapped[str] = mapped_column(String(200), default="")
    # Full UA for diagnostic display
    user_agent: Mapped[str] = mapped_column(String(500), default="")
    first_seen: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    last_seen: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
