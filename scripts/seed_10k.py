"""
Seed 10,000+ app log records.
This extends the existing database — adds more students + fills app_logs to 10K+.
Usage: python -m scripts.seed_10k
"""
import asyncio
import uuid
from datetime import datetime, timedelta
import random
import string

import bcrypt
from app.database import async_session, init_db
from app.models.user import User
from app.models.student import Student
from app.models.app_log import AppLog
from app.models.location_log import LocationLog

random.seed(42)

FIRST_NAMES = [
    "Andi", "Siti", "Budi", "Dewi", "Rizky", "Putri", "Fajar", "Nella",
    "Yoga", "Anisa", "Bayu", "Citra", "Dimas", "Eka", "Fahri", "Gita",
    "Hadi", "Ika", "Joko", "Kartika", "Lina", "Mira", "Nico", "Oki",
    "Putra", "Qori", "Rani", "Sandi", "Tia", "Umar", "Vina", "Wahyu",
    "Yuni", "Zain", "Ayu", "Bima", "Candra", "Diah", "Eko", "Fitri",
    "Gani", "Hana", "Indra", "Jihan", "Kiki", "Luki", "Mila", "Naufal",
    "Olivia", "Panji", "Raka", "Salsa", "Toni", "Vino", "Wati", "Zara",
    "Adi", "Bella", "Chandra", "Dina", "Eri", "Fani", "Galang", "Hesti",
    "Irfan", "Jasmin", "Kevin", "Lia", "Miko", "Nindi", "Oscar", "Pia",
    "Quincy", "Rizka", "Santi", "Tino", "Ulul", "Vera", "Willi", "Yosi",
]
LAST_NAMES = [
    "Pratama", "Santoso", "Lestari", "Ramadhan", "Anjani", "Nugroho",
    "Safitri", "Setiawan", "Wijaya", "Kusuma", "Hidayat", "Rahman",
    "Utami", "Saputra", "Ningsih", "Hartono", "Wibowo", "Susanto",
    "Kurniawan", "Andika", "Permana", "Darmawan", "Gunawan", "Siregar",
    "Oktaviani", "Purnama", "Rahayu", "Santika", "Maulana", "Fadil",
    "Arifin", "Putra", "Yuda", "Hsb", "Basri", "Polii", "Sibua",
]
STUDY_PROGRAMS = ["Informatika", "Sistem Informasi", "Teknik Komputer"]
SEMESTERS = [1, 3, 5, 7]


def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


async def seed():
    await init_db()

    async with async_session() as s:
        # Check existing logs count
        from sqlalchemy import select, func, text
        logs_q = await s.execute(select(func.count()).select_from(AppLog))
        existing_logs = logs_q.scalar() or 0
        print(f"Existing app logs: {existing_logs}")

        # Check existing students
        stu_q = await s.execute(select(func.count()).select_from(Student))
        existing_students = (stu_q.scalar() or 0)
        print(f"Existing students: {existing_students}")

        # Get existing student IDs
        stu_result = await s.execute(select(Student))
        existing_student_ids = {row.id for row in stu_result.scalars().all()}
        print(f"Existing student IDs: {len(existing_student_ids)}")

        target_logs = 10000
        needed = max(0, target_logs - existing_logs)
        print(f"Need to add: {needed} app logs")

        # Add more students if needed
        target_students = 120  # Want ~120 students total
        students_to_add = max(0, target_students - existing_students)
        new_students = []

        if students_to_add > 0:
            print(f"\n➕ Adding {students_to_add} new students...")
            # Get advisor (first dosen)
            dosen_q = await s.execute(select(User).where(User.role == "dosen").limit(1))
            advisor = dosen_q.scalar_one_or_none()
            advisor_id = advisor.id if advisor else None

            for i in range(students_to_add):
                idx = existing_students + i
                fname = random.choice(FIRST_NAMES)
                lname = random.choice(LAST_NAMES)
                name = f"{fname} {lname}"
                nim = f"41142{idx:03d}"
                prog = random.choice(STUDY_PROGRAMS)
                sem = random.choice(SEMESTERS)

                u = User(
                    id=str(uuid.uuid4()),
                    username=nim,
                    email=f"{nim.lower()}@student.ung.ac.id",
                    hashed_password=hash_pw("mahasiswa123"),
                    role="mahasiswa",
                    is_active=True,
                )
                s.add(u)
                await s.flush()

                stu = Student(
                    id=str(uuid.uuid4()),
                    user_id=u.id,
                    nim=nim,
                    full_name=name,
                    study_program=prog,
                    semester=sem,
                    advisor_id=advisor_id,
                )
                s.add(stu)
                new_students.append(stu)

                if len(new_students) % 20 == 0:
                    print(f"   ... added {len(new_students)}/{students_to_add} students")

            await s.commit()
            print(f"✅ Added {len(new_students)} students")

        # Get all student IDs including new ones
        stu_result = await s.execute(select(Student))
        all_students = list(stu_result.scalars().all())
        all_student_ids = [s.id for s in all_students]
        print(f"\nTotal students now: {len(all_student_ids)}")

        # Generate 10,000 app logs
        print(f"\n📊 Generating {needed} new app logs...")
        await _seed_app_logs(s, all_students, needed)

        # Add location logs for new students
        print(f"\n📍 Generating location logs for {len(new_students)} new students...")
        await _seed_location_logs(s, new_students)

        await s.commit()

    await _print_stats()


async def _seed_app_logs(s, students, target: int):
    """Generate app logs to reach target total."""
    today = datetime.utcnow().date()

    # Group students by distraction type (for variety)
    # Divide into 3 groups: high, medium, low distraction
    profiles = [
        # High distraction (33%)
        dict(social=(60, 240), game=(30, 150), unlocks=(40, 120), during_class=0.6),
        # Medium distraction (34%)
        dict(social=(20, 120), game=(5, 60), unlocks=(15, 60), during_class=0.3),
        # Low distraction (33%)
        dict(social=(5, 60), game=(0, 20), unlocks=(3, 25), during_class=0.1),
    ]

    batch = []
    inserted = 0

    while inserted < target:
        # Pick random student and profile
        stu = random.choice(students)
        prof = random.choice(profiles)

        # Random day in last 90 days
        days_back = random.randint(0, 89)
        day = today - timedelta(days=days_back)
        weekday = day.weekday()
        is_school_day = weekday < 5

        social = random.randint(*prof["social"])
        game = random.randint(*prof["game"])
        unlocks = random.randint(*prof["unlocks"])
        during_class = random.random() < prof["during_class"] if is_school_day else False
        other = random.randint(10, 120) if is_school_day else random.randint(60, 240)
        total_time = social + game + other

        # Idempotency key
        idem = f"{stu.id}-{day.isoformat()}-{uuid.uuid4().hex[:6]}"

        log = AppLog(
            id=str(uuid.uuid4()),
            student_id=stu.id,
            log_date=datetime.combine(day, datetime.min.time()),
            total_screen_time_minutes=total_time,
            social_media_time_minutes=social,
            game_time_minutes=game,
            other_app_time_minutes=other,
            unlock_count=unlocks,
            used_during_class=during_class,
            device_id=f"dev-{stu.nim}",
            idempotency_key=idem,
            is_synced=random.random() < 0.7,
        )
        batch.append(log)
        inserted += 1

        if len(batch) >= 500:
            s.add_all(batch)
            await s.flush()
            print(f"   ... inserted {inserted}/{target} logs ({len(batch)} batch)")
            batch = []

    if batch:
        s.add_all(batch)
        await s.flush()
        print(f"   ✅ Total inserted: {inserted}/{target}")


async def _seed_location_logs(s, students):
    """Generate location logs for new students."""
    if not students:
        return

    today = datetime.utcnow().date()
    campus_lat, campus_lon = 0.5387, 123.0645

    batch = []
    total = 0

    for stu in students:
        for d in range(30):  # 30 days of location data
            day = today - timedelta(days=d)
            if day.weekday() >= 5:
                continue

            for hour in [8, 10, 12, 14, 16]:
                is_on_campus = random.random() < 0.75
                if is_on_campus:
                    lat = campus_lat + random.uniform(-0.002, 0.002)
                    lon = campus_lon + random.uniform(-0.002, 0.002)
                    matches = random.random() < 0.7
                else:
                    lat = campus_lat + random.uniform(-0.05, 0.05)
                    lon = campus_lon + random.uniform(-0.05, 0.05)
                    matches = False

                loc = LocationLog(
                    id=str(uuid.uuid4()),
                    student_id=stu.id,
                    recorded_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=hour),
                    latitude=lat,
                    longitude=lon,
                    campus_id=None,
                    is_on_campus=is_on_campus,
                    matches_schedule=matches,
                    idempotency_key=f"loc-{stu.id}-{day.isoformat()}-{hour}",
                    is_synced=random.random() < 0.7,
                )
                batch.append(loc)
                total += 1

                if len(batch) >= 500:
                    s.add_all(batch)
                    await s.flush()
                    batch = []

    if batch:
        s.add_all(batch)
        await s.flush()

    print(f"   ✅ Inserted ~{total} location logs")


async def _print_stats():
    from app.database import engine
    from sqlalchemy import select, func
    async with async_session() as s:
        logs_q = await s.execute(select(func.count()).select_from(AppLog))
        locs_q = await s.execute(select(func.count()).select_from(LocationLog))
        stu_q = await s.execute(select(func.count()).select_from(Student))

        logs_count = logs_q.scalar()
        locs_count = locs_q.scalar()
        stu_count = stu_q.scalar()

        avg_screen = await s.execute(select(func.avg(AppLog.total_screen_time_minutes)))
        avg_unlocks = await s.execute(select(func.avg(AppLog.unlock_count)))

        print(f"\n{'='*50}")
        print(f"  📈 Dataset Statistics")
        print(f"{'='*50}")
        print(f"   Students       : {stu_count}")
        print(f"   App Logs       : {logs_count:,}")
        print(f"   Location Logs  : {locs_count:,}")
        print(f"   Total Records  : {stu_count + logs_count + locs_count:,}")
        print(f"\n   Avg screen time: {avg_screen.scalar():.1f} min/day")
        print(f"   Avg unlocks    : {avg_unlocks.scalar():.1f}/day")

        # Cluster distribution preview
        during_class_q = await s.execute(
            select(
                func.sum(AppLog.used_during_class.cast(Integer)),
                func.count(AppLog.id)
            )
        )
        during_count, total_count = during_class_q.one()
        print(f"   Used during class: {during_count}/{total_count} records ({100*during_count/total_count:.1f}%)")

        print(f"\n✅ Dataset ready for data mining pipeline!")

    await engine.dispose()


if __name__ == "__main__":
    print("=" * 50)
    print("  Sistem Monitoring Distraksi — Seed 10K Dataset")
    print("=" * 50)
    asyncio.run(seed())
