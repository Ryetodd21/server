import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, ForeignKey, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(150), default="")
    # Jabatan: admin | dosen | mahasiswa | kaprodi | dekan | etc.
    jabatan: Mapped[str] = mapped_column(String(80), default="")
    # Jurusan / program studi affiliation
    jurusan: Mapped[str] = mapped_column(String(120), default="")
    # NIP — Nomor Induk Pegawai (for dosen/staff); NIM for mahasiswa handled separately
    nip: Mapped[str] = mapped_column(String(40), default="")
    # Phone number for contact
    phone: Mapped[str] = mapped_column(String(30), default="")
    # Avatar URL or base64
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    # Bio / description
    bio: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(20), default="mahasiswa")  # admin, dosen, mahasiswa
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime] = mapped_column(AwareDateTime, nullable=True)
    # Round 27 — Firebase Cloud Messaging device token. The Android client
    # pushes the latest token here whenever Firebase refreshes it (see
    # `FirebaseMessagingService.onNewToken` on the Android side). Stored
    # as one-per-user (latest wins); a `None` value means the user has
    # signed out and we should not push to this device. Length cap: FCM
    # tokens are typically ~160 chars but the FCM v1 spec allows up to
    # 4096 — Text column has no realistic limit.
    fcm_token: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)

    # Relations
    student: Mapped["Student"] = relationship(
        "Student", back_populates="user", uselist=False, foreign_keys="Student.user_id"
    )
    tokens: Mapped[list["Token"]] = relationship("Token", back_populates="user", cascade="all, delete-orphan")


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    token: Mapped[str] = mapped_column(String(500), index=True)
    token_type: Mapped[str] = mapped_column(String(20))  # access | refresh
    expires_at: Mapped[datetime] = mapped_column(AwareDateTime)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)

    user: Mapped["User"] = relationship("User", back_populates="tokens")
