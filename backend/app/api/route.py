"""POST /api/route — «Прогулка»: пеший маршрут по культурным объектам.

Пользователь ставит точку на карте и просит прогулку: сервис отбирает объекты
в радиусе и выстраивает порядок обхода (жадный ближайший сосед). Расстояния
считает PostGIS, время — оценка по скорости пешехода 4,8 км/ч.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.objects import validate_categories, validate_coords, validate_radius
from app.config import Settings, get_settings
from app.database import get_db
from app.schemas.route_schemas import RouteRequest, RouteResponse
from app.spatial import routes as route_spatial

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["route"])


@router.post(
    "/route",
    response_model=RouteResponse,
    summary="Прогулка: пеший маршрут по объектам от выбранной точки",
    description=(
        "Выбирает объекты в радиусе от точки старта и выстраивает порядок "
        "обхода жадным алгоритмом (каждый следующий — ближайший к текущей "
        "точке). Перегоны — геодезические расстояния в метрах (PostGIS), "
        "время — оценка по средней скорости пешехода 4,8 км/ч."
    ),
)
def walk_route(
    payload: RouteRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    validate_coords(payload.latitude, payload.longitude)
    validate_radius(payload.radius_meters, settings)
    categories = validate_categories(payload.categories)
    logger.info(
        "Прогулка от (%.5f, %.5f): радиус %.0f м, до %d остановок",
        payload.latitude,
        payload.longitude,
        payload.radius_meters,
        payload.count,
    )
    return route_spatial.build_walk_route(
        db,
        payload.latitude,
        payload.longitude,
        radius_m=payload.radius_meters,
        count=payload.count,
        categories=categories,
        not_keyword=payload.not_keyword,
    )
