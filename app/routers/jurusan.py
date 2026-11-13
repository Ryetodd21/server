from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.program_studi import Jurusan, Fakultas, ProgramStudi
from app.schemas.jurusan import (
    JurusanCreate,
    JurusanUpdate,
    JurusanResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/jurusan", tags=["Jurusan"])


async def get_admin_only(request: Request) -> dict:
    payload = await get_current_user(request)
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Hanya admin yang dapat mengelola jurusan",
        )
    return payload


@router.get("", response_model=list[JurusanResponse])
async def list_jurusan(
    fakultas_id: str | None = Query(default=None, description="Filter by fakultas"),
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """List all jurusan. Any authenticated user can read.

    Optional `fakultas_id` filter — useful for cascading dropdowns.
    """
    stmt = select(Jurusan).order_by(Jurusan.code)
    if fakultas_id:
        stmt = stmt.where(Jurusan.fakultas_id == fakultas_id)
    if not include_inactive:
        stmt = stmt.where(Jurusan.is_active.is_(True))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=JurusanResponse, status_code=201)
async def create_jurusan(
    data: JurusanCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    # Round 15: case-insensitive duplicate check (matches the program_studi
    # pattern). The schema's `code_not_empty` validator already normalises to
    # upper-case, but a raw SQL INSERT or a bypass path could insert lower-case
    # — defense in depth.
    existing = await db.execute(
        select(Jurusan).where(func.lower(Jurusan.code) == data.code.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Code '{data.code}' sudah digunakan",
        )
    # Verify the parent fakultas exists. JurusanCreate.fakultas_id is required,
    # so a None value is rejected by Pydantic before reaching here.
    parent = await db.execute(
        select(Fakultas).where(Fakultas.id == data.fakultas_id)
    )
    if parent.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=404,
            detail="Fakultas tidak ditemukan",
        )
    row = Jurusan(**data.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/{jurusan_id}", response_model=JurusanResponse)
async def update_jurusan(
    jurusan_id: str,
    data: JurusanUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    result = await db.execute(select(Jurusan).where(Jurusan.id == jurusan_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Jurusan tidak ditemukan")

    updates = data.model_dump(exclude_unset=True)
    # Round 15: treat explicit empty string the same as missing (exclude_unset
    # already does that for absent keys). Reject any fakultas_id value that
    # doesn't resolve to an existing fakultas — the column is NOT NULL so a
    # silent None would surface as an IntegrityError on commit.
    if "fakultas_id" in updates and updates["fakultas_id"]:
        parent = await db.execute(
            select(Fakultas).where(Fakultas.id == updates["fakultas_id"])
        )
        if parent.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=404,
                detail="Fakultas tidak ditemukan",
            )
    if "code" in updates and updates["code"]:
        dup = await db.execute(
            select(Jurusan).where(
                func.lower(Jurusan.code) == updates["code"].lower(),
                Jurusan.id != jurusan_id,
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Code '{updates['code']}' sudah digunakan",
            )
    for field, value in updates.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{jurusan_id}", status_code=204)
async def delete_jurusan(
    jurusan_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    """Soft-delete a jurusan if any prodi still references it; hard-delete
    otherwise. Mirrors the program_studi/delete pattern.

    Without this guard, deleting a referenced jurusan would either:
    - Crash with FK violation (`ON DELETE RESTRICT` not set, but prodi row has
      `ON DELETE SET NULL` which silently orphans every child).
    - Wipe the parent's children without any audit trail — admins would have
      no way to discover what disappeared and why.

    Response codes:
    - 200 + body `{"detail": "Dinonaktifkan", "referenced_by_prodi": N}` when
      soft-deleted (data preserved, recoverable).
    - 204 (empty body) when hard-deleted (no references).
    """
    result = await db.execute(select(Jurusan).where(Jurusan.id == jurusan_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Jurusan tidak ditemukan")

    # Count referencing program_studi rows. Even though the FK uses
    # ON DELETE SET NULL (so the children survive with nulled parent),
    # the data loss is still silent and unintentional — soft-delete instead
    # so the operator can re-attach the children first.
    ref_count = await db.execute(
        select(func.count(ProgramStudi.id)).where(ProgramStudi.jurusan_id == jurusan_id)
    )
    referenced = ref_count.scalar() or 0
    if referenced > 0:
        row.is_active = False
        await db.commit()
        # Dynamic status so the response can carry the body. FastAPI normally
        # doesn't allow returning a body with status_code=204 — and admins need
        # the count to know why a Jurusan suddenly became non-active.
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content={"detail": "Dinonaktifkan", "referenced_by_prodi": referenced},
        )

    await db.delete(row)
    await db.commit()
    # Hard-delete path: FastAPI default 204 when handler returns None.
    return None
