"""Схемы OSM-слоя заведений (идея 28, этап H)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AmenityItem(BaseModel):
    """Заведение из OSM: кафе, туалет или парковка."""

    id: int
    name: str
    category: str = Field(description="cafe | toilet | parking")
    address: str | None = None
    opening_hours: str | None = None
    latitude: float
    longitude: float
    distance_meters: float | None = Field(
        default=None, description="Метры от точки отсчёта (считает PostGIS)"
    )


class AmenitiesResponse(BaseModel):
    """Заведения в радиусе (GET /api/amenities)."""

    items: list[AmenityItem]
    total: int = Field(description="Сколько заведений попало в радиус (без учёта страницы)")
    radius_meters: float
    category: str | None = Field(default=None, description="Фильтр категории, если задан")
    latitude: float
    longitude: float
