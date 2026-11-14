from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.schedule import Schedule, Subject, Lecturer
from app.models.student import Student
from app.models.user import User
from app.schemas.schedule import (
    ScheduleCreate, ScheduleUpdate, ScheduleResponse,
    ScheduleDirectory, SubjectResponse, LecturerResponse,
)
from app.routers.auth import get_current_user, get_current_user_id
from uuid import UUID, uuid5

router = APIRouter(prefix="/schedules", tags=["Schedules"])


def master_schedule_id(student_id: str, slot_id: str) -> str:
    return str(uuid5(UUID(student_id), slot_id))


async def sync_master_schedules(db: AsyncSession, student: Student):
    from app.models.jadwal_mata_kuliah import JadwalMataKuliah
    from app.models.mata_kuliah import MataKuliah
    from app.models.program_studi import ProgramStudi
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    # UUIDv5 keeps one stable schedule per student/slot, including concurrent fetches.
    # Existing UUIDv4 personal schedules and historical log references are retained.
    slot_ids = (await db.execute(select(JadwalMataKuliah.__table__.c.id))).scalars().all()
    managed_ids = {master_schedule_id(student.id, sid) for sid in slot_ids}
    existing = (await db.execute(select(Schedule).where(Schedule.student_id == student.id))).scalars().all()
    manual_keys = {(s.subject_id, s.day_of_week, s.start_time, s.end_time)
                   for s in existing if s.id not in managed_ids and s.is_active}
    rows = (await db.execute(
        select(JadwalMataKuliah, MataKuliah, Subject.id, User.full_name)
        .join(MataKuliah, JadwalMataKuliah.mata_kuliah_id == MataKuliah.id)
        .join(ProgramStudi, MataKuliah.program_studi_id == ProgramStudi.id)
        .outerjoin(Subject, Subject.code == MataKuliah.code)
        .outerjoin(User, MataKuliah.dosen_id == User.id)
        .where(ProgramStudi.is_active.is_(True),
               (ProgramStudi.name == student.program_studi) | (ProgramStudi.code == student.program_studi)
               | (ProgramStudi.id == student.program_studi),
               MataKuliah.is_active.is_(True), JadwalMataKuliah.is_active.is_(True))
    )).all()
    active_ids = set()
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    for slot, course, subject_id, lecturer_name in rows:
        day = slot.day_of_week or 7  # Master: Sunday=0; Android/personal: Sunday=7.
        start, end = slot.start_time.strftime("%H:%M"), slot.end_time.strftime("%H:%M")
        if (subject_id, day, start, end) in manual_keys:
            continue
        schedule_id = master_schedule_id(student.id, slot.id)
        active_ids.add(schedule_id)
        values = dict(student_id=student.id, subject_id=subject_id, subject_name_raw=course.name,
                      lecturer_name_raw=lecturer_name, day_of_week=day, start_time=start, end_time=end,
                      campus_id=course.campus_id, room=slot.room, is_active=True)
        stmt = insert(Schedule).values(id=schedule_id, **values)
        await db.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=values))
    for schedule in existing:
        if schedule.id in managed_ids and schedule.id not in active_ids:
            schedule.is_active = False
    await db.commit()
    db.expire_all()


async def _get_or_provision_student(db: AsyncSession, user_id: str) -> Student | None:
    """Get or auto-provision a Student row for a user.

    Unlike get_student_id in logs.py, this does NOT raise — it returns None so
    the caller can return [] instead of an error. This mirrors the previous
    behaviour while fixing the silent-empty result for users who haven't synced
    yet (e.g. fresh registration from web).
    """
    student_result = await db.execute(select(Student).where(Student.user_id == user_id))
    student = student_result.scalar_one_or_none()
    if student:
        return student

    user_res = await db.execute(select(User).where(User.id == user_id))
    user = user_res.scalar_one_or_none()
    if user is None:
        return None

    nim_candidate = (user.username or user.email.split("@")[0])[:20] or f"mhs-{user_id[:8]}"
    exists_res = await db.execute(select(Student.id).where(Student.nim == nim_candidate))
    if exists_res.scalar_one_or_none() is not None:
        nim_candidate = f"{nim_candidate[:14]}-{user_id[:4]}"

    student = Student(
        user_id=user.id,
        nim=nim_candidate,
        full_name=user.full_name or user.username or "Mahasiswa",
        program_studi=user.jurusan or "",
        angkatan=2024,
        semester=1,
        is_active=True,
    )
    db.add(student)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        res = await db.execute(select(Student).where(Student.user_id == user_id))
        return res.scalar_one_or_none()
    await db.refresh(student)
    return student


def schedule_to_response(schedule: Schedule) -> ScheduleResponse:
    return ScheduleResponse(
        id=schedule.id,
        student_id=schedule.student_id,
        day_of_week=schedule.day_of_week,
        start_time=schedule.start_time,
        end_time=schedule.end_time,
        campus_id=schedule.campus_id,
        room=schedule.room,
        subject_name_raw=schedule.subject_name_raw,
        lecturer_name_raw=schedule.lecturer_name_raw,
        subject_id=schedule.subject_id,
        lecturer_id=schedule.lecturer_id,
        is_active=schedule.is_active,
        created_at=schedule.created_at,
        subject_name=getattr(schedule.subject, 'name', None) or schedule.subject_name_raw,
        lecturer_name=getattr(schedule.lecturer, 'name', None) or schedule.lecturer_name_raw,
        campus_name=schedule.campus.name if schedule.campus else None,
    )


@router.get("", response_model=list[ScheduleResponse])
async def get_schedules(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    # Auto-provision Student row so fresh registrations (who haven't synced
    # via /api/logs/batch yet) can still see/add schedules. Returns [] when
    # the User itself doesn't exist yet (shouldn't happen for authenticated users).
    student = await _get_or_provision_student(db, user_id)
    if not student:
        return []

    student_id = student.id
    await sync_master_schedules(db, student)

    result = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.subject),
            selectinload(Schedule.lecturer),
            selectinload(Schedule.campus),
        )
        .where(Schedule.student_id == student_id, Schedule.is_active == True)
        .order_by(Schedule.day_of_week, Schedule.start_time)
    )
    schedules = result.scalars().all()
    return [schedule_to_response(s) for s in schedules]


@router.post("", response_model=ScheduleResponse)
async def create_schedule(
    data: ScheduleCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    student = await _get_or_provision_student(db, user_id)
    if not student:
        raise HTTPException(status_code=404, detail="User not found")

    schedule = Schedule(student_id=student.id, **data.model_dump())
    db.add(schedule)
    await db.commit()
    # Re-fetch with eager relationships to avoid async lazy-load in helper
    # (db.refresh() only reloads scalar columns, not relationships).
    result = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.subject),
            selectinload(Schedule.lecturer),
            selectinload(Schedule.campus),
        )
        .where(Schedule.id == schedule.id)
    )
    return schedule_to_response(result.scalar_one())


@router.put("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: str,
    data: ScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    if UUID(schedule_id).version == 5:
        raise HTTPException(403, "Jadwal prodi dikelola melalui menu Akademik")
    student = await _get_or_provision_student(db, user_id)
    if not student:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    result = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.subject),
            selectinload(Schedule.lecturer),
            selectinload(Schedule.campus),
        )
        .where(Schedule.id == schedule_id, Schedule.student_id == student.id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    for key, value in update_data.items():
        setattr(schedule, key, value)
    await db.commit()
    return schedule_to_response(schedule)


@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        managed = UUID(schedule_id).version == 5
    except ValueError:
        raise HTTPException(404, "Schedule not found")
    if managed:
        raise HTTPException(403, "Jadwal prodi dikelola melalui menu Akademik")
    student = await _get_or_provision_student(db, user_id)
    if not student:
        raise HTTPException(status_code=404, detail="User not found")

    result = await db.execute(
        delete(Schedule).where(Schedule.id == schedule_id, Schedule.student_id == student.id)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await db.commit()
    return {"message": "Schedule deleted"}


@router.get("/directory", response_model=ScheduleDirectory)
async def get_schedule_directory(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _payload: dict = Depends(get_current_user),
):
    """Subject + lecturer directory used by the Android schedule picker.

    Subject rows are mirrored from `mata_kuliah` (admin master list) so the
    dropdown stays in sync with courses created via /api/mata-kuliah. The
    campus_id / campus_name on each Subject comes from the mirror join — if
    a Subject has no matching mata_kuliah row (legacy data), campus fields
    are null and Android falls back to "Lokasi belum ditentukan".
    """
    from app.models.mata_kuliah import MataKuliah
    from app.models.campus import Campus

    subject_rows = (await db.execute(select(Subject))).scalars().all()
    # Single round-trip: pull every mata_kuliah with its campus name once,
    # index by code, then project.
    mk_rows = (
        await db.execute(
            select(MataKuliah.code, MataKuliah.campus_id, Campus.name.label("campus_name"))
            .join(Campus, MataKuliah.campus_id == Campus.id)
        )
    ).all()
    mk_index: dict[str, tuple[str | None, str | None]] = {
        code: (campus_id, campus_name) for code, campus_id, campus_name in mk_rows
    }

    lecturer_rows = (await db.execute(select(Lecturer))).scalars().all()

    def _project_subject(s: Subject) -> SubjectResponse:
        campus_id, campus_name = mk_index.get(s.code or "", (None, None))
        return SubjectResponse(
            id=s.id,
            name=s.name,
            code=s.code,
            campus_id=campus_id,
            campus_name=campus_name,
        )

    return ScheduleDirectory(
        subjects=[_project_subject(s) for s in subject_rows],
        lecturers=[LecturerResponse.model_validate(l) for l in lecturer_rows],
    )
