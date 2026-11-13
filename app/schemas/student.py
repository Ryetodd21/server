from pydantic import BaseModel, ConfigDict
from typing import Optional


class StudentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    nim: str
    full_name: str
    study_program: str
    semester: int
    advisor_name: Optional[str] = None
    cluster_label: Optional[int] = None
    cluster_name: Optional[str] = None
    distraction_score: Optional[float] = None
    last_log_date: Optional[str] = None


class StudentListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[StudentResponse]
    cluster_distribution: dict[str, int] = {}
    active_model_version: Optional[int] = None


class StudentCreate(BaseModel):
    nim: str
    full_name: str
    program_studi: str = ""
    semester: int = 1
    advisor_id: str | None = None

    # When provided, create a linked User account for this student (mahasiswa role).
    # Without this, the students.user_id NOT NULL constraint would 500 — the create
    # endpoint never received a user_id and there was no way to supply one. Now admin
    # and dosen can register a student account through POST /api/students.
    create_user_account: bool = False
    initial_password: str | None = None
    email: str | None = None
