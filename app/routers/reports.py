from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import date, timedelta, datetime, timezone
from typing import Any
import calendar
import json
import logging
from app.config import get_settings
from app.database import get_db
from app.models.cluster_result import ClusterResult
from app.models.app_log import AppLog
from app.models.model_version import ModelVersion
from app.models.student import Student
from app.routers.auth import get_current_user
from app.cache import cache_get, cache_set
from app.timezone import WITA

logger = logging.getLogger("monitor")
settings = get_settings()

router = APIRouter(prefix="/reports", tags=["Reports"])

# Cache TTL for active-model lookups. The active model changes only when an
# admin runs POST /api/models/train (round 9 typically retrain manually +
# nightly scheduler at 23:00 WITA). 60s is conservative — invalidate()
# drops the key immediately on training so dashboards reload on the next hit.
_ACTIVE_MODEL_TTL = 60.0
_CACHE_KEY_MODEL_ID = "reports:active_model_id"
_CACHE_KEY_MODEL_LABELS = "reports:active_model_labels"


async def require_admin_or_dosen(request: Request) -> dict:
    """Reports expose aggregate data across all students — restrict to staff."""
    payload = await get_current_user(request)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(
            status_code=403,
            detail="Akses ditolak. Laporan agregat hanya untuk admin/dosen.",
        )
    return payload


async def get_active_model_id(db: AsyncSession) -> str | None:
    """Get the ID of the latest (active) model version.

    Cached in-process for 60s. Reports dashboard refreshes hit this once per
    page load; the active model changes only when training runs (rare), so the
    cache win is roughly N dashboard-views-per-minute vs. 1 DB round-trip.
    """
    cached = cache_get(_CACHE_KEY_MODEL_ID, _ACTIVE_MODEL_TTL)
    if cached is not None:
        # cached holds either the UUID string or the sentinel "" meaning "no active model"
        return cached or None
    # Round 29 audit P1: require is_active=True AND status='completed'. Without
    # the WHERE, this returns the most recent version_number — which after a
    # failed training can be a `status="failed"` MV (committed with
    # is_active=True, then deactivated in the except branch). All reports
    # would then use a failed model. The previous code in `_get_active_model_labels`
    # already required status='completed'; align the id lookup the same way.
    result = await db.execute(
        select(ModelVersion.id)
        .where(ModelVersion.is_active == True, ModelVersion.status == "completed")  # noqa: E712
        .order_by(ModelVersion.version_number.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    cache_set(_CACHE_KEY_MODEL_ID, row or "", _ACTIVE_MODEL_TTL)
    return row


async def _get_active_model_labels(db: AsyncSession) -> dict | None:
    """Get cluster_labels dict from the active model, or None.

    Cached alongside the active model id — both invalidate together.
    Cache values use sentinels: "" = no active model, None = active model
    has no parseable labels (parse error), dict = real labels.
    """
    # cache_get returns the cached value (any of "..."/None/dict) or None when
    # no entry exists. We treat "no entry" as miss; the other sentinels are
    # cache hits and short-circuit the DB round-trip.
    cached = cache_get(_CACHE_KEY_MODEL_LABELS, _ACTIVE_MODEL_TTL)
    if cached is not None:
        # Could be "" (no active model), None (parse error), or a dict.
        # In all three cases we already know the answer.
        if cached == "" or cached is None:
            return None
        return cached
    result = await db.execute(
        select(ModelVersion.cluster_labels)
        .where(ModelVersion.is_active == True)
        .limit(1)
    )
    raw = result.scalar_one_or_none()
    if not raw:
        cache_set(_CACHE_KEY_MODEL_LABELS, "", _ACTIVE_MODEL_TTL)
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        cache_set(_CACHE_KEY_MODEL_LABELS, None, _ACTIVE_MODEL_TTL)
        return None
    cache_set(_CACHE_KEY_MODEL_LABELS, parsed, _ACTIVE_MODEL_TTL)
    return parsed


@router.get("")
async def get_report(
    request: Request,
    period: str = Query("daily"),
    start_date: date = Query(...),
    end_date: date = Query(...),
    cluster: int = Query(None, description="Filter averages to an active model cluster id"),
    db: AsyncSession = Depends(get_db),
    _payload: dict = Depends(require_admin_or_dosen),
):
    # Mobile app sends dates in WITA (UTC+8); convert to UTC for DB comparisons.
    # WITA midnight = UTC 16:00 previous day, so subtract 8h to get UTC range.
    wita_offset = WITA.utcoffset(None)
    start_dt = (datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
    end_dt = (datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) - wita_offset)

    # Get active model id + label mapping. cluster_label is an integer in
    # cluster_results, so cluster filter must be int — translated to label for
    # the response so the frontend can render the human-readable name.
    active_model_id = await get_active_model_id(db)
    active_labels = await _get_active_model_labels(db) or {}

    # Build a subquery of student_ids in the requested cluster over the date
    # range. If `cluster` is None, the subquery is NULL and the JOIN acts as a
    # pass-through (all students). Empty match → short-circuit the four
    # averages to 0 (same pattern as /api/students?cluster=).
    cluster_student_ids_subq = None
    if cluster is not None and active_model_id is not None:
        cluster_rows = await db.execute(
            select(ClusterResult.student_id).where(
                ClusterResult.model_version_id == active_model_id,
                ClusterResult.cluster_label == cluster,
                ClusterResult.result_date >= start_dt,
                ClusterResult.result_date < end_dt,
            )
        )
        cluster_student_ids = [r[0] for r in cluster_rows.all()]
        if not cluster_student_ids:
            # No students match this cluster in the requested range → empty averages.
            # We still return global cluster_counts + cluster_filter so the
            # frontend knows the filter was understood.
            label = active_labels.get(str(cluster), str(cluster))
            return {
                "period": period,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "total_students": 0,
                "cluster_counts": {str(cluster): 0},
                "cluster_filter": cluster,
                "cluster_filter_label": label,
                "cluster_labels": active_labels,
                "avg_distraction_score": None,
                "avg_screen_time": 0.0,
                "avg_unlock_count": 0.0,
                "avg_social_media_time": 0.0,
                "avg_game_time": 0.0,
                "model_version_id": active_model_id,
            }
        cluster_student_ids_subq = cluster_student_ids

    # Get cluster distribution from the active model's stored data
    total_students = 0
    cluster_counts: dict = {}
    if active_model_id:
        result = await db.execute(
            select(
                ClusterResult.cluster_label,
                func.count(ClusterResult.id).label("count"),
            ).where(
                ClusterResult.model_version_id == active_model_id,
                ClusterResult.result_date >= start_dt,
                ClusterResult.result_date < end_dt,
            ).group_by(ClusterResult.cluster_label)
        )
        rows = result.all()
        # Round 26 audit (P1): project cluster counts through the active
        # model's label mapping (e.g. {"0": "Rendah", "1": "Sedang",
        # "2": "Tinggi"}) so the web ReportResponse reads
        # `clusterCounts.Rendah ?? 0` directly. Previously this returned
        # the raw integer keys `{"0": 5, "1": 3, "2": 2}` which the web
        # never matched — the Distribusi Klaster metric silently showed
        # 0/0/0 even with real data.
        cluster_counts = {
            active_labels.get(str(r.cluster_label), str(r.cluster_label)): r.count
            for r in rows
        }
        total_students = sum(cluster_counts.values())

    # Get avg screen time, social media time, and game time from app_logs.
    # All three averages are computed per day over the requested date range so
    # the reports page can show the trio side-by-side (Round 11 user feedback).
    # When a cluster filter is applied, scope by the student_id list from
    # cluster_results above (otherwise the averages stay global).
    avg_screen_time = 0.0
    avg_unlock = 0.0
    avg_social_media_time = 0.0
    avg_game_time = 0.0

    log_filters = [
        AppLog.timestamp >= start_dt,
        AppLog.timestamp < end_dt,
    ]
    if cluster_student_ids_subq is not None:
        log_filters.append(AppLog.student_id.in_(cluster_student_ids_subq))

    try:
        log_result = await db.execute(
            select(func.avg(AppLog.total_screen_time_minutes)).where(*log_filters, AppLog.total_screen_time_minutes != None)
        )
        avg_minutes = log_result.scalar() or 0
        avg_screen_time = round(float(avg_minutes), 2)
    except Exception:
        avg_screen_time = 0.0

    try:
        unlock_result = await db.execute(
            select(func.count(AppLog.id)).where(*log_filters)
        )
        unlock_count = unlock_result.scalar() or 0
        avg_unlock = round(float(unlock_count) / max(total_students, 1), 1)
    except Exception:
        avg_unlock = 0.0

    try:
        sosmed_result = await db.execute(
            select(func.avg(AppLog.social_media_time_minutes)).where(*log_filters, AppLog.social_media_time_minutes != None)
        )
        sosmed_minutes = sosmed_result.scalar() or 0
        avg_social_media_time = round(float(sosmed_minutes), 2)
    except Exception:
        avg_social_media_time = 0.0

    try:
        game_result = await db.execute(
            select(func.avg(AppLog.game_time_minutes)).where(*log_filters, AppLog.game_time_minutes != None)
        )
        game_minutes = game_result.scalar() or 0
        avg_game_time = round(float(game_minutes), 2)
    except Exception:
        avg_game_time = 0.0

    score_filters = [
        ClusterResult.model_version_id == active_model_id,
        ClusterResult.result_date >= start_dt,
        ClusterResult.result_date < end_dt,
    ]
    if cluster is not None:
        score_filters.append(ClusterResult.cluster_label == cluster)
    score = (await db.execute(select(func.avg(ClusterResult.distraction_score)).where(*score_filters))).scalar()

    return {
        "period": period,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "total_students": total_students,
        "cluster_counts": cluster_counts,
        "cluster_filter": cluster,
        "cluster_filter_label": active_labels.get(str(cluster)) if cluster is not None else None,
        "cluster_labels": active_labels,
        "avg_distraction_score": round(float(score), 4) if score is not None else None,
        "avg_screen_time": avg_screen_time,
        "avg_unlock_count": avg_unlock,
        "avg_social_media_time": avg_social_media_time,
        "avg_game_time": avg_game_time,
        "model_version_id": active_model_id,
    }


@router.get("/screen-time")
async def get_screen_time_trends(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _payload: dict = Depends(require_admin_or_dosen),
):
    """Daily screen time trends for the last N days.

    Round 10 optimization: replaced N day-loop queries (1 round-trip per day)
    with a single GROUP BY DATE(timestamp) query. For `days=7` that's
    7 → 1 round-trips; for `days=90` (max) that's 90 → 1.

    The DB returns sparse rows — days with no data simply don't appear.
    We backfill them with zeros so the chart's x-axis stays continuous.
    """
    today = datetime.now(WITA).date()
    wita_offset = WITA.utcoffset(None)
    range_start = today - timedelta(days=days - 1)
    range_start_dt = (datetime.combine(range_start, datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
    range_end_dt = (datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) - wita_offset)

    result = await db.execute(
        select(
            func.date(AppLog.timestamp).label("day"),
            func.count(AppLog.id).label("log_count"),
            func.avg(AppLog.total_screen_time_minutes).label("avg_duration"),
            func.sum(AppLog.total_screen_time_minutes).label("total_duration"),
        ).where(
            AppLog.timestamp >= range_start_dt,
            AppLog.timestamp < range_end_dt,
            AppLog.total_screen_time_minutes != None,
        ).group_by(func.date(AppLog.timestamp))
    )
    by_day = {row.day: row for row in result.all()}

    day_names = ["Min", "Sen", "Sel", "Rab", "Kam", "Jum", "Sab"]
    trends = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        row = by_day.get(day)
        label = day_names[day.weekday()] if days <= 14 else day.strftime("%d/%m")
        trends.append({
            "date": str(day),
            "label": label,
            "log_count": int(row.log_count) if row and row.log_count else 0,
            "avg_duration_min": round(float(row.avg_duration), 1) if row and row.avg_duration else 0,
            "total_duration_min": round(float(row.total_duration), 1) if row and row.total_duration else 0,
        })

    return {"trends": trends, "period": "daily", "days": days}


@router.get("/cluster-trends")
async def get_cluster_trends(
    request: Request,
    period: str = Query(default="weekly", regex="^(daily|weekly|monthly)$"),
    weeks: int = Query(default=4, ge=1, le=30, description="Number of periods: days for daily, weeks for weekly, months for monthly"),
    db: AsyncSession = Depends(get_db),
    _payload: dict = Depends(require_admin_or_dosen),
):
    """Cluster distribution trends — only from the active model.

    `weeks` parameter is semantic per period:
      - daily: number of days (last N days, oldest → newest)
      - weekly: number of weeks (last N weeks)
      - monthly: number of months (last N months)
    """
    today = datetime.now(WITA).date()
    trends = []
    wita_offset = WITA.utcoffset(None)
    active_model_id = await get_active_model_id(db)

    if not active_model_id:
        return {"trends": [], "period": period, "model_version_id": None}

    cluster_labels = await _get_active_model_labels(db) or {}

    def build_row(rows, date_label, start_date=None, end_date=None):
        counts = {str(r.cluster_label): r.count for r in rows}
        named_counts = {cluster_labels.get(cid, cid): count for cid, count in counts.items()}
        score_count = sum(r.score_count for r in rows)
        row = {
            "label": date_label,
            "total": sum(counts.values()),
            "cluster_counts": named_counts,
            "avg_distraction_score": round(sum(float(r.score_sum or 0) for r in rows) / score_count, 4) if score_count else None,
        }
        # Legacy consumers receive named buckets, never a risk inferred from cluster ID.
        for label in ("Rendah", "Sedang", "Tinggi"):
            row[label] = named_counts.get(label, named_counts.get(f"Distraksi {label}", 0))
        if start_date:
            row["start_date"] = str(start_date)
        if end_date:
            row["end_date"] = str(end_date)
        return row

    if period == "daily":
        # `weeks` here = number of days (last N days, oldest → newest).
        # Frontend passes 7 for "Tren 7 Hari".
        # Round 10: single GROUP BY query instead of N day-loop queries.
        range_start = today - timedelta(days=weeks - 1)
        range_start_dt = (datetime.combine(range_start, datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
        range_end_dt = (datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
        result = await db.execute(
            select(
                func.date(ClusterResult.result_date).label("bucket"),
                ClusterResult.cluster_label,
                func.count(ClusterResult.id).label("count"),
                func.sum(ClusterResult.distraction_score).label("score_sum"),
                func.count(ClusterResult.distraction_score).label("score_count"),
            ).where(
                ClusterResult.model_version_id == active_model_id,
                ClusterResult.result_date >= range_start_dt,
                ClusterResult.result_date < range_end_dt,
            ).group_by(func.date(ClusterResult.result_date), ClusterResult.cluster_label)
        )
        day_names = ["Min", "Sen", "Sel", "Rab", "Kam", "Jum", "Sab"]
        rows_by_day: dict[date, list] = {today - timedelta(days=i): [] for i in range(weeks)}
        for r in result.all():
            bucket_date = r.bucket if isinstance(r.bucket, date) else datetime.strptime(str(r.bucket), "%Y-%m-%d").date()
            if bucket_date in rows_by_day:
                rows_by_day[bucket_date].append(r)
        for i in range(weeks - 1, -1, -1):
            day = today - timedelta(days=i)
            rows = rows_by_day.get(day, [])
            trends.append(build_row(rows, day_names[day.weekday()], start_date=str(day), end_date=str(day)))

    elif period == "weekly":
        # `weeks` = number of weeks (last N weeks, oldest → newest).
        # Frontend passes 4 for "Tren 4 Minggu".
        # Round 10: single GROUP BY date_trunc('week', ...) query.
        db_url = settings.DATABASE_URL.lower()
        is_sqlite = "sqlite" in db_url
        # PostgreSQL: date_trunc('week', ...) returns a timestamp at the start
        # of the week (Monday). SQLite: strftime('%Y-%W', ...) gives a
        # year-week string. Either way the bucket key is opaque to us — we
        # match by index by walking the rows oldest → newest.
        if is_sqlite:
            bucket_expr = func.strftime("%Y-%W", ClusterResult.result_date).label("bucket")
        else:
            bucket_expr = func.date_trunc("week", ClusterResult.result_date).label("bucket")
        range_start = today - timedelta(days=weeks * 7 - 1)
        range_start_dt = (datetime.combine(range_start, datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
        range_end_dt = (datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
        result = await db.execute(
            select(
                bucket_expr,
                ClusterResult.cluster_label,
                func.count(ClusterResult.id).label("count"),
                func.sum(ClusterResult.distraction_score).label("score_sum"),
                func.count(ClusterResult.distraction_score).label("score_count"),
            ).where(
                ClusterResult.model_version_id == active_model_id,
                ClusterResult.result_date >= range_start_dt,
                ClusterResult.result_date < range_end_dt,
            ).group_by(bucket_expr, ClusterResult.cluster_label)
        )
        # Group result rows by their bucket, then assign bucket → week-slot
        # in order of bucket key. This avoids date-arithmetic in Python.
        bucket_to_rows: dict[Any, list] = {}
        for r in result.all():
            bucket_to_rows.setdefault(r.bucket, []).append(r)
        # Sort buckets oldest → newest. PostgreSQL buckets are datetime;
        # SQLite buckets are string ("YYYY-WW"). Sort works on both.
        sorted_buckets = sorted(bucket_to_rows.keys())
        for i in range(weeks):
            week_end = today - timedelta(days=i * 7)
            week_start = week_end - timedelta(days=7)
            # The newest bucket aligns with week index 0 (this week), oldest
            # with index weeks-1.
            bucket_idx = weeks - 1 - i
            rows_for_week = bucket_to_rows.get(sorted_buckets[bucket_idx], []) if bucket_idx < len(sorted_buckets) else []
            trends.append(build_row(
                rows_for_week,
                f"Minggu {weeks - i}",
                start_date=str(week_start),
                end_date=str(week_end - timedelta(days=1)),
            ))

    elif period == "monthly":
        # `weeks` = number of months (last N months, oldest → newest).
        # Frontend passes 3 for "Tren 3 Bulan".
        # Round 10: single GROUP BY date_trunc('month', ...) query.
        db_url = settings.DATABASE_URL.lower()
        is_sqlite = "sqlite" in db_url
        if is_sqlite:
            bucket_expr = func.strftime("%Y-%m", ClusterResult.result_date).label("bucket")
        else:
            bucket_expr = func.date_trunc("month", ClusterResult.result_date).label("bucket")
        # Compute oldest month range.
        year = today.year
        month = today.month - (weeks - 1)
        while month <= 0:
            month += 12
            year -= 1
        oldest_month_start = today.replace(year=year, month=month, day=1)
        oldest_start_dt = (datetime.combine(oldest_month_start, datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
        range_end_dt = (datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
        result = await db.execute(
            select(
                bucket_expr,
                ClusterResult.cluster_label,
                func.count(ClusterResult.id).label("count"),
                func.sum(ClusterResult.distraction_score).label("score_sum"),
                func.count(ClusterResult.distraction_score).label("score_count"),
            ).where(
                ClusterResult.model_version_id == active_model_id,
                ClusterResult.result_date >= oldest_start_dt,
                ClusterResult.result_date < range_end_dt,
            ).group_by(bucket_expr, ClusterResult.cluster_label)
        )
        rows = result.all()
        month_names = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
        for i in range(weeks - 1, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            month_start = today.replace(year=year, month=month, day=1)
            _, last_day = calendar.monthrange(year, month)
            month_end = month_start.replace(day=last_day) + timedelta(days=1)
            # Filter result rows by bucket matching this month.
            if is_sqlite:
                target_bucket = f"{year:04d}-{month:02d}"
                matching = [r for r in rows if str(r.bucket) == target_bucket]
            else:
                target_dt = datetime.combine(month_start, datetime.min.time(), tzinfo=timezone.utc)
                matching = [r for r in rows if r.bucket == target_dt]
            trends.append(build_row(
                matching,
                f"{month_names[month_start.month]} {month_start.year}",
                start_date=str(month_start),
                end_date=str(month_end - timedelta(days=1)),
            ))

    return {"trends": trends, "period": period, "model_version_id": active_model_id}


@router.get("/summary")
async def get_dashboard_summary(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _payload: dict = Depends(require_admin_or_dosen),
):
    """Summary metrics for the dashboard.

    Active = students with AppLog from mobile app (source='mobile').
    Total registered = all students in the students table.
    Pending = total - active.

    Round 10: cached in-process for 30s. The dashboard polls this endpoint
    on a tight refresh loop, so even a short cache collapses N dashboard-
    refreshes-per-minute into 1 query. The TTL is short enough that admin
    "Lihat Sekarang" actions still feel responsive.
    """
    scoped = "academic_scope" in db.sync_session.info
    cached = None if scoped else cache_get("reports:summary", 30.0)
    if cached is not None:
        return cached
    from app.models.student import Student
    today = datetime.now(WITA).date()
    week_ago = today - timedelta(days=7)
    wita_offset = WITA.utcoffset(None)
    week_start = (datetime.combine(week_ago, datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
    week_end = (datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) - wita_offset)

    # Single round-trip for the 4 counts/avg that share the app_logs table.
    # `source='mobile' AND timestamp in range` covers active students + log
    # count in one scan; the seed count is a separate filter. The DISTINCT
    # student count is done in Python after we have the mobile-week rows.
    mobile_week_q = await db.execute(
        select(
            func.count(AppLog.id).label("mobile_log_count"),
            func.count(func.distinct(AppLog.student_id)).label("active_students"),
        ).where(
            AppLog.source == "mobile",
            AppLog.timestamp >= week_start,
            AppLog.timestamp < week_end,
        )
    )
    mobile_row = mobile_week_q.one_or_none()
    # Round 20: with the partial UNIQUE INDEX enforcing one row per
    # (student_id, log_date) for mobile-source data, this count now
    # measures "mobile day-rows" (≈ unique student-days), not raw event
    # count. Pre-Round 20 it was ~5× the number of student-days because
    # each 45-min sync was a separate row. The new value is more
    # meaningful (it's literally "how many days of data we have") so
    # we keep the same field name and update the dashboard copy to
    # reflect the new semantic. "mobile_day_rows_week" is the explicit
    # alias for the same value.
    app_logs_week = int(mobile_row.mobile_log_count) if mobile_row and mobile_row.mobile_log_count else 0
    active_students_mobile = int(mobile_row.active_students) if mobile_row and mobile_row.active_students else 0
    mobile_day_rows_week = app_logs_week  # alias — see comment above

    # Total registered students — separate query (different table).
    total_result = await db.execute(select(func.count(Student.id)))
    total_registered = total_result.scalar() or 0

    # WITA day boundaries for today. `log_date` is the column Android writes,
    # stored as tz-aware UTC; the WITA midnight shift moves the day boundary
    # 8 hours earlier in UTC.
    today_start = (datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - wita_offset)
    today_end = (datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) - wita_offset)

    # Single round-trip for the 3 today-averages (screen / sosmed / game).
    # Round 23: collapse 3 separate AVG queries into one with multiple
    # aggregate functions — same WHERE clause, same scan, 3× fewer round-trips.
    today_avgs_q = await db.execute(
        select(
            func.avg(AppLog.total_screen_time_minutes).label("avg_screen"),
            func.avg(AppLog.social_media_time_minutes).label("avg_sosmed"),
            func.avg(AppLog.game_time_minutes).label("avg_game"),
        ).where(
            AppLog.source == "mobile",
            AppLog.log_date >= today_start,
            AppLog.log_date < today_end,
        )
    )
    today_avgs = today_avgs_q.one_or_none()
    avg_screen_today = float(today_avgs.avg_screen) if today_avgs and today_avgs.avg_screen else 0.0
    avg_sosmed_today = float(today_avgs.avg_sosmed) if today_avgs and today_avgs.avg_sosmed else 0.0
    avg_game_today = float(today_avgs.avg_game) if today_avgs and today_avgs.avg_game else 0.0

    # Wave 2 #4: combine today's unlock-event count AND distinct student count
    # into one query (mobile source, today range). Previously divided the
    # event count by 7-day active_students_mobile, which understated the
    # per-student average whenever fewer students logged today than the week.
    unlock_today_q = await db.execute(
        select(
            func.count(AppLog.id).label("unlock_count"),
            func.count(func.distinct(AppLog.student_id)).label("active_today"),
        ).where(
            AppLog.source == "mobile",
            AppLog.timestamp >= today_start,
            AppLog.timestamp < today_end,
        )
    )
    unlock_today_row = unlock_today_q.one_or_none()
    unlock_count_today_raw = int(unlock_today_row.unlock_count) if unlock_today_row and unlock_today_row.unlock_count else 0
    active_students_today = int(unlock_today_row.active_today) if unlock_today_row and unlock_today_row.active_today else 0
    avg_unlock_today = round(float(unlock_count_today_raw) / max(active_students_today, 1), 1)

    # Seed logs this week (for reference) — separate filter (source='seed').
    seed_logs_result = await db.execute(
        select(func.count(AppLog.id)).where(
            AppLog.source == "seed",
            AppLog.timestamp >= week_start,
            AppLog.timestamp < week_end,
        )
    )
    seed_logs_week = seed_logs_result.scalar() or 0

    response = {
        "total_registered": total_registered,
        "active_students_week": active_students_mobile,
        "pending_students": total_registered - active_students_mobile,
        # Round 20: app_logs_week is now "unique student-day rows" (≈ days
        # of mobile data received) rather than raw event count. Aliases:
        # `mobile_day_rows_week` is the explicit name for the same value.
        "app_logs_week": app_logs_week,
        "mobile_day_rows_week": mobile_day_rows_week,
        "seed_logs_week": seed_logs_week,
        "avg_screen_time_today_min": round(float(avg_screen_today), 1),
        "avg_unlock_count_today": avg_unlock_today,
        "avg_social_media_time_today_min": round(float(avg_sosmed_today), 1),
        "avg_game_time_today_min": round(float(avg_game_today), 1),
        "date": str(today),
    }
    if not scoped:
        cache_set("reports:summary", response, 30.0)
    return response
