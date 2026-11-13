from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.program_studi import Fakultas, Jurusan
from app.schemas.fakultas import (
    FakultasCreate,
    FakultasUpdate,
    FakultasResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/fakultas", tags=["Fakultas"])


async def get_admin_only(request: Request) -> dict:
    payload = await get_current_user(request)
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Hanya admin yang dapat mengelola fakultas",
        )
    return payload


@router.get("", response_model=list[FakultasResponse])
async def list_fakultas(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """List all fakultas. Any authenticated user can read."""
    stmt = select(Fakultas).order_by(Fakultas.code)
    if not include_inactive:
        stmt = stmt.where(Fakultas.is_active.is_(True))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=FakultasResponse, status_code=201)
async def create_fakultas(
    data: FakultasCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    existing = await db.execute(
        select(Fakultas).where(func.lower(Fakultas.code) == data.code.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Code '{data.code}' sudah digunakan",
        )
    row = Fakultas(**data.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/{fakultas_id}", response_model=FakultasResponse)
async def update_fakultas(
    fakultas_id: str,
    data: FakultasUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    result = await db.execute(select(Fakultas).where(Fakultas.id == fakultas_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Fakultas tidak ditemukan")

    updates = data.model_dump(exclude_unset=True)
    if "code" in updates and updates["code"]:
        dup = await db.execute(
            select(Fakultas).where(
                func.lower(Fakultas.code) == updates["code"].lower(),
                Fakultas.id != fakultas_id,
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


@router.delete("/{fakultas_id}", status_code=204)
async def delete_fakultas(
    fakultas_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_admin_only),
):
    """Soft-delete a fakultas if any jurusan still references it; hard-delete
    otherwise. Mirrors the program_studi and jurusan delete pattern.

    Without this guard, deleting a referenced fakultas would cascade-delete
    all of its jurusans (Jurusan FK uses `ON DELETE CASCADE`), which would
    then SET NULL every prodi.jurusan_id that pointed through them. That
    three-step silent data loss is exactly the round 12.1 orphan-prodi path.

    Response codes:
    - 200 + body `{"detail": "Dinonaktifkan", "referenced_by_jurusan": N}` when
      soft-deleted (data preserved, recoverable).
    - 204 (empty body) when hard-deleted (no references).
    """
    result = await db.execute(select(Fakultas).where(Fakultas.id == fakultas_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Fakultas tidak ditemukan")

    ref_count = await db.execute(
        select(func.count(Jurusan.id)).where(Jurusan.fakultas_id == fakultas_id)
    )
    referenced = ref_count.scalar() or 0
    if referenced > 0:
        row.is_active = False
        await db.commit()
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content={"detail": "Dinonaktifkan", "referenced_by_jurusan": referenced},
        )

    await db.delete(row)
    await db.commit()
    return None
