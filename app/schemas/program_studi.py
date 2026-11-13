from pydantic import BaseModel, ConfigDict, field_validator
from datetime import datetime


class ProgramStudiResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str = ""
    jurusan_id: str | None = None
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProgramStudiCreate(BaseModel):
    code: str
    name: str
    description: str = ""
    # Round 15: parent jurusan is RECOMMENDED but still optional for backwards
    # compatibility with legacy rows. The Akademik tab enforces it at the UI
    # layer; orphan prodi (created without a parent) are cleaned up by
    # cleanup_orphan_prodi() in main.py lifespan.
    jurusan_id: str | None = None

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


class ProgramStudiUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    jurusan_id: str | None = None
