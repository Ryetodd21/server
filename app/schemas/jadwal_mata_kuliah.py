from datetime import datetime, time
from pydantic import BaseModel, ConfigDict, field_validator


class JadwalMataKuliahResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    mata_kuliah_id: str
    day_of_week: int
    start_time: time
    end_time: time
    room: str = ""
    is_active: bool = True
    created_at: datetime | None = None


def _parse_hhmm(v: str) -> time:
    """Accept HH:MM or HH:MM:SS. Raises ValueError on malformed input."""
    parts = v.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError("Waktu harus berformat HH:MM")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("Waktu harus berformat HH:MM")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Waktu di luar rentang valid")
    return time(hour=h, minute=m)


class JadwalMataKuliahCreate(BaseModel):
    mata_kuliah_id: str
    day_of_week: int
    start_time: str  # HH:MM string, parsed below
    end_time: str
    room: str = ""

    @field_validator("day_of_week")
    @classmethod
    def dow_in_range(cls, v: int) -> int:
        # Round 26 audit (P1): the rest of the app uses ISO 1-7 for
        # day-of-week (Senin=1 … Minggu=7) — `ScheduleRequest.day_of_week`
        # validator, the DAYS_ID array on the web akademik page, the
        # ScheduleDto on Android. This schema was the odd one out
        # (0-6, Minggu=0). Switch to ISO so the same form value the
        # client validates also passes server-side.
        if v < 1 or v > 7:
            raise ValueError("Hari harus antara 1 (Senin) dan 7 (Minggu)")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def parse_time(cls, v: str) -> time:
        return _parse_hhmm(v)


class JadwalMataKuliahUpdate(BaseModel):
    day_of_week: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    room: str | None = None
    is_active: bool | None = None

    @field_validator("start_time", "end_time")
    @classmethod
    def parse_time(cls, v):
        if v is None:
            return v
        return _parse_hhmm(v)


# ─── Internal helper used by the router to coerce request payload ───

def time_to_hhmm(t: time | None) -> str:
    if t is None:
        return ""
    return f"{t.hour:02d}:{t.minute:02d}"


def hhmm_to_time(s: str) -> time:
    return _parse_hhmm(s)
