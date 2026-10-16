"""Схемы AI-эндпоинтов (§22, §31, §32)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import ObjectWithDistance
from app.schemas.route_schemas import RouteResponse


class AIQueryRequest(BaseModel):
    """Запрос на естественном языке (§22)."""

    query: str = Field(min_length=2, max_length=500, description="Текст пользователя")
    latitude: float | None = Field(default=None, ge=-90, le=90,
                                    description="Координаты пользователя")
    longitude: float | None = Field(default=None, ge=-180, le=180)
    map_latitude: float | None = Field(
        default=None, ge=-90, le=90,
        description="Точка, выбранная пользователем на карте (центр радиуса)",
    )
    map_longitude: float | None = Field(default=None, ge=-180, le=180)
    session_id: str | None = Field(default=None, description="Идентификатор диалога (§40)")
    favorite_ids: list[int] | None = Field(
        default=None,
        max_length=200,
        description="Избранное пользователя (идея 25): по нему строится профиль вкуса "
        "для action=taste — усредняются эмбеддинги избранных объектов",
    )
    not_keyword: str | None = Field(
        default=None,
        max_length=100,
        description="Исключить объекты с этим ключевым словом (доски скрыты в UI по умолчанию)",
    )


class AIItem(ObjectWithDistance):
    """Позиция выдачи AI-помощника.

    Дополнительно к карточке объекта рекомендации несут балл гибридного
    скоринга и вклад факторов («почему этот объект», идеи 16–17).
    """

    score: float | None = Field(default=None, description="Балл гибридного ранжирования (§28)")
    factors: dict[str, float] | None = Field(
        default=None,
        description="Вклад факторов в балл: semantic / spatial / category (идея 17)",
    )


class CollectionInfo(BaseModel):
    """Сохранённая подборка (идея 26): имя и размер — фронт хранит её сам."""

    name: str = Field(description="Человеческое имя: «Топ-7 музеев»")
    count: int = Field(description="Сколько объектов в подборке")


class QuestPoint(BaseModel):
    """Точка квеста (идея 27): вопрос и подсказка из описания объекта."""

    id: int
    name: str = Field(description="Ответ квеста — название объекта")
    category: str = Field(default="museum", description="Категория объекта (цвет метки)")
    question: str = Field(description="Вопрос, сгенерированный из описания")
    hint: str = Field(default="", description="Подсказка — первое предложение описания")
    latitude: float
    longitude: float
    leg_meters: float = Field(description="Перегон от предыдущей точки")
    cumulative_meters: float = Field(description="Накопленные метры от старта")


class QuestResponse(BaseModel):
    """Квест: маршрут из N точек с вопросами (идея 27)."""

    theme: str | None = Field(default=None, description="Тема квеста («история»)")
    count: int
    total_meters: float
    walk_minutes: int
    points: list[QuestPoint]


class AIQueryResponse(BaseModel):
    """Результат разбора и исполнения запроса (§24, §31)."""

    action: str = Field(description="Разобранное действие")
    explanation: str = Field(description="Как сервис понял запрос (по-русски)")
    structured: dict = Field(description="Валидированный структурированный запрос (§25)")
    resolved_place: dict | None = Field(
        default=None, description="Куда разрешилось имя места (§26)"
    )
    total: int
    items: list[AIItem]
    statistics: dict | None = None
    offset: int = Field(
        default=0,
        description="Сколько объектов было пропущено до этой страницы («покажи ещё», §40)",
    )
    session_id: str | None = Field(
        default=None,
        description="Идентификатор диалога (§40): отправьте его со следующим запросом, "
        "чтобы работали уточнения («а теперь только музеи», «покажи ещё»)",
    )
    route: RouteResponse | None = Field(
        default=None,
        description="Пеший маршрут (action=walk): остановки, полилиния, метры и время",
    )
    collection: CollectionInfo | None = Field(
        default=None, description="Подборка (action=collection): имя и размер (идея 26)"
    )
    quest: QuestResponse | None = Field(
        default=None, description="Квест (action=quest): точки, вопросы, подсказки (идея 27)"
    )


class UnrecognizedItem(BaseModel):
    """Одна запись журнала «не понял» (идея 23)."""

    time: str = Field(description="Момент записи (UTC, ISO)")
    kind: str = Field(description="Повод: other / fallback / unparsed")
    kind_label: str = Field(default="", description="Человекочитаемый повод")
    query: str = Field(description="Исходный запрос пользователя")
    detail: str = Field(default="", description="Детали (ошибка схемы и т.п.)")


class UnrecognizedResponse(BaseModel):
    """Журнал запросов, которые помощник не понял (идея 23)."""

    total: int = Field(description="Сколько записей в журнале (в памяти процесса)")
    counts: dict[str, int] = Field(description="Записи по поводам")
    items: list[UnrecognizedItem]


class RecommendRequest(BaseModel):
    """Параметры рекомендации (§30)."""

    query: str | None = Field(default=None, max_length=500, description="Пожелания текстом")
    categories: list[str] | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    map_latitude: float | None = Field(default=None, ge=-90, le=90,
                                        description="Точка, выбранная на карте")
    map_longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: float | None = Field(default=None, gt=0, le=10000)
    limit: int = Field(default=10, ge=1, le=100)
    not_keyword: str | None = Field(
        default=None,
        max_length=100,
        description="Исключить объекты с этим ключевым словом (доски скрыты в UI по умолчанию)",
    )


class RecommendedItem(ObjectWithDistance):
    score: float | None = Field(default=None, description="Итоговый балл гибридного ранжирования (§28)")
    factors: dict[str, float] | None = Field(
        default=None,
        description="Вклад факторов в балл: semantic / spatial / category (идея 17)",
    )


class RecommendResponse(BaseModel):
    items: list[RecommendedItem]
    total: int
    weights: dict[str, float] = Field(description="Использованные веса (§28)")
    explanation: str
