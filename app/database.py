import logging
import re
from datetime import timezone as _timezone, datetime as _datetime

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import create_engine, text, select, func
from sqlalchemy.types import DateTime, TypeDecorator
import sqlalchemy
from app.config import get_settings
from fastapi import Request

settings = get_settings()
logger = logging.getLogger("monitor")

# Whitelist pattern for table/column identifiers — defense-in-depth against
# SQL injection if any migrations list is ever extended with values from
# env/config. PostgreSQL and SQLite both accept lowercase + underscore.
# Defined at module scope so all three migration functions can share it.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class AwareDateTime(TypeDecorator):
    """DateTime that normalizes to tz-aware UTC on both read and write.

    Bridges the SQLite / PostgreSQL gap introduced by the
    TIMESTAMP WITH TIME ZONE migration:

    - PostgreSQL: `DateTime(timezone=True)` maps to native
      `TIMESTAMP WITH TIME ZONE`. Read returns tz-aware datetime in UTC.
    - SQLite: SQLAlchemy has no native tz-aware datetime — `DateTime(timezone=True)`
      is stored as `DATETIME` (no offset), and ORM reads return naive datetimes.
      Without normalization, every comparison like
      `record.expires_at < datetime.now(timezone.utc)` would raise TypeError on dev.

    This decorator:
    - On write: converts naive → UTC, normalizes all writes to UTC.
    - On read:  if the result is naive (SQLite path), stamps tzinfo=UTC.

    Net effect: every value crossing this boundary is tz-aware UTC, regardless
    of dialect. The rest of the codebase can treat all datetimes as UTC.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=_timezone.utc)
        return value.astimezone(_timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite dropped tz info — restore as UTC. This is correct because
            # all writes go through process_bind_param which normalized to UTC.
            value = value.replace(tzinfo=_timezone.utc)
        return value

engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Sync engine for ML pipeline (sklearn + SQLAlchemy sync)
_sync_engine = None


def get_sync_engine():
    """Lazily-initialized synchronous engine for ML pipeline."""
    global _sync_engine
    if _sync_engine is None:
        sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
        if "sqlite" in sync_url and "+aiosqlite" in settings.DATABASE_URL:
            sync_url = sync_url.replace("sqlite", "sqlite")
        _sync_engine = create_engine(sync_url, echo=settings.DEBUG)
    return _sync_engine


def get_sync_session():
    """Return a context manager that yields a fresh synchronous Session
    bound to the sync engine. Use this from background tasks (ML serving,
    scheduler) where the FastAPI request-scoped `get_db` async session
    isn't available.

    Usage:
        with get_sync_session()() as db:
            ...
    """
    from contextlib import contextmanager
    from sqlalchemy.orm import sessionmaker, Session

    SessionLocal = sessionmaker(
        bind=get_sync_engine(),
        class_=Session,
        expire_on_commit=False,
    )

    @contextmanager
    def _ctx():
        s = SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return _ctx


class Base(DeclarativeBase):
    pass


async def get_db(request: Request) -> AsyncSession:
    async with async_session() as session:
        try:
            from app.academic_scope import configure_request_scope
            await configure_request_scope(session, request)
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables — run once at startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def run_migrations():
    """Lightweight idempotent migrations — add columns that may be missing on
    tables that pre-date the current model. Safe to run on every startup."""
    migrations = [
        # Users table
        ("users", "full_name", "VARCHAR(150) NOT NULL DEFAULT ''"),
        ("users", "jabatan", "VARCHAR(80) NOT NULL DEFAULT ''"),
        ("users", "jurusan", "VARCHAR(120) NOT NULL DEFAULT ''"),
        ("users", "nip", "VARCHAR(40) NOT NULL DEFAULT ''"),
        ("users", "phone", "VARCHAR(30) NOT NULL DEFAULT ''"),
        ("users", "avatar_url", "TEXT NOT NULL DEFAULT ''"),
        ("users", "bio", "TEXT NOT NULL DEFAULT ''"),
        ("users", "last_login", "DATETIME"),
        # Students table
        ("students", "program_studi", "VARCHAR(100) NOT NULL DEFAULT ''"),
        ("students", "angkatan", "INTEGER NOT NULL DEFAULT 2024"),
        ("students", "semester", "INTEGER NOT NULL DEFAULT 1"),
        ("students", "advisor_id", "VARCHAR(36)"),
        ("students", "phone", "VARCHAR(30) NOT NULL DEFAULT ''"),
        ("students", "avatar_url", "TEXT NOT NULL DEFAULT ''"),
        # Cluster results
        ("cluster_results", "cluster_label", "INTEGER NOT NULL DEFAULT 0"),
        ("cluster_results", "cluster_name", "VARCHAR(100) NOT NULL DEFAULT ''"),
        ("cluster_results", "result_date", "DATETIME"),
        # Model versions
        ("model_versions", "started_at", "DATETIME"),
        ("model_versions", "completed_at", "DATETIME"),
        ("model_versions", "train_size", "INTEGER NOT NULL DEFAULT 0"),
        ("model_versions", "test_size", "INTEGER NOT NULL DEFAULT 0"),
        ("model_versions", "pca_model_data", "TEXT"),
        ("model_versions", "kmeans_model_data", "TEXT"),
        ("model_versions", "scaler_model_data", "TEXT"),
        ("model_versions", "feature_columns", "TEXT"),
        # Round 8: program_studi sits inside a jurusan (department)
        ("program_studi", "jurusan_id", "VARCHAR(36)"),
        # Location logs — new columns from mobile app
        ("location_logs", "recorded_at", "DATETIME"),
        ("location_logs", "campus_id", "VARCHAR(36)"),
        ("location_logs", "schedule_id", "VARCHAR(36)"),
        ("location_logs", "idempotency_key", "VARCHAR(200)"),
    ]

    db_url = settings.DATABASE_URL.lower()
    is_sqlite = "sqlite" in db_url

    if is_sqlite:
        col_query = "PRAGMA table_info({table})"
        col_extractor = lambda row: row[1]
    else:
        # PostgreSQL: use information_schema
        col_query = "SELECT column_name FROM information_schema.columns WHERE table_name = :table AND table_schema = 'public'"
        col_extractor = lambda row: row[0]

    async with engine.begin() as conn:
        for table, column, ddl in migrations:
            # Validate identifiers — table and column come from the literal
            # migrations list today, but the pattern is fragile.
            if not _IDENT_RE.match(table) or not _IDENT_RE.match(column):
                logger.error(
                    f"[run_migrations] Rejected invalid identifier: "
                    f"table={table!r} column={column!r}"
                )
                continue
            try:
                if is_sqlite:
                    res = await conn.execute(text(col_query.format(table=table)))
                else:
                    res = await conn.execute(text(col_query), {"table": table})
                cols = {col_extractor(row) for row in res.fetchall()}
                if column not in cols:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    )
            except sqlalchemy.exc.ProgrammingError as e:
                # First-run case: table doesn't exist yet (Base.metadata.create_all
                # hasn't run) OR column already added by a concurrent process.
                # Both are safe to ignore.
                logger.debug(
                    f"[run_migrations] {table}.{column} skipped: "
                    f"{type(e).__name__}"
                )
            except Exception as e:
                # ALTER failures (lock conflict, type mismatch, reserved keyword,
                # permissions) need operator attention — log loudly and re-raise
                # so the failure isn't swallowed.
                logger.error(
                    f"[run_migrations] FAILED to add {table}.{column}: "
                    f"{type(e).__name__}: {e}"
                )
                raise


# All (table, column) pairs that hold timestamps. Source of truth mirrors the
# SQLAlchemy models' DateTime(timezone=True) declarations.
_TZ_AWARE_COLUMNS: list[tuple[str, str]] = [
    # users
    ("users", "last_login"), ("users", "created_at"), ("users", "updated_at"),
    # tokens
    ("tokens", "created_at"), ("tokens", "expires_at"),
    # students
    ("students", "created_at"), ("students", "updated_at"),
    # campuses
    ("campuses", "created_at"), ("campuses", "updated_at"),
    # schedule_requests
    ("schedule_requests", "created_at"), ("schedule_requests", "processed_at"),
    # schedules / subjects / lecturers
    ("schedules", "created_at"), ("schedules", "updated_at"),
    ("subjects", "created_at"), ("lecturers", "created_at"),
    # location_logs
    ("location_logs", "timestamp"), ("location_logs", "created_at"), ("location_logs", "recorded_at"),
    # model_versions
    ("model_versions", "training_date"), ("model_versions", "started_at"),
    ("model_versions", "completed_at"), ("model_versions", "created_at"),
    # cluster_results
    ("cluster_results", "result_date"), ("cluster_results", "computed_at"),
    # app_logs
    ("app_logs", "timestamp"), ("app_logs", "log_date"), ("app_logs", "created_at"),
]


async def run_type_migrations():
    """Convert existing datetime columns to TIMESTAMP WITH TIME ZONE.

    On PostgreSQL: ALTER COLUMN ... TYPE TIMESTAMP WITH TIME ZONE
                   USING col AT TIME ZONE 'UTC'. Idempotent — checks
                   information_schema.column data_type before altering.
    On SQLite: no-op (SQLAlchemy stores DateTime(timezone=True) as ISO-8601
               string with offset, transparent to the dialect).

    Existing rows were written as naive UTC (datetime.now(timezone.utc) then
    strip-tz). Interpreting them as UTC via `AT TIME ZONE 'UTC'` preserves
    the original instant — no data loss.
    """
    db_url = settings.DATABASE_URL.lower()
    if "sqlite" in db_url:
        return

    async with engine.begin() as conn:
        for table, column in _TZ_AWARE_COLUMNS:
            try:
                res = await conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema='public' "
                        "AND table_name=:table AND column_name=:column"
                    ),
                    {"table": table, "column": column},
                )
                row = res.fetchone()
                if row is None:
                    # Column doesn't exist yet — Base.metadata.create_all or
                    # run_migrations() will handle it on next startup.
                    continue
                data_type = (row[0] or "").lower()
                if "timestamp" in data_type and "with time zone" in data_type:
                    # Already tz-aware.
                    continue
                # Validate identifier — defense-in-depth (the table/column
                # pair is from a module-level constant, not user input, but
                # the same protection as run_migrations above).
                if not _IDENT_RE.match(table) or not _IDENT_RE.match(column):
                    logger.error(
                        f"[run_type_migrations] Skipping invalid identifier: "
                        f"table={table!r} column={column!r}"
                    )
                    continue
                logger.info(
                    f"[run_type_migrations] ALTER {table}.{column}: "
                    f"{data_type} → timestamp with time zone"
                )
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} ALTER COLUMN {column} "
                        f"TYPE TIMESTAMP WITH TIME ZONE "
                        f"USING {column} AT TIME ZONE 'UTC'"
                    )
                )
            except sqlalchemy.exc.ProgrammingError as e:
                # Table or column may have been renamed/dropped — non-fatal.
                logger.debug(
                    f"[run_type_migrations] {table}.{column} skipped: "
                    f"{type(e).__name__}"
                )
            except Exception as e:
                logger.error(
                    f"[run_type_migrations] FAILED {table}.{column}: "
                    f"{type(e).__name__}: {e}"
                )
                # Don't re-raise — type migration is best-effort; an operator
                # running the ALTER manually is fine. The app still works on
                # TIMESTAMP WITHOUT TIME ZONE columns (writes will be tz-aware
                # but compared/written as-is by SQLAlchemy).


# (table, column, target_pg_type) for ALTER COLUMN ... TYPE migrations beyond
# the TIMESTAMP WITH TIME ZONE work. Each entry widens an undersized string
# column that was set before the feature JSON grew beyond its initial limit.
_STRING_WIDEN_COLUMNS: list[tuple[str, str, str]] = [
    # cluster_results.features was String(2000); JSON-serialized feature dicts
    # grow with feature engineering — switch to TEXT to remove the silent
    # truncation (SQLite) / hard error (PostgreSQL) at write time.
    ("cluster_results", "features", "TEXT"),
]


async def run_string_widen_migrations():
    """Widen undersized String columns that risk silent truncation.

    PostgreSQL: ALTER COLUMN ... TYPE TEXT. Idempotent (re-running on TEXT is
                a no-op).
    SQLite:     no-op (SQLite uses type affinity, no fixed column width).
    """
    db_url = settings.DATABASE_URL.lower()
    if "sqlite" in db_url:
        return

    async with engine.begin() as conn:
        for table, column, target in _STRING_WIDEN_COLUMNS:
            try:
                if not _IDENT_RE.match(table) or not _IDENT_RE.match(column):
                    logger.error(
                        f"[run_string_widen_migrations] Skipping invalid "
                        f"identifier: table={table!r} column={column!r}"
                    )
                    continue
                res = await conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema='public' "
                        "AND table_name=:table AND column_name=:column"
                    ),
                    {"table": table, "column": column},
                )
                row = res.fetchone()
                if row is None:
                    continue
                data_type = (row[0] or "").lower()
                # Skip if already widened to TEXT or larger.
                if data_type in ("text", "json", "jsonb"):
                    continue
                logger.info(
                    f"[run_string_widen_migrations] ALTER {table}.{column}: "
                    f"{data_type} → {target.lower()}"
                )
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} ALTER COLUMN {column} "
                        f"TYPE {target}"
                    )
                )
            except sqlalchemy.exc.ProgrammingError as e:
                logger.debug(
                    f"[run_string_widen_migrations] {table}.{column} "
                    f"skipped: {type(e).__name__}"
                )
            except Exception as e:
                logger.error(
                    f"[run_string_widen_migrations] FAILED {table}.{column}: "
                    f"{type(e).__name__}: {e}"
                )


# Schema relaxations — columns that should NOT be NOT NULL but historically were.
# Mirror in `relax_migrations_for_postgres` call in main.py lifespan.
_RELAX_COLUMNS: list[tuple[str, str]] = [
    # students.user_id: was NOT NULL with ON DELETE CASCADE; now nullable with
    # ON DELETE SET NULL. A Student record can exist without a User account for
    # legacy imports (NIMs from old spreadsheet before the mobile app launched)
    # and for graduated-but-still-tracked cases. The POST /api/students endpoint
    # also supports auto-creating a User row via create_user_account=true so a
    # single admin/dosen call can register both halves of the relationship.
    ("students", "user_id"),
]


# (table, column) pairs that should have indexes for the hot read paths.
# Round 10 optimization: the reports dashboard and the ML pipeline both scan
# app_logs by timestamp range + source filter, and cluster_results by
# model_version_id + result_date. Without these indexes, PostgresSeqScan
# triggers on every dashboard refresh.
# Each entry is (table, column, index_name). The migration creates
# CREATE INDEX IF NOT EXISTS so it's idempotent.
_INDEX_DEFINITIONS: list[tuple[str, str, str]] = [
    # app_logs hot filters
    ("app_logs", "timestamp", "idx_app_logs_timestamp"),
    ("app_logs", "student_id", "idx_app_logs_student_id"),
    ("app_logs", "source", "idx_app_logs_source"),
    # Round 29 audit P1 #5: the ML serving module's
    # `aggregate_student_features_sync` filters on `log_date` for the
    # per-student per-day aggregate. Without an index, this is a full
    # scan of app_logs (currently ~10K rows but grows nightly). Adding
    # the index costs ~100KB on PostgreSQL and speeds up nightly batch
    # scoring from O(N) to O(1) per student.
    ("app_logs", "log_date", "idx_app_logs_log_date"),
    # Composite for the common (student_id, log_date) lookup used by
    # both the trainer's pivot and the serving module's aggregate.
    # Cheaper than the two single-column indexes combined for the
    # "WHERE student_id=? AND log_date=?" pattern.
    ("app_logs", "student_id, log_date", "idx_app_logs_student_log_date"),
    # cluster_results: every reports/* query filters by both
    ("cluster_results", "model_version_id", "idx_cluster_results_model_version_id"),
    ("cluster_results", "result_date", "idx_cluster_results_result_date"),
    # model_versions: is_active is the hot filter
    ("model_versions", "is_active", "idx_model_versions_is_active"),
    # Round 15: program_studi.jurusan_id backs the cascade dropdown filter
    # ("list all prodi under this jurusan") on /register and /akademik.
    ("program_studi", "jurusan_id", "idx_program_studi_jurusan_id"),
]


async def run_index_migrations():
    """Create indexes for hot read paths. Idempotent (CREATE INDEX IF NOT EXISTS).

    Run-once-per-startup. Cost is O(0) on second run (PostgreSQL short-circuits
    via IF NOT EXISTS; SQLite ignores duplicates natively).
    """
    for table, column, index_name in _INDEX_DEFINITIONS:
        # Whitelist each column because composite indexes use comma-separated
        # column names while table and index names remain single identifiers.
        columns = [part.strip() for part in column.split(",")]
        if (
            not _IDENT_RE.match(table)
            or not columns
            or not all(_IDENT_RE.match(part) for part in columns)
            or not _IDENT_RE.match(index_name)
        ):
            logger.error(
                f"[run_index_migrations] Skipping invalid identifier: "
                f"table={table!r} column={column!r} index={index_name!r}"
            )
            continue
        column_sql = ", ".join(columns)
        try:
            # One transaction per index prevents a failed concurrent CREATE from
            # aborting later index migrations in the same API worker.
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {index_name} "
                        f"ON {table} ({column_sql})"
                    )
                )
            logger.info(
                f"[run_index_migrations] OK {index_name} on "
                f"{table}({column_sql})"
            )
        except sqlalchemy.exc.ProgrammingError as e:
            # Table doesn't exist yet (Base.metadata.create_all hasn't run)
            # OR column was renamed. Both are non-fatal.
            logger.debug(
                f"[run_index_migrations] {index_name} skipped: "
                f"{type(e).__name__}"
            )
        except sqlalchemy.exc.IntegrityError as e:
            logger.debug(
                f"[run_index_migrations] {index_name} already exists or was "
                f"created concurrently: {type(e).__name__}"
            )
        except Exception as e:
            logger.error(
                f"[run_index_migrations] FAILED {index_name}: "
                f"{type(e).__name__}: {e}"
            )
            # Index creation is best-effort; the app still works without it.


# ─── Autovacuum tuning — per-table scale factors for hot small tables ────
# Round 24: pg_stat_user_tables snapshot showed
#   login_devices: 14 live, 21 dead (60% bloat) — never autovacuumed
#   tokens:       14 live, 16 dead (53% bloat) — never autovacuumed
# Both tables churn rows on every auth event (login, logout, refresh) but
# PostgreSQL's default autovacuum_vacuum_scale_factor=0.2 only triggers
# when 20% of the table is dead. For a 14-row table that's 2.8 tuples
# — *almost* always under the threshold. Result: dead tuples accumulate
# forever, table files balloon, and SELECTs slow down as Postgres reads
# more dead pages than live ones.
#
# Fix: lower the per-table threshold so autovacuum kicks in earlier.
# 0.05 = "vacuum when 5% of rows are dead" (≈ 1 tuple on a 14-row
# table). 0.02 for analyze keeps the planner stats fresh.
#
# Same pattern would help `cluster_results` (churned on every retrain)
# and `app_logs` (heavily UPDATEd via Round 20 UPSERT), but those
# tables are large enough that 0.2 already triggers within minutes
# of churn. Small-table tuning is where the bloat lives.
_AUTOVACUUM_TUNING: list[tuple[str, float, float]] = [
    # (table, vacuum_scale_factor, analyze_scale_factor)
    ("login_devices", 0.05, 0.02),
    ("tokens", 0.05, 0.02),
    # Round 20: app_logs UPSERT churns ~16 rows per mobile sync. Still
    # triggers autovacuum at default (0.2) within minutes, but tightening
    # to 0.05 keeps the table size honest on long-lived installs.
    ("app_logs", 0.05, 0.02),
    # cluster_results: rows deleted+re-inserted on every retrain. Default
    # 0.2 is fine for a large model, but the analyze threshold matters
    # more here — planner stats on cluster_label distribution are
    # critical for the /reports/cluster-trends GROUP BY path.
    ("cluster_results", 0.10, 0.02),
]


async def run_autovacuum_tuning_migrations():
    """Per-table autovacuum scale factor tuning.

    Run-once-per-startup. ALTER TABLE ... SET (...) with these storage
    parameters is persistent (survives restarts) and idempotent (re-running
    just re-applies the same value, no harm). SQLite ignores these options
    silently — no-op on the dev path.
    """
    async with engine.begin() as conn:
        db_url = settings.DATABASE_URL.lower()
        is_sqlite = "sqlite" in db_url
        if is_sqlite:
            logger.debug(
                "[run_autovacuum_tuning_migrations] SQLite — skipping "
                "(autovacuum options are PostgreSQL-only)"
            )
            return

        for table, vacuum_scale, analyze_scale in _AUTOVACUUM_TUNING:
            if not _IDENT_RE.match(table):
                logger.error(
                    f"[run_autovacuum_tuning_migrations] Skipping invalid "
                    f"identifier: {table!r}"
                )
                continue
            try:
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} SET ("
                        f"autovacuum_vacuum_scale_factor = {vacuum_scale}, "
                        f"autovacuum_analyze_scale_factor = {analyze_scale}"
                        f")"
                    )
                )
                logger.info(
                    f"[run_autovacuum_tuning_migrations] OK {table}: "
                    f"vacuum_scale={vacuum_scale} analyze_scale={analyze_scale}"
                )
            except sqlalchemy.exc.ProgrammingError as e:
                # Table doesn't exist yet (Base.metadata.create_all hasn't run
                # or this is a fresh install). Non-fatal — re-runs on next
                # startup will catch up.
                logger.debug(
                    f"[run_autovacuum_tuning_migrations] {table} skipped: "
                    f"{type(e).__name__}: {str(e)[:120]}"
                )
            except Exception as e:
                # Don't re-raise — tuning is best-effort. Tables still get
                # autovacuumed at the default 0.2 scale factor, just less
                # aggressively.
                logger.error(
                    f"[run_autovacuum_tuning_migrations] FAILED {table}: "
                    f"{type(e).__name__}: {e}"
                )


# ─── Round 15: unique-constraint + orphan-prodi cleanup ───────────────────
async def run_unique_constraint_migrations():
    """Add UNIQUE constraint on jurusan.code (round 15 fix).

    The original model declared `code: Mapped[str] = mapped_column(String(20), index=True)`
    without `unique=True`, which meant two jurusans could share the same code.
    This migration:
    1. Detects existing duplicates (any case-insensitive group with count > 1).
    2. Soft-deletes the duplicates (`is_active=false`) — recoverable via include_inactive=true.
    3. Adds the UNIQUE constraint.

    Idempotent: checks for the constraint before altering. SQLite path is a
    no-op because the model-side `unique=True` already created a unique index
    via Base.metadata.create_all on the first run after the model was updated.
    """
    db_url = settings.DATABASE_URL.lower()
    if "sqlite" in db_url:
        return

    async with engine.begin() as conn:
        try:
            res = await conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint WHERE conname = 'jurusan_code_key'"
                )
            )
            if res.fetchone() is not None:
                return

            # Find duplicates before adding the constraint.
            dup_check = await conn.execute(
                text(
                    "SELECT LOWER(code) AS code_lc, COUNT(*) AS n "
                    "FROM jurusan GROUP BY LOWER(code) HAVING COUNT(*) > 1"
                )
            )
            dups = dup_check.fetchall()
            if dups:
                logger.warning(
                    f"[run_unique_constraint_migrations] Duplicate Jurusan codes "
                    f"detected: {[d[0] for d in dups]} — soft-deleting all but the "
                    f"earliest row by created_at."
                )
                await conn.execute(
                    text(
                        "UPDATE jurusan SET is_active = false "
                        "WHERE id IN ("
                        "  SELECT id FROM ("
                        "    SELECT id, ROW_NUMBER() OVER ("
                        "      PARTITION BY LOWER(code) ORDER BY created_at ASC, id ASC"
                        "    ) AS rn FROM jurusan"
                        "  ) t WHERE rn > 1"
                        ")"
                    )
                )

            await conn.execute(
                text("ALTER TABLE jurusan ADD CONSTRAINT jurusan_code_key UNIQUE (code)")
            )
            logger.info(
                "[run_unique_constraint_migrations] Added UNIQUE constraint on jurusan.code"
            )
        except sqlalchemy.exc.ProgrammingError as e:
            logger.debug(
                f"[run_unique_constraint_migrations] skipped: {type(e).__name__}"
            )
        except Exception as e:
            logger.error(
                f"[run_unique_constraint_migrations] FAILED: {type(e).__name__}: {e}"
            )
            # Don't re-raise — the duplicate-check in the router still
            # prevents new INSERTs from creating the same problem.


async def run_student_user_unique_migration():
    """Keep auto-provisioned Student rows one-to-one with User rows."""
    if "sqlite" in settings.DATABASE_URL.lower():
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE students SET user_id = NULL WHERE id IN ("
                "SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
                "PARTITION BY user_id ORDER BY created_at ASC, id ASC"
                ") AS rn FROM students WHERE user_id IS NOT NULL) "
                "WHERE rn > 1)"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_students_user_id "
                "ON students (user_id) WHERE user_id IS NOT NULL"
            ))
        return

    async with engine.begin() as conn:
        duplicate_users = await conn.execute(text(
            "SELECT user_id FROM students WHERE user_id IS NOT NULL "
            "GROUP BY user_id HAVING COUNT(*) > 1"
        ))
        for (user_id,) in duplicate_users:
            rows = await conn.execute(text(
                "SELECT id FROM students WHERE user_id = :user_id "
                "ORDER BY created_at ASC, id ASC"
            ), {"user_id": user_id})
            keep = rows.fetchone()
            if keep:
                await conn.execute(text(
                    "UPDATE students SET user_id = NULL WHERE user_id = :user_id AND id != :keep"
                ), {"user_id": user_id, "keep": keep[0]})
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_students_user_id "
            "ON students (user_id) WHERE user_id IS NOT NULL"
        ))


async def cleanup_orphan_prodi():
    """Round 15: clean up orphan ProgramStudi rows (jurusan_id IS NULL).

    Pre-round-12.1 registrations created prodi without a parent jurusan.
    Those orphans (e.g. the PTI row) pollute the cascade dropdown on /register
    and /akademik ("Tanpa Jurusan" section).

    Cleanup policy:
    - Soft-delete (is_active=false) any orphan prodi that has no student rows
      referencing it via the legacy `students.program_studi` text column.
      Hidden from default list, but recoverable.
    - Log any orphan prodi that DOES have student references — operator must
      manually reassign those students before re-running the cleanup.
    """
    from app.models.program_studi import ProgramStudi
    from app.models.student import Student

    async with async_session() as session:
        try:
            orphans = await session.execute(
                select(ProgramStudi).where(ProgramStudi.jurusan_id.is_(None))
            )
            orphan_rows = orphans.scalars().all()
            if not orphan_rows:
                return

            cleaned = []
            needs_attention = []
            for row in orphan_rows:
                ref_count = await session.execute(
                    select(func.count(Student.id)).where(Student.program_studi == row.name)
                )
                referenced = ref_count.scalar() or 0
                if referenced == 0:
                    row.is_active = False
                    cleaned.append((row.code, row.name))
                else:
                    needs_attention.append((row.code, row.name, referenced))

            if cleaned:
                logger.warning(
                    f"[cleanup_orphan_prodi] Soft-deleted {len(cleaned)} orphan prodi "
                    f"(no student references): {cleaned}"
                )
            if needs_attention:
                logger.warning(
                    f"[cleanup_orphan_prodi] {len(needs_attention)} orphan prodi have "
                    f"student references and were LEFT ACTIVE — reassign students via "
                    f"/students first: {needs_attention}"
                )
            await session.commit()
        except Exception as e:
            logger.error(
                f"[cleanup_orphan_prodi] FAILED: {type(e).__name__}: {e}"
            )
            await session.rollback()


async def run_geofence_columns_migration():
    """Round 17-ish: add geofence detection columns to app_logs.

    Two columns added (idempotent via information_schema check):

    - `app_logs.campus_id` (nullable FK → campuses.id) — the campus the
      student should be at during class time. Resolved at ingest by the
      geofence helper from Schedule → MataKuliah → campus_id.
    - `app_logs.geofence_status` (nullable VARCHAR(20)) — "inside" /
      "outside" / "unknown" verdict against the campus's
      lat/lon/radius_meters circle.

    Existing rows are left NULL. We don't backfill geofence status from
    historical data — that would require re-processing location fixes
    that may have been already dropped (per the 7-day Room retention in
    Android). New rows fill both columns automatically on ingest.
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        # SQLite + PostgreSQL: information_schema.columns works on both.
        # The PRAGMA path is SQLite-specific and we already use it in
        # run_mata_kuliah_campus_migration, so we mirror the dialect split.
        if settings.DATABASE_URL.startswith("sqlite"):
            cols = await conn.run_sync(
                lambda sync_conn: sync_conn.execute(
                    text("PRAGMA table_info(app_logs)")
                ).fetchall()
            )
            existing = {row[1] for row in cols}
            if "campus_id" not in existing:
                await conn.exec_driver_sql(
                    "ALTER TABLE app_logs ADD COLUMN campus_id VARCHAR(36)"
                )
                logger.info(
                    "[run_geofence_columns_migration] Added app_logs.campus_id"
                )
            if "geofence_status" not in existing:
                await conn.exec_driver_sql(
                    "ALTER TABLE app_logs ADD COLUMN geofence_status VARCHAR(20)"
                )
                logger.info(
                    "[run_geofence_columns_migration] Added app_logs.geofence_status"
                )
        else:
            existing_cols = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'app_logs'"
                        )
                    )
                ).fetchall()
            }
            if "campus_id" not in existing_cols:
                await conn.execute(
                    text(
                        "ALTER TABLE app_logs ADD COLUMN campus_id VARCHAR(36) "
                        "REFERENCES campuses(id) ON DELETE SET NULL"
                    )
                )
                logger.info(
                    "[run_geofence_columns_migration] Added app_logs.campus_id (PostgreSQL)"
                )
            if "geofence_status" not in existing_cols:
                await conn.execute(
                    text(
                        "ALTER TABLE app_logs ADD COLUMN geofence_status VARCHAR(20)"
                    )
                )
                logger.info(
                    "[run_geofence_columns_migration] Added app_logs.geofence_status (PostgreSQL)"
                )


async def run_mata_kuliah_campus_migration():
    """Round-17-ish: bind mata_kuliah to campuses and make `subjects.code` unique.

    Two coordinated schema changes:

    1. Add `mata_kuliah.campus_id` FK to campuses. Rows already in production
       (created before this round) are backfilled to the first active campus
       (or the UNG default lat/lon fallback if no campus row exists at all).
       Subsequent inserts without `campus_id` are rejected by the NOT NULL
       constraint — `MataKuliahCreate.campus_id` is required.

    2. Add UNIQUE INDEX on `subjects.code`. The mata_kuliah → subjects
       bridge (see `routers/mata_kuliah._get_or_create_subject`) dedupes by
       code; without the index, two mata_kuliah rows with the same code
       would create two Subject rows with the same code and break the
       Android dropdown.

    Both changes are idempotent — safe to run on every startup.
    """
    from sqlalchemy import text

    if settings.DATABASE_URL.startswith("sqlite"):
        # SQLite: ALTER TABLE only supports ADD COLUMN. NOT NULL enforcement
        # uses ALTER COLUMN (supported on SQLite ≥ 3.35). The migration
        # order is: ensure fallback campus → add column nullable → backfill
        # → enforce NOT NULL. Both steps are idempotent via information
        # schema checks.
        async with engine.begin() as conn:
            cols = await conn.run_sync(
                lambda sync_conn: sync_conn.execute(
                    text("PRAGMA table_info(mata_kuliah)")
                ).fetchall()
            )
            has_campus = any(row[1] == "campus_id" for row in cols)
            if not has_campus:
                # Ensure at least one campus row exists; otherwise backfill
                # would fail the FK.
                await conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO campuses (id, name, latitude, longitude, "
                    "radius_meters, is_active, created_at, updated_at) "
                    "VALUES ('default-campus', 'Kampus Utama', "
                    f"{settings.DEFAULT_CAMPUS_LAT}, {settings.DEFAULT_CAMPUS_LON}, "
                    "200.0, 1, datetime('now'), datetime('now'))"
                )
                any_campus = (
                    await conn.exec_driver_sql("SELECT id FROM campuses LIMIT 1")
                ).first()
                if any_campus is None:
                    logger.error(
                        "[run_mata_kuliah_campus_migration] No campus row exists; "
                        "cannot backfill mata_kuliah.campus_id. Operator must "
                        "create a campus row first."
                    )
                    return
                await conn.exec_driver_sql(
                    "ALTER TABLE mata_kuliah ADD COLUMN campus_id VARCHAR(36)"
                )
                await conn.exec_driver_sql(
                    f"UPDATE mata_kuliah SET campus_id = '{any_campus[0]}' "
                    "WHERE campus_id IS NULL"
                )
                await conn.exec_driver_sql(
                    "ALTER TABLE mata_kuliah ALTER COLUMN campus_id SET NOT NULL"
                )
                logger.info(
                    f"[run_mata_kuliah_campus_migration] Added mata_kuliah.campus_id, "
                    f"backfilled to campus {any_campus[0]}"
                )

            # UNIQUE index on subjects.code (idempotent).
            await conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_subjects_code ON subjects(code)"
            )
    else:
        # PostgreSQL: information_schema checks for both column and constraint.
        async with engine.begin() as conn:
            existing_col = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'mata_kuliah' AND column_name = 'campus_id'"
                    )
                )
            ).scalar()
            if existing_col is None:
                # Ensure fallback campus exists.
                any_campus = (
                    await conn.execute(text("SELECT id FROM campuses LIMIT 1"))
                ).scalar()
                if any_campus is None:
                    await conn.execute(
                        text(
                            "INSERT INTO campuses (id, name, latitude, longitude, "
                            "radius_meters, is_active, created_at, updated_at) "
                            "VALUES (:id, :name, :lat, :lon, :r, TRUE, NOW(), NOW())"
                        ),
                        {
                            "id": "default-campus",
                            "name": "Kampus Utama",
                            "lat": settings.DEFAULT_CAMPUS_LAT,
                            "lon": settings.DEFAULT_CAMPUS_LON,
                            "r": 200.0,
                        },
                    )
                    any_campus = "default-campus"
                    logger.info(
                        "[run_mata_kuliah_campus_migration] Seeded fallback campus 'Kampus Utama'"
                    )
                # Add column nullable, backfill, then enforce NOT NULL.
                await conn.execute(
                    text(
                        "ALTER TABLE mata_kuliah ADD COLUMN campus_id VARCHAR(36) "
                        "REFERENCES campuses(id) ON DELETE RESTRICT"
                    )
                )
                await conn.execute(
                    text(
                        "UPDATE mata_kuliah SET campus_id = :cid WHERE campus_id IS NULL"
                    ),
                    {"cid": any_campus},
                )
                await conn.execute(
                    text("ALTER TABLE mata_kuliah ALTER COLUMN campus_id SET NOT NULL")
                )
                logger.info(
                    f"[run_mata_kuliah_campus_migration] Added mata_kuliah.campus_id, "
                    f"backfilled to campus {any_campus}"
                )

            existing_idx = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM pg_indexes "
                        "WHERE schemaname = 'public' AND indexname = 'uq_subjects_code'"
                    )
                )
            ).scalar()
            if existing_idx is None:
                await conn.execute(
                    text("CREATE UNIQUE INDEX uq_subjects_code ON subjects(code)")
                )
                logger.info(
                    "[run_mata_kuliah_campus_migration] Created UNIQUE INDEX uq_subjects_code"
                )


async def run_relax_migrations():
    """Schema relaxations — drop NOT NULL where the model now permits NULL.

    Idempotent. Skips on SQLite (column-rebuild is heavy; the model allows NULL
    there from the start, no migration needed). On PostgreSQL runs
    `ALTER COLUMN ... DROP NOT NULL`.
    """
    db_url = settings.DATABASE_URL.lower()
    is_sqlite = "sqlite" in db_url
    if is_sqlite:
        logger.debug("[run_relax_migrations] SQLite: skip (model is already nullable)")
        return

    async with engine.begin() as conn:
        for table, column in _RELAX_COLUMNS:
            try:
                if not _IDENT_RE.match(table) or not _IDENT_RE.match(column):
                    logger.error(
                        f"[run_relax_migrations] Skipping invalid identifier: "
                        f"table={table!r} column={column!r}"
                    )
                    continue
                res = await conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema='public' "
                        "AND table_name=:table AND column_name=:column"
                    ),
                    {"table": table, "column": column},
                )
                row = res.fetchone()
                if row is None:
                    continue
                if row[0] == "YES":
                    continue
                logger.info(
                    f"[run_relax_migrations] ALTER {table}.{column}: "
                    f"SET NOT NULL → DROP NOT NULL"
                )
                await conn.execute(
                    text(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")
                )
                # Also drop the CASCADE FK to avoid orphan deletion cascading
                # the Student row when its User is removed. SET NULL preserves
                # the Student row but nulls the link.
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
                        f"{table}_user_id_fkey"
                    )
                )
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} ADD CONSTRAINT "
                        f"{table}_user_id_fkey FOREIGN KEY ({column}) "
                        f"REFERENCES users(id) ON DELETE SET NULL"
                    )
                )
            except sqlalchemy.exc.ProgrammingError as e:
                logger.debug(
                    f"[run_relax_migrations] {table}.{column} "
                    f"skipped: {type(e).__name__}"
                )
            except Exception as e:
                logger.error(
                    f"[run_relax_migrations] FAILED {table}.{column}: "
                    f"{type(e).__name__}: {e}"
                )
                raise


# ─── Daily-unique id per (student, date) for mobile-source app_logs ───────
# Round 20: a single (student_id, log_date) pair now maps to AT MOST ONE
# app_logs row when source='mobile'. Each Android sync uploads the day's
# running totals (a single row per day), and the server UPSERTs that row in
# place — no more "5 entries for the same day" symptom in the dashboard.
#
# Seed data is unaffected (seed rows model multiple sessions per day for
# ML training; the partial index WHERE source='mobile' excludes them).
#
# Before the unique index can be created, the migration dedupes any existing
# mobile rows that already violate the constraint (typically created by the
# legacy "one-row-per-45-min-interval" sync loop): keep the latest row per
# (student_id, log_date), delete the rest.


async def run_app_logs_schedule_id_migration():
    """Replace mobile daily uniqueness with (student, day, schedule)."""
    index = "uq_app_logs_student_date_schedule_mobile"
    async with engine.begin() as conn:
        if "sqlite" in settings.DATABASE_URL.lower():
            await conn.exec_driver_sql("DROP INDEX IF EXISTS uq_app_logs_student_date_mobile")
            exists = (await conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:name"), {"name": index})).scalar()
            if exists is None:
                await conn.exec_driver_sql(f"CREATE UNIQUE INDEX {index} ON app_logs (student_id, log_date, schedule_id) WHERE source = 'mobile'")
        else:
            await conn.execute(text("DROP INDEX IF EXISTS uq_app_logs_student_date_mobile"))
            await conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {index} ON app_logs (student_id, log_date, schedule_id) WHERE source = 'mobile'"))


async def run_app_logs_daily_id_migration():
    """Idempotent migration: dedupe existing mobile rows + create partial
    UNIQUE index enforcing "one row per (student, date) for mobile data".

    Safe to run on every startup. Idempotent: a second run finds 0 duplicates
    and 0 missing index, no-op.

    SQLite: `CREATE UNIQUE INDEX ... WHERE source = 'mobile'`
    PostgreSQL: same syntax, supported since PG 9.5 partial-index feature.
    """
    db_url = settings.DATABASE_URL.lower()
    is_sqlite = "sqlite" in db_url

    if is_sqlite:
        await _dedupe_mobile_app_logs_sqlite()
        await _create_mobile_daily_unique_index_sqlite()
    else:
        await _dedupe_mobile_app_logs_postgres()
        await _create_mobile_daily_unique_index_postgres()


async def _dedupe_mobile_app_logs_sqlite():
    """SQLite: aggregate legacy mobile rows per (student_id, date(log_date))
    BEFORE creating the UNIQUE INDEX. Pre-Round 20 Android collectors
    inserted one row per 45-min interval, each holding the *delta* for
    that interval — NOT the day's running total. Keeping only the latest
    row would drop every other interval's contribution and store a wrong
    (delta-only) value in the surviving row.

    The fix mirrors the Android MIGRATION_3_4 logic: fold older rows'
    per-interval deltas INTO a single day-total row using SUM/MAX, then
    drop the duplicates. SUM accumulates the per-interval minute deltas
    into the day's total. MAX takes the latest unlock_count (unlock_count
    is itself a daily running counter, so SUM would double-count). MAX
    also collapses the used_during_class boolean (a single class-time
    interval flips the day flag).

    **Date normalization is critical**: the `log_date` column may be
    stored at any time-of-day (00:00:00 UTC for /api/logs/batch ingest,
    but the actual collection time for MQTT ingest). Grouping by
    `log_date` literally would miss same-day duplicates whose timestamps
    differ. We group by `strftime('%Y-%m-%d', log_date, 'localtime')`
    instead. The single surviving row's `log_date` is normalized to
    `00:00:00` in the WITA timezone so future UPSERTs and the Android
    `log_date = "YYYY-MM-DD"` payload all line up.
    """
    async with engine.begin() as conn:
        # How many duplicate groups exist (for logging).
        dup_check = await conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT student_id, date(log_date, 'localtime') AS day "
                "  FROM app_logs "
                "  WHERE source = 'mobile' "
                "  GROUP BY student_id, day "
                "  HAVING COUNT(*) > 1"
                ") d"
            )
        )
        dup_count = dup_check.scalar() or 0
        if dup_count == 0:
            return
        logger.info(
            f"[run_app_logs_daily_id_migration] Aggregating {dup_count} "
            f"(student_id, date(log_date)) groups (SQLite path)"
        )
        # Step 1: aggregate into a fresh row per (student_id, date).
        # The new row's log_date is normalized to 00:00:00 in WITA so it
        # lines up with /api/logs/batch ingest (which also stores
        # midnight-WITA via AwareDateTime) and Android payloads (which
        # send "YYYY-MM-DD").
        await conn.execute(
            text(
                """
                INSERT INTO app_logs (
                    id, student_id, log_date, app_package, app_name,
                    duration_seconds, category, is_foreground, timestamp,
                    total_screen_time_minutes, social_media_time_minutes,
                    game_time_minutes, other_app_time_minutes,
                    unlock_count, used_during_class, schedule_id, device_id,
                    idempotency_key, is_synced, source,
                    campus_id, geofence_status, created_at
                )
                SELECT
                    (SELECT id FROM app_logs a2
                     WHERE a2.student_id = a.student_id
                       AND date(a2.log_date, 'localtime') = date(a.log_date, 'localtime')
                     ORDER BY COALESCE(a2.created_at, a2.timestamp) DESC, a2.id DESC
                     LIMIT 1) AS id,
                    a.student_id,
                    datetime(date(a.log_date, 'localtime') || ' 00:00:00', 'localtime') AS log_date,
                    '' AS app_package,
                    '' AS app_name,
                    0 AS duration_seconds,
                    'other' AS category,
                    1 AS is_foreground,
                    MAX(COALESCE(a.timestamp, a.created_at)) AS timestamp,
                    SUM(a.total_screen_time_minutes) AS total_screen_time_minutes,
                    SUM(a.social_media_time_minutes) AS social_media_time_minutes,
                    SUM(a.game_time_minutes) AS game_time_minutes,
                    SUM(a.other_app_time_minutes) AS other_app_time_minutes,
                    MAX(a.unlock_count) AS unlock_count,
                    MAX(a.used_during_class) AS used_during_class,
                    (SELECT schedule_id FROM app_logs a3
                     WHERE a3.student_id = a.student_id
                       AND date(a3.log_date, 'localtime') = date(a.log_date, 'localtime')
                       AND a3.schedule_id IS NOT NULL
                     ORDER BY COALESCE(a3.created_at, a3.timestamp) DESC LIMIT 1) AS schedule_id,
                    (SELECT device_id FROM app_logs a4
                     WHERE a4.student_id = a.student_id
                       AND date(a4.log_date, 'localtime') = date(a.log_date, 'localtime')
                       AND a4.device_id IS NOT NULL
                     ORDER BY COALESCE(a4.created_at, a4.timestamp) DESC LIMIT 1) AS device_id,
                    (SELECT idempotency_key FROM app_logs a5
                     WHERE a5.student_id = a.student_id
                       AND date(a5.log_date, 'localtime') = date(a.log_date, 'localtime')
                     ORDER BY COALESCE(a5.created_at, a5.timestamp) DESC LIMIT 1) AS idempotency_key,
                    1 AS is_synced,
                    'mobile' AS source,
                    (SELECT campus_id FROM app_logs a6
                     WHERE a6.student_id = a.student_id
                       AND date(a6.log_date, 'localtime') = date(a.log_date, 'localtime')
                       AND a6.campus_id IS NOT NULL
                     ORDER BY COALESCE(a6.created_at, a6.timestamp) DESC LIMIT 1) AS campus_id,
                    (SELECT geofence_status FROM app_logs a7
                     WHERE a7.student_id = a.student_id
                       AND date(a7.log_date, 'localtime') = date(a.log_date, 'localtime')
                       AND a7.geofence_status IS NOT NULL
                     ORDER BY COALESCE(a7.created_at, a7.timestamp) DESC LIMIT 1) AS geofence_status,
                    MAX(COALESCE(a.created_at, a.timestamp)) AS created_at
                FROM app_logs a
                WHERE a.source = 'mobile'
                GROUP BY a.student_id, date(a.log_date, 'localtime')
                """
            )
        )
        # Step 2: delete the older duplicates, keeping only the row whose
        # id matches the one we just wrote. Group by date, not raw
        # log_date, so same-day rows at different times are treated as
        # the same group.
        await conn.execute(
            text(
                """
                DELETE FROM app_logs
                WHERE source = 'mobile'
                  AND id NOT IN (
                    SELECT id FROM (
                      SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY student_id, date(log_date, 'localtime')
                        ORDER BY COALESCE(created_at, timestamp) DESC, id DESC
                      ) AS rn
                      FROM app_logs WHERE source = 'mobile'
                    ) WHERE rn = 1
                  )
                """
            )
        )


async def _create_mobile_daily_unique_index_sqlite():
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='index' AND name='uq_app_logs_student_date_mobile'"
                )
            )
        ).scalar()
        if existing is not None:
            return
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_app_logs_student_date_mobile "
            "ON app_logs (student_id, log_date) WHERE source = 'mobile'"
        )
        logger.info(
            "[run_app_logs_daily_id_migration] Created partial UNIQUE INDEX "
            "uq_app_logs_student_date_mobile (SQLite)"
        )


async def _dedupe_mobile_app_logs_postgres():
    """PostgreSQL: aggregate legacy mobile rows per (student_id, date(log_date))
    BEFORE creating the UNIQUE INDEX. See _dedupe_mobile_app_logs_sqlite
    for the rationale — pre-Round 20 Android collectors sent per-interval
    deltas, so the "latest row only" strategy would store a wrong day total.

    Groups by `DATE(log_date)` so same-day rows at different times of day
    (e.g., 00:00 UTC from /api/logs/batch vs the actual collection
    timestamp from MQTT ingest) are folded together. The new row's
    `log_date` is normalized to `00:00:00` in WITA so future UPSERTs
    and the Android `log_date = "YYYY-MM-DD"` payload all line up.
    """
    async with engine.begin() as conn:
        dup_check = await conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT 1 FROM app_logs "
                "  WHERE source = 'mobile' "
                "  GROUP BY student_id, DATE(log_date) "
                "  HAVING COUNT(*) > 1"
                ") d"
            )
        )
        dup_count = dup_check.scalar() or 0
        if dup_count == 0:
            return
        logger.info(
            f"[run_app_logs_daily_id_migration] Aggregating {dup_count} "
            f"(student_id, DATE(log_date)) groups (PostgreSQL path)"
        )
        # Step 1: aggregate into a fresh row per (student_id, date).
        # DISTINCT ON picks the latest row per group for the id-mirroring
        # columns; SUM/MAX aggregates the per-interval deltas into day totals.
        await conn.execute(
            text(
                """
                INSERT INTO app_logs (
                    id, student_id, log_date, app_package, app_name,
                    duration_seconds, category, is_foreground, timestamp,
                    total_screen_time_minutes, social_media_time_minutes,
                    game_time_minutes, other_app_time_minutes,
                    unlock_count, used_during_class, schedule_id, device_id,
                    idempotency_key, is_synced, source,
                    campus_id, geofence_status, created_at
                )
                SELECT
                    latest.id,
                    a.student_id,
                    -- Round 29 audit P1: normalize to UTC midnight, not WITA
                    -- midnight. AwareDateTime.process_bind_param (database.py)
                    -- stamps naive datetimes as UTC — so /api/logs/batch ingest
                    -- stores log_date at 00:00 UTC. Storing migrated rows at
                    -- 00:00 WITA (= 16:00 UTC previous day) caused an 8-hour
                    -- drift that defeated the partial UNIQUE INDEX
                    -- `uq_app_logs_student_date_mobile` (database.py:1191) and
                    -- broke date-based cluster-trends / screen-time queries.
                    DATE(a.log_date)::timestamp AS log_date,
                    '' AS app_package,
                    '' AS app_name,
                    0 AS duration_seconds,
                    'other' AS category,
                    TRUE AS is_foreground,
                    MAX(COALESCE(a.timestamp, a.created_at)) AS timestamp,
                    SUM(a.total_screen_time_minutes) AS total_screen_time_minutes,
                    SUM(a.social_media_time_minutes) AS social_media_time_minutes,
                    SUM(a.game_time_minutes) AS game_time_minutes,
                    SUM(a.other_app_time_minutes) AS other_app_time_minutes,
                    MAX(a.unlock_count) AS unlock_count,
                    BOOL_OR(a.used_during_class) AS used_during_class,
                    (SELECT schedule_id FROM app_logs a3
                     WHERE a3.student_id = a.student_id
                       AND DATE(a3.log_date) = DATE(a.log_date)
                       AND a3.schedule_id IS NOT NULL
                     ORDER BY COALESCE(a3.created_at, a3.timestamp) DESC LIMIT 1) AS schedule_id,
                    (SELECT device_id FROM app_logs a4
                     WHERE a4.student_id = a.student_id
                       AND DATE(a4.log_date) = DATE(a.log_date)
                       AND a4.device_id IS NOT NULL
                     ORDER BY COALESCE(a4.created_at, a4.timestamp) DESC LIMIT 1) AS device_id,
                    (SELECT idempotency_key FROM app_logs a5
                     WHERE a5.student_id = a.student_id
                       AND DATE(a5.log_date) = DATE(a.log_date)
                     ORDER BY COALESCE(a5.created_at, a5.timestamp) DESC LIMIT 1) AS idempotency_key,
                    TRUE AS is_synced,
                    'mobile' AS source,
                    (SELECT campus_id FROM app_logs a6
                     WHERE a6.student_id = a.student_id
                       AND DATE(a6.log_date) = DATE(a.log_date)
                       AND a6.campus_id IS NOT NULL
                     ORDER BY COALESCE(a6.created_at, a6.timestamp) DESC LIMIT 1) AS campus_id,
                    (SELECT geofence_status FROM app_logs a7
                     WHERE a7.student_id = a.student_id
                       AND DATE(a7.log_date) = DATE(a.log_date)
                       AND a7.geofence_status IS NOT NULL
                     ORDER BY COALESCE(a7.created_at, a7.timestamp) DESC LIMIT 1) AS geofence_status,
                    MAX(COALESCE(a.created_at, a.timestamp)) AS created_at
                FROM app_logs a
                JOIN LATERAL (
                    SELECT id FROM app_logs a2
                    WHERE a2.student_id = a.student_id
                      AND DATE(a2.log_date) = DATE(a.log_date)
                      AND a2.source = 'mobile'
                    ORDER BY COALESCE(a2.created_at, a2.timestamp) DESC, a2.id DESC
                    LIMIT 1
                ) latest ON TRUE
                WHERE a.source = 'mobile'
                GROUP BY a.student_id, a.log_date, latest.id
                ON CONFLICT (id) DO NOTHING
                """
            )
        )
        # Step 2: delete the older duplicates, keeping only the row whose
        # id matches the one we just wrote.
        await conn.execute(
            text(
                """
                DELETE FROM app_logs a
                USING (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY student_id, DATE(log_date)
                        ORDER BY COALESCE(created_at, timestamp) DESC, id DESC
                    ) AS rn
                    FROM app_logs WHERE source = 'mobile'
                ) d
                WHERE a.id = d.id AND d.rn > 1
                """
            )
        )


async def _create_mobile_daily_unique_index_postgres():
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = 'public' "
                    "AND indexname = 'uq_app_logs_student_date_mobile'"
                )
            )
        ).scalar()
        if existing is not None:
            return
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_app_logs_student_date_mobile "
                "ON app_logs (student_id, log_date) "
                "WHERE source = 'mobile'"
            )
        )
        logger.info(
            "[run_app_logs_daily_id_migration] Created partial UNIQUE INDEX "
            "uq_app_logs_student_date_mobile (PostgreSQL)"
        )


# Round 27 — add users.fcm_token column for FCM device registration.
# The column was added to the SQLAlchemy model; this migration runs the
# equivalent DDL on existing databases (SQLite + PostgreSQL) so the
# `Base.metadata.create_all` from a fresh install matches the schema
# after running this on a live database.
async def run_fcm_token_migration() -> None:
    from app.models.user import User  # noqa: F401
    db_url = settings.DATABASE_URL
    is_sqlite = "sqlite" in db_url
    col = User.__table__.columns["fcm_token"]
    target_ddl = "TEXT"

    # Validate identifier via the same whitelist the rest of the
    # migration suite uses. _IDENT_RE is defined at module scope.
    if not _IDENT_RE.match("fcm_token") or not _IDENT_RE.match("users"):
        logger.error(
            "[run_fcm_token_migration] Refusing to run: identifier rejected"
        )
        return

    async with engine.begin() as conn:
        try:
            if is_sqlite:
                cols = (
                    await conn.execute(text("PRAGMA table_info(users)"))
                ).fetchall()
                if any(c[1] == "fcm_token" for c in cols):
                    return
                await conn.execute(
                    text(f"ALTER TABLE users ADD COLUMN fcm_token {target_ddl}")
                )
            else:
                exists = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='users' AND column_name='fcm_token'"
                        )
                    )
                ).scalar()
                if exists is not None:
                    return
                await conn.execute(
                    text(f"ALTER TABLE users ADD COLUMN fcm_token {target_ddl}")
                )
            logger.info("[run_fcm_token_migration] users.fcm_token added")
        except Exception:
            logger.exception(
                "[run_fcm_token_migration] FAILED to add users.fcm_token"
            )
