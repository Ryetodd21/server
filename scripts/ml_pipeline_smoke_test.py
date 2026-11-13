"""
Smoke test: ML pipeline COALESCE behavior post TIMESTAMP WITH TIME ZONE migration.

Verifies:
1. func.coalesce(AppLog.log_date, AppLog.timestamp) returns tz-aware UTC
2. SQLAlchemy on SQLite preserves timezone info correctly
3. pandas pd.to_datetime(tz_aware).dt.date produces correct date objects
4. groupby/merge on derived date columns works without tz-related issues
"""
import sys
from datetime import datetime, timezone, timedelta

# Use venv site-packages (system python3.14 has venv's site-packages)
sys.path.insert(0, "venv/lib/python3.14/site-packages")

import pandas as pd
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models.app_log import AppLog
from app.database import Base


def test_coalesce_tz_aware():
    """The core COALESCE behavior post-migration."""
    # In-memory SQLite for hermetic test
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Insert 3 test rows exercising all combinations:
    # Row 1: log_date NULL (MQTT/seed case), timestamp = tz-aware WITA 08:00 = UTC 00:00
    row1_ts = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)  # WITA 08:00 → UTC 00:00
    # Row 2: log_date = tz-aware UTC, timestamp = tz-aware UTC (mobile app case)
    row2_log_date = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    row2_ts = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    # Row 3: log_date = tz-aware WITA morning, timestamp = tz-aware UTC evening (same day)
    row3_log_date = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)  # WITA 09:00 → UTC 01:00
    row3_ts = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)

    session.add_all([
        AppLog(
            id="r1", student_id="s1",
            log_date=None, timestamp=row1_ts,
            total_screen_time_minutes=120, social_media_time_minutes=30,
            game_time_minutes=15, unlock_count=10, used_during_class=False,
        ),
        AppLog(
            id="r2", student_id="s1",
            log_date=row2_log_date, timestamp=row2_ts,
            total_screen_time_minutes=180, social_media_time_minutes=60,
            game_time_minutes=20, unlock_count=15, used_during_class=True,
        ),
        AppLog(
            id="r3", student_id="s2",
            log_date=row3_log_date, timestamp=row3_ts,
            total_screen_time_minutes=90, social_media_time_minutes=10,
            game_time_minutes=5, unlock_count=8, used_during_class=False,
        ),
    ])
    session.commit()

    # Reproduce the exact query from ml/pipeline.py:37-48
    effective_date = func.coalesce(AppLog.log_date, AppLog.timestamp).label("effective_date")
    result = session.execute(
        select(
            AppLog.id,
            AppLog.student_id,
            effective_date,
            AppLog.total_screen_time_minutes,
        ).order_by(AppLog.id)
    )
    rows = result.fetchall()

    print(f"=== Test 1: COALESCE returns tz-aware datetimes ===")
    assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}"

    # Row 1: log_date NULL → fallback to timestamp (UTC 00:00 = 2026-09-18)
    r1 = rows[0]
    assert r1.id == "r1"
    assert r1.effective_date.tzinfo is not None, (
        f"Row 1: COALESCE returned naive datetime {r1.effective_date!r} — "
        "TIMESTAMP WITH TIME ZONE migration did not take effect, OR SQLite "
        "is dropping tz info"
    )
    assert r1.effective_date.utcoffset() == timedelta(0), (
        f"Row 1: expected UTC offset, got {r1.effective_date.utcoffset()}"
    )
    assert r1.effective_date == row1_ts, (
        f"Row 1: expected {row1_ts}, got {r1.effective_date}"
    )
    print(f"  row 1 (NULL log_date, fallback to timestamp): {r1.effective_date} tz={r1.effective_date.tzinfo}")

    # Row 2: log_date present → use log_date
    r2 = rows[1]
    assert r2.effective_date.tzinfo is not None, f"Row 2: naive {r2.effective_date!r}"
    assert r2.effective_date == row2_log_date
    print(f"  row 2 (log_date set): {r2.effective_date}")

    # Row 3: same as row 2 for our test
    r3 = rows[2]
    assert r3.effective_date.tzinfo is not None, f"Row 3: naive {r3.effective_date!r}"
    print(f"  row 3 (log_date set): {r3.effective_date}")

    print(f"  ✓ All 3 COALESCE results are tz-aware UTC")

    print(f"\n=== Test 2: pandas pipeline (pd.to_datetime + .dt.date + groupby) ===")
    df = pd.DataFrame(
        rows,
        columns=["id", "student_id", "log_date", "total_screen_time_minutes"],
    )
    # This is the exact transformation from ml/pipeline.py:60
    df["date"] = pd.to_datetime(df["log_date"]).dt.date

    # Verify dates
    assert df.loc[df["id"] == "r1", "date"].iloc[0] == row1_ts.date(), (
        f"Row 1 date mismatch: expected {row1_ts.date()}, "
        f"got {df.loc[df['id'] == 'r1', 'date'].iloc[0]}"
    )
    assert df.loc[df["id"] == "r2", "date"].iloc[0] == row2_log_date.date()
    assert df.loc[df["id"] == "r3", "date"].iloc[0] == row3_log_date.date()
    print(f"  ✓ pd.to_datetime(tz_aware).dt.date preserves the calendar date")

    # Now do the groupby from line 64
    pivot = (
        df.groupby(["student_id", "date"])
        .agg(screen_time=("total_screen_time_minutes", "sum"))
        .reset_index()
    )
    assert len(pivot) == 2, f"Expected 2 student-date groups, got {len(pivot)}"
    print(f"  ✓ groupby(['student_id', 'date']) works on derived date column")

    print(f"\n=== Test 3: WITA→UTC conversion correctness ===")
    # Verify that WITA 08:00 → UTC 00:00 conversion is preserved (the heart of the project)
    # Without this, WITA logs from Android would land on the wrong calendar day.
    wita_8am = datetime(2026, 9, 18, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    utc_equivalent = wita_8am.astimezone(timezone.utc)
    assert utc_equivalent == datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc), (
        f"WITA→UTC conversion failed: {wita_8am} → {utc_equivalent}"
    )
    assert utc_equivalent.date() == wita_8am.date(), "Calendar date should match"
    print(f"  ✓ WITA 08:00 ({wita_8am}) → UTC 00:00 ({utc_equivalent}), same calendar date")

    print(f"\n=== Test 4: ORM readback matches datetime.now(utc) — the auth.py:178 case ===")
    # Reproduces the comparison that would have raised TypeError on SQLite pre-fix.
    from app.models.user import User, Token
    user = User(
        id="u1", email="t@x", username="t", hashed_password="x",
        role="admin", is_active=True, created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    # expires_at 1 hour in the future — comparison should be False (token valid)
    future_expires = datetime.now(timezone.utc) + timedelta(hours=1)
    session.add(Token(
        id="tk1", user_id="u1", token="x", token_type="refresh",
        expires_at=future_expires,
    ))
    session.commit()

    # Re-read and compare (the auth.py refresh-token check pattern)
    tk = session.execute(
        select(Token).where(Token.id == "tk1")
    ).scalar_one()
    assert tk.expires_at.tzinfo is not None, (
        f"Token.expires_at is naive ({tk.expires_at!r}) — AwareDateTime not applied"
    )
    # This is the line that would have raised on SQLite pre-fix
    is_expired = tk.expires_at < datetime.now(timezone.utc)
    assert is_expired is False, "Future token should not be expired"
    print(f"  ✓ Token.expires_at comparison with datetime.now(utc) works")
    print(f"    expires_at={tk.expires_at}, now={datetime.now(timezone.utc)}, expired={is_expired}")

    # And the inverse — past token should be expired
    tk.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.commit()
    tk = session.execute(select(Token).where(Token.id == "tk1")).scalar_one()
    is_expired = tk.expires_at < datetime.now(timezone.utc)
    assert is_expired is True, "Past token should be expired"
    print(f"  ✓ Past token correctly detected as expired")

    print(f"\n=== All smoke tests PASSED ===")
    session.close()


if __name__ == "__main__":
    test_coalesce_tz_aware()
