"""Academic visibility for request sessions; background jobs remain unscoped."""
from fastapi import HTTPException
from sqlalchemy import event, func, or_, select
from sqlalchemy.orm import with_loader_criteria

from app.models.program_studi import Fakultas, Jurusan, ProgramStudi
from app.models.student import Student
from app.models.user import User
from app.models.mata_kuliah import MataKuliah
from app.models.jadwal_mata_kuliah import JadwalMataKuliah
from app.models.schedule import Schedule, Subject
from app.models.schedule_request import ScheduleRequest
from app.models.request_akademik import RequestAkademik
from app.models.app_log import AppLog
from app.models.location_log import LocationLog
from app.models.cluster_result import ClusterResult


async def resolve_programs(db, payload, jurusan_id=None, program_studi_id=None):
    """None means unrestricted; an empty list deliberately grants no rows."""
    role = payload.get("role")
    if role == "admin" and not (jurusan_id or program_studi_id):
        return None
    programs = (await db.execute(select(ProgramStudi))).scalars().all()
    departments = (await db.execute(select(Jurusan))).scalars().all()
    if role == "dosen":
        affiliation = (payload.get("jurusan") or "").strip().casefold()
        matching = {p.jurusan_id for p in programs
                    if affiliation and affiliation in {p.id.casefold(), p.code.casefold(), p.name.casefold()}}
        matching.update(d.id for d in departments
                        if affiliation and affiliation in {d.id.casefold(), d.code.casefold(), d.name.casefold()})
        matching.discard(None)
        # Legacy free text must resolve to exactly one department.
        programs = [p for p in programs if p.jurusan_id in matching] if len(matching) == 1 else []
    elif role == "mahasiswa":
        affiliation = (await db.execute(select(Student.program_studi).where(
            Student.user_id == payload["sub"]))).scalar_one_or_none()
        affiliation = (affiliation or payload.get("jurusan") or "").strip().casefold()
        programs = [p for p in programs if affiliation and affiliation in {
            p.id.casefold(), p.code.casefold(), p.name.casefold()}]
        if len(programs) != 1:
            programs = []
    elif role != "admin":
        programs = []
    if jurusan_id:
        programs = [p for p in programs if p.jurusan_id == jurusan_id]
    if program_studi_id:
        programs = [p for p in programs if p.id == program_studi_id]
    return programs


def install_scope(db, programs, payload):
    if programs is None:
        return
    ids = [p.id for p in programs]
    names = list({v for p in programs for v in (p.id, p.code, p.name)})
    department_ids = list({p.jurusan_id for p in programs if p.jurusan_id})
    st, mk, jd, us, jr = (m.__table__.c for m in (Student, MataKuliah, JadwalMataKuliah, User, Jurusan))
    student_ids = select(st.id).where(st.program_studi.in_(names))
    course_ids = select(mk.id).where(mk.program_studi_id.in_(ids))
    department_names = select(jr.name).where(jr.id.in_(department_ids))
    user_condition = or_(us.id.in_(select(st.user_id).where(st.program_studi.in_(names))),
                         us.jurusan.in_(names + department_ids), us.jurusan.in_(department_names))
    user_ids = select(us.id).where(user_condition)
    criteria = {
        ProgramStudi: ProgramStudi.id.in_(ids),
        Jurusan: Jurusan.id.in_(department_ids),
        Fakultas: Fakultas.id.in_(select(jr.fakultas_id).where(jr.id.in_(department_ids))),
        MataKuliah: MataKuliah.program_studi_id.in_(ids),
        JadwalMataKuliah: JadwalMataKuliah.mata_kuliah_id.in_(course_ids),
        Subject: Subject.code.in_(select(mk.code).where(mk.program_studi_id.in_(ids), mk.is_active.is_(True))),
    }
    if payload.get("role") != "mahasiswa":
        criteria.update({
            Student: Student.program_studi.in_(names),
            User: or_(User.id.in_(user_ids), User.id == payload.get("sub")),
            Schedule: Schedule.student_id.in_(student_ids),
            ScheduleRequest: ScheduleRequest.student_id.in_(student_ids),
            RequestAkademik: RequestAkademik.requested_by_id.in_(user_ids),
            AppLog: AppLog.student_id.in_(student_ids),
            LocationLog: LocationLog.student_id.in_(student_ids),
            ClusterResult: ClusterResult.student_id.in_(student_ids),
        })
    session = db.sync_session
    session.info["academic_scope"] = tuple(sorted(ids))

    @event.listens_for(session, "do_orm_execute")
    def restrict_queries(state):
        if state.is_select or state.is_update or state.is_delete:
            state.statement = state.statement.options(*[
                with_loader_criteria(model, condition, include_aliases=True)
                for model, condition in criteria.items()
            ])

    if payload.get("role") != "dosen":
        return

    @event.listens_for(session, "before_flush")
    def validate_writes(session, _context, _instances):
        def exists(table, value, condition):
            return value is not None and session.connection().execute(
                select(table.c.id).where(table.c.id == value, condition)
            ).first() is not None

        for row in session.new.union(session.dirty):
            valid = True
            if isinstance(row, Student):
                valid = row.program_studi in names
            elif isinstance(row, User):
                valid = row.role == "mahasiswa" and row.jurusan in names
            elif isinstance(row, MataKuliah):
                valid = row.program_studi_id in ids
            elif isinstance(row, JadwalMataKuliah):
                valid = exists(MataKuliah.__table__, row.mata_kuliah_id, mk.program_studi_id.in_(ids))
            elif isinstance(row, (Schedule, ScheduleRequest, AppLog, LocationLog, ClusterResult)):
                valid = exists(Student.__table__, row.student_id, st.program_studi.in_(names))
            if not valid:
                raise HTTPException(403, "Data di luar jurusan dosen")


async def configure_request_scope(db, request):
    from app.routers.auth import authenticate_access_token, get_token_from_request
    path = request.url.path.removeprefix("/api")
    prefixes = ("/students", "/reports", "/models", "/logs", "/ai", "/mata-kuliah",
                "/jadwal-mata-kuliah", "/schedules", "/schedule-requests", "/request-akademik",
                "/program-studi", "/jurusan", "/fakultas", "/users")
    if not any(path == p or path.startswith(p + "/") for p in prefixes):
        return
    if path.startswith("/users/me") or path == "/users/register":
        return
    token = get_token_from_request(request)
    if not token:
        return  # Public directories; protected routes still enforce authentication.
    payload = await authenticate_access_token(token)
    jurusan_id = request.query_params.get("jurusan_id") or request.headers.get("x-jurusan-id")
    program_id = request.query_params.get("program_studi_id") or request.headers.get("x-program-studi-id")
    # Filters narrow reads only; role restrictions apply to mutations too.
    if request.method not in {"GET", "HEAD"}:
        jurusan_id = program_id = None
    programs = await resolve_programs(db, payload, jurusan_id, program_id)
    install_scope(db, programs, payload)


async def scoped_distribution(db, model_id):
    rows = (await db.execute(select(ClusterResult.student_id, ClusterResult.cluster_label)
                            .where(ClusterResult.model_version_id == model_id)
                            .order_by(ClusterResult.result_date.desc()))).all()
    latest = {}
    for student_id, label in rows:
        latest.setdefault(student_id, label)
    counts = {}
    for label in latest.values():
        counts[str(label)] = counts.get(str(label), 0) + 1
    return counts
