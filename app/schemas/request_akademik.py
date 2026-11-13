from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator


class RequestAkademikResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    type: str
    name: str
    description: str = ""
    payload: str = ""
    target_id: str | None = None
    requested_by_id: str | None = None
    status: str = "pending"
    reviewed_by_id: str | None = None
    review_notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RequestAkademikCreate(BaseModel):
    type: str
    name: str
    description: str = ""
    payload: str = ""
    target_id: str | None = None

    @field_validator("type")
    @classmethod
    def type_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("course", "schedule_slot"):
            raise ValueError("Type harus 'course' atau 'schedule_slot'")
        return v

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Nama wajib diisi")
        if len(v) > 200:
            raise ValueError("Nama maksimal 200 karakter")
        return v


class RequestAkademikReview(BaseModel):
    """Approve or reject a request. Status string validated here."""
    status: str
    review_notes: str = ""

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("approved", "rejected"):
            raise ValueError("Status harus 'approved' atau 'rejected'")
        return v
