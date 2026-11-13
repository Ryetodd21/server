from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator


class MataKuliahResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    sks: int = 2
    semester: int = 1
    program_studi_id: str | None = None
    dosen_id: str | None = None
    campus_id: str | None = None
    campus_name: str | None = None
    description: str = ""
    is_active: bool = True
    created_at: datetime | None = None


class MataKuliahCreate(BaseModel):
    code: str
    name: str
    sks: int = 2
    semester: int = 1
    # Required — mata kuliah terikat ke satu program studi agar penjadwalan &
    # rekap klaster per prodi konsisten.
    program_studi_id: str
    dosen_id: str | None = None
    # Required — Android & geofence attendance detection need to know
    # which campus the student should be at during this course.
    campus_id: str
    description: str = ""

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("Code wajib diisi")
        if len(v) > 20:
            raise ValueError("Code maksimal 20 karakter")
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

    @field_validator("sks")
    @classmethod
    def sks_in_range(cls, v: int) -> int:
        if v < 1 or v > 8:
            raise ValueError("SKS harus antara 1 dan 8")
        return v

    @field_validator("semester")
    @classmethod
    def semester_in_range(cls, v: int) -> int:
        if v < 1 or v > 14:
            raise ValueError("Semester harus antara 1 dan 14")
        return v

    @field_validator("campus_id")
    @classmethod
    def campus_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Lokasi kampus wajib diisi")
        return v

    @field_validator("program_studi_id")
    @classmethod
    def program_studi_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Program studi wajib diisi")
        return v


class MataKuliahUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    sks: int | None = None
    semester: int | None = None
    program_studi_id: str | None = None
    dosen_id: str | None = None
    campus_id: str | None = None
    description: str | None = None
    is_active: bool | None = None
