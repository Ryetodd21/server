from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.jadwal_mata_kuliah import JadwalMataKuliah
from app.models.mata_kuliah import MataKuliah
from app.schemas.jadwal_mata_kuliah import (
    JadwalMataKuliahCreate,
    JadwalMataKuliahUpdate,
    JadwalMataKuliahResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/jadwal-mata-kuliah", tags=["Jadwal Mata Kuliah"])


async def require_staff(request: Request) -> dict:
    payload = await get_current_user(request)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Hanya admin/dosen yang boleh mengelola jadwal")
    return payload


@router.get("", response_model=list[JadwalMataKuliahResponse])
async def list_jadwal(
    mata_kuliah_id: str | None = None,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    stmt = select(JadwalMataKuliah).order_by(JadwalMataKuliah.day_of_week, JadwalMataKuliah.start_time)
    if mata_kuliah_id:
        stmt = stmt.where(JadwalMataKuliah.mata_kuliah_id == mata_kuliah_id)
    if not include_inactive:
        stmt = stmt.where(JadwalMataKuliah.is_active.is_(True))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=JadwalMataKuliahResponse, status_code=201)
async def create_jadwal(
    data: JadwalMataKuliahCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_staff),
):
    # Verify the parent mata_kuliah exists so we don't create orphan rows.
    parent = await db.execute(select(MataKuliah.id).where(MataKuliah.id == data.mata_kuliah_id))
    if parent.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Mata kuliah tidak ditemukan")

    # Persist the parsed `time` objects (schema already coerced HH:MM strings).
    row = JadwalMataKuliah(
        mata_kuliah_id=data.mata_kuliah_id,
        day_of_week=data.day_of_week,
        start_time=data.start_time,
        end_time=data.end_time,
        room=data.room.strip(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/{jadwal_id}", response_model=JadwalMataKuliahResponse)
async def update_jadwal(
    jadwal_id: str,
    data: JadwalMataKuliahUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_staff),
):
    result = await db.execute(select(JadwalMataKuliah).where(JadwalMataKuliah.id == jadwal_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Jadwal tidak ditemukan")
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{jadwal_id}", status_code=204)
async def delete_jadwal(
    jadwal_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_staff),
):
    result = await db.execute(select(JadwalMataKuliah).where(JadwalMataKuliah.id == jadwal_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Jadwal tidak ditemukan")
    row.is_active = False
    await db.commit()
    return None
