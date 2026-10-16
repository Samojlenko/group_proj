"""Схемы «Прогулки»: запрос и ответ пешего маршрута (§33)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RouteRequest(BaseModel):
    """Точка старта, радиус поиска и число остановок прогулки."""

    latitude: float = Field(ge=-90, le=90, description="Широта точки старта")
    longitude: float = Field(ge=-180, le=180, description="Долгота точки старта")
    radius_meters: float = Field(
        default=1500, gt=0, le=10000,
        description="Радиус поиска объектов вокруг точки старта, м",
    )
    count: int = Field(default=6, ge=2, le=12, description="Сколько остановок в маршруте")
    categories: list[str] | None = Field(default=None, description="Категории для отбора")
    not_keyword: str | None = Field(
        default=None, max_length=100,
        description="Исключить объекты с этим ключевым словом (скрытые доски)",
    )


class RoutePoint(BaseModel):
    latitude: float
    longitude: float


class RouteStop(BaseModel):
    """Остановка маршрута: объект и перегон до него от предыдущей точки."""

    id: int
    name: str
    category: str
    latitude: float
    longitude: float
    leg_meters: float = Field(description="Расстояние от предыдущей точки, м")
    cumulative_meters: float = Field(description="Суммарно от старта, м")
    distance_from_start_meters: float | None = None


class RouteResponse(BaseModel):
    """Готовый маршрут прогулки."""

    start: RoutePoint
    radius_meters: float
    count: int
    total_meters: float
    walk_minutes: int
    items: list[RouteStop]
    polyline: list[list[float]]
