"""Схемы ответов API (Pydantic v2)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """Ответ GET /api/health (§17)."""

    status: str = Field(description="ok | degraded")
    database: str = Field(description="ok | error")
    postgis_version: str | None = None
    pgvector: bool | None = None
    objects_count: int | None = None
    districts_count: int | None = None
    ai_enabled: bool
    ai_available: bool | None = Field(
        default=None, description="Доступен ли AI inference server (503, если нет)"
    )
    ai_model: str | None = None
    version: str = "1.0.0"


class CategoryInfo(BaseModel):
    code: str
    label: str


class ObjectBase(ORMModel):
    id: int
    name: str
    category: str
    description: str | None = None
    short_description: str | None = None
    long_description: str | None = None
    address: str | None = None
    latitude: float
    longitude: float
    website: str | None = None
    phone: str | None = None
    opening_hours: str | None = None
    working_hours: str | None = None
    image_url: str | None = None
    themes: list[str] | None = None
    art_types: list[str] | None = None
    keywords: list[str] | None = None
    metro_stations: list[str] | None = None
    year_established: int | None = None
    is_free: bool | None = None
    ticket_price: str | None = None
    source: str
    source_id: str | None = None


class ObjectWithDistance(ObjectBase):
    distance_meters: float | None = None
    distance_km: float | None = None


class DistanceResponse(BaseModel):
    id_from: int
    id_to: int
    distance_meters: float
    distance_km: float


class StatisticsResponse(BaseModel):
    total: int
    by_category: dict[str, int]
    with_website: int
    with_description: int
    bbox: dict[str, float] | None = None
    updated_at: datetime | None = None
