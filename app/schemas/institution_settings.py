from pydantic import BaseModel, Field


class InstitutionSettingsResponse(BaseModel):
    university_name: str = ""
    faculty: str = ""
    program: str = ""
    time_zone: str = "WITA (UTC+8)"
    logo_data_url: str = ""

    model_config = {"from_attributes": True}


class InstitutionSettingsUpdate(BaseModel):
    university_name: str | None = None
    faculty: str | None = None
    program: str | None = None
    time_zone: str | None = None
    logo_data_url: str | None = None
