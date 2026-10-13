"""AI-эндпоинты: NL-запрос (§22–§25, §31) и рекомендации (§28–§32).

Архитектурные ограничения ТЗ:
  * LLM не имеет доступа к SQL и к схеме БД (§23) — на выходе только JSON;
  * результат проходит Pydantic-валидацию до каких-либо запросов (§25);
  * имя места разрешается в координаты через собственную БД (§26);
  * ранжирование рекомендаций считает SQL (pgvector + PostGIS, §45).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.client import AIServiceUnavailable as LLMError
from app.ai.client import check_available as available
from app.ai.parser import AIQueryError, parse_with_fallback
from app.ai.recommend import recommend as run_recommend
from app.ai.resolver import PlaceNotFound, resolve_place
from app.ai.rules import parse_rules
from app.ai.schemas_ai import StructuredQuery
from app.config import Settings, get_settings
from app.constants import CATEGORY_LABELS
from app.database import get_db
from app.schemas.ai_schemas import (
    AIQueryRequest,
    AIQueryResponse,
    RecommendRequest,
    RecommendResponse,
    RecommendedItem,
)
from app.spatial import queries as q

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ai"])


def _ensure_ai(settings: Settings) -> None:
    """AI отключён конфигурацией → 503. Недоступность сервера проверяется
    позже: запрос уйдёт в rule-based fallback (§31, §53) вместо отказа."""
    if not settings.ai_enabled:
        raise HTTPException(503, "AI-функции отключены (AI_ENABLED=false)")


def _labels(codes: list[str]) -> str:
    return ", ".join(CATEGORY_LABELS.get(c, c) for c in codes) or "все категории"


def _place_from_text(text: str) -> str | None:
    """Достаёт название места из текста запроса для spatial-компонента (§26, §30).

    Использует тот же шаблонный разбор, что и rule-based fallback (§53):
    «недалеко от Невского проспекта» -> «Невского проспекта». Падежные формы
    разруливает сам resolver через пересечение стемов слов.
    """
    from app.ai.rules import _find_place

    return _find_place(text)


def _resolve_target(
    db: Session, settings: Settings, sq: StructuredQuery, req_lat: float | None, req_lon: float | None
) -> tuple[float | None, float | None, dict | None]:
    """Определяет точку отсчёта: место (§26) либо координаты пользователя."""
    if sq.place:
        try:
            place = resolve_place(db, settings, sq.place)
        except PlaceNotFound as exc:
            raise HTTPException(422, str(exc)) from exc
        return place["latitude"], place["longitude"], place
    return req_lat, req_lon, None


@router.post(
    "/query",
    response_model=AIQueryResponse,
    summary="Запрос на естественном языке (§22–§25, §31)",
)
def ai_query(
    payload: AIQueryRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    _ensure_ai(settings)

    fallback_used = False
    if not available(settings):
        # сервер недоступен или модель не загружена — не тратим таймаут LLM
        logger.warning("LLM недоступен, пробуем rule-based разбор (§53)")
        sq = parse_rules(payload.query, payload.latitude, payload.longitude)
        if sq is None:
            raise HTTPException(
                503,
                "AI inference server недоступен, а запрос не распознан шаблонами. "
                "Попробуйте переформулировать или повторите позже",
            )
        fallback_used = True
    else:
        try:
            sq = parse_with_fallback(settings, payload.query, payload.latitude, payload.longitude)
        except LLMError as exc:
            # сервер отвалился во время запроса — пробуем шаблоны (§31, §53)
            logger.warning("LLM недоступен во время запроса, пробуем rule-based: %s", exc)
            sq = parse_rules(payload.query, payload.latitude, payload.longitude)
            if sq is None:
                raise HTTPException(
                    503,
                    f"AI inference server недоступен, а запрос не распознан шаблонами: {exc}",
                ) from exc
            fallback_used = True
        except AIQueryError as exc:
            raise HTTPException(422, f"Запрос не прошёл валидацию (§25): {exc}") from exc

    limit = min(sq.limit, settings.max_limit)
    categories = sq.categories or None

    # помечаем, что запрос разобран шаблонами, а не моделью (§53)
    fallback_note = " (запрос разобран шаблонами: AI-модель недоступна, §53)" if fallback_used else ""

    # запрос вне зоны компетенции сервиса: внятный отказ вместо пустого списка
    if sq.action == "other":
        return {
            "action": "other",
            "explanation": (
                "Я умею отвечать только на вопросы о местах сервиса: список и поиск "
                "объектов по категориям и названию, что находится рядом и в радиусе "
                "от точки, статистика по базе и рекомендации, что посмотреть. "
                "Спросите, например: «музеи рядом с Дворцовой площадью» или "
                "«что посмотреть недалеко от Невского проспекта»"
            ) + fallback_note,
            "structured": sq.model_dump(),
            "resolved_place": None,
            "total": 0,
            "items": [],
            "statistics": None,
        }

    if sq.action == "statistics":
        return {
            "action": "statistics",
            "explanation": "Запрошена статистика по объектам сервиса" + fallback_note,
            "structured": sq.model_dump(),
            "resolved_place": None,
            "total": 1,
            "items": [],
            "statistics": q.statistics(db),
        }

    if sq.action == "recommend":
        lat, lon, place = _resolve_target(db, settings, sq, payload.latitude, payload.longitude)
        result = run_recommend(
            db, settings,
            query_text=sq.text or payload.query,
            categories=categories,
            latitude=lat, longitude=lon,
            radius_meters=sq.radius_meters,
            limit=limit,
        )
        target = f" около «{place['label']}»" if place else ""
        return {
            "action": "recommend",
            "explanation": f"Подобрано по гибридному скорингу (семантика + пространство + "
                           f"категория){target}" + fallback_note,
            "structured": sq.model_dump(),
            "resolved_place": place,
            "total": len(result["items"]),
            "items": result["items"],
            "statistics": None,
        }

    # --- прочие действия: нужны координаты для пространственных -------------
    lat = sq.latitude if sq.latitude is not None else payload.latitude
    lon = sq.longitude if sq.longitude is not None else payload.longitude
    place_info = None
    if sq.place:
        lat, lon, place_info = _resolve_target(db, settings, sq, lat, lon)

    if sq.action == "list":
        items, total = q.list_objects(
            db, categories=categories, theme=sq.theme, keyword=sq.keyword,
            has_website=sq.has_website, has_description=sq.has_description,
            within_district=sq.within_district, limit=limit, lat=lat, lon=lon,
        )
        explanation = f"Список объектов: {_labels(sq.categories)}"
        if sq.within_district:
            explanation += ", только Центральный район"

    elif sq.action == "search":
        if not sq.text:
            raise HTTPException(422, "Для поиска не задан текст (text)")
        items, total = q.text_search(
            db, sq.text, categories=categories, limit=limit, lat=lat, lon=lon
        )
        explanation = f"Полнотекстовый поиск по запросу «{sq.text}»"

    elif sq.action in ("nearby", "radius_search"):
        if lat is None or lon is None:
            raise HTTPException(
                422,
                "Не задана точка отсчёта: укажите место («рядом с Дворцовой площадью») "
                "или передайте координаты пользователя",
            )
        radius = sq.radius_meters or (1000.0 if sq.action == "nearby" else None)
        if radius is None:
            raise HTTPException(422, "Для поиска в радиусе не задан radius_meters")
        if radius > settings.max_radius_meters:
            raise HTTPException(400, f"Радиус больше допустимого ({settings.max_radius_meters:.0f} м)")
        items, total = q.radius_objects(
            db, lat, lon, radius, categories=categories, limit=limit,
        )
        where_label = f"«{place_info['label']}»" if place_info else "заданной точки"
        explanation = (f"Объекты в радиусе {radius:.0f} м от {where_label}"
                       f" ({_labels(sq.categories)})")
    else:  # pragma: no cover - Literal защищает от неизвестных действий
        raise HTTPException(422, f"Неизвестное действие: {sq.action}")

    explanation += fallback_note
    return {
        "action": sq.action,
        "explanation": explanation,
        "structured": sq.model_dump(),
        "resolved_place": place_info,
        "total": total,
        "items": items,
        "statistics": None,
    }


@router.post(
    "/recommend",
    response_model=RecommendResponse,
    summary="Рекомендации: семантика + пространство + категория (§28–§30)",
)
def recommend(
    payload: RecommendRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    unknown = [c for c in (payload.categories or []) if c not in CATEGORY_LABELS]
    if unknown:
        raise HTTPException(400, f"Неизвестные категории: {', '.join(unknown)}")

    # Семантика требует AI; без текстового запроса рекомендации считаются
    # spatial + category и должны работать даже при отключённом AI (§53)
    if payload.query and not settings.ai_enabled:
        raise HTTPException(503, "AI-функции отключены (AI_ENABLED=false)")
    if payload.query and not available(settings):
        raise HTTPException(503, "AI inference server недоступен для семантики")

    # место из текста («недалеко от Невского проспекта») -> центр для spatial (§26).
    # Явные координаты пользователя имеют приоритет.
    lat, lon, place = payload.latitude, payload.longitude, None
    if payload.query and (lat is None or lon is None):
        name = _place_from_text(payload.query)
        if name:
            try:
                place = resolve_place(db, settings, name)
                lat, lon = place["latitude"], place["longitude"]
            except PlaceNotFound:
                logger.info("Место из запроса рекомендаций не найдено: %s", name)

    result = run_recommend(
        db, settings,
        query_text=payload.query,
        categories=payload.categories,
        latitude=lat,
        longitude=lon,
        radius_meters=payload.radius_meters,
        limit=min(payload.limit, settings.max_limit),
    )
    weights = {
        "semantic": result["weights"]["sem"] if result["components"]["semantic"] else 0.0,
        "spatial": result["weights"]["spatial"] if result["components"]["spatial"] else 0.0,
        "category": result["weights"]["category"],
    }
    parts = []
    if result["components"]["semantic"]:
        parts.append("семантическая близость к запросу")
    if result["components"]["spatial"]:
        parts.append("пространственная близость")
    if result["components"]["category"]:
        parts.append("соответствие категориям")
    target = f" около «{place['label']}»" if place else ""
    return {
        "items": [RecommendedItem(**i) for i in result["items"]],
        "total": len(result["items"]),
        "weights": weights,
        "explanation": "Ранжирование по: " + (", ".join(parts) or "базовый порядок") + target,
    }
