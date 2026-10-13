"""Схемы AI-эндпоинтов (§22, §31, §32)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import ObjectWithDistance


class AIQueryRequest(BaseModel):
    """Запрос на естественном языке (§22)."""

    query: str = Field(min_length=2, max_length=500, description="Текст пользователя")
    latitude: float | None = Field(default=None, ge=-90, le=90,
                                    description="Координаты пользователя")
    longitude: float | None = Field(default=None, ge=-180, le=180)
    session_id: str | None = Field(default=None, description="Идентификатор диалога (§40)")


class AIQueryResponse(BaseModel):
    """Результат разбора и исполнения запроса (§24, §31)."""

    action: str = Field(description="Разобранное действие")
    explanation: str = Field(description="Как сервис понял запрос (по-русски)")
    structured: dict = Field(description="Валидированный структурированный запрос (§25)")
    resolved_place: dict | None = Field(
        default=None, description="Куда разрешилось имя места (§26)"
    )
    total: int
    items: list[ObjectWithDistance]
    statistics: dict | None = None


class RecommendRequest(BaseModel):
    """Параметры рекомендации (§30)."""

    query: str | None = Field(default=None, max_length=500, description="Пожелания текстом")
    categories: list[str] | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: float | None = Field(default=None, gt=0, le=10000)
    limit: int = Field(default=10, ge=1, le=100)


class RecommendedItem(ObjectWithDistance):
    score: float | None = Field(default=None, description="Итоговый балл гибридного ранжирования (§28)")


class RecommendResponse(BaseModel):
    items: list[RecommendedItem]
    total: int
    weights: dict[str, float] = Field(description="Использованные веса (§28)")
    explanation: str
