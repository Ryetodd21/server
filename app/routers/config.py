"""
Client-facing config endpoint — surfaces campus geofence + MQTT broker URL
to mobile clients (Android). The Android `CampusConfigDto` reads this so the
broker URL is discoverable from the server, not hardcoded.

GET /api/config/campus — any authenticated user
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.campus import Campus
from app.routers.auth import get_current_user

router = APIRouter(prefix="/config", tags=["Config"])


class CampusConfigResponse(BaseModel):
    """Response shape consumed by Android `CampusConfigDto`.

    Field names match `@SerializedName(...)` exactly:
      campus_id, campus_lat, campus_lon, geofence_radius_meters, mqtt_broker_url
    """
    campus_id: str | None = None
    campus_lat: float
    campus_lon: float
    geofence_radius_meters: int
    mqtt_broker_url: str
    # Round 27: VPS mosquitto as fallback. Android tries primary first, on
    # N consecutive failures, switches to fallback. Empty string means
    # "no fallback configured" — Android only retries primary.
    mqtt_broker_url_fallback: str = ""


@router.get("/campus", response_model=CampusConfigResponse)
async def get_campus_config(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """Returns the active campus geofence + MQTT broker URL for mobile clients.

    - Lat/lon/radius: first active row in `campuses` table, else UNG defaults.
    - broker URL: from `settings.MQTT_PUBLIC_URL` if set (Android must be able
      to reach this from the public internet). Falls back to `MQTT_BROKER_URL`
      for dev. The internal subscriber keeps using `MQTT_BROKER_URL` directly —
      this endpoint is the only place the public URL leaks out.
    """
    from app.config import get_settings
    settings = get_settings()
    advertised_broker_url = settings.MQTT_PUBLIC_URL or settings.MQTT_BROKER_URL

    result = await db.execute(
        select(Campus)
        .where(Campus.is_active == True)  # noqa: E712
        .order_by(Campus.created_at)
        .limit(1)
    )
    campus = result.scalar_one_or_none()

    if campus is not None:
        return CampusConfigResponse(
            campus_id=campus.id,
            campus_lat=campus.latitude,
            campus_lon=campus.longitude,
            geofence_radius_meters=int(campus.radius_meters),
            mqtt_broker_url=advertised_broker_url,
            mqtt_broker_url_fallback=settings.MQTT_FALLBACK_URL,
        )

    # No campus configured — fall back to UNG campus 1 defaults so the client still
    # gets a usable response. campus_id stays null (no DB row to reference).
    # radius_meters=200.0 matches CampusCreate schema default; aligned per audit 2026-10-04.
    from app.schemas.campus import CampusCreate
    return CampusConfigResponse(
        campus_id=None,
        campus_lat=settings.DEFAULT_CAMPUS_LAT,
        campus_lon=settings.DEFAULT_CAMPUS_LON,
        geofence_radius_meters=int(CampusCreate.model_fields["radius_meters"].default),
        mqtt_broker_url=advertised_broker_url,
        mqtt_broker_url_fallback=settings.MQTT_FALLBACK_URL,
    )
