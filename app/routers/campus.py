from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from app.database import get_db
from app.models.campus import Campus
from app.models.user import User
from app.schemas.campus import CampusCreate, CampusUpdate, CampusResponse
from app.routers.auth import get_current_user

router = APIRouter(prefix="/campuses", tags=["Campuses"])


async def get_admin_user(request: Request) -> dict:
    payload = await get_current_user(request)
    if payload.get("role") not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return payload


@router.get("", response_model=list[CampusResponse])
async def get_campuses(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    result = await db.execute(select(Campus))
    return result.scalars().all()


@router.post("", response_model=CampusResponse)
async def create_campus(
    data: CampusCreate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_admin_user),
):
    campus = Campus(**data.model_dump())
    db.add(campus)
    await db.commit()
    await db.refresh(campus)
    return campus


@router.put("/{campus_id}", response_model=CampusResponse)
async def update_campus(
    campus_id: str,
    data: CampusUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_admin_user),
):
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    result = await db.execute(update(Campus).where(Campus.id == campus_id).values(**update_data))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Campus not found")
    await db.commit()
    campus_result = await db.execute(select(Campus).where(Campus.id == campus_id))
    campus = campus_result.scalar_one_or_none()
    if not campus:
        raise HTTPException(status_code=404, detail="Campus not found")
    return campus


@router.delete("/{campus_id}", status_code=204)
async def delete_campus(
    campus_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_admin_user),
):
    result = await db.execute(delete(Campus).where(Campus.id == campus_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Campus not found")
    await db.commit()
    return None
