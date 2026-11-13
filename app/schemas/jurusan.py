from pydantic import BaseModel, ConfigDict, field_validator
from datetime import datetime


class JurusanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str = ""
    fakultas_id: str | None = None
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JurusanCreate(BaseModel):
    code: str
    name: str
    description: str = ""
    # Round 15: parent fakultas is REQUIRED. The DB column is NOT NULL,
    # so the schema must mirror that — otherwise a None value would be
    # rejected by Pydantic v2 with a confusing "Input should be a valid
    # string" message, OR worse, accepted as None by the model and then
    # crash with IntegrityError on commit.
    fakultas_id: str

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Code wajib diisi")
        v = v.strip().upper()
        if len(v) > 20:
            raise ValueError("Code maksimal 20 karakter")
        return v

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Nama wajib diisi")
        if len(v) > 150:
            raise ValueError("Nama maksimal 150 karakter")
        return v.strip()


class JurusanUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    description: str | None = None
    # None means "do not update"; an empty string would mean "set to None" which
    # is invalid here (the column is NOT NULL). The router validates that the
    # PUT body either omits fakultas_id entirely or supplies a non-empty value.
    fakultas_id: str | None = None
    is_active: bool | None = None
