"""
PCA + K-Means Pipeline — dijalankan setiap malam pukul 23.00 WITA.
Menggunakan seluruh dataset kumulatif (historis + hari berjalan).
"""
import gc
import pickle
import base64
from typing import Any

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    calinski_harabasz_score,
    davies_bouldin_score,
    silhouette_score,
)

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import get_settings

settings = get_settings()


def build_feature_dataframe(session: Session) -> pd.DataFrame:
    """Ambil seluruh app_logs + location_logs, agregasi per student-date."""
    from app.models.app_log import AppLog
    from app.models.location_log import LocationLog

    # Aggregate app logs per student per day
    # Use pre-aggregated columns (from mobile app batch): social_media_time_minutes, game_time_minutes, etc.
    # Individual per-app rows (from MQTT/seed) have 0 in aggregated cols EXCEPT
    # unlock_count (cumulative per day, not a delta), so SUM() is correct for the
    # time columns. For unlock_count, MAX() is correct because the column holds
    # the day's cumulative total -- not a per-session delta.
    # log_date may be NULL for MQTT/seed rows — fall back to DATE(timestamp) so we never get 1970.
    effective_date = func.coalesce(AppLog.log_date, AppLog.timestamp).label("effective_date")
    logs_result = session.execute(
        select(
            AppLog.student_id,
            effective_date,
            AppLog.total_screen_time_minutes,
            AppLog.social_media_time_minutes,
            AppLog.game_time_minutes,
            AppLog.unlock_count,
            AppLog.used_during_class,
        )
    )
    log_rows = logs_result.fetchall()

    df = pd.DataFrame(
        log_rows,
        columns=["student_id", "log_date", "total_screen_time_minutes", "social_media_time_minutes", "game_time_minutes", "unlock_count", "used_during_class"],
    )

    if df.empty:
        raise ValueError("No log data available for training")

    # log_date guaranteed non-null thanks to COALESCE; convert to plain date for grouping.
    df["date"] = pd.to_datetime(df["log_date"]).dt.date

    # Aggregate per student-date using pre-aggregated columns
    # Round 26 audit (P1): the previous `sum` aggregations on
    # `total_screen_time_minutes` / `social_media_time_minutes` /
    # `game_time_minutes` silently inflated values 8-20x for seed
    # data. The seed loader writes multiple app_log rows per
    # (student, date) to simulate multiple class sessions, but each
    # row already stores the day's *total* time, not a session delta.
    # The `app_logs` table is pre-aggregated (Round 20 UNIQUE INDEX
    # only applies to `source='mobile'`, so seed rows are not deduped
    # on disk). `sum` over N rows/day/student multiplied the day's
    # screen time by N. Use `max` instead — the latest row carries
    # the canonical day total (the seed loader writes them in
    # ascending time order with the final row as the cumulative).
    # `unlock_count` was already `max` (correct — also pre-aggregated).
    pivot = (
        df.groupby(["student_id", "date"])
        .agg(
            screen_time=("total_screen_time_minutes", "max"),
            sosmed_time=("social_media_time_minutes", "max"),
            game_time=("game_time_minutes", "max"),
            unlock_count=("unlock_count", "max"),
            used_during_class_int=("used_during_class", "mean"),
        )
        .reset_index()
    )
    pivot["used_during_class_int"] = pivot["used_during_class_int"].fillna(0)

    # Aggregate location info
    loc_result = session.execute(
        select(
            LocationLog.student_id,
            LocationLog.timestamp,
            LocationLog.is_on_campus,
            LocationLog.matches_schedule,
        )
    )
    loc_rows = loc_result.fetchall()
    if loc_rows:
        loc_df = pd.DataFrame(
            loc_rows,
            columns=["student_id", "timestamp", "is_on_campus", "matches_schedule"],
        )
        loc_df["date"] = pd.to_datetime(loc_df["timestamp"]).dt.date
        agg_loc = loc_df.groupby(["student_id", "date"]).agg(
            campus_duration=("is_on_campus", "sum"),
            schedule_match_rate=("matches_schedule", "mean"),
        ).reset_index()
        pivot = pivot.merge(agg_loc, on=["student_id", "date"], how="left")
        pivot["campus_duration"] = pivot["campus_duration"].fillna(0)
        pivot["schedule_match_rate"] = pivot["schedule_match_rate"].fillna(0)
    else:
        pivot["campus_duration"] = 0
        pivot["schedule_match_rate"] = 0
    # DataFrame mentah per-baris bisa jauh lebih besar dari pivot; lepas
    # sebelum return supaya training malam tidak menahan dua salinan.
    del df, log_rows
    if loc_rows:
        del loc_df, agg_loc
    del loc_rows
    return pivot


def compute_distraction_score(row: dict[str, Any], cluster_label: int) -> float:
    """Hitung skor distraksi 0.0–1.0 berdasarkan fitur.

    Round 29 audit P1 #4: this is now a thin re-export of the shared
    implementation in `app.ml.score`. Kept here so existing call sites
    (and the test suite, if any) continue to work without edits.
    Drift between trainer and serving is now structurally impossible.
    """
    from app.ml.score import compute_distraction_score as _shared
    return _shared(row, cluster_label)


def _cluster_quality_metrics(
    X: np.ndarray,
    labels: np.ndarray,
    k_n: int,
) -> dict[str, float | None]:
    """Compute unsupervised cluster-quality metrics for one partition.

    K-Means has no ground-truth labels, so "accuracy" in the classification
    sense does not exist. These are the standard internal indices instead:

    - `silhouette`: [-1, 1] — how much closer a point is to its own cluster
      than the next-nearest one. Higher is better; ~0 means overlapping.
    - `davies_bouldin`: [0, inf) — average ratio of within-cluster scatter to
      between-cluster separation. Lower is better.
    - `calinski_harabasz`: [0, inf) — ratio of between- to within-cluster
      dispersion, scaled by degrees of freedom. Higher is better.

    Every metric needs at least 2 distinct clusters and more samples than
    clusters; otherwise it is undefined and we return None rather than a
    misleading 0.0. On a degenerate assignment (all points in one cluster)
    silhouette raises, so it is guarded separately.
    """
    metrics: dict[str, float | None] = {
        "silhouette": None,
        "davies_bouldin": None,
        "calinski_harabasz": None,
        "n_samples": int(len(X)),
        "n_clusters_present": int(len(set(labels.tolist()))),
    }
    if k_n <= 1 or len(X) <= k_n or metrics["n_clusters_present"] < 2:
        return metrics

    try:
        metrics["silhouette"] = round(float(silhouette_score(X, labels)), 4)
    except ValueError:
        # Raised when the number of unique labels is < 2 or == n_samples.
        metrics["silhouette"] = None
    try:
        metrics["davies_bouldin"] = round(float(davies_bouldin_score(X, labels)), 4)
    except ValueError:
        metrics["davies_bouldin"] = None
    try:
        metrics["calinski_harabasz"] = round(float(calinski_harabasz_score(X, labels)), 4)
    except ValueError:
        metrics["calinski_harabasz"] = None
    return metrics


def _check_holdout_gate(
    holdout_metrics: dict[str, float | None],
    min_silhouette: float | None,
    has_holdout: bool = True,
) -> tuple[bool, str]:
    """Decide whether a trained model is good enough to be marked completed.

    Returns `(ok, reason)`. `reason` is a human-readable explanation that the
    training caller surfaces as `ModelVersion.error_message` when ok is False.

    The gate is deliberately lenient about *missing* metrics: a holdout too
    small to compute silhouette (e.g. tiny dev datasets) is not treated as a
    failure — the admin still sees `silhouette=None` in the model history.
    Only a *computed* value below the configured floor blocks activation.
    """
    if min_silhouette is None:
        return True, ""

    if not has_holdout:
        return (
            True,
            "Dataset terlalu kecil untuk split holdout; gate silhouette dilewati.",
        )

    value = holdout_metrics.get("silhouette")
    if value is None:
        return (
            True,
            "Silhouette holdout tidak dapat dihitung (klaster degenerate "
            "atau sampel uji terlalu sedikit); gate dilewati.",
        )

    if value < min_silhouette:
        return (
            False,
            f"Model gagal evaluasi holdout: silhouette pada data uji = {value}, "
            f"di bawah ambang minimal {min_silhouette}. Model tidak diaktifkan; "
            f"model aktif sebelumnya tetap dipakai. Periksa kualitas fitur/data "
            f"atau turunkan ML_MIN_HOLDOUT_SILHOUETTE.",
        )

    return True, ""


def train_pipeline(
    session: Session,
    pca_n: int = None,
    k_n: int = None,
) -> dict:
    """
    Jalankan PCA + K-Means pipeline.
    Returns dict dengan metadata model dan hasil clustering.
    """
    pca_n = pca_n or settings.PCA_N_COMPONENTS
    k_n = k_n or settings.KMEANS_N_CLUSTERS

    df = build_feature_dataframe(session)

    if df.empty:
        raise ValueError(
            "Tidak ada data log untuk training. Pastikan mahasiswa sudah upload data penggunaan aplikasi."
        )

    feature_cols = [
        "screen_time", "sosmed_time", "game_time",
        "unlock_count", "campus_duration", "schedule_match_rate", "used_during_class_int",
    ]
    for col in feature_cols:
        if col not in df.columns:
            df[col] = 0
    df[feature_cols] = df[feature_cols].fillna(0)

    X = df[feature_cols].values
    student_ids = df["student_id"].values
    dates = pd.to_datetime(df["date"]).values
    del df

    # Minimum corpus size before a model may be fitted at all. The implicit
    # old floor was `len(X) >= k_n` (3 rows by default) — a silhouette or
    # inertia computed on 3 points is noise, and the resulting centroids
    # cannot be trusted to classify anyone. Enforced here (not in the router)
    # so the RQ worker path and the HTTP path share the same gate.
    min_rows = settings.ML_MIN_DATASET_SIZE
    if len(X) < min_rows:
        raise ValueError(
            f"Data tidak cukup untuk training ({len(X)} student-date rows, "
            f"butuh minimal {min_rows}). Kumpulkan lebih banyak log harian "
            f"sebelum melatih model."
        )

    # KMeans requires at least k_n samples and at least k_n unique data points
    if len(X) < k_n:
        raise ValueError(
            f"Data tidak cukup untuk clustering ({len(X)} student-date rows, butuh minimal {k_n}). "
            f"Training membutuhkan setidaknya {k_n} data point dari minimal 1 mahasiswa."
        )

    # Check for sufficient data variation (all zeros = no variation)
    if X.sum() == 0:
        raise ValueError(
            "Semua fitur bernilai 0 — tidak ada variasi data. "
            "Pastikan data screen time, social media time, dan game time sudah terisi."
        )

    if len(X) > 5:
        # Only the feature matrices are needed: the holdout evaluation below
        # measures cluster quality on `X_test`, not per-student outcomes, so
        # the student_id/date side arrays from the split are not used.
        X_train, X_test = train_test_split(
            X, test_size=settings.ML_TEST_SIZE, random_state=settings.ML_RANDOM_STATE
        )
        has_holdout = True
    else:
        X_train, X_test = X, X
        has_holdout = False

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    actual_pca_n = min(pca_n, X_train_scaled.shape[1], X_train_scaled.shape[0])
    pca = PCA(n_components=actual_pca_n)
    X_train_pca = pca.fit_transform(X_train_scaled)
    X_test_pca = pca.transform(X_test_scaled)

    kmeans = KMeans(n_clusters=k_n, random_state=settings.ML_RANDOM_STATE, n_init=settings.ML_N_INIT)
    train_labels = kmeans.fit_predict(X_train_pca)

    sil_score = None
    if len(X_train_pca) > k_n and k_n > 1:
        sil_score = round(float(silhouette_score(X_train_pca, train_labels)), 4)

    # ─── Holdout (test-set) evaluation ────────────────────────────────────
    # The test split is never shown to the fit calls above, so scoring it is a
    # genuine out-of-sample check. Previously the split existed but its labels
    # were only computed for the final all-data pass — no quality gate ran on
    # it, so a model that memorised the training rows (or a degenerate one that
    # put every test row in a single cluster) was still marked `completed`.
    # We now measure the same cluster-quality metrics on the holdout, and the
    # caller refuses to flip `status='completed'` when they fall below the
    # configured floor.
    test_labels = kmeans.predict(X_test_pca)
    holdout_metrics = _cluster_quality_metrics(X_test_pca, test_labels, k_n)
    # Metrics on the training partition too, for a train-vs-holdout comparison
    # (large gaps hint at overfitting even in an unsupervised setting).
    train_metrics = _cluster_quality_metrics(X_train_pca, train_labels, k_n)
    if sil_score is None:
        sil_score = train_metrics["silhouette"]

    holdout_ok, holdout_reason = _check_holdout_gate(
        holdout_metrics, settings.ML_MIN_HOLDOUT_SILHOUETTE, has_holdout
    )
    if not holdout_ok:
        raise ValueError(holdout_reason)

    from collections import Counter

    pca_data = base64.b64encode(pickle.dumps(pca)).decode()
    kmeans_data = base64.b64encode(pickle.dumps(kmeans)).decode()
    scaler_data = base64.b64encode(pickle.dumps(scaler)).decode()

    cluster_means = {}
    for lbl in range(k_n):
        mask = train_labels == lbl
        cluster_means[lbl] = float(X_train[mask, 0].mean() if mask.sum() > 0 else 0)

    sorted_labels = sorted(cluster_means, key=cluster_means.get)
    # Distraction level labels are now a real Settings field, not a getattr
    # lookup against a non-existent attr. Override via env
    # DISTRACTION_LABELS="Rendah,Sedang,Tinggi" etc.
    distraction_levels = settings.DISTRACTION_LABELS.split(",")
    label_map = {}
    for i, cluster_id in enumerate(sorted_labels):
        label_map[cluster_id] = distraction_levels[i] if i < len(distraction_levels) else f"Cluster {cluster_id}"

    X_all_scaled = scaler.transform(X)
    X_all_pca = pca.transform(X_all_scaled)
    all_labels = kmeans.predict(X_all_pca)

    # Distribution over the FULL dataset (train + holdout), not just the
    # training partition. `dataset_size` and `clustering_results` both cover
    # every row, so a train-only count made the dashboard's cluster totals
    # silently under-sum (e.g. 38 shown for a 48-row model). Computed here
    # because `all_labels` only exists after the all-data predict above.
    cluster_dist = dict(Counter(all_labels.tolist()))

    results = []
    for i, (sid, date_val, label) in enumerate(zip(student_ids, dates, all_labels)):
        row_dict = {col: float(val) for col, val in zip(feature_cols, X[i])}
        # Round 26 audit (P1): `used_during_class_int` is a fractional
        # mean of the boolean `used_during_class` per (student, date).
        # Values fall in [0.0, 1.0] and the distraction score multiplies
        # them by `SCORE_WEIGHT_CLASS` (typically 0.20). Casting to `int`
        # collapsed any value < 0.5 to 0 and any value >= 0.5 to 1 — so
        # a student with 0.4 class-time use (a real mid-value) got 0×
        # the weight, while a student with 0.6 got 1×. The PCA + KMeans
        # features already passed this as a float; only this consumer
        # did the lossy cast. Keep it as float; let the downstream
        # render decide whether to display as percentage.
        row_dict["used_during_class_int"] = row_dict.get("used_during_class_int", 0.0)
        score = compute_distraction_score(row_dict, label)
        pca_coords = X_all_pca[i].tolist()
        results.append({
            "student_id": str(sid),
            "result_date": str(date_val)[:10],
            "cluster_label": int(label),
            "pca_x": pca_coords[0] if len(pca_coords) > 0 else None,
            "pca_y": pca_coords[1] if len(pca_coords) > 1 else None,
            "pca_z": pca_coords[2] if len(pca_coords) > 2 else None,
            "distraction_score": score,
            "features": row_dict,
        })

    # Lepas salinan DataFrame/array besar sebelum worker kembali idle.
    sizes = {"dataset_size": len(X), "train_size": len(X_train), "test_size": len(X_test)}
    del X, X_train, X_test, X_train_pca, X_test_pca, X_all_scaled, X_all_pca
    gc.collect()

    return {
        "pca_data": pca_data,
        "kmeans_data": kmeans_data,
        "scaler_data": scaler_data,
        "pca_n_components": actual_pca_n,
        "kmeans_n_clusters": k_n,
        **sizes,
        "inertia": float(kmeans.inertia_),
        "silhouette_score": sil_score,
        # Holdout (out-of-sample) evaluation — the gate that must pass before
        # the caller flips `status='completed'`. Kept in the result dict so the
        # trainer can persist it as evidence, not just use it transiently.
        "holdout_metrics": holdout_metrics,
        "train_metrics": train_metrics,
        "holdout_min_silhouette": settings.ML_MIN_HOLDOUT_SILHOUETTE,
        "cluster_distribution": cluster_dist,
        "cluster_labels": label_map,
        "clustering_results": results,
        "feature_columns": feature_cols,
    }
