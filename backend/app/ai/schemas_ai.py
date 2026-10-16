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
                    "statistics", "recommend", "other",
                    "distance", "compare", "walk", "analytics",
                    "surprise", "taste", "collection", "quest"] = "list"
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
    # --- интенты этапа B: расстояние, сравнение, прогулка, аналитика --------
    place2: str | None = Field(default=None, description="Второе место («от A до B»)")
    duration_minutes: int | None = Field(
        default=None, description="Сколько времени на прогулку (walk), минуты"
    )
    open_now: bool | None = Field(
        default=None, description="Оставить только объекты, открытые сейчас"
    )
    metric: Literal["oldest", "newest"] | None = Field(
        default=None, description="Метрика аналитики: самый старый / самый новый"
    )
    count: int | None = Field(default=None, description="Остановок в прогулке (1..12)")

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

    @field_validator("within_district", "has_website", "has_description", "open_now", mode="before")
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
        """Нормализация после разбора: дефолты и починка типовых ошибок LLM.

        LLM нередко выбирает action="search" без text («чем заняться с детьми
        рядом с Эрмитажем») — вместо ошибки превращаем это в осмысленный запрос:
        есть место -> nearby, есть категории -> list. Иначе — ValueError (→
        повтор, §24, и дружелюбный отказ наружу).
        """
        if self.action == "search" and not self.text:
            if self.place:
                self.action = "nearby"
            elif self.categories:
                self.action = "list"
            else:
                raise ValueError("для поиска не задан текст (text)")
        if self.action == "radius_search" and self.radius_meters is None:
            self.radius_meters = 1000.0
        if self.action == "nearby" and self.radius_meters is None:
            self.radius_meters = 1000.0
        if self.action in ("distance", "compare") and not (self.place and self.place2):
            raise ValueError("для distance/compare нужны два места: place и place2")
        if self.action == "walk":
            # радиус = сколько можно пройти за названное время (80 м/мин),
            # иначе дефолт как у кнопки «Прогулка» в интерфейсе
            if self.duration_minutes is not None and self.radius_meters is None:
                self.radius_meters = min(max(self.duration_minutes * 80.0, 600.0), 3000.0)
            elif self.radius_meters is None:
                self.radius_meters = 1500.0
            if self.count is None:
                # примерно 15 минут на остановку: короткая прогулка — меньше точек
                self.count = min(max(round((self.duration_minutes or 90) / 15), 3), 12)
        if self.action == "analytics" and self.metric is None:
            self.metric = "oldest"
        if self.action == "quest":
            # квест — короткая прогулка с вопросами: 5 точек и радиус прогулки
            if self.count is None:
                self.count = 5
            if self.radius_meters is None:
                self.radius_meters = 1500.0
        if self.action == "collection" and self.count is None:
            # «собери подборку» без числа — топ-5; с «топ-N» count приходит из разбора
            self.count = 5
        return self

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _duration(cls, v):
        """LLM иногда шлёт строку/дроби — приводим к целым минутам (5..240)."""
        if v is None:
            return None
        try:
            n = int(float(v))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"duration_minutes должно быть числом, получено {v!r}") from exc
        if n <= 0:
            raise ValueError("duration_minutes должен быть положительным")
        return min(max(n, 5), 240)

    @field_validator("count", mode="before")
    @classmethod
    def _count(cls, v):
        if v is None:
            return None
        try:
            n = int(float(v))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"count должно быть числом, получено {v!r}") from exc
        return min(max(n, 1), 12)

    @field_validator("place", "place2", "theme", "keyword", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        if isinstance(v, str):
            v = v.strip() or None
        return v

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
