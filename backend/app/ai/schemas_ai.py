"""Валидация структурированного запроса, полученного от LLM (§25).

Модель обязана быть валидной до попадания в SQL: неизвестные категории,
внеопределённые координаты и радиусы отсекаются здесь, а не в БД.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# синонимы, которые модель может выдать вместо кодов категорий (§6)
CATEGORY_SYNONYMS: dict[str, str] = {
    "музей": "museum", "музеи": "museum", "museums": "museum", "museum": "museum",
    "памятник": "monument", "памятники": "monument", "monuments": "monument",
    "monument": "monument", "скульптура": "monument",
    "мемориал": "memorial", "мемориалы": "memorial", "мемориальная доска": "memorial",
    "memorials": "memorial", "memorial": "memorial",
    "галерея": "gallery", "галереи": "gallery", "gallery": "gallery", "galleries": "gallery",
}

MAX_RADIUS_M = 10000.0
MAX_LIMIT = 100


class StructuredQuery(BaseModel):
    """Описание того, что хочет пользователь — в терминах API (§22–§24)."""

    # "other" — запрос вне схемы (погода, переводы, свободный вопрос):
    # модель признаёт, что не может ответить, вместо выдуманного результата
    action: Literal["list", "nearby", "radius_search", "search",
                    "statistics", "recommend", "other"] = "list"
    categories: list[str] = Field(default_factory=list)
    text: str | None = Field(default=None, description="Текстовый запрос для поиска")
    theme: str | None = None
    keyword: str | None = None
    place: str | None = Field(default=None, description="Именованное место (§26)")
    latitude: float | None = None
    longitude: float | None = None
    radius_meters: float | None = None
    limit: int = 20
    has_website: bool | None = None
    has_description: bool | None = None
    within_district: bool = False

    @field_validator("categories", mode="before")
    @classmethod
    def _norm_categories(cls, v: list[str]) -> list[str]:
        # промпт велит ставить null в ненужные поля, поэтому null -> []
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("categories должен быть списком")
        out: list[str] = []
        for raw in v:
            key = str(raw).strip().lower()
            code = CATEGORY_SYNONYMS.get(key)
            if code is None:
                raise ValueError(
                    f"неизвестная категория '{raw}' (допустимы: museum, monument, "
                    f"memorial, gallery либо их русские названия)"
                )
            if code not in out:
                out.append(code)
        return out

    @field_validator("within_district", "has_website", "has_description", mode="before")
    @classmethod
    def _boolish(cls, v):
        """Нормализация bool-полей: LLM нередко шлёт null/строку/0-1.

        null -> False (для within_district) либо None (для has_*), строки
        «true»/«false» и 0/1 -> bool. Не распознанное -> ValueError (→ retry, §24).
        """
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, str):
            key = v.strip().lower()
            if key in ("true", "да", "yes", "1"):
                return True
            if key in ("false", "нет", "no", "0", ""):
                return False
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        raise ValueError(f"ожидалось boolean, получено {v!r}")

    @field_validator("within_district", mode="before")
    @classmethod
    def _within_default(cls, v):
        return False if v is None else v

    @model_validator(mode="after")
    def _radius_default(self) -> "StructuredQuery":
        """LLM часто выбирает radius_search, забывая радиус: берём разумный
        дефолт 1000 м вместо ошибки (значение всё равно валидируется выше)."""
        if self.action == "radius_search" and self.radius_meters is None:
            self.radius_meters = 1000.0
        if self.action == "nearby" and self.radius_meters is None:
            self.radius_meters = 1000.0
        return self

    @field_validator("latitude")
    @classmethod
    def _lat(cls, v: float | None) -> float | None:
        if v is not None and not (-90 <= v <= 90):
            raise ValueError("latitude вне диапазона [-90, 90]")
        return v

    @field_validator("longitude")
    @classmethod
    def _lon(cls, v: float | None) -> float | None:
        if v is not None and not (-180 <= v <= 180):
            raise ValueError("longitude вне диапазона [-180, 180]")
        return v

    @field_validator("radius_meters")
    @classmethod
    def _radius(cls, v: float | None) -> float | None:
        if v is not None and not (0 < v <= MAX_RADIUS_M):
            raise ValueError(f"radius_meters должен быть в (0, {MAX_RADIUS_M:.0f}]")
        return v

    @field_validator("limit")
    @classmethod
    def _limit(cls, v: int) -> int:
        if not (1 <= v <= MAX_LIMIT):
            raise ValueError(f"limit должен быть в [1, {MAX_LIMIT}]")
        return v

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            v = v.strip() or None
            if v is not None and len(v) < 2:
                raise ValueError("text слишком короткий (минимум 2 символа)")
        return v
