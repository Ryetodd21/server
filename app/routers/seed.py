"""
Seed Data Router — Admin only.

POST /api/seed/generate — Generate seed students + ~10k logs
DELETE /api/seed/clear   — Clear all seed data
GET  /api/seed/status    — Check seed data status
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.database import get_sync_engine
from app.seed_data import generate_seed, clear_seed_data, STUDENTS
from app.routers.auth import get_current_user

router = APIRouter(prefix="/seed", tags=["Seed"])


async def require_admin(request: Request) -> dict:
    payload = await get_current_user(request)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Hanya admin yang bisa mengakses seed data.")
    return payload


class SeedStatusResponse(BaseModel):
    students_in_definition: int
    students_in_db: int
    logs_in_db: int
    logs_from_seed: int
    seed_data_ready: bool


class SeedGenerateResponse(BaseModel):
    students_created: int
    users_created: int
    logs_created: int
    total_logs: int
    students_skipped: int
    days: int


class SeedClearResponse(BaseModel):
    logs_cleared: int
    students_cleared: int


@router.get("/status", response_model=SeedStatusResponse)
async def seed_status(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Check current seed data status."""
    engine = get_sync_engine()
    with Session(engine) as db:
        students_in_db = db.execute(text("SELECT COUNT(*) FROM students")).scalar() or 0
        logs_in_db = db.execute(text("SELECT COUNT(*) FROM app_logs")).scalar() or 0
        logs_from_seed = db.execute(
            text("SELECT COUNT(*) FROM app_logs WHERE source = 'seed'")
        ).scalar() or 0

    return SeedStatusResponse(
        students_in_definition=len(STUDENTS),
        students_in_db=students_in_db,
        logs_in_db=logs_in_db,
        logs_from_seed=logs_from_seed,
        seed_data_ready=logs_from_seed >= 9000,
    )


@router.post("/generate", response_model=SeedGenerateResponse)
async def seed_generate(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Generate seed data: 15 students + ~10,000 app logs."""
    engine = get_sync_engine()
    with Session(engine) as db:
        try:
            result = generate_seed(db)
        except Exception as e:
            logging.getLogger("monitor").exception("[seed/generate] failed")
            raise HTTPException(status_code=500, detail="Seed gagal")

    return SeedGenerateResponse(**result)


@router.delete("/clear", response_model=SeedClearResponse)
async def seed_clear(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Clear all seed data (students, users, logs)."""
    engine = get_sync_engine()
    with Session(engine) as db:
        try:
            result = clear_seed_data(db)
        except Exception as e:
            logging.getLogger("monitor").exception("[seed/clear] failed")
            raise HTTPException(status_code=500, detail="Clear gagal")

    return SeedClearResponse(**result)
