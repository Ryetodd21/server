from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.routers.auth import get_current_user
import uuid
from app.models.student import Student
from app.models.user import User
from app.models.cluster_result import ClusterResult
from app.models.app_log import AppLog
from app.models.model_version import ModelVersion
from app.schemas.student import StudentResponse, StudentListResponse, StudentCreate
import json

router = APIRouter(prefix="/students", tags=["Students"])


async def require_admin_or_dosen(request: Request) -> dict:
    """Admin or Dosen only — student list exposed to staff for monitoring."""
    payload = await get_current_user(request)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(
            status_code=403,
            detail="Hanya admin/dosen yang boleh melihat daftar mahasiswa.",
        )
    return payload


def _parse_json_field(value, default=None):
    """Safely parse JSON-encoded column."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


@router.get("", response_model=StudentListResponse)
async def list_students(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200),
    search: str = Query(default=""),
    cluster: int = Query(default=None, ge=0, le=4),
    db: AsyncSession = Depends(get_db),
    _payload: dict = Depends(require_admin_or_dosen),
):
    """List all students with cluster info from latest model run. Admin/Dosen only.

    Cluster filter is applied via `Student.id IN (matching_ids)` BEFORE
    pagination, so `total` reflects the filtered count (was: filter applied
    post-pagination in Python — `total` ignored the filter, confusing UI).
    """
    # Get latest active model
    active_model = (await db.execute(
        select(ModelVersion).where(ModelVersion.is_active == True)
    )).scalar_one_or_none()

    cluster_distribution = {}
    cluster_labels_map = {}
    if active_model:
        cluster_distribution = _parse_json_field(active_model.cluster_distribution, {})
        cluster_labels_map = _parse_json_field(active_model.cluster_labels, {})

    # Map student_id -> latest cluster info (must run BEFORE filter so we
    # know which students to include in the cluster-filtered query).
    cluster_map: dict[str, ClusterResult] = {}
    if active_model:
        cr_query = (
            select(ClusterResult)
            .where(ClusterResult.model_version_id == active_model.id)
            .order_by(desc(ClusterResult.result_date))
        )
        cr_rows = (await db.execute(cr_query)).scalars().all()
        for cr in cr_rows:
            if cr.student_id not in cluster_map:
                cluster_map[cr.student_id] = cr

    # Base query with eager load
    query = select(Student).options(selectinload(Student.advisor))
    count_q = select(func.count()).select_from(Student)
    if search:
        like = f"%{search}%"
        query = query.where(
            (Student.full_name.ilike(like)) | (Student.nim.ilike(like))
        )
        count_q = count_q.where(
            (Student.full_name.ilike(like)) | (Student.nim.ilike(like))
        )
    if cluster is not None and active_model:
        matching_ids = [
            sid for sid, cr in cluster_map.items()
            if cr.cluster_label == cluster
        ]
        if not matching_ids:
            # No students in this cluster — short-circuit with empty result.
            return StudentListResponse(
                total=0,
                page=page,
                page_size=page_size,
                items=[],
                cluster_distribution={
                    str(k): int(v) for k, v in cluster_distribution.items()
                },
                active_model_version=active_model.version_number,
            )
        query = query.where(Student.id.in_(matching_ids))
        count_q = count_q.where(Student.id.in_(matching_ids))

    total = (await db.execute(count_q)).scalar() or 0

    # Paginate
    query = query.order_by(Student.nim.asc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).scalars().all()

    # Map student_id -> latest log date
    log_subq = (
        select(
            AppLog.student_id,
            func.max(AppLog.timestamp).label("last_log"),
        )
        .group_by(AppLog.student_id)
        .subquery()
    )
    log_rows = await db.execute(select(log_subq.c.student_id, log_subq.c.last_log))
    log_map = {sid: last_log for sid, last_log in log_rows.all()}

    # Build response
    items = []
    for s in rows:
        cr = cluster_map.get(s.id)
        last_log = log_map.get(s.id)

        cluster_name = None
        if cr and cluster_labels_map:
            cluster_name = cluster_labels_map.get(str(cr.cluster_label))

        items.append(StudentResponse(
            id=s.id,
            nim=s.nim,
            full_name=s.full_name,
            study_program=s.program_studi or "",
            semester=s.semester or 1,
            advisor_name=getattr(s.advisor, "full_name", None) if s.advisor else None,
            cluster_label=cr.cluster_label if cr else None,
            cluster_name=cluster_name,
            distraction_score=cr.distraction_score if cr else None,
            last_log_date=last_log.strftime("%Y-%m-%d") if last_log else None,
        ))

    return StudentListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
        cluster_distribution={str(k): int(v) for k, v in cluster_distribution.items()},
        active_model_version=active_model.version_number if active_model else None,
    )


@router.post("", response_model=StudentResponse)
async def create_student(
    data: StudentCreate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
):
    """Tambah mahasiswa baru — admin/dosen only.

    If `create_user_account=true`, also creates a User row with role=mahasiswa
    and links it via students.user_id. Without that flag, the student row is
    created without a user account (legacy data-import style). When `advisor_id`
    is provided, it must reference an existing dosen user — wired up so dosen
    PA can be assigned at registration time.
    """
    if payload.get("role") not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Hanya admin/dosen boleh menambah mahasiswa")

    # Check duplicate NIM
    existing = await db.execute(select(Student).where(Student.nim == data.nim))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="NIM sudah terdaftar")

    # Validate advisor_id (dosen PA) if provided — must be an active dosen user.
    if data.advisor_id:
        advisor = (await db.execute(
            select(User).where(User.id == data.advisor_id)
        )).scalar_one_or_none()
        if not advisor:
            raise HTTPException(status_code=400, detail="Dosen PA tidak ditemukan")
        if advisor.role not in ("admin", "dosen"):
            raise HTTPException(status_code=400, detail="Dosen PA harus user dengan peran admin atau dosen")

    # If create_user_account=true, create a linked User (mahasiswa role) first.
    user_id = None
    if data.create_user_account:
        if not data.email or not data.initial_password:
            raise HTTPException(
                status_code=400,
                detail="email dan initial_password wajib diisi bila create_user_account=true",
            )
        # Reuse the password-strength policy from auth schemas.
        from app.schemas.auth import _validate_password_strength
        try:
            _validate_password_strength(data.initial_password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        existing_user = await db.execute(
            select(User).where((User.email == data.email.lower()) | (User.username == data.nim))
        )
        if existing_user.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email sudah dipakai user lain")

        from app.routers.auth import get_password_hash
        user = User(
            id=str(uuid.uuid4()),
            username=data.nim,
            email=data.email.lower(),
            hashed_password=get_password_hash(data.initial_password),
            full_name=data.full_name,
            role="mahasiswa",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        user_id = user.id

    student = Student(
        id=str(uuid.uuid4()),
        user_id=user_id,
        nim=data.nim,
        full_name=data.full_name,
        program_studi=data.program_studi,
        semester=data.semester,
        advisor_id=data.advisor_id,
    )
    db.add(student)
    await db.commit()
    await db.refresh(student)

    # Wave 2 #2: async session can't lazy-load `student.advisor` after refresh
    # (refresh reloads scalar columns only). Fetch the advisor explicitly via
    # a bounded SELECT — the list endpoint uses selectinload; for single-row
    # create an extra query is acceptable. Round 14 fixed the list endpoint
    # with getattr; this endpoint regressed to direct attribute access.
    advisor_name = None
    if student.advisor_id:
        res = await db.execute(select(User).where(User.id == student.advisor_id))
        advisor = res.scalar_one_or_none()
        advisor_name = getattr(advisor, "full_name", None) if advisor else None

    return StudentResponse(
        id=student.id,
        nim=student.nim,
        full_name=student.full_name,
        study_program=student.program_studi or "",
        semester=student.semester or 1,
        advisor_name=advisor_name,
    )
