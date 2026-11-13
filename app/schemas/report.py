from pydantic import BaseModel
from datetime import date


class ReportFilter(BaseModel):
    period: str = "daily"  # daily | weekly | monthly
    start_date: date
    end_date: date
    cluster: int | None = None
    study_program: str | None = None
    lecturer_id: str | None = None
    subject_id: str | None = None


class ReportResponse(BaseModel):
    period: str
    start_date: date
    end_date: date
    total_students: int
    cluster_counts: dict  # {0: count, 1: count, 2: count}
    avg_screen_time: float
    avg_unlock_count: float
    data: list[dict]
