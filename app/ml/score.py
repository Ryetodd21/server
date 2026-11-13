"""
Shared scoring logic between ML trainer (`pipeline.py`) and ML serving
(`serving.py`).

Round 29 audit P1 #4: the `compute_distraction_score` formula was
duplicated in `pipeline.py:120-133` and `serving.py:306-328`, with a
comment in serving.py asserting parity but no compile-time guarantee.
A drift between the two (e.g. one gets updated to use a new weight,
the other doesn't) silently produces inconsistent scores between
training and serving — the trainer's idea of "high distraction"
diverges from what the dashboard shows. Centralizing in this module
makes drift impossible.

The cluster_label parameter is kept (for future per-cluster weighting
experiments) but currently unused — the score is a feature-based
function that does not depend on which cluster the student lands in.
"""
from typing import Any

from app.config import get_settings


def compute_distraction_score(
    row: dict[str, Any],
    cluster_label: int | None = None,  # currently unused, kept for future per-cluster weights
) -> float:
    """Hitung skor distraksi 0.0–1.0 berdasarkan fitur.

    The formula:
        screen_time          → min(sot / SCORE_SCREEN_TIME_CAP, 1.0) * SCORE_WEIGHT_SCREEN
        sosmed + game        → min(media / SCORE_MEDIA_CAP, 1.0) * SCORE_WEIGHT_MEDIA
        unlock_count         → min(unlock / SCORE_UNLOCK_CAP, 1.0) * SCORE_WEIGHT_UNLOCK
        used_during_class    → value * SCORE_WEIGHT_CLASS
        schedule_mismatch    → SCORE_WEIGHT_SCHEDULE if schedule_match_rate < threshold

    Round 29 audit: the `cluster_label` parameter is accepted but not
    used. Kept in the signature so future per-cluster calibration
    (e.g. "Sangat Tinggi" cluster gets a small boost) doesn't require
    touching every call site.

    Uses `get_settings()` per-call to match the pattern in
    `pipeline.py` — settings is re-read on every invocation so
    runtime env changes (e.g. in tests) are picked up without
    module reload.
    """
    cfg = get_settings()
    score = 0.0
    st_norm = min(row.get("screen_time", 0) / cfg.SCORE_SCREEN_TIME_CAP, 1.0)
    score += st_norm * cfg.SCORE_WEIGHT_SCREEN
    media_norm = min(
        (row.get("sosmed_time", 0) + row.get("game_time", 0)) / cfg.SCORE_MEDIA_CAP,
        1.0,
    )
    score += media_norm * cfg.SCORE_WEIGHT_MEDIA
    unlock_norm = min(row.get("unlock_count", 0) / cfg.SCORE_UNLOCK_CAP, 1.0)
    score += unlock_norm * cfg.SCORE_WEIGHT_UNLOCK
    score += row.get("used_during_class_int", 0) * cfg.SCORE_WEIGHT_CLASS
    if row.get("schedule_match_rate", 0) < cfg.SCORE_SCHEDULE_THRESHOLD:
        score += cfg.SCORE_WEIGHT_SCHEDULE
    return round(min(score, 1.0), 3)
