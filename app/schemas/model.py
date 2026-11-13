from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime, date


class ModelConfigUpdate(BaseModel):
    pca_n_components: int = 2
    kmeans_n_clusters: int = 3
    cluster_labels: dict | None = None


class TrainRequest(BaseModel):
    pca_n_components: int = 2
    kmeans_n_clusters: int = 3


class TrainResponse(BaseModel):
    message: str
    version_number: int


# ─── Server-side ML serving (Round 28) ─────────────────────────────────────

class PredictRequest(BaseModel):
    """Score a single student on a specific date using the active model.

    `target_date` defaults to today (WITA) if not provided. The endpoint
    aggregates the same 7 feature columns the trainer used, applies
    scaler → PCA → KMeans, and returns the cluster + a normalized
    0.0–1.0 distraction score. Also returns the raw feature values
    so the dashboard can show "what drove this score" tooltips.
    """
    student_id: str
    target_date: date | None = None


class PredictResponse(BaseModel):
    student_id: str
    target_date: date
    cluster_label: int
    cluster_name: str
    distraction_score: float
    pca_x: float | None
    pca_y: float | None
    pca_z: float | None
    features_used: dict
    model_version_number: int
    # Round 29 audit P0 #6: when the (student, date) pair has no
    # app_log/location_log data, the endpoint used to score an
    # all-zeros feature vector and return a phantom cluster
    # assignment with `distraction_score=0.0` — a contradictory result
    # the dashboard could not disambiguate from a legitimate
    # "Sangat Rendah" classification. Now the endpoint detects
    # no-data and returns `skipped=True` with a reason. The cluster
    # fields are still required by the type but hold sentinel values
    # (-1, "Tidak ada data") so dashboards can render a clear
    # "Belum ada data untuk tanggal ini" card.
    skipped: bool = False
    skipped_reason: str | None = None


class BatchPredictRequest(BaseModel):
    """Score many students in one call. `target_date` defaults to today.

    Use cases:
    - Admin clicks "Re-score this date" on the dashboard.
    - AI assistant's "predict everyone's cluster for yesterday" prompt.
    - Cron pre-warm before the dashboard opens for the day.
    """
    student_ids: list[str] = Field(..., min_length=1, max_length=500)
    target_date: date | None = None


class BatchPredictResponse(BaseModel):
    target_date: date
    model_version_number: int
    results: list[PredictResponse]
    skipped: list[dict] = []  # {student_id, reason} for ids with no data


class ModelVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version_number: int
    training_date: datetime
    dataset_size: int
    is_active: bool
    status: str
    pca_n_components: int
    kmeans_n_clusters: int
    silhouette_score: float | None
    inertia: float | None
    train_size: int | None
    test_size: int | None
    cluster_distribution: dict | None
    cluster_labels: dict | None
    # Round 24: surfaced to the dashboard's training-failed banner so admin
    # sees *why* a model training failed (insufficient data, pipeline error,
    # etc.) without having to navigate to /models and click into the row.
    error_message: str | None = None


class ClusterResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    student_id: str
    result_date: datetime
    cluster_label: int
    distraction_score: float | None
    pca_x: float | None
    pca_y: float | None


class LatestResultResponse(BaseModel):
    student_id: str
    result_date: datetime
    cluster_label: int
    distraction_score: float | None
    cluster_name: str | None


class ClusterPointResponse(BaseModel):
    student_id: str
    full_name: str
    nim: str
    pca_x: float
    pca_y: float
    cluster_label: int
    distraction_score: float
