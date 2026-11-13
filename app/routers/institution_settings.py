import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.institution_settings import InstitutionSettings
from app.schemas.institution_settings import (
    InstitutionSettingsResponse,
    InstitutionSettingsUpdate,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/institution", tags=["Institution"])

# Fixed singleton row id — never create a second row.
_INSTITUTION_ROW_ID = "00000000-0000-0000-0000-000000000001"


async def _get_or_create_settings(db: AsyncSession) -> InstitutionSettings:
    result = await db.execute(
        select(InstitutionSettings).where(InstitutionSettings.id == _INSTITUTION_ROW_ID)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = InstitutionSettings(id=_INSTITUTION_ROW_ID)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _require_admin(request: Request) -> dict:
    payload = await get_current_user(request)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Hanya admin yang dapat mengubah pengaturan institusi")
    return payload


@router.get("", response_model=InstitutionSettingsResponse)
async def get_institution_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """Any authenticated user can read institution info."""
    settings = await _get_or_create_settings(db)
    return InstitutionSettingsResponse.model_validate(settings)


@router.put("", response_model=InstitutionSettingsResponse)
async def update_institution_settings(
    data: InstitutionSettingsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(_require_admin),
):
    """Admin can update institution info."""
    settings = await _get_or_create_settings(db)
    for field, value in data.model_dump().items():
        if value is not None:
            setattr(settings, field, value)
    await db.commit()
    await db.refresh(settings)
    return InstitutionSettingsResponse.model_validate(settings)
