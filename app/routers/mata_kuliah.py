from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.mata_kuliah import MataKuliah
from app.models.campus import Campus
from app.models.schedule import Subject
from app.schemas.mata_kuliah import MataKuliahCreate, MataKuliahUpdate, MataKuliahResponse
from app.routers.auth import get_current_user

router = APIRouter(prefix="/mata-kuliah", tags=["Mata Kuliah"])


async def require_staff(request: Request) -> dict:
    """Course CRUD is admin/dosen only — mahasiswa should request new courses
    via /api/request-akademik instead of editing the master list directly."""
    payload = await get_current_user(request)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Hanya admin/dosen yang boleh mengelola mata kuliah")
    return payload


async def _get_or_create_subject(db: AsyncSession, code: str, name: str) -> Subject:
    """Mirror mata_kuliah -> subjects table. The Android client loads the
    schedule directory (`GET /api/schedules/directory`) which still queries
    the `subjects` table — without this bridge, courses created in the
    website would never appear in the Android request-jadwal dropdown.

    Idempotent on `(subjects.code)` UNIQUE — if a Subject with the same
    code already exists, we update its `name` so the two tables stay in sync.
    """
    stmt = select(Subject).where(Subject.code == code)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        if existing.name != name:
            existing.name = name
        return existing
    new_subject = Subject(code=code, name=name)
    db.add(new_subject)
    return new_subject


@router.get("", response_model=list[MataKuliahResponse])
async def list_mata_kuliah(
    program_studi_id: str | None = None,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """List mata kuliah. Any authenticated user can read.
    Optional filters: program_studi_id, include_inactive (admin sees soft-deleted).
    """
    stmt = select(MataKuliah, Campus.name).join(
        Campus, MataKuliah.campus_id == Campus.id
    ).order_by(MataKuliah.semester, MataKuliah.code)
    if program_studi_id:
        stmt = stmt.where(MataKuliah.program_studi_id == program_studi_id)
    if not include_inactive:
        stmt = stmt.where(MataKuliah.is_active.is_(True))
    result = await db.execute(stmt)
    rows = result.all()
    # Project (MataKuliah, campus_name) tuples into response shape.
    return [
        MataKuliahResponse.model_validate(
            {**mk.__dict__, "campus_name": campus_name}
        )
        for mk, campus_name in rows
    ]


@router.post("", response_model=MataKuliahResponse, status_code=201)
async def create_mata_kuliah(
    data: MataKuliahCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_staff),
):
    # Verify the campus exists. RESTRICT on the FK would also block the insert
    # but the resulting IntegrityError is uglier than a clean 422 here.
    campus_row = (await db.execute(select(Campus).where(Campus.id == data.campus_id))).scalar_one_or_none()
    if campus_row is None:
        raise HTTPException(
            status_code=422,
            detail=f"Lokasi kampus '{data.campus_id}' tidak ditemukan",
        )

    row = MataKuliah(**data.model_dump())
    db.add(row)
    # Sync to subjects table so Android schedule directory reflects it.
    await _get_or_create_subject(db, data.code, data.name)
    await db.commit()
    await db.refresh(row)
    return MataKuliahResponse.model_validate(
        {**row.__dict__, "campus_name": campus_row.name}
    )


@router.put("/{mk_id}", response_model=MataKuliahResponse)
async def update_mata_kuliah(
    mk_id: str,
    data: MataKuliahUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_staff),
):
    result = await db.execute(select(MataKuliah).where(MataKuliah.id == mk_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Mata kuliah tidak ditemukan")
    updates = data.model_dump(exclude_unset=True)

    # If the new campus_id is set, verify it before mutating.
    new_campus_id = updates.get("campus_id")
    if new_campus_id is not None:
        campus_row = (await db.execute(select(Campus).where(Campus.id == new_campus_id))).scalar_one_or_none()
        if campus_row is None:
            raise HTTPException(
                status_code=422,
                detail=f"Lokasi kampus '{new_campus_id}' tidak ditemukan",
            )
        campus_name = campus_row.name
    else:
        # Pull the existing campus name for the response projection.
        campus_name = (
            await db.execute(select(Campus.name).where(Campus.id == row.campus_id))
        ).scalar_one_or_none()

    for field, value in updates.items():
        setattr(row, field, value)

    # Mirror code/name edits into subjects table so Android dropdown follows.
    await _get_or_create_subject(db, row.code, row.name)

    await db.commit()
    await db.refresh(row)
    return MataKuliahResponse.model_validate(
        {**row.__dict__, "campus_name": campus_name}
    )


@router.delete("/{mk_id}", status_code=204)
async def delete_mata_kuliah(
    mk_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_staff),
):
    """Soft-delete: flips is_active to False. The row stays so historical
    AppLog / Schedule references aren't orphaned. The mirrored Subject
    row is left intact — deleting it would orphan Android dropdown items
    that already reference it via `Schedule.subject_id`.
    """
    result = await db.execute(select(MataKuliah).where(MataKuliah.id == mk_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Mata kuliah tidak ditemukan")
    row.is_active = False
    await db.commit()
    return None
