"""
Seed Data Generator — Monitor UNG.

Membuat:
- 15 mahasiswa (5 Rendah, 5 Sedang, 5 Tinggi distraksi)
- ~10,000 AppLog entries (session-level, siap mining)
- Associated User accounts (inactive, untuk login mahasiswa)

Usage:
    python -m app.seed_data

Atau dari API:
    POST /api/seed/generate  (admin only)
    DELETE /api/seed/clear   (admin only)
"""
import uuid
import random
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Generator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_sync_engine
from app.config import get_settings
from app.timezone import WITA

settings = get_settings()
from app.models.user import User
from app.models.student import Student
from app.models.app_log import AppLog

SEED_BASE = datetime(2026, 8, 1, tzinfo=WITA)
SEED_END = datetime.now(WITA).replace(hour=23, minute=59, second=59) - timedelta(days=1)

# ─── Student definitions ────────────────────────────────────────────────────────
# Distraction level menentukan pattern usage
STUDENTS = [
    # ── Cluster 0: Rendah (Fokus) ──
    {"nim": "A1A122001", "name": "Rizki Ramadhan",        "prodi": "Informatika",      "level": "rendah",  "base_sot": 35,  "base_unlock": 12, "used_class_pct": 0.10},
    {"nim": "A1A122002", "name": "Putri Ayu Lestari",    "prodi": "Informatika",      "level": "rendah",  "base_sot": 42,  "base_unlock": 15, "used_class_pct": 0.12},
    {"nim": "A1A122003", "name": "Ahmad Faisal Hidayat",  "prodi": "Sistem Informasi",  "level": "rendah",  "base_sot": 50,  "base_unlock": 18, "used_class_pct": 0.08},
    {"nim": "A1A122004", "name": "Siti Nurhaliza",        "prodi": "Teknik Komputer",  "level": "rendah",  "base_sot": 38,  "base_unlock": 14, "used_class_pct": 0.10},
    {"nim": "A1A122005", "name": "Budi Santoso",          "prodi": "Informatika",      "level": "rendah",  "base_sot": 55,  "base_unlock": 20, "used_class_pct": 0.15},
    # ── Cluster 1: Sedang (Moderat) ──
    {"nim": "B1B124001", "name": "Dewi Kusuma Wardani",   "prodi": "Informatika",      "level": "sedang",  "base_sot": 85,  "base_unlock": 28, "used_class_pct": 0.40},
    {"nim": "B1B124002", "name": "Farhan Rivaldi",        "prodi": "Sistem Informasi",  "level": "sedang",  "base_sot": 95,  "base_unlock": 32, "used_class_pct": 0.45},
    {"nim": "B1B124003", "name": "Nadia Putriani",        "prodi": "Teknik Komputer",  "level": "sedang",  "base_sot": 78,  "base_unlock": 25, "used_class_pct": 0.35},
    {"nim": "B1B124004", "name": "Hendra Wijaya",         "prodi": "Informatika",      "level": "sedang",  "base_sot": 110, "base_unlock": 38, "used_class_pct": 0.50},
    {"nim": "B1B124005", "name": "Rini Amalia Safitri",   "prodi": "Sistem Informasi",  "level": "sedang",  "base_sot": 90,  "base_unlock": 30, "used_class_pct": 0.42},
    # ── Cluster 2: Tinggi (Distract) ──
    {"nim": "C1C126001", "name": "Zacky Pratama",         "prodi": "Informatika",      "level": "tinggi",  "base_sot": 155, "base_unlock": 55, "used_class_pct": 0.80},
    {"nim": "C1C126002", "name": "Anisa Rahmawati",       "prodi": "Sistem Informasi",  "level": "tinggi",  "base_sot": 175, "base_unlock": 62, "used_class_pct": 0.85},
    {"nim": "C1C126003", "name": "Yoga Prasetyo",         "prodi": "Teknik Komputer",  "level": "tinggi",  "base_sot": 165, "base_unlock": 58, "used_class_pct": 0.78},
    {"nim": "C1C126004", "name": "Lina Marlina",          "prodi": "Informatika",      "level": "tinggi",  "base_sot": 185, "base_unlock": 68, "used_class_pct": 0.88},
    {"nim": "C1C126005", "name": "Bayu Firmansyah",       "prodi": "Sistem Informasi",  "level": "tinggi",  "base_sot": 160, "base_unlock": 52, "used_class_pct": 0.75},
]

# ─── App categories ────────────────────────────────────────────────────────────

# Social media apps (distracting)
SOCIAL_APPS = [
    ("com.instagram.android", "Instagram"),
    ("com.facebook.katana", "Facebook"),
    ("com.twitter.android", "X (Twitter)"),
    ("com.snapchat.android", "Snapchat"),
    ("com.zhiliaoapp.musically", "TikTok"),
    ("com.whatsapp", "WhatsApp"),
    ("org.telegram.messenger", "Telegram"),
]

# Game apps
GAME_APPS = [
    ("com.supercell.clashofclans", "Clash of Clans"),
    ("com.mobile.legends", "Mobile Legends"),
    ("com.pubg.krmobile", "PUBG Mobile"),
    ("com.innersloth.amongus", "Among Us"),
    ("com.roblox.client", "Roblox"),
]

# Productive / neutral apps
PRODUCTIVE_APPS = [
    ("com.google.android.apps.books", "Google Books"),
    ("com.microsoft.office.word", "Microsoft Word"),
    ("com.spotify.music", "Spotify"),
    ("com.google.android.gm", "Gmail"),
    ("com.android.chrome", "Chrome"),
]


def _hash_password(password: str) -> str:
    import bcrypt as _bcrypt
    return _bcrypt.hashpw(
        password.encode("utf-8"),
        _bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS),
    ).decode("utf-8")


def _generate_sessions(
    student: dict,
    day: datetime,
    is_weekend: bool,
    rng: random.Random,
) -> list[dict]:
    """
    Generate session-level app logs for one day for one student.
    Returns list of session dicts ready for AppLog creation.
    """
    level = student["level"]

    # Weekend = more free time, different pattern
    multiplier = 1.4 if is_weekend else 1.0

    # Base values with daily variation (±30%)
    sot_variation = rng.uniform(0.7, 1.3)
    sot = int(student["base_sot"] * multiplier * sot_variation)

    unlock_variation = rng.uniform(0.7, 1.3)
    unlock_count = int(student["base_unlock"] * multiplier * unlock_variation)

    used_during_class = rng.random() < student["used_class_pct"]

    # Distribute SOT across categories
    if level == "rendah":
        social_pct = rng.uniform(0.20, 0.35)
        game_pct = rng.uniform(0.10, 0.20)
    elif level == "sedang":
        social_pct = rng.uniform(0.35, 0.50)
        game_pct = rng.uniform(0.20, 0.30)
    else:  # tinggi
        social_pct = rng.uniform(0.45, 0.60)
        game_pct = rng.uniform(0.25, 0.35)

    social_min = int(sot * social_pct)
    game_min = int(sot * game_pct)
    other_min = sot - social_min - game_min

    sessions = []

    # Generate session timestamps throughout the day
    if is_weekend:
        # Weekend: scattered throughout 10:00-23:00
        start_hour = 10
        end_hour = 23
    else:
        # Weekday: morning (07-09), lunch (11-13), evening (16-22)
        peak_hours = list(range(7, 9)) + list(range(11, 14)) + list(range(16, 23))
        start_hour = min(peak_hours)
        end_hour = max(peak_hours) + 1

    # Build session list
    # Strategy: generate 8-20 sessions per day
    n_sessions = rng.randint(8, 20)
    session_times = sorted(
        [rng.randint(start_hour, end_hour - 1) for _ in range(n_sessions)]
    )

    for hour in session_times:
        # Determine category based on distribution
        r = rng.random()
        if r < social_pct and social_min > 0:
            pkg, name = rng.choice(SOCIAL_APPS)
            cat = "social_media"
            duration = rng.randint(2, max(3, social_min // max(n_sessions // 2, 1)))
            social_min = max(0, social_min - duration)
        elif r < social_pct + game_pct and game_min > 0:
            pkg, name = rng.choice(GAME_APPS)
            cat = "game"
            duration = rng.randint(3, max(5, game_min // max(n_sessions // 3, 1)))
            game_min = max(0, game_min - duration)
        else:
            if other_min <= 0:
                continue
            pkg, name = rng.choice(PRODUCTIVE_APPS)
            cat = "productive"
            duration = rng.randint(2, max(2, other_min // max(n_sessions // 2, 1)))
            other_min = max(0, other_min - duration)

        # Used during class: morning-lunch hours on weekdays
        is_class_hour = not is_weekend and 7 <= hour <= 13
        session_used_class = used_during_class and is_class_hour and rng.random() < 0.7

        minute = rng.randint(0, 59)
        session_ts = day.replace(hour=hour, minute=minute, second=0)

        sessions.append({
            "app_package": pkg,
            "app_name": name,
            "duration_seconds": duration * 60,
            "category": cat,
            "is_foreground": rng.random() < 0.8,
            "timestamp": session_ts,
            "total_screen_time_minutes": sot,
            "social_media_time_minutes": social_min,
            "game_time_minutes": game_min,
            "other_app_time_minutes": other_min,
            "unlock_count": unlock_count,
            "used_during_class": session_used_class,
            "is_synced": True,
            "source": "seed",
        })

    return sessions


def _date_range(start: datetime, end: datetime) -> Generator[datetime, None, None]:
    """Generate dates from start to end (inclusive)."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def generate_seed(db: Session) -> dict:
    """
    Generate seed data in the database.
    Returns summary of what was created.

    Re-runnable: if a Student with the same NIM or a User with the same email
    already exists (e.g. a previous run crashed mid-loop and orphaned a User
    row), reuses the existing id instead of failing on UNIQUE.
    """
    rng = random.Random(42)  # Fixed seed for reproducibility
    engine = get_sync_engine()

    # Track what we create
    users_created = 0
    students_created = 0
    logs_created = 0
    skipped = 0

    # Check if data already exists
    existing = db.execute(text("SELECT COUNT(*) FROM students")).scalar() or 0
    if existing > 0:
        skipped = existing
        print(f"[seed] {existing} students already exist — skipping student creation")

    # ── Create students + user accounts ────────────────────────────────────────
    student_records = {}

    for i, sdef in enumerate(STUDENTS):
        # Check if NIM already exists
        existing_student = db.execute(
            text("SELECT id FROM students WHERE nim = :nim"),
            {"nim": sdef["nim"]}
        ).fetchone()

        if existing_student:
            student_id = existing_student[0]
        else:
            # Check if a User with this email already exists — a previous run
            # may have committed the User row and then crashed before the
            # Student row (orphan user). Reuse it instead of failing UNIQUE.
            email = f"{sdef['nim'].lower()}@student.ung.ac.id"
            existing_user = db.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": email}
            ).fetchone()

            if existing_user:
                user_id = existing_user[0]
                print(f"[seed] reusing orphan user for {email}")
            else:
                # Create User first
                user_id = str(uuid.uuid4())
                password_hash = _hash_password("seed123456")
                db.execute(
                    text("""
                        INSERT INTO users (id, username, email, hashed_password, full_name,
                                          jabatan, jurusan, nip, phone, avatar_url, bio,
                                          is_active, role, created_at, updated_at)
                        VALUES (:id, :username, :email, :hp, :fn, :jab, :jur, :nip, :phone,
                                '', '', False, 'mahasiswa', :created_at, :updated_at)
                    """),
                    {
                        "id": user_id,
                        "username": sdef["nim"],
                        "email": email,
                        "hp": password_hash,
                        "fn": sdef["name"],
                        "jab": "mahasiswa",
                        "jur": sdef["prodi"],
                        "nip": sdef["nim"],
                        "phone": "",
                        "created_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
                users_created += 1

            # Create Student
            student_id = str(uuid.uuid4())
            db.execute(
                text("""
                    INSERT INTO students (id, user_id, nim, full_name, program_studi,
                                         angkatan, semester, phone, avatar_url,
                                         is_active, created_at, updated_at)
                    VALUES (:id, :uid, :nim, :fn, :prodi, 2022, 5, '', '', True,
                            :ts, :ts)
                """),
                {
                    "id": student_id,
                    "uid": user_id,
                    "nim": sdef["nim"],
                    "fn": sdef["name"],
                    "prodi": sdef["prodi"],
                    "ts": datetime.now(timezone.utc),
                },
            )
            students_created += 1

        student_records[sdef["nim"]] = (student_id, sdef)

    db.commit()

    # ── Generate AppLog entries ────────────────────────────────────────────────
    print(f"[seed] Generating AppLog entries...")

    batch = []
    BATCH_SIZE = 500

    for day in _date_range(SEED_BASE, SEED_END):
        is_weekend = day.weekday() >= 5  # Saturday=5, Sunday=6

        for sdef in STUDENTS:
            student_id, sdata = student_records[sdef["nim"]]

            sessions = _generate_sessions(sdef, day, is_weekend, rng)

            for sess in sessions:
                log_id = str(uuid.uuid4())
                batch.append({
                    "id": log_id,
                    "student_id": student_id,
                    "app_package": sess["app_package"],
                    "app_name": sess["app_name"],
                    "duration_seconds": sess["duration_seconds"],
                    "category": sess["category"],
                    "is_foreground": sess["is_foreground"],
                    "timestamp": sess["timestamp"],
                    "total_screen_time_minutes": sess["total_screen_time_minutes"],
                    "social_media_time_minutes": sess["social_media_time_minutes"],
                    "game_time_minutes": sess["game_time_minutes"],
                    "other_app_time_minutes": sess["other_app_time_minutes"],
                    "unlock_count": sess["unlock_count"],
                    "used_during_class": sess["used_during_class"],
                    "is_synced": True,
                    "source": "seed",
                    "created_at": datetime.now(timezone.utc),
                })

                if len(batch) >= BATCH_SIZE:
                    db.execute(
                        text("""
                            INSERT INTO app_logs (id, student_id, app_package, app_name,
                                duration_seconds, category, is_foreground, timestamp,
                                total_screen_time_minutes, social_media_time_minutes,
                                game_time_minutes, other_app_time_minutes,
                                unlock_count, used_during_class, is_synced,
                                source, created_at)
                            VALUES (:id, :student_id, :app_package, :app_name,
                                :duration_seconds, :category, :is_foreground, :timestamp,
                                :total_screen_time_minutes, :social_media_time_minutes,
                                :game_time_minutes, :other_app_time_minutes,
                                :unlock_count, :used_during_class, :is_synced,
                                :source, :created_at)
                        """),
                        batch,
                    )
                    logs_created += len(batch)
                    batch = []

    # Flush remaining
    if batch:
        db.execute(
            text("""
                INSERT INTO app_logs (id, student_id, app_package, app_name,
                    duration_seconds, category, is_foreground, timestamp,
                    total_screen_time_minutes, social_media_time_minutes,
                    game_time_minutes, other_app_time_minutes,
                    unlock_count, used_during_class, is_synced,
                    source, created_at)
                VALUES (:id, :student_id, :app_package, :app_name,
                    :duration_seconds, :category, :is_foreground, :timestamp,
                    :total_screen_time_minutes, :social_media_time_minutes,
                    :game_time_minutes, :other_app_time_minutes,
                    :unlock_count, :used_during_class, :is_synced,
                    :source, :created_at)
            """),
            batch,
        )
        logs_created += len(batch)

    db.commit()

    total = db.execute(text("SELECT COUNT(*) FROM app_logs")).scalar() or 0

    return {
        "students_created": students_created,
        "users_created": users_created,
        "logs_created": logs_created,
        "total_logs": total,
        "students_skipped": skipped,
        "days": (SEED_END - SEED_BASE).days + 1,
    }


def clear_seed_data(db: Session) -> dict:
    """Delete all seed data (students, users, app_logs).

    Also cleans up orphan seed User rows (a previous run may have committed
    the User and then crashed before the Student row) — those still have the
    `@student.ung.ac.id` email pattern but no linked Student row, so the
    normal student-link delete misses them. Without this cleanup, the next
    `POST /api/seed/generate` would re-trigger the orphan-reuse path.
    """
    cleared_logs = db.execute(text("SELECT COUNT(*) FROM app_logs WHERE source = 'seed'")).scalar() or 0
    cleared_students = db.execute(text("SELECT COUNT(*) FROM students")).scalar() or 0

    db.execute(text("DELETE FROM app_logs WHERE source = 'seed'"))
    db.execute(text("DELETE FROM students WHERE nim LIKE 'A1A%' OR nim LIKE 'B1B%' OR nim LIKE 'C1C%'"))

    # Get user IDs to delete
    users_to_delete = db.execute(text("""
        SELECT u.id FROM users u
        JOIN students s ON s.user_id = u.id
        WHERE s.nim LIKE 'A1A%' OR s.nim LIKE 'B1B%' OR s.nim LIKE 'C1C%'
    """)).fetchall()
    for (uid,) in users_to_delete:
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})

    # Catch orphans: User rows with the seed email pattern but no Student row.
    # `NOT EXISTS` is fine here — students is a small table.
    db.execute(text("""
        DELETE FROM users
        WHERE email LIKE '%@student.ung.ac.id'
          AND NOT EXISTS (SELECT 1 FROM students s WHERE s.user_id = users.id)
    """))

    db.commit()

    return {
        "logs_cleared": cleared_logs,
        "students_cleared": cleared_students,
    }


if __name__ == "__main__":
    print("=== Monitor UNG — Seed Data Generator ===")
    print(f"Period: {SEED_BASE.date()} → {SEED_END.date()}")
    print(f"Students: {len(STUDENTS)} (5 Rendah, 5 Sedang, 5 Tinggi)")
    print("")

    engine = get_sync_engine()
    with Session(engine) as db:
        result = generate_seed(db)

    print("")
    print(f"=== Done ===")
    print(f"  Students created:  {result['students_created']}")
    print(f"  Users created:     {result['users_created']}")
    print(f"  Logs created:      {result['logs_created']}")
    print(f"  Total logs in DB:  {result['total_logs']}")
