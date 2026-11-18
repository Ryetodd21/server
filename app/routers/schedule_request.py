import hashlib
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from app.academic_scope import match_program, normalize_academic_name
from app.database import get_db
from app.models.campus import Campus
from app.models.jadwal_mata_kuliah import JadwalMataKuliah
from app.models.mata_kuliah import MataKuliah
from app.models.program_studi import ProgramStudi
from app.models.schedule import Schedule, Subject
from app.models.schedule_request import ScheduleRequest
from app.models.student import Student
from app.models.user import User
from app.schemas.jadwal_mata_kuliah import hhmm_to_time
from app.schemas.schedule_request import (
    ScheduleRequestCreate, ScheduleRequestApproveReject,
    ScheduleRequestResponse, ScheduleRequestListResponse,
)
from app.routers.auth import get_current_user, get_current_user_id
from app.routers.mata_kuliah import _get_or_create_subject
from app.routers.schedule import master_schedule_id

router = APIRouter(prefix="/schedule-requests", tags=["Schedule Requests"])

# _to_response() reads req.subject / req.lecturer. db.refresh() reloads scalar
# columns only, so the relations must be named or the helper lazy-loads them
# outside the greenlet context (MissingGreenlet → HTTP 500 when subject_id or
# lecturer_id is set; null IDs short-circuit and hide the bug).
_REQ_RELATIONS = ["subject", "lecturer"]


async def require_admin_or_dosen(request: Request) -> dict:
    """Require admin or dosen role."""
    payload = await get_current_user(request)
    role = payload.get("role")
    if role not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Admin/Dosen only")
    return payload


def _to_response(
    req: ScheduleRequest,
    student_name: str | None = None,
    published_to_master: bool | None = None,
) -> ScheduleRequestResponse:
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
        published_to_master=published_to_master,
    )


# ─── Publish approved requests into the prodi master schedule ─────────────────

def _synthetic_course_code(name: str) -> str:
    """Stable placeholder code for a course that only arrived as free text.

    `mata_kuliah.code` is required and limited to 20 chars; the hash keeps
    repeated approvals of the same name from creating a second course.
    """
    digest = hashlib.sha1(normalize_academic_name(name).encode("utf-8")).hexdigest()
    return f"REQ-{digest[:8].upper()}"


async def _resolve_student_program(db: AsyncSession, student: Student) -> ProgramStudi | None:
    """Resolve the requesting student's prodi using the exact-match rules.

    Mirrors `sync_master_schedules()`: `students.program_studi` is admin free
    text and often stale, so the user's own affiliation is the fallback.
    """
    candidates = [(student.program_studi or "").strip()]
    if student.user_id:
        user_jurusan = (
            await db.execute(select(User.jurusan).where(User.id == student.user_id))
        ).scalar_one_or_none()
        if user_jurusan:
            candidates.append(user_jurusan.strip())

    programs = (
        await db.execute(select(ProgramStudi).where(ProgramStudi.is_active.is_(True)))
    ).scalars().all()
    return next((p for p in (match_program(programs, c) for c in candidates) if p), None)


async def _find_course_in_prodi(
    db: AsyncSession, prodi_id: str, code: str, name: str
) -> MataKuliah | None:
    """Find the prodi's course row by code first, then by normalized name."""
    courses = (
        await db.execute(select(MataKuliah).where(MataKuliah.program_studi_id == prodi_id))
    ).scalars().all()
    wanted_code = code.strip().upper()
    wanted_name = normalize_academic_name(name)
    for course in courses:
        if wanted_code and (course.code or "").strip().upper() == wanted_code:
            return course
    for course in courses:
        if wanted_name and normalize_academic_name(course.name) == wanted_name:
            return course
    return None


async def _resolve_course_campus_id(
    db: AsyncSession, req: ScheduleRequest, prodi_id: str
) -> str | None:
    """Campus for a newly created course: request → prodi courses → any campus."""
    if req.campus_id:
        campus_id = (
            await db.execute(select(Campus.id).where(Campus.id == req.campus_id))
        ).scalar_one_or_none()
        if campus_id:
            return campus_id
    campus_id = (
        await db.execute(
            select(MataKuliah.campus_id).where(MataKuliah.program_studi_id == prodi_id).limit(1)
        )
    ).scalar_one_or_none()
    if campus_id:
        return campus_id
    return (
        await db.execute(
            select(Campus.id).where(Campus.is_active.is_(True)).order_by(Campus.name).limit(1)
        )
    ).scalar_one_or_none()


async def _promote_request_to_master(
    db: AsyncSession, req: ScheduleRequest, student: Student
) -> JadwalMataKuliah | None:
    """Publish an approved request into the prodi's master schedule.

    Creating `mata_kuliah` + `jadwal_mata_kuliah` is what makes the request
    show up in section 03 of the Akademik dashboard: `sync_master_schedules()`
    copies every master slot to all students of that prodi, so the schedule
    becomes readable by other students on Android and on the website.

    Returns None (without raising) when the data needed to publish is missing —
    no prodi match, no course name, no campus. Callers still create the
    requester's personal `schedule` row in that case.
    """
    if not req.day_of_week or not req.start_time or not req.end_time:
        return None

    prodi = await _resolve_student_program(db, student)
    if prodi is None:
        return None

    subject = None
    if req.subject_id:
        subject = (
            await db.execute(select(Subject).where(Subject.id == req.subject_id))
        ).scalar_one_or_none()

    name = ((subject.name if subject else None) or req.subject_name_raw or "").strip()
    if not name:
        return None
    code = ((subject.code if subject else None) or "").strip().upper() or _synthetic_course_code(name)

    course = await _find_course_in_prodi(db, prodi.id, code, name)
    if course is None:
        campus_id = await _resolve_course_campus_id(db, req, prodi.id)
        if not campus_id:
            return None
        course = MataKuliah(
            code=code,
            name=name,
            sks=2,
            semester=student.semester or 1,
            program_studi_id=prodi.id,
            campus_id=campus_id,
            description="Dibuat otomatis dari request jadwal mahasiswa.",
            is_active=True,
        )
        db.add(course)
        await db.flush()
        # Mirror into `subjects` so the Android schedule directory and the
        # `Subject.code == MataKuliah.code` join in sync_master_schedules work.
        await _get_or_create_subject(db, code, name)
    elif not course.is_active:
        course.is_active = True

    try:
        start, end = hhmm_to_time(req.start_time), hhmm_to_time(req.end_time)
    except ValueError:
        # Legacy rows can hold malformed times; skip publishing, keep approval.
        return None
    slot = (
        await db.execute(
            select(JadwalMataKuliah).where(
                JadwalMataKuliah.mata_kuliah_id == course.id,
                JadwalMataKuliah.day_of_week == req.day_of_week,
                JadwalMataKuliah.start_time == start,
                JadwalMataKuliah.end_time == end,
            )
        )
    ).scalar_one_or_none()
    if slot is None:
        slot = JadwalMataKuliah(
            mata_kuliah_id=course.id,
            day_of_week=req.day_of_week,
            start_time=start,
            end_time=end,
            room=(req.room or "").strip(),
            is_active=True,
        )
        db.add(slot)
        await db.flush()
    elif not slot.is_active:
        slot.is_active = True
    return slot


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
    await db.refresh(req, _REQ_RELATIONS)

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

    Besides the requester's personal `schedules` row, the slot is published
    into the prodi master schedule (`mata_kuliah` + `jadwal_mata_kuliah`) so
    it shows in section 03 of the Akademik dashboard and reaches every
    student of that prodi via `sync_master_schedules()`.

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

    student = (
        await db.execute(select(Student).where(Student.id == req.student_id))
    ).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student profile not found")

    # Wave 2 #1: reject approval with NULL time fields instead of silently
    # fabricating 08:00/10:00 — that would create Schedule rows the requester
    # never asked for. Both fields are nullable in the model (legacy data,
    # manual SQL, etc.), so this guard is the safety net.
    if not req.start_time or not req.end_time:
        raise HTTPException(
            status_code=400,
            detail="Request tidak memiliki start_time atau end_time, tidak bisa disetujui",
        )

    # Publish into the prodi master schedule first so the slot also appears in
    # section 03 and reaches every student of that prodi via
    # sync_master_schedules(). None = data for publishing is incomplete; the
    # requester still gets a personal row below (previous behaviour).
    slot = await _promote_request_to_master(db, req, student)

    # Create the schedule for the student. When the request was promoted, the
    # row uses the same UUIDv5 id sync_master_schedules() would generate, so
    # the student does not end up with a duplicate personal + master row.
    schedule_id = None
    if slot is not None:
        try:
            schedule_id = master_schedule_id(req.student_id, slot.id)
        except ValueError:
            # Legacy non-UUID student ids keep the plain personal row.
            slot = None

    schedule = None
    if schedule_id:
        # A second request for the same slot reuses the managed row instead of
        # colliding on the primary key.
        schedule = (
            await db.execute(select(Schedule).where(Schedule.id == schedule_id))
        ).scalar_one_or_none()

    if schedule is None:
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
        if schedule_id:
            schedule.id = schedule_id
        db.add(schedule)
    else:
        schedule.is_active = True
        schedule.day_of_week = req.day_of_week or 1
        schedule.start_time = req.start_time
        schedule.end_time = req.end_time
        schedule.room = req.room
        schedule.campus_id = req.campus_id
        schedule.subject_id = req.subject_id
        schedule.lecturer_id = req.lecturer_id
        schedule.subject_name_raw = req.subject_name_raw
        schedule.lecturer_name_raw = req.lecturer_name_raw

    req.status = "approved"
    req.processed_by = admin_user.get("sub")
    req.processed_at = datetime.now(timezone.utc)
    req.admin_notes = data.admin_notes

    await db.commit()
    await db.refresh(req, _REQ_RELATIONS)

    user_result = await db.execute(select(User).where(User.id == req.requester_id))
    user = user_result.scalar_one_or_none()
    return _to_response(
        req,
        student_name=user.full_name if user else None,
        published_to_master=slot is not None,
    )


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
    await db.refresh(req, _REQ_RELATIONS)

    user_result = await db.execute(select(User).where(User.id == req.requester_id))
    user = user_result.scalar_one_or_none()
    return _to_response(req, student_name=user.full_name if user else None)
