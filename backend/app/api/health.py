"""GET /api/health — проверка состояния сервиса (§17)."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.ai import client as ai_client
from app.config import Settings, get_settings
from app.database import check_db, get_db
from app.schemas import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["service"])


@router.get(
    "/api/health",
    response_model=HealthResponse,
    summary="Состояние сервиса",
    description="Проверяет подключение к БД, наличие PostGIS/pgvector и доступность AI inference server.",
)
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    ai_available: bool | None = None
    db_status = "ok"
    postgis_version = None
    pgvector = None
    objects_count = None
    districts_count = None

    try:
        info = check_db()
        postgis_version = info.get("postgis_version")
        pgvector = info.get("pgvector")
        objects_count = info.get("objects_count")
        districts_count = info.get("districts_count")
    except Exception as exc:  # noqa: BLE001
        db_status = "error"
        logger.error("health: ошибка подключения к БД: %s", exc)

    if settings.ai_enabled:
        ai_available = ai_client.check_available(settings)

    status = "ok"
    if db_status != "ok":
        status = "degraded"
    elif settings.ai_enabled and not ai_available:
        # AI опционален (§53): GIS-режим работает, но сервис помечаем как degraded
        status = "degraded"

    return HealthResponse(
        status=status,
        database=db_status,
        postgis_version=postgis_version,
        pgvector=pgvector,
        objects_count=objects_count,
        districts_count=districts_count,
        ai_enabled=settings.ai_enabled,
        ai_available=ai_available,
        ai_model=settings.ai_model if settings.ai_enabled else None,
    )
