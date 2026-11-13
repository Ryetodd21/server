"""
Seed initial data — admin user, lecturer, students, dummy logs.
Usage: PYTHONPATH=. ./venv/bin/python scripts/seed.py
"""
import asyncio
import uuid
from datetime import datetime, timedelta
import random

import bcrypt
from app.database import async_session, init_db
from app.models.user import User
from app.models.student import Student
from app.models.campus import Campus
from app.models.schedule import Subject, Lecturer, Schedule
from app.models.app_log import AppLog
from app.models.location_log import LocationLog


def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


async def seed():
    await init_db()
    async with async_session() as s:
        # Admin
        admin = User(
            id=str(uuid.uuid4()),
            username="admin",
            email="admin@ung.ac.id",
            hashed_password=hash_pw("admin123"),
            role="admin",
            is_active=True,
            full_name="Administrator",
        )
        s.add(admin)

        # Dosen
        dosen = User(
            id=str(uuid.uuid4()),
            username="dosen1",
            email="dosen1@ung.ac.id",
            hashed_password=hash_pw("dosen123"),
            role="dosen",
            is_active=True,
            full_name="Dr. Ahmad Hasan, M.Kom.",
        )
        s.add(dosen)
        lecturer = Lecturer(
            id=str(uuid.uuid4()),
            name="Dr. Ahmad Hasan, M.Kom.",
            nidn="198501012010121001",
        )
        s.add(lecturer)

        # Campus
        campus = Campus(
            id=str(uuid.uuid4()),
            name="UNG - Kampus Utama",
            latitude=0.5387,
            longitude=123.0645,
            radius_meters=300,
        )
        s.add(campus)

        # Subject
        subject = Subject(
            id=str(uuid.uuid4()),
            code="IF211",
            name="Basis Data",
        )
        s.add(subject)

        # Students
        names = [
            "Andi Pratama", "Siti Nurhaliza", "Budi Santoso", "Dewi Lestari",
            "Rizky Ramadhan", "Putri Anjani", "Fajar Nugroho", "Nella Safitri",
            "Yoga Pratama", "Anisa Putri", "Bayu Setiawan", "Citra Dewi",
            "Hendro Wijaya", "Maya Sari", "Reza Pratama", "Indah Permata",
            "Dimas Setiawan", "Rina Anggraini", "Bayu Pamungkas", "Sinta Dewi",
        ]
        students = []
        for i, name in enumerate(names):
            nim = f"411423{i+1:03d}"
            u = User(
                id=str(uuid.uuid4()),
                username=nim,
                email=f"{nim}@student.ung.ac.id",
                hashed_password=hash_pw("mahasiswa123"),
                role="mahasiswa",
                is_active=True,
                full_name=name,
            )
            s.add(u)
            stu = Student(
                id=str(uuid.uuid4()),
                user_id=u.id,
                nim=nim,
                full_name=name,
                program_studi="Informatika",
                semester=5,
                angkatan=2022,
                advisor_id=dosen.id,
            )
            s.add(stu)
            students.append(stu)

        # Schedule samples
        for stu in students[:5]:
            sched = Schedule(
                id=str(uuid.uuid4()),
                subject_id=subject.id,
                lecturer_id=lecturer.id,
                student_id=stu.id,
                day_of_week=0,
                start_time="07:30",
                end_time="10:00",
                room="Lab 1",
                campus_id=campus.id,
            )
            s.add(sched)

        # App logs (14 days, per student per day)
        today = datetime.utcnow().date()
        for stu in students:
            for d in range(14):
                day = today - timedelta(days=d)
                social = random.randint(20, 180)
                game = random.randint(0, 60)
                other = random.randint(10, 90)
                total = social + game + other
                log = AppLog(
                    id=str(uuid.uuid4()),
                    student_id=stu.id,
                    timestamp=datetime.combine(day, datetime.min.time()) + timedelta(hours=random.randint(8, 18)),
                    app_package=f"app.{random.choice(['instagram', 'tiktok', 'mlbb', 'chrome'])}.android",
                    app_name=random.choice(["Instagram", "TikTok", "Mobile Legends", "Chrome"]),
                    duration_seconds=total * 60,
                    category=random.choice(["social_media", "game", "other"]),
                    is_foreground=True,
                )
                s.add(log)

        # Location logs (weekdays at campus)
        for stu in students:
            for d in range(14):
                day = today - timedelta(days=d)
                if day.weekday() < 5:
                    log = LocationLog(
                        id=str(uuid.uuid4()),
                        student_id=stu.id,
                        timestamp=datetime.combine(day, datetime.min.time()) + timedelta(hours=random.randint(7, 16)),
                        latitude=campus.latitude + random.uniform(-0.001, 0.001),
                        longitude=campus.longitude + random.uniform(-0.001, 0.001),
                        is_on_campus=True,
                        matches_schedule=(d % 2 == 0),
                    )
                    s.add(log)

        await s.commit()
        print(f"Seeded: 1 admin, 1 dosen, {len(students)} students, {14*len(students)} app logs, {14*len(students)} location logs")

    from app.database import engine
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
