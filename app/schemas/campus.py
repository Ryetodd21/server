from pydantic import BaseModel, ConfigDict
from datetime import datetime


class CampusBase(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_meters: float = 200.0


class CampusCreate(CampusBase):
    pass


class CampusUpdate(BaseModel):
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_meters: float | None = None
    is_active: bool | None = None


class CampusResponse(CampusBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    is_active: bool
    created_at: datetime
