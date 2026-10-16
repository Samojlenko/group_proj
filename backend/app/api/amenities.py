"""Эндпоинты OSM-слоя заведений (идея 28, этап H).

GET /api/amenities        — кафе/туалеты/парковки рядом с точкой;
GET /api/amenities/geojson — FeatureCollection для слоя на карте.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.schemas.amenity_schemas import AmenitiesResponse
from app.spatial import amenities as amenities_q

router = APIRouter(prefix="/api", tags=["amenities"])

AMENITY_PATTERN = "^(cafe|toilet|parking)$"


@router.get(
    "/amenities",
    response_model=AmenitiesResponse,
    summary="Заведения рядом с точкой (идея 28)",
)
def amenities_near(
    lat: float = Query(..., ge=-90, le=90, description="Широта точки отсчёта"),
    lon: float = Query(..., ge=-180, le=180, description="Долгота точки отсчёта"),
    radius_meters: float = Query(default=800.0, gt=0, le=10000),
    category: str | None = Query(default=None, pattern=AMENITY_PATTERN),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if radius_meters > settings.max_radius_meters:
        raise HTTPException(
            400, f"Радиус больше допустимого ({settings.max_radius_meters:.0f} м)"
        )
    items, total = amenities_q.amenities_near(
        db, lat, lon, radius_meters, category=category, limit=limit, offset=offset
    )
    return {
        "items": items,
        "total": total,
        "radius_meters": radius_meters,
        "category": category,
        "latitude": lat,
        "longitude": lon,
    }


@router.get(
    "/amenities/geojson",
    summary="Слой заведений на карте (идея 28)",
)
def amenities_geojson(
    category: str | None = Query(default=None, pattern=AMENITY_PATTERN),
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
) -> dict:
    return amenities_q.amenities_geojson(db, category=category, limit=limit)
