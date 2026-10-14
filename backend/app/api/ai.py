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
from app.ai.rules import is_deictic, parse_rules
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

# Пользователю — только человеческий текст: технические детали разбора
# (Pydantic, номера параграфов) уходят в лог, а не в интерфейс (§44).
PARSE_FAILED_HINT = (
    "Не получилось разобрать запрос. Попробуйте переформулировать — например: "
    "«музеи рядом с Эрмитажем», «галереи в радиусе 1 км от Невского проспекта», "
    "«что посмотреть недалеко от Дворцовой площади»"
)
PLACE_NOT_FOUND_HINT = (
    "Не удалось найти такое место на карте. Назовите улицу, площадь или объект — "
    "например «Невский проспект», «Дворцовая площадь», «Эрмитаж» — либо кликните "
    "по карте и спросите «что рядом с этой точкой»"
)
NO_ORIGIN_HINT = (
    "Не понятно, от какой точки считать. Укажите место («рядом с Эрмитажем») "
    "или кликните по карте — тогда появится центр поиска"
)


def _pick_coords(
    sq_lat: float | None, sq_lon: float | None,
    req_lat: float | None, req_lon: float | None,
    map_lat: float | None, map_lon: float | None,
    prefer_map: bool = False,
) -> tuple[float | None, float | None]:
    """Точка отсчёта для пространственного запроса.

    Пара берётся целиком: смешивать широту одного источника с долготой другого
    нельзя — получится точка в другом полушарии.

    Порядок:

    1. координаты, которые модель явно поставила в запросе (или извлекли шаблоны);
    2. далее — точка с карты, если человек о ней и говорит («что здесь?»,
       «от этой точки», «у метки»): именно её он и имел в виду, даже когда
       браузер уже отдал геолокацию;
    3. геолокация пользователя;
    4. точка с карты как последний вариант — лучше считать от неё, чем
       отвечать «не понятно, от какой точки считать».
    """
    tail = [(map_lat, map_lon), (req_lat, req_lon)] if prefer_map else [
        (req_lat, req_lon), (map_lat, map_lon),
    ]
    for lat, lon in [(sq_lat, sq_lon), *tail]:
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


def _origin(payload: AIQueryRequest, sq: StructuredQuery) -> tuple[float | None, float | None]:
    """Точка отсчёта запроса — с учётом того, что человек указал точку на карте."""
    return _pick_coords(
        sq.latitude, sq.longitude,
        payload.latitude, payload.longitude,
        payload.map_latitude, payload.map_longitude,
        prefer_map=payload.map_latitude is not None and is_deictic(payload.query),
    )


def _parse_rules(payload: AIQueryRequest):
    """Rule-based fallback (§53): карта участвует наравне с геолокацией."""
    return parse_rules(
        payload.query, payload.latitude, payload.longitude,
        payload.map_latitude, payload.map_longitude,
    )


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
            logger.info("Место не разрешилось: %s (%s)", sq.place, exc)
            raise HTTPException(422, f"«{sq.place}» — {PLACE_NOT_FOUND_HINT}") from exc
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
        sq = _parse_rules(payload)
        if sq is None:
            raise HTTPException(
                503,
                "AI inference server недоступен, а запрос не распознан шаблонами. "
                "Попробуйте переформулировать или повторите позже",
            )
        fallback_used = True
    else:
        # координаты точки на карте («что рядом с этой точкой») — дополнительный
        # контекст для модели; в запрос они попадают только при наличии (§22)
        map_ctx: dict = {}
        if payload.map_latitude is not None and payload.map_longitude is not None:
            map_ctx = {"map_lat": payload.map_latitude, "map_lon": payload.map_longitude}
        try:
            sq = parse_with_fallback(
                settings, payload.query, payload.latitude, payload.longitude, **map_ctx
            )
        except LLMError as exc:
            # сервер отвалился во время запроса — пробуем шаблоны (§31, §53)
            logger.warning("LLM недоступен во время запроса, пробуем rule-based: %s", exc)
            sq = _parse_rules(payload)
            if sq is None:
                raise HTTPException(
                    503,
                    "AI inference server недоступен, а запрос не распознан шаблонами: "
                    "попробуйте переформулировать или повторите позже",
                ) from exc
            fallback_used = True
        except AIQueryError as exc:
            # детали (какая именно модель ошиблась) — в лог, пользователю — подсказка
            logger.warning("Разбор запроса не удался: %s", exc)
            raise HTTPException(422, PARSE_FAILED_HINT) from exc

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
        user_lat, user_lon = _pick_coords(
            None, None, payload.latitude, payload.longitude,
            payload.map_latitude, payload.map_longitude,
        )
        lat, lon, place = _resolve_target(db, settings, sq, user_lat, user_lon)
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
    lat, lon = _pick_coords(
        sq.latitude, sq.longitude,
        payload.latitude, payload.longitude,
        payload.map_latitude, payload.map_longitude,
    )
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
            # схема это уже чинит (search без текста -> nearby/list), до сюда
            # доходим только в бесперспективном случае
            logger.warning("action=search без text: %s", sq.model_dump())
            raise HTTPException(422, PARSE_FAILED_HINT)
        items, total = q.text_search(
            db, sq.text, categories=categories, limit=limit, lat=lat, lon=lon
        )
        explanation = f"Полнотекстовый поиск по запросу «{sq.text}»"

    elif sq.action in ("nearby", "radius_search"):
        if lat is None or lon is None:
            raise HTTPException(422, NO_ORIGIN_HINT)
        radius = sq.radius_meters or (1000.0 if sq.action == "nearby" else None)
        if radius is None:
            raise HTTPException(422, "Для поиска в радиусе не задан radius_meters")
        if radius > settings.max_radius_meters:
            raise HTTPException(400, f"Радиус больше допустимого ({settings.max_radius_meters:.0f} м)")
        items, total, _in_radius = q.radius_objects(
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

    # Точка отсчёта: место из текста («недалеко от Невского проспекта») важнее
    # геолокации — иначе при включённом GPS запрос про Невский отработает от
    # пользователя. Дальше — координаты пользователя, затем точка на карте.
    lat, lon = _pick_coords(
        None, None, payload.latitude, payload.longitude,
        payload.map_latitude, payload.map_longitude,
    )
    place = None
    if payload.query:
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
