from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.program_studi import ProgramStudi, Jurusan
from app.models.student import Student
from app.schemas.program_studi import (
    ProgramStudiCreate,
    ProgramStudiUpdate,
    ProgramStudiResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/program-studi", tags=["Program Studi"])


async def get_admin_only(request: Request) -> dict:
    payload = await get_current_user(request)
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Hanya admin yang dapat mengelola program studi",
        )
    return payload


@router.get("", response_model=list[ProgramStudiResponse])
async def list_program_studi(
    jurusan_id: str | None = Query(default=None, description="Filter by parent jurusan"),
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """List active program studi. Public for registration dropdown —
    unauthenticated callers (the /register page) need to populate the
    program_studi dropdown without triggering the apiFetch 401 → refresh →
    window.location.href='/login' cycle that previously bounced the user
    off the form ~3s after mount.

    Authenticated callers still get the same data; only write endpoints
    below enforce the admin role.
    """
    stmt = select(ProgramStudi).order_by(ProgramStudi.code)
    if jurusan_id:
        stmt = stmt.where(ProgramStudi.jurusan_id == jurusan_id)
    if not include_inactive:
        stmt = stmt.where(ProgramStudi.is_active.is_(True))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=ProgramStudiResponse, status_code=201)
async def create_program_studi(
    data: ProgramStudiCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    # code is unique — reject duplicates case-insensitively (PostgreSQL collation
    # may be case-insensitive depending on locale; normalize both sides so
    # "PTI", "pti", and "Pti" all conflict with each other).
    existing = await db.execute(
        select(ProgramStudi).where(func.lower(ProgramStudi.code) == data.code.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Code '{data.code}' sudah digunakan",
        )
    # Validate parent jurusan exists when supplied
    if data.jurusan_id:
        parent = await db.execute(
            select(Jurusan).where(Jurusan.id == data.jurusan_id)
        )
        if parent.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=404,
                detail="Jurusan tidak ditemukan",
            )
    row = ProgramStudi(**data.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/{prodi_id}", response_model=ProgramStudiResponse)
async def update_program_studi(
    prodi_id: str,
    data: ProgramStudiUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    result = await db.execute(select(ProgramStudi).where(ProgramStudi.id == prodi_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Program studi tidak ditemukan")

    updates = data.model_dump(exclude_unset=True)
    if "code" in updates:
        # Check duplicate code when renaming — case-insensitive
        dup = await db.execute(
            select(ProgramStudi).where(
                func.lower(ProgramStudi.code) == updates["code"].lower(),
                ProgramStudi.id != prodi_id,
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Code '{updates['code']}' sudah digunakan",
            )
    if "jurusan_id" in updates and updates["jurusan_id"]:
        parent = await db.execute(
            select(Jurusan).where(Jurusan.id == updates["jurusan_id"])
        )
        if parent.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=404,
                detail="Jurusan tidak ditemukan",
            )
    for field, value in updates.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{prodi_id}", status_code=204)
async def delete_program_studi(
    prodi_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    """Soft-delete (deactivate) a program studi row.

    Hard delete returns 409 if any student still references the code in
    `students.program_studi` (free-text column — see comments in router).

    Response codes:
    - 200 + body `{"detail": "Dinonaktifkan", "referenced_by_students": N}` when
      soft-deleted (data preserved, recoverable).
    - 204 (empty body) when hard-deleted (no references).
    """
    result = await db.execute(select(ProgramStudi).where(ProgramStudi.id == prodi_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Program studi tidak ditemukan")

    # Check active references via the legacy free-text column.
    ref_count = await db.execute(
        select(func.count(Student.id)).where(Student.program_studi == row.name)
    )
    referenced = ref_count.scalar() or 0
    if referenced > 0:
        # Soft delete (toggle is_active=false) keeps the row but hides it
        # from the default list. Admin can purge later from a separate path.
        row.is_active = False
        await db.commit()
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content={"detail": "Dinonaktifkan", "referenced_by_students": referenced},
        )

    # No references — hard delete.
    await db.delete(row)
    await db.commit()
    return None
