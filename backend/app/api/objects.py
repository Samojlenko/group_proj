"""REST API пространственных данных (§11–§17).

Все endpoint'ы отвечают 400 при некорректных параметрах и 404, когда объект
не найден (§44). Радиус и limit ограничены конфигурацией (§42).
Пространственные вычисления выполняются только в PostGIS (§45).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.constants import ALLOWED_CATEGORIES, CATEGORY_LABELS
from app.database import get_db
from app.schemas import (
    CategoryInfo,
    DistanceResponse,
    ObjectBase,
    ObjectWithDistance,
    StatisticsResponse,
)
from app.spatial import queries as sq

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["objects"])


# --- валидаторы ---------------------------------------------------------------

def validate_coords(latitude: float, longitude: float) -> None:
    if not (-90 <= latitude <= 90):
        raise HTTPException(400, "latitude должна быть в диапазоне [-90, 90]")
    if not (-180 <= longitude <= 180):
        raise HTTPException(400, "longitude должна быть в диапазоне [-180, 180]")


def validate_categories(categories: list[str] | None) -> list[str] | None:
    if not categories:
        return None
    unknown = [c for c in categories if c not in ALLOWED_CATEGORIES]
    if unknown:
        raise HTTPException(
            400,
            f"Неизвестные категории: {', '.join(unknown)}. "
            f"Допустимы: {', '.join(sorted(ALLOWED_CATEGORIES))}",
        )
    return categories


def validate_radius(radius_meters: float, settings: Settings) -> float:
    if radius_meters <= 0:
        raise HTTPException(400, "radius_meters должен быть больше 0")
    if radius_meters > settings.max_radius_meters:
        raise HTTPException(
            400,
            f"Слишком большой радиус: максимум {settings.max_radius_meters:.0f} м (§42)",
        )
    return radius_meters


def validate_limit(limit: int, settings: Settings) -> int:
    if limit < 1:
        raise HTTPException(400, "limit должен быть >= 1")
    if limit > settings.max_limit:
        raise HTTPException(
            400, f"Слишком большой limit: максимум {settings.max_limit} (§42)"
        )
    return limit


def get_object_or_404(db: Session, object_id: int) -> dict:
    obj = sq.get_object(db, object_id)
    if obj is None:
        raise HTTPException(404, f"Объект с id={object_id} не найден")
    return obj


def _with_distance(row: dict) -> dict:
    d = row.get("distance_meters")
    if d is not None:
        row["distance_meters"] = round(d, 1)
        row["distance_km"] = round(d / 1000, 3)
    return row


# --- список, GeoJSON ----------------------------------------------------------

@router.get("/objects", response_model=dict, summary="Список объектов с фильтрами (§15, §17)")
def list_objects(
    category: list[str] | None = Query(default=None, description="Категория (можно несколько)"),
    q: str | None = Query(default=None, description="Поиск по названию/описанию"),
    theme: str | None = Query(default=None, description="Тематика"),
    keyword: str | None = Query(default=None, description="Ключевое слово"),
    not_keyword: str | None = Query(
        default=None, description="Исключить объекты с этим ключевым словом (напр., «мемориальная доска»)"
    ),
    has_website: bool | None = Query(default=None),
    has_description: bool | None = Query(default=None),
    within_district: bool = Query(default=False, description="Только внутри Центрального района (§10)"),
    latitude: float | None = Query(default=None),
    longitude: float | None = Query(default=None, description="Если заданы — сортировка по расстоянию"),
    limit: int = Query(default=50, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    cats = validate_categories(category)
    limit = validate_limit(limit, settings)
    if (latitude is None) != (longitude is None):
        raise HTTPException(400, "Укажите и latitude, и longitude вместе (или ни одного)")
    if latitude is not None and longitude is not None:
        validate_coords(latitude, longitude)
    if q is not None and len(q.strip()) < 2:
        raise HTTPException(400, "Поисковый запрос слишком короткий (минимум 2 символа)")

    items, total = sq.list_objects(
        db,
        categories=cats,
        q=q,
        theme=theme,
        keyword=keyword,
        not_keyword=not_keyword,
        has_website=has_website,
        has_description=has_description,
        within_district=within_district,
        limit=limit,
        offset=offset,
        lat=latitude,
        lon=longitude,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/objects/geojson", summary="Объекты в GeoJSON (§16)")
def objects_geojson(
    category: list[str] | None = Query(default=None),
    q: str | None = Query(default=None),
    theme: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    not_keyword: str | None = Query(default=None),
    has_website: bool | None = Query(default=None),
    has_description: bool | None = Query(default=None),
    within_district: bool = Query(default=False),
    limit: int = Query(default=5000, ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if limit > 5000:
        raise HTTPException(400, "Слишком большой limit для GeoJSON: максимум 5000")
    return sq.geojson_objects(
        db,
        categories=validate_categories(category),
        q=q,
        theme=theme,
        keyword=keyword,
        not_keyword=not_keyword,
        has_website=has_website,
        has_description=has_description,
        within_district=within_district,
        limit=limit,
    )


# --- пространственные запросы (порядок объявления важен: /nearby раньше /{id}) ---

@router.get(
    "/objects/nearby",
    response_model=list[ObjectWithDistance],
    summary="Ближайшие объекты к точке (§11.1)",
)
def nearby(
    latitude: float = Query(..., description="Широта"),
    longitude: float = Query(..., description="Долгота"),
    category: list[str] | None = Query(default=None),
    not_keyword: str | None = Query(
        default=None, description="Исключить объекты с этим ключевым словом"
    ),
    limit: int = Query(default=10, ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[dict]:
    validate_coords(latitude, longitude)
    limit = validate_limit(limit, settings)
    rows = sq.nearby_objects(
        db, latitude, longitude,
        categories=validate_categories(category),
        not_keyword=not_keyword,
        limit=limit,
    )
    return [_with_distance(r) for r in rows]


@router.get("/objects/radius", response_model=dict, summary="Поиск в радиусе (§12)")
def radius(
    latitude: float = Query(...),
    longitude: float = Query(...),
    radius_meters: float = Query(..., gt=0, description="Радиус в метрах"),
    category: list[str] | None = Query(default=None),
    not_keyword: str | None = Query(
        default=None,
        description="Исключить объекты с этим ключевым словом (напр., «мемориальная доска»)",
    ),
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Страница ``items`` ограничена ``limit`` (MAX_LIMIT, §42), но подсветка на
    карте считается по ``in_radius`` — всем объектам внутри круга, без LIMIT.
    """
    validate_coords(latitude, longitude)
    validate_radius(radius_meters, settings)
    limit = validate_limit(limit, settings)
    items, total, in_radius = sq.radius_objects(
        db,
        latitude,
        longitude,
        radius_meters,
        categories=validate_categories(category),
        not_keyword=not_keyword,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_with_distance(r) for r in items],
        "total": total,
        "in_radius": in_radius,
        "radius_meters": radius_meters,
        "center": {"latitude": latitude, "longitude": longitude},
        "limit": limit,
        "offset": offset,
    }


@router.get("/objects/search", response_model=dict, summary="Текстовый поиск (§15)")
def search(
    q: str = Query(..., min_length=2, description="Текст запроса"),
    category: list[str] | None = Query(default=None),
    not_keyword: str | None = Query(
        default=None, description="Исключить объекты с этим ключевым словом"
    ),
    latitude: float | None = Query(default=None),
    longitude: float | None = Query(default=None),
    limit: int = Query(default=20, ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if (latitude is None) != (longitude is None):
        raise HTTPException(400, "Укажите и latitude, и longitude вместе (или ни одного)")
    if latitude is not None and longitude is not None:
        validate_coords(latitude, longitude)
    rows, total = sq.text_search(
        db,
        q.strip(),
        categories=validate_categories(category),
        not_keyword=not_keyword,
        limit=validate_limit(limit, settings),
        lat=latitude,
        lon=longitude,
    )
    return {"items": rows, "total": total, "query": q}


# --- карточка объекта и её окружение ------------------------------------------

@router.get("/objects/{object_id}", response_model=ObjectBase, summary="Карточка объекта (§33)")
def get_object(object_id: int, db: Session = Depends(get_db)) -> dict:
    return get_object_or_404(db, object_id)


@router.get(
    "/objects/{object_id}/distance/{other_id}",
    response_model=DistanceResponse,
    summary="Расстояние между объектами (§13)",
)
def distance(object_id: int, other_id: int, db: Session = Depends(get_db)) -> dict:
    if object_id == other_id:
        return {"id_from": object_id, "id_to": other_id, "distance_meters": 0.0, "distance_km": 0.0}
    result = sq.distance_between(db, object_id, other_id)
    if result is None:
        raise HTTPException(404, "Один из объектов не найден")
    return result


@router.get(
    "/objects/{object_id}/nearby",
    response_model=dict,
    summary="Что рядом с объектом: список + счётчики по категориям (§14, §34)",
)
def object_nearby(
    object_id: int,
    radius_meters: float = Query(default=1000, gt=0),
    category: list[str] | None = Query(default=None),
    not_keyword: str | None = Query(
        default=None, description="Исключить объекты с этим ключевым словом"
    ),
    limit: int = Query(default=100, ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    get_object_or_404(db, object_id)
    validate_radius(radius_meters, settings)
    limit = validate_limit(limit, settings)
    items = sq.near_object(
        db, object_id, radius_meters,
        categories=validate_categories(category),
        not_keyword=not_keyword,
        limit=limit,
    )
    counts = sq.counts_near_object(db, object_id, radius_meters, not_keyword=not_keyword)
    return {
        "object_id": object_id,
        "radius_meters": radius_meters,
        "counts_by_category": counts,
        "items": [_with_distance(r) for r in items],
    }


# --- справочники и статистика ---------------------------------------------------

@router.get("/categories", response_model=list[CategoryInfo], summary="Категории объектов (§6)")
def categories(
    not_keyword: str | None = Query(default=None, description="Исключить объекты с этим ключевым словом"),
    db: Session = Depends(get_db),
) -> list[dict]:
    counts = sq.category_counts(db, not_keyword=not_keyword)
    codes = list(CATEGORY_LABELS) + [c for c in counts if c not in CATEGORY_LABELS]
    return [
        {"code": code, "label": CATEGORY_LABELS.get(code, code)}
        for code in codes
        if code in CATEGORY_LABELS or counts.get(code)
    ]


@router.get("/statistics", response_model=StatisticsResponse, summary="Статистика (§36)")
def statistics(
    not_keyword: str | None = Query(default=None, description="Исключить объекты с этим ключевым словом"),
    db: Session = Depends(get_db),
) -> dict:
    return sq.statistics(db, not_keyword=not_keyword)
