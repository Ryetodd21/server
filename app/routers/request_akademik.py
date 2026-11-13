from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.request_akademik import RequestAkademik
from app.models.user import User
from app.schemas.request_akademik import (
    RequestAkademikCreate,
    RequestAkademikReview,
    RequestAkademikResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/request-akademik", tags=["Request Akademik"])


async def get_user_id(request: Request) -> str:
    payload = await get_current_user(request)
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Sesi tidak valid")
    return sub


@router.get("", response_model=list[RequestAkademikResponse])
async def list_requests(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
):
    """List requests. Admins/dosen see all (for review); mahasiswa see their own."""
    stmt = select(RequestAkademik).order_by(RequestAkademik.created_at.desc())
    if status:
        stmt = stmt.where(RequestAkademik.status == status)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        stmt = stmt.where(RequestAkademik.requested_by_id == payload.get("sub"))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=RequestAkademikResponse, status_code=201)
async def create_request(
    data: RequestAkademikCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Any authenticated user can submit a request. Admin/dosen review separately."""
    row = RequestAkademik(
        type=data.type,
        name=data.name,
        description=data.description.strip(),
        payload=data.payload.strip(),
        target_id=data.target_id,
        requested_by_id=user_id,
        status="pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/{req_id}/review", response_model=RequestAkademikResponse)
async def review_request(
    req_id: str,
    data: RequestAkademikReview,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
):
    """Approve or reject a pending request. Admin/dosen only.

    Note: approving does NOT auto-create the underlying mata_kuliah / jadwal.
    The admin uses the request as a checklist and creates the row separately
    via /api/mata-kuliah or /api/jadwal-mata-kuliah so they have full
    control over the master data.
    """
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Hanya admin/dosen yang boleh mereview permintaan")

    result = await db.execute(select(RequestAkademik).where(RequestAkademik.id == req_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Permintaan tidak ditemukan")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail=f"Permintaan sudah {row.status}")

    row.status = data.status
    row.review_notes = data.review_notes.strip()
    row.reviewed_by_id = payload.get("sub")
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{req_id}", status_code=204)
async def delete_request(
    req_id: str,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
):
    """Delete a request. Only the requester (own pending requests) or admin
    can delete. Approved/rejected requests stay in the audit log."""
    result = await db.execute(select(RequestAkademik).where(RequestAkademik.id == req_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Permintaan tidak ditemukan")
    role = payload.get("role", "")
    sub = payload.get("sub")
    if role != "admin" and row.requested_by_id != sub:
        raise HTTPException(status_code=403, detail="Tidak boleh menghapus permintaan orang lain")
    await db.delete(row)
    await db.commit()
    return None
