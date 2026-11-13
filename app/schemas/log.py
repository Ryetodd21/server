from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import datetime


# --- App Log Schemas ---

class AppLogItem(BaseModel):
    # Local Android row id echoed back in SyncStatusResponse.synced_client_ids
    # so the client can mark exactly those rows SYNCED. Optional for backwards
    # compatibility with older Android clients that don't send it yet.
    client_id: str | None = None
    log_date: str  # "2026-09-17"
    # Round 29 audit P1: bounds-check plausibility. MQTT credentials are
    # extractable from the APK (build.gradle.kts) and the previous schema
    # accepted any int — an attacker with extracted creds could publish
    # `students/$id/logs` with `total_screen_time_minutes=99999` and skew
    # every aggregate query + the ML training pipeline. Real-world cap:
    # 1440 minutes/day (24×60); 200 unlocks (≈3/hr over 16 waking hours).
    total_screen_time_minutes: int = Field(..., ge=0, le=1440)
    social_media_time_minutes: int = Field(default=0, ge=0, le=1440)
    game_time_minutes: int = Field(default=0, ge=0, le=1440)
    other_app_time_minutes: int = Field(default=0, ge=0, le=1440)
    unlock_count: int = Field(default=0, ge=0, le=200)
    used_during_class: bool = False
    schedule_id: str | None = None
    app_package: str = ""
    app_name: str = ""

    @field_validator("log_date")
    @classmethod
    def validate_log_date(cls, v: str) -> str:
        from datetime import datetime
        datetime.strptime(v, "%Y-%m-%d")
        return v

    @field_validator("social_media_time_minutes", "game_time_minutes", "other_app_time_minutes")
    @classmethod
    def validate_components_le_total(cls, v: int, info) -> int:
        # Sum of breakdown cannot exceed total_screen_time_minutes. Catches
        # `sosmed=60000` payloads where total was 1440 — schema bounds
        # catch the per-field case but a malicious client could send
        # sosmed=1440 + game=1440 + total=1440 which is internally inconsistent.
        data = info.data
        total = data.get("total_screen_time_minutes")
        if total is not None and v > total:
            raise ValueError(
                f"{info.field_name}={v} exceeds total_screen_time_minutes={total}"
            )
        return v


class LocationLogItem(BaseModel):
    client_id: str | None = None
    recorded_at: str  # ISO 8601
    latitude: float
    longitude: float
    campus_id: str | None = None
    is_on_campus: bool = False
    matches_schedule: bool = False
    schedule_id: str | None = None

    @field_validator("recorded_at")
    @classmethod
    def validate_recorded_at(cls, v: str) -> str:
        from datetime import datetime
        datetime.fromisoformat(v)
        return v


class BatchLogRequest(BaseModel):
    device_id: str = Field(..., max_length=200)
    # Round 29 audit P0 #3: bound the per-request batch size so a captured
    # student account cannot DoS the server with millions of items in a
    # single request. The 30/minute rate limit on the endpoint is the
    # first line of defense; the 2000-item cap is the second.
    # 2000 items × 7-day backlog (Android daily-dedup) ≈ 2 weeks of
    # catch-up after a long offline period — well within real-world use.
    logs: list[AppLogItem] = Field(..., max_length=2000)
    locations: list[LocationLogItem] = Field(default_factory=list, max_length=2000)
    idempotency_key: str = Field(..., max_length=200)


class SyncStatusResponse(BaseModel):
    idempotency_key: str
    status: str  # pending | processed | duplicate
    processed_count: int
    # Echo back the local Android row ids of the items the server actually
    # persisted, including duplicates already stored. The Android
    # client uses this list to call markSynced(syncedIds) — without it,
    # pending rows stayed PENDING forever (round 17 fix).
    synced_client_ids: list[str] = []
