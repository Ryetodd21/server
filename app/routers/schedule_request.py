from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.schedule import Schedule
from app.models.schedule_request import ScheduleRequest
from app.models.student import Student
from app.models.user import User
from app.schemas.schedule_request import (
    ScheduleRequestCreate, ScheduleRequestApproveReject,
    ScheduleRequestResponse, ScheduleRequestListResponse,
)
from app.routers.auth import get_current_user, get_current_user_id

router = APIRouter(prefix="/schedule-requests", tags=["Schedule Requests"])


async def require_admin_or_dosen(request: Request) -> dict:
    """Require admin or dosen role."""
    payload = await get_current_user(request)
    role = payload.get("role")
    if role not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Admin/Dosen only")
    return payload


def _to_response(req: ScheduleRequest, student_name: str | None = None) -> ScheduleRequestResponse:
    return ScheduleRequestResponse(
        id=req.id,
        requester_id=req.requester_id,
        student_id=req.student_id,
        request_type=req.request_type,
        description=req.description,
        status=req.status,
        admin_notes=req.admin_notes,
        processed_by=req.processed_by,
        processed_at=req.processed_at,
        created_at=req.created_at,
        day_of_week=req.day_of_week,
        start_time=req.start_time,
        end_time=req.end_time,
        room=req.room,
        subject_id=req.subject_id,
        lecturer_id=req.lecturer_id,
        subject_name_raw=req.subject_name_raw,
        lecturer_name_raw=req.lecturer_name_raw,
        campus_id=req.campus_id,
        subject_name=req.subject.name if req.subject else req.subject_name_raw,
        lecturer_name=req.lecturer.name if req.lecturer else req.lecturer_name_raw,
        student_name=student_name,
    )


# ─── User endpoints ────────────────────────────────────────────────────────────

@router.post("", response_model=ScheduleRequestResponse)
async def create_schedule_request(
    data: ScheduleRequestCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Mahasiswa mengajukan request jadwal baru."""
    student_result = await db.execute(select(Student).where(Student.user_id == user_id))
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")

    req = ScheduleRequest(
        requester_id=user_id,
        student_id=student.id,
        request_type="create",
        description=data.description,
        day_of_week=data.day_of_week,
        start_time=data.start_time,
        end_time=data.end_time,
        room=data.room,
        campus_id=data.campus_id,
        subject_id=data.subject_id,
        lecturer_id=data.lecturer_id,
        subject_name_raw=data.subject_name_raw,
        lecturer_name_raw=data.lecturer_name_raw,
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    return _to_response(req, student_name=user.full_name if user else None)


@router.get("/mine", response_model=ScheduleRequestListResponse)
async def get_my_requests(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Mahasiswa melihat request jadwal miliknya."""
    query = (
        select(ScheduleRequest)
        .options(
            selectinload(ScheduleRequest.subject),
            selectinload(ScheduleRequest.lecturer),
        )
        .where(
            ScheduleRequest.requester_id == user_id,
            ScheduleRequest.request_type == "create",
        )
    )
    if status:
        query = query.where(ScheduleRequest.status == status)
    query = query.order_by(ScheduleRequest.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    requests = result.scalars().all()

    # Fetch user names for each request
    requester_ids = {r.requester_id for r in requests}
    users_result = await db.execute(select(User).where(User.id.in_(requester_ids)))
    users = {u.id: u.full_name for u in users_result.scalars().all()}

    return ScheduleRequestListResponse(
        requests=[_to_response(r, student_name=users.get(r.requester_id)) for r in requests],
        total=len(requests),
    )


# ─── Admin/Dosen endpoints ─────────────────────────────────────────────────────

@router.get("", response_model=ScheduleRequestListResponse)
async def list_requests(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
    status: str | None = None,
    request_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Admin/Dosen melihat semua request jadwal."""
    query = (
        select(ScheduleRequest)
        .options(
            selectinload(ScheduleRequest.subject),
            selectinload(ScheduleRequest.lecturer),
        )
        .where(ScheduleRequest.request_type == "create")
    )
    if status:
        query = query.where(ScheduleRequest.status == status)
    if request_type:
        query = query.where(ScheduleRequest.request_type == request_type)
    query = query.order_by(ScheduleRequest.created_at.desc()).limit(limit).offset(offset)

    # Round 26 audit (P1): the count query at the old line 158-160 only
    # filtered on `request_type == "create"`, missing the optional
    # `status` and `request_type` filters the main query applied. With
    # the UI in mind (when `status=approved` is passed), the `total`
    # reported was the count of all `create` requests, not the count
    # matching the visible page. Pagination math drifted silently
    # (page size > total, missing pages, "x of y" off-by-tens). Build
    # the count query from the same filters as the main query.
    count_subq = query.with_only_columns(ScheduleRequest.id).order_by(None).subquery()
    count_q = select(func.count()).select_from(count_subq)
    total_result = await db.execute(count_q)
    total = total_result.scalar() or 0

    result = await db.execute(query)
    requests = result.scalars().all()

    requester_ids = {r.requester_id for r in requests}
    users_result = await db.execute(select(User).where(User.id.in_(requester_ids)))
    users = {u.id: u.full_name for u in users_result.scalars().all()}

    return ScheduleRequestListResponse(
        requests=[_to_response(r, student_name=users.get(r.requester_id)) for r in requests],
        total=total,
    )


@router.post("/{request_id}/approve", response_model=ScheduleRequestResponse)
async def approve_request(
    request_id: str,
    data: ScheduleRequestApproveReject,
    db: AsyncSession = Depends(get_db),
    admin_user: dict = Depends(require_admin_or_dosen),
):
    """Admin/Dosen menyetujui request — jadwal dibuat untuk mahasiswa.

    Row-locks the ScheduleRequest via SELECT ... FOR UPDATE so two concurrent
    approve calls can't both read status='pending' and create duplicate
    Schedule rows. On SQLite (single-writer) the lock is a no-op but the
    transaction still serializes correctly via BEGIN IMMEDIATE.
    """
    req_result = await db.execute(
        select(ScheduleRequest)
        .options(
            selectinload(ScheduleRequest.subject),
            selectinload(ScheduleRequest.lecturer),
        )
        .where(
            ScheduleRequest.id == request_id,
            ScheduleRequest.request_type == "create",
        )
        .with_for_update()
    )
    req = req_result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")

    if not req.student_id:
        raise HTTPException(status_code=400, detail="Request tidak memiliki student_id, tidak bisa disetujui")

    # Wave 2 #1: reject approval with NULL time fields instead of silently
    # fabricating 08:00/10:00 — that would create Schedule rows the requester
    # never asked for. Both fields are nullable in the model (legacy data,
    # manual SQL, etc.), so this guard is the safety net.
    if not req.start_time or not req.end_time:
        raise HTTPException(
            status_code=400,
            detail="Request tidak memiliki start_time atau end_time, tidak bisa disetujui",
        )

    # Create the schedule for the student
    schedule = Schedule(
        student_id=req.student_id,
        day_of_week=req.day_of_week or 1,
        start_time=req.start_time,
        end_time=req.end_time,
        room=req.room,
        campus_id=req.campus_id,
        subject_id=req.subject_id,
        lecturer_id=req.lecturer_id,
        subject_name_raw=req.subject_name_raw,
        lecturer_name_raw=req.lecturer_name_raw,
        is_active=True,
    )
    db.add(schedule)

    req.status = "approved"
    req.processed_by = admin_user.get("sub")
    req.processed_at = datetime.now(timezone.utc)
    req.admin_notes = data.admin_notes

    await db.commit()
    await db.refresh(req)

    user_result = await db.execute(select(User).where(User.id == req.requester_id))
    user = user_result.scalar_one_or_none()
    return _to_response(req, student_name=user.full_name if user else None)


@router.post("/{request_id}/reject", response_model=ScheduleRequestResponse)
async def reject_request(
    request_id: str,
    data: ScheduleRequestApproveReject,
    db: AsyncSession = Depends(get_db),
    admin_user: dict = Depends(require_admin_or_dosen),
):
    """Admin/Dosen menolak request jadwal.

    Row-locks the ScheduleRequest so two concurrent reject calls can't both
    succeed (or fight with a concurrent approve). See approve_request for
    the race-condition rationale.
    """
    req_result = await db.execute(
        select(ScheduleRequest)
        .where(
            ScheduleRequest.id == request_id,
            ScheduleRequest.request_type == "create",
        )
        .with_for_update()
    )
    req = req_result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")

    req.status = "rejected"
    req.processed_by = admin_user.get("sub")
    req.processed_at = datetime.now(timezone.utc)
    req.admin_notes = data.admin_notes

    await db.commit()
    await db.refresh(req)

    user_result = await db.execute(select(User).where(User.id == req.requester_id))
    user = user_result.scalar_one_or_none()
    return _to_response(req, student_name=user.full_name if user else None)
