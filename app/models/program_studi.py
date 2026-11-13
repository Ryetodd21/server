import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base, AwareDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Fakultas(Base):
    """Top-level academic faculty. A fakultas sits above one or more
    `jurusan`, and belongs to one of the four UNG campus locations
    (kampus). Admin-managed; readable by any authenticated user."""

    __tablename__ = "fakultas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text, default="")
    # Soft FK to `campus.id` (string UUID). Nullable so a fakultas can
    # exist without being pinned to a campus until admin decides.
    campus_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)

    jurusans: Mapped[list["Jurusan"]] = relationship(
        "Jurusan", back_populates="fakultas", cascade="all, delete-orphan"
    )


class Jurusan(Base):
    """Academic department / program group. A jurusan sits inside a fakultas
    and contains one or more `program_studi`. Admin-managed; readable by
    any authenticated user."""

    __tablename__ = "jurusan"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Round 15: UNIQUE index added — see run_unique_constraint_migrations() in
    # database.py. The model alone (with unique=True) keeps new INSERTs honest;
    # existing duplicate rows are soft-deleted by the migration before the
    # constraint is enforced.
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text, default="")
    # NOT NULL — the parent fakultas is required. The Pydantic schema mirrors
    # this (JurusanCreate.fakultas_id is no longer optional). Soft-delete on
    # the parent cascades the FK (ON DELETE CASCADE); deletes do not preserve
    # orphaned jurusans.
    fakultas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("fakultas.id", ondelete="CASCADE"), index=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)

    fakultas: Mapped["Fakultas"] = relationship("Fakultas", back_populates="jurusans")
    program_studis: Mapped[list["ProgramStudi"]] = relationship(
        "ProgramStudi", back_populates="jurusan"
    )


class ProgramStudi(Base):
    """Master list of academic programs (program studi) selectable across the
    system — settings, student registration, lecturer affiliation.

    Replaces the previous free-text `jurusan` / `program_studi` string on
    `users` and `students`, which led to inconsistent values ("Informatika"
    vs "Teknik Informatika" vs "IF"). Admins manage this list; everyone
    authenticated can read it.

    Round 8: now sits inside a `jurusan` (department), which itself sits
    inside a `fakultas`. The legacy `jurusan` text on user/student rows
    remains as a denormalised snapshot for backwards compatibility.

    Round 15: orphan prodi (those created without a parent jurusan, e.g. the
    pre-round-12.1 PTI row) are cleaned up via `cleanup_orphan_prodi()` in
    `app/main.py` lifespan. New prodi must have a parent — the dialog on
    /register and the Akademik tab on /akademik both enforce this.
    """

    __tablename__ = "program_studi"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    # FK to `jurusan.id`. Nullable for legacy rows; new rows should always have
    # a parent — see ProgramStudiCreate.jurusan_id + Akademik dialog validation.
    # Round 15: index=True added for the cascade filter query.
    jurusan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jurusan.id", ondelete="SET NULL"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, default=_utc_now, onupdate=_utc_now)

    jurusan: Mapped["Jurusan | None"] = relationship("Jurusan", back_populates="program_studis")
