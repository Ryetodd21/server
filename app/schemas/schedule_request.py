from pydantic import BaseModel, ConfigDict, field_validator
from datetime import datetime
import re


class ScheduleRequestCreate(BaseModel):
    """User submits a schedule creation request."""
    day_of_week: int
    start_time: str
    end_time: str
    room: str | None = None
    subject_id: str | None = None
    lecturer_id: str | None = None
    subject_name_raw: str | None = None
    lecturer_name_raw: str | None = None
    campus_id: str | None = None
    description: str = "Request jadwal baru"

    @field_validator("day_of_week")
    @classmethod
    def validate_day_of_week(cls, v: int) -> int:
        if not 1 <= v <= 7:
            raise ValueError("day_of_week must be between 1 (Monday) and 7 (Sunday)")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError("Time must be in HH:MM format (e.g. 07:30 or 14:00)")
        hours, minutes = v.split(":")
        hours, minutes = int(hours), int(minutes)
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            raise ValueError("Time must be in HH:MM format (e.g. 07:30 or 14:00)")
        return v


class ScheduleRequestApproveReject(BaseModel):
    admin_notes: str | None = None


class ScheduleRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    requester_id: str
    student_id: str | None
    request_type: str
    description: str
    status: str
    admin_notes: str | None
    processed_by: str | None
    processed_at: datetime | None
    created_at: datetime

    # Embedded schedule data (if request_type = create)
    day_of_week: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    room: str | None = None
    subject_id: str | None = None
    lecturer_id: str | None = None
    subject_name_raw: str | None = None
    lecturer_name_raw: str | None = None
    campus_id: str | None = None
    subject_name: str | None = None
    lecturer_name: str | None = None
    student_name: str | None = None


class ScheduleRequestListResponse(BaseModel):
    requests: list[ScheduleRequestResponse]
    total: int
