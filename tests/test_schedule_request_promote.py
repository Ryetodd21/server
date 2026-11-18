"""Approved student schedule requests must reach the prodi master schedule.

Section 03 of the Akademik page lists `jadwal_mata_kuliah`, and
`sync_master_schedules()` copies those slots to every student of the prodi.
If approval only writes the requester's personal `schedules` row, other
students on Android never see the slot. These checks pin that contract.

The suite runs without pytest-asyncio, so each check drives its own event
loop through `asyncio.run()`.
"""
import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.campus import Campus
from app.models.jadwal_mata_kuliah import JadwalMataKuliah
from app.models.mata_kuliah import MataKuliah
from app.models.program_studi import Fakultas, Jurusan, ProgramStudi
from app.models.schedule import Schedule, Subject
from app.models.schedule_request import ScheduleRequest
from app.models.student import Student
from app.models.user import User
from app.routers.schedule import _get_or_provision_student, sync_master_schedules
from app.routers.schedule_request import _promote_request_to_master, approve_request
from app.schemas.schedule_request import ScheduleRequestApproveReject


@asynccontextmanager
async def _session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db, program_name="Sistem Informasi"):
    fakultas = Fakultas(code="FT", name="Teknik")
    db.add(fakultas)
    await db.flush()
    jurusan = Jurusan(code="TI", name="Teknik Informatika", fakultas_id=fakultas.id)
    db.add(jurusan)
    await db.flush()
    prodi = ProgramStudi(code="SI", name=program_name, jurusan_id=jurusan.id)
    campus = Campus(name="Kampus 1", latitude=0.5, longitude=123.0)
    user = User(username="mhs1", email="mhs1@example.com", hashed_password="x", role="mahasiswa")
    db.add_all([prodi, campus, user])
    await db.flush()
    student = Student(
        user_id=user.id,
        nim="531001",
        full_name="Mahasiswa Satu",
        program_studi=program_name,
        semester=3,
    )
    db.add(student)
    await db.flush()
    return prodi, campus, student


def _request(student, **overrides):
    values = dict(
        requester_id=student.user_id,
        student_id=student.id,
        request_type="create",
        description="Request jadwal baru",
        day_of_week=3,
        start_time="10:00",
        end_time="11:40",
        room="Ruang 3",
        subject_name_raw="Basis Data Lanjut",
    )
    values.update(overrides)
    return ScheduleRequest(**values)


def test_promote_creates_prodi_course_and_slot():
    async def scenario():
        async with _session() as db:
            prodi, campus, student = await _seed(db)
            req = _request(student)
            db.add(req)
            await db.commit()

            slot = await _promote_request_to_master(db, req, student)
            await db.commit()

            assert slot is not None
            course = (
                await db.execute(select(MataKuliah).where(MataKuliah.program_studi_id == prodi.id))
            ).scalar_one()
            assert course.name == "Basis Data Lanjut"
            assert course.campus_id == campus.id
            assert slot.mata_kuliah_id == course.id
            assert (slot.day_of_week, slot.start_time.strftime("%H:%M"),
                    slot.end_time.strftime("%H:%M")) == (3, "10:00", "11:40")
            # Android reads the directory from `subjects`; the bridge must run too.
            subject = (
                await db.execute(select(Subject).where(Subject.code == course.code))
            ).scalar_one_or_none()
            assert subject is not None and subject.name == "Basis Data Lanjut"

    asyncio.run(scenario())


def test_promote_reuses_course_and_slot():
    async def scenario():
        async with _session() as db:
            _, _, student = await _seed(db)
            first = _request(student)
            second = _request(student, start_time="13:00", end_time="14:40")
            db.add_all([first, second])
            await db.commit()

            slot_a = await _promote_request_to_master(db, first, student)
            slot_b = await _promote_request_to_master(db, second, student)
            await db.commit()

            assert slot_a is not None and slot_b is not None
            course_count = (await db.execute(select(func.count()).select_from(MataKuliah))).scalar()
            subject_count = (await db.execute(select(func.count()).select_from(Subject))).scalar()
            slot_count = (await db.execute(select(func.count()).select_from(JadwalMataKuliah))).scalar()
            assert (course_count, subject_count, slot_count) == (1, 1, 2)
            # Approving the exact same slot again must not duplicate the master row.
            assert await _promote_request_to_master(db, first, student) is slot_a

    asyncio.run(scenario())


def test_promote_skips_when_prodi_cannot_be_resolved():
    async def scenario():
        async with _session() as db:
            _, _, student = await _seed(db)
            # No ProgramStudi row matches the free text on student/user.
            student.program_studi = "Program Tidak Dikenal"
            req = _request(student)
            db.add(req)
            await db.commit()

            assert await _promote_request_to_master(db, req, student) is None
            assert (await db.execute(select(func.count()).select_from(MataKuliah))).scalar() == 0

    asyncio.run(scenario())


def test_promote_uses_subject_directory_when_present():
    async def scenario():
        async with _session() as db:
            prodi, _, student = await _seed(db)
            subject = Subject(code="IF201", name="Algoritma")
            db.add(subject)
            await db.flush()
            req = _request(student, subject_id=subject.id, subject_name_raw=None)
            db.add(req)
            await db.commit()

            slot = await _promote_request_to_master(db, req, student)
            await db.commit()

            assert slot is not None
            course = (
                await db.execute(select(MataKuliah).where(MataKuliah.program_studi_id == prodi.id))
            ).scalar_one()
            assert (course.code, course.name) == ("IF201", "Algoritma")

    asyncio.run(scenario())


def test_approving_twice_reuses_the_managed_schedule_row():
    async def scenario():
        async with _session() as db:
            _, _, student = await _seed(db)
            first = _request(student)
            second = _request(student)
            db.add_all([first, second])
            await db.commit()

            response_a = await approve_request(
                first.id, ScheduleRequestApproveReject(), db=db, admin_user={"sub": "admin-1"}
            )
            response_b = await approve_request(
                second.id, ScheduleRequestApproveReject(), db=db, admin_user={"sub": "admin-1"}
            )

            assert response_a.published_to_master is True
            assert response_b.published_to_master is True
            rows = (
                await db.execute(select(Schedule).where(Schedule.student_id == student.id))
            ).scalars().all()
            # One master row + one shared schedule row, not a duplicate pair.
            assert len(rows) == 1

    asyncio.run(scenario())


def test_approved_slot_is_visible_to_other_students():
    """The whole point: a slot created from one request is copied to peers."""
    async def scenario():
        async with _session() as db:
            _, _, student = await _seed(db)
            peer_user = User(
                username="mhs2", email="mhs2@example.com", hashed_password="x", role="mahasiswa"
            )
            db.add(peer_user)
            await db.flush()
            peer = Student(
                user_id=peer_user.id,
                nim="531002",
                full_name="Mahasiswa Dua",
                program_studi="Sistem Informasi",
                semester=3,
            )
            db.add(peer)
            await db.commit()

            req = _request(student)
            db.add(req)
            await db.commit()
            assert await _promote_request_to_master(db, req, student) is not None
            await db.commit()

            peer_id = peer.id
            peer_row = await _get_or_provision_student(db, peer_user.id)
            await sync_master_schedules(db, peer_row)
            rows = (
                await db.execute(
                    select(Schedule).where(
                        Schedule.student_id == peer_id,
                        Schedule.subject_name_raw == "Basis Data Lanjut",
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].day_of_week == 3

    asyncio.run(scenario())
