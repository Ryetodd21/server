from pydantic import BaseModel, ConfigDict
from datetime import datetime


class ScheduleBase(BaseModel):
    day_of_week: int
    start_time: str  # "07:30"
    end_time: str
    campus_id: str | None = None
    room: str | None = None
    subject_name_raw: str | None = None
    lecturer_name_raw: str | None = None
    reminder_h1: bool = True
    reminder_h3: bool = True
    reminder_h1_before: bool = True


class ScheduleCreate(ScheduleBase):
    subject_id: str | None = None
    lecturer_id: str | None = None


class ScheduleUpdate(BaseModel):
    day_of_week: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    campus_id: str | None = None
    room: str | None = None
    subject_id: str | None = None
    lecturer_id: str | None = None
    subject_name_raw: str | None = None
    lecturer_name_raw: str | None = None
    reminder_h1: bool | None = None
    reminder_h3: bool | None = None
    reminder_h1_before: bool | None = None
    is_active: bool | None = None


class ScheduleResponse(ScheduleBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    student_id: str
    subject_id: str | None
    lecturer_id: str | None
    is_active: bool
    created_at: datetime

    subject_name: str | None = None
    lecturer_name: str | None = None
    campus_name: str | None = None


class SubjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    code: str | None
    # Bridge dari mata_kuliah.campus_id (lihat routers/mata_kuliah). Android
    # butuh info lokasi untuk dropdown pengajuan jadwal & deteksi kehadiran.
    campus_id: str | None = None
    campus_name: str | None = None


class LecturerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    nidn: str | None


class ScheduleDirectory(BaseModel):
    subjects: list[SubjectResponse]
    lecturers: list[LecturerResponse]
