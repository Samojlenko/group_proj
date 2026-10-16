"""AI-эндпоинты: NL-запрос (§22–§25, §31) и рекомендации (§28–§32).

Архитектурные ограничения ТЗ:
  * LLM не имеет доступа к SQL и к схеме БД (§23) — на выходе только JSON;
  * результат проходит Pydantic-валидацию до каких-либо запросов (§25);
  * имя места разрешается в координаты через собственную БД (§26);
  * ранжирование рекомендаций считает SQL (pgvector + PostGIS, §45).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai import sessions
from app.ai.client import AIServiceUnavailable as LLMError
from app.ai.client import check_available as available
from app.ai.hours import is_open_now
from app.ai.parser import AIQueryError, parse_with_fallback
from app.ai.recommend import recommend as run_recommend
from app.ai.resolver import PlaceNotFound, resolve_place
from app.ai.rules import detect_refinement, is_deictic, merge_refinement, parse_rules
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
from app.spatial.routes import WALK_SPEED_M_PER_MIN, build_walk_route

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


def _prefer_map(payload: AIQueryRequest) -> bool:
    """Человек явно говорит о точке, которую сам поставил на карте.

    Такая точка важнее геолокации: спросив «что здесь?» или «от этой точки»,
    он имеет в виду место, куда ткнул мышкой, а не своё физическое положение.
    """
    return payload.map_latitude is not None and is_deictic(payload.query)


def _not_keyword(query: str | None, not_keyword: str | None) -> str | None:
    """Скрытые подтипы (мемориальные доски) не должны попадать в выдачу AI —
    иначе «Найдено: 310», а на карте подсвечено 117.

    Исключение: человек явно спросил про доски («покажи мемориальные доски») —
    тогда фильтр снимаем, он должен их увидеть.
    """
    if query and "доск" in query.lower():
        return None
    return not_keyword


def _origin(payload: AIQueryRequest, sq: StructuredQuery) -> tuple[float | None, float | None]:
    """Точка отсчёта запроса — с учётом того, что человек указал точку на карте."""
    sq_lat, sq_lon = sq.latitude, sq.longitude
    if _prefer_map(payload) and sq_lat is not None and sq_lon is not None:
        # модель могла просто повторить GPS («рядом с этой точкой» она иногда
        # трактует как «рядом со мной») — тогда координаты не ее выбор, а эхо:
        # человек имел в виду точку на карте, она и должна победить
        if (
            payload.latitude is not None
            and payload.longitude is not None
            and abs(sq_lat - payload.latitude) < 1e-6
            and abs(sq_lon - payload.longitude) < 1e-6
        ):
            sq_lat, sq_lon = None, None
    return _pick_coords(
        sq_lat, sq_lon,
        payload.latitude, payload.longitude,
        payload.map_latitude, payload.map_longitude,
        prefer_map=_prefer_map(payload),
    )


def _origin_label(payload: AIQueryRequest, lat: float | None, lon: float | None) -> str:
    """Человеку важно видеть, от какой точки посчитано расстояние.

    Сравниваем с допуском: бэкенд и фронт приходят с разной точностью,
    поэтому «это та же точка» проверяется приближённо, а не по `==`.
    """
    def same(a: float | None, b: float | None) -> bool:
        return a is not None and b is not None and abs(a - b) < 1e-6

    if same(lat, payload.map_latitude) and same(lon, payload.map_longitude):
        return "точки на карте"
    if same(lat, payload.latitude) and same(lon, payload.longitude):
        return "вашего местоположения"
    return "заданной точки"


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


def _continuation_note(offset: int, got: int, total: int, cap: int) -> str:
    """Пометка к объяснению при «покажи ещё»: продолжение, лимит или конец."""
    if offset <= 0:
        return ""
    if got:
        return f" (продолжение, показаны с №{offset + 1})"
    if total > offset:
        return (f" — показать разом можно не больше {cap} объектов: "
                "уточните запрос")
    return " — больше объектов нет"


# единственное число для сравнений: «оба музей» читалось бы плохо,
# поэтому в предложении используем «музей/памятник/...» с учётом рода
_CATEGORY_SINGULAR = {
    "museum": "музей", "monument": "памятник",
    "memorial": "мемориал", "gallery": "галерея",
}


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма числа: 1 остановка, 2 остановки, 5 остановок."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _fmt_distance(meters: float) -> str:
    """«1,2 км» / «350 м» — человеку читается лучше, чем «1234.5»."""
    if meters >= 1000:
        return f"{meters / 1000:.1f} км".replace(".", ",")
    return f"{meters:.0f} м"


def _resolve_pair(db: Session, settings: Settings, sq: StructuredQuery) -> tuple[dict, dict]:
    """Два места для distance/compare: оба обязательны и должны найтись (§26)."""
    if not sq.place or not sq.place2:
        raise HTTPException(
            422,
            "Нужны два места — например: «сколько от Эрмитажа до Русского музея»",
        )
    resolved = []
    for name in (sq.place, sq.place2):
        try:
            resolved.append(resolve_place(db, settings, name))
        except PlaceNotFound as exc:
            logger.info("Место пары не разрешилось: %s (%s)", name, exc)
            raise HTTPException(422, f"«{name}» — {PLACE_NOT_FOUND_HINT}") from exc
    return resolved[0], resolved[1]


def _compare_text(a: dict, b: dict, meters: float) -> str:
    """Человеческое сравнение двух объектов по данным карточек."""

    def entry(o: dict) -> str:
        bits = [_CATEGORY_SINGULAR.get(o["category"], o["category"])]
        if o.get("year_established"):
            bits.append(f"{o['year_established']} г.")
        if o.get("is_free"):
            bits.append("вход свободный")
        elif o.get("ticket_price"):
            bits.append(f"билет: {o['ticket_price']}")
        return f"«{o['name']}» ({', '.join(bits)})"

    parts = [f"{entry(a)} против {entry(b)}."]
    ya, yb = a.get("year_established"), b.get("year_established")
    if ya and yb and ya != yb:
        older, newer = (a, b) if ya < yb else (b, a)
        years = abs(ya - yb)
        parts.append(
            f"«{older['name']}» старше на {years} "
            f"{_plural(years, 'год', 'года', 'лет')}."
        )
    if a.get("address") and b.get("address"):
        parts.append(f"Адреса: {a['address']}; {b['address']}.")
    walk_min = max(1, round(meters / WALK_SPEED_M_PER_MIN))
    parts.append(f"Между ними — {_fmt_distance(meters)} (пешком ≈{walk_min} мин).")
    return " ".join(parts)


def _apply_open_now(items: list[dict], total: int, cap: int) -> tuple[list[dict], str]:
    """Фильтр «открыто сейчас»: часы — текст, поэтому разбор идёт в Python.

    Объекты без распознаваемых часов в выдачу не попадают (лучше промолчать,
    чем показать закрытый музей), но в пояснении они честно посчитаны.
    """
    opened: list[dict] = []
    unknown = 0
    for it in items:
        state = is_open_now(it.get("opening_hours") or it.get("working_hours"))
        if state is True:
            opened.append(it)
        elif state is None:
            unknown += 1
    note = f"; открыто сейчас: {len(opened)} из {total} проверенных"
    if total >= cap:
        note += f" (проверены первые {cap})"
    if unknown:
        note += f", без распознанных часов: {unknown}"
    return opened, note


def _execute(
    db: Session,
    settings: Settings,
    payload: AIQueryRequest,
    sq: StructuredQuery,
    *,
    offset: int = 0,
    source_query: str | None = None,
    fallback_note: str = "",
) -> dict:
    """Исполняет валидированный разбор: SQL-запросы и человеческое объяснение.

    ``offset`` — «покажи ещё»: сколько объектов этой ветки уже показано;
    ``source_query`` — исходный текст запроса: уточнение меняет разбор, но
    семантику рекомендаций логичнее считать от исходной формулировки.
    """
    limit = min(sq.limit, settings.max_limit)
    categories = sq.categories or None

    # скрытые подтипы (мемориальные доски) отсекаются так же, как на карте (§8)
    not_kw = _not_keyword(payload.query, payload.not_keyword)

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
            "statistics": q.statistics(db, not_keyword=not_kw),
        }

    if sq.action == "recommend":
        user_lat, user_lon = _origin(payload, sq)
        lat, lon, place = _resolve_target(db, settings, sq, user_lat, user_lon)
        result = run_recommend(
            db, settings,
            query_text=sq.text or source_query or payload.query,
            categories=categories,
            latitude=lat, longitude=lon,
            radius_meters=sq.radius_meters,
            limit=min(offset + limit, settings.max_limit),
            not_keyword=not_kw,
        )
        target = f" около «{place['label']}»" if place else ""
        explanation = (
            "Подобрано по гибридному скорингу (семантика + пространство + "
            f"категория){target}"
        )
        items = result["items"]
        if sq.open_now:
            opened, hours_note = _apply_open_now(items, len(items), settings.max_limit)
            items = opened[offset:]
            explanation += hours_note
        else:
            items = items[offset:]
        explanation += _continuation_note(
            offset, len(items), len(items), settings.max_limit
        ) + fallback_note
        return {
            "action": "recommend",
            "explanation": explanation,
            "structured": sq.model_dump(),
            "resolved_place": place,
            "total": len(items),
            "items": items,
            "statistics": None,
            "offset": offset,
        }

    # --- интенты этапа B: расстояние, сравнение, прогулка, аналитика --------

    if sq.action in ("distance", "compare"):
        a, b = _resolve_pair(db, settings, sq)
        meters = q.distance_between_points(
            db, a["latitude"], a["longitude"], b["latitude"], b["longitude"]
        )
        pair_objs = [
            obj for info in (a, b)
            if info.get("object_id")
            and (obj := q.get_object(db, info["object_id"])) is not None
        ]
        if sq.action == "distance":
            walk_min = max(1, round(meters / WALK_SPEED_M_PER_MIN))
            explanation = (
                f"От «{a['label']}» до «{b['label']}» — {_fmt_distance(meters)} "
                f"по прямой (пешком примерно {walk_min} мин)"
            )
            items = pair_objs
        else:
            if len(pair_objs) != 2:
                # одна из пар — улица или площадь: сравнивать карточки нельзя
                missing = sq.place if not a.get("object_id") else sq.place2
                raise HTTPException(
                    422,
                    f"«{missing}» — место на карте, а не объект сервиса: сравнивать "
                    "можно музеи, памятники, мемориалы и галереи",
                )
            explanation = _compare_text(pair_objs[0], pair_objs[1], meters)
            items = pair_objs
        explanation += fallback_note
        return {
            "action": sq.action,
            "explanation": explanation,
            "structured": sq.model_dump(),
            "resolved_place": a,
            "total": len(items),
            "items": items,
            "statistics": None,
            "offset": 0,
        }

    if sq.action == "walk":
        lat, lon = _origin(payload, sq)
        place_info = None
        if sq.place:
            lat, lon, place_info = _resolve_target(db, settings, sq, lat, lon)
        if lat is None or lon is None:
            raise HTTPException(422, NO_ORIGIN_HINT)
        route = build_walk_route(
            db, lat, lon,
            radius_m=sq.radius_meters or 1500.0,
            count=sq.count or 6,
            categories=categories,
            not_keyword=not_kw,
        )
        by_id = q.objects_by_ids(db, [stop["id"] for stop in route["items"]])
        items = []
        for stop in route["items"]:
            obj = by_id.get(stop["id"])
            if obj:
                # в списке рядом с названием — сколько уже пройдено от старта
                items.append({**obj, "distance_meters": stop["cumulative_meters"]})
        where_label = (
            f"«{place_info['label']}»" if place_info else _origin_label(payload, lat, lon)
        )
        if items:
            stops = len(items)
            explanation = (
                f"Прогулка от {where_label}: {stops} "
                f"{_plural(stops, 'остановка', 'остановки', 'остановок')}, "
                f"{_fmt_distance(route['total_meters'])}, "
                f"примерно {route['walk_minutes']} мин пешком"
            )
            if sq.duration_minutes:
                fits = route["walk_minutes"] <= sq.duration_minutes
                explanation += (
                    f" — в {sq.duration_minutes} мин укладывается" if fits
                    else f" — за {sq.duration_minutes} мин не успеть"
                )
        else:
            explanation = (
                f"От {where_label} в радиусе {route['radius_meters']:.0f} м "
                "не нашлось объектов для прогулки — попробуйте увеличить радиус"
            )
        explanation += fallback_note
        return {
            "action": "walk",
            "explanation": explanation,
            "structured": sq.model_dump(),
            "resolved_place": place_info,
            "total": len(items),
            "items": items,
            "statistics": None,
            "offset": 0,
            # маршрут целиком: остановки, полилиния и метры — рисуется на карте
            "route": route,
        }

    if sq.action == "analytics":
        metric = sq.metric or "oldest"
        objs = q.objects_by_year(
            db, categories=categories, not_keyword=not_kw,
            newest=(metric == "newest"), limit=min(limit, 10),
        )
        if objs:
            head = "Самый старый" if metric == "oldest" else "Самый новый"
            first = objs[0]
            explanation = (
                f"{head} по году основания ({_labels(sq.categories).lower()}) — "
                f"«{first['name']}» ({first['year_established']} г.)"
            )
            rest = ", ".join(
                f"«{o['name']}» ({o['year_established']})" for o in objs[1:4]
            )
            if rest:
                explanation += f"; далее: {rest}"
        else:
            explanation = (
                "В базе нет данных о годах основания для этого запроса — "
                "уточните категорию, например «самый старый музей»"
            )
        explanation += fallback_note
        return {
            "action": "analytics",
            "explanation": explanation,
            "structured": sq.model_dump(),
            "resolved_place": None,
            "total": len(objs),
            "items": objs,
            "statistics": None,
            "offset": 0,
        }

    # --- прочие действия: нужны координаты для пространственных -------------
    lat, lon = _origin(payload, sq)
    place_info = None
    if sq.place:
        lat, lon, place_info = _resolve_target(db, settings, sq, lat, lon)

    if sq.action == "list":
        # при «открыто сейчас» нужен запас строк: часы разбираются в Python,
        # часть объектов отсеется — страница всё равно должна быть полной
        items, total = q.list_objects(
            db, categories=categories, theme=sq.theme, keyword=sq.keyword,
            has_website=sq.has_website, has_description=sq.has_description,
            within_district=sq.within_district,
            limit=settings.max_limit if sq.open_now else limit,
            offset=0 if sq.open_now else offset,
            lat=lat, lon=lon, not_keyword=not_kw,
        )
        explanation = f"Список объектов: {_labels(sq.categories)}"
        if sq.within_district:
            explanation += ", только Центральный район"
        if sq.open_now:
            opened, hours_note = _apply_open_now(items, total, settings.max_limit)
            items = opened[offset:offset + limit]
            total = len(opened)
            explanation += hours_note

    elif sq.action == "search":
        if not sq.text:
            # схема это уже чинит (search без текста -> nearby/list), до сюда
            # доходим только в бесперспективном случае
            logger.warning("action=search без text: %s", sq.model_dump())
            raise HTTPException(422, PARSE_FAILED_HINT)
        # у text_search пока нет параметра offset — добираем и срезаем сами
        fetch_limit = (
            settings.max_limit if sq.open_now
            else min(offset + limit, settings.max_limit)
        )
        items, total = q.text_search(
            db, sq.text, categories=categories, not_keyword=not_kw,
            limit=fetch_limit, lat=lat, lon=lon,
        )
        explanation = f"Полнотекстовый поиск по запросу «{sq.text}»"
        if sq.open_now:
            opened, hours_note = _apply_open_now(items, total, settings.max_limit)
            items = opened[offset:offset + limit]
            total = len(opened)
            explanation += hours_note
        else:
            items = items[offset:]

    elif sq.action in ("nearby", "radius_search"):
        if lat is None or lon is None:
            raise HTTPException(422, NO_ORIGIN_HINT)
        radius = sq.radius_meters or (1000.0 if sq.action == "nearby" else None)
        if radius is None:
            raise HTTPException(422, "Для поиска в радиусе не задан radius_meters")
        if radius > settings.max_radius_meters:
            raise HTTPException(400, f"Радиус больше допустимого ({settings.max_radius_meters:.0f} м)")
        items, total, _in_radius = q.radius_objects(
            db, lat, lon, radius, categories=categories,
            limit=settings.max_limit if sq.open_now else limit,
            offset=0 if sq.open_now else offset,
            not_keyword=not_kw,
        )
        # человек должен видеть, откуда посчитано: «точки на карте» — это
        # та точка, которую он поставил, а не его геолокация
        where_label = (
            f"«{place_info['label']}»" if place_info else _origin_label(payload, lat, lon)
        )
        explanation = (f"Объекты в радиусе {radius:.0f} м от {where_label}"
                       f" ({_labels(sq.categories)})")
        if sq.open_now:
            opened, hours_note = _apply_open_now(items, total, settings.max_limit)
            items = opened[offset:offset + limit]
            total = len(opened)
            explanation += hours_note
    else:  # pragma: no cover - Literal защищает от неизвестных действий
        raise HTTPException(422, f"Неизвестное действие: {sq.action}")

    explanation += _continuation_note(offset, len(items), total, settings.max_limit)
    explanation += fallback_note
    return {
        "action": sq.action,
        "explanation": explanation,
        "structured": sq.model_dump(),
        "resolved_place": place_info,
        "total": total,
        "items": items,
        "statistics": None,
        "offset": offset,
    }


def _run_refinement(
    db: Session,
    settings: Settings,
    payload: AIQueryRequest,
    session: sessions.Session,
    last: sessions.Turn,
    refinement: dict,
) -> dict:
    """Быстрый путь уточнения: патч на прошлый разбор, без обращения к LLM."""
    kind = refinement["kind"]

    if kind == "nth":
        # «покажи 2-й» — адресуемся к списку прошлой выдачи
        idx = refinement["n"] - 1
        if idx >= len(last.item_ids):
            raise HTTPException(
                422,
                f"В прошлом ответе было {len(last.item_ids)} объектов — "
                f"№{refinement['n']} не существует. Спросите заново",
            )
        obj = q.get_object(db, last.item_ids[idx])
        if obj is None:
            raise HTTPException(404, "Объект из прошлого ответа больше не доступен")
        return {
            "action": last.action,
            "explanation": (
                f"Объект №{refinement['n']} из прошлого ответа: «{obj['name']}»"
            ),
            "structured": last.structured,
            "resolved_place": None,
            "total": 1,
            "items": [obj],
            "statistics": None,
        }

    merged = merge_refinement(last.structured, refinement)
    try:
        sq = StructuredQuery.model_validate(merged)
    except ValidationError as exc:
        logger.warning("Уточнение не прошло валидацию: %s (%s)", merged, exc)
        raise HTTPException(422, PARSE_FAILED_HINT) from exc

    offset = 0
    if kind == "more":
        offset = last.shown
        if refinement.get("count"):
            sq = sq.model_copy(update={"limit": refinement["count"]})

    result = _execute(db, settings, payload, sq, offset=offset,
                      source_query=last.query)

    if result["action"] != "other":
        if kind == "more":
            # продолжаем ту же ветку: список объектов растёт, смещение — тоже
            sessions.store.remember(session, sessions.Turn(
                query=last.query,
                structured=last.structured,
                action=result["action"],
                item_ids=last.item_ids + [i["id"] for i in result["items"]],
                total=result["total"],
                shown=offset + len(result["items"]),
            ))
        else:
            sessions.store.remember(session, sessions.Turn(
                query=last.query,
                structured=sq.model_dump(),
                action=result["action"],
                item_ids=[i["id"] for i in result["items"]],
                total=result["total"],
                shown=len(result["items"]),
            ))
    return result


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

    session = sessions.store.get_or_create(payload.session_id)
    last = session.last

    # Уточнение к прошлому ответу («а теперь только музеи», «покажи ещё»):
    # разбирается правилами мгновенно и не зависит от доступности модели (§40).
    refinement = detect_refinement(payload.query) if last is not None else None
    if refinement is not None:
        result = _run_refinement(db, settings, payload, session, last, refinement)
        result["session_id"] = session.id
        return result

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
        # Контекст диалога (§40): только когда это уже не первый обмен — модель
        # использует его для уточняющих фраз, самостоятельные разбирает как есть.
        if last is not None:
            map_ctx["context"] = json.dumps(
                {"query": last.query, "parsed": last.structured}, ensure_ascii=False
            )
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

    # помечаем, что запрос разобран шаблонами, а не моделью (§53)
    fallback_note = " (запрос разобран шаблонами: AI-модель недоступна, §53)" if fallback_used else ""

    result = _execute(db, settings, payload, sq,
                      source_query=payload.query, fallback_note=fallback_note)

    # Помним состоявшийся обмен: без этого «а теперь только музеи» и «покажи
    # ещё» не к чему прицепить (§40).
    if result["action"] != "other":
        sessions.store.remember(session, sessions.Turn(
            query=payload.query,
            structured=sq.model_dump(),
            action=result["action"],
            item_ids=[i["id"] for i in result["items"]],
            total=result["total"],
            shown=len(result["items"]),
        ))

    result["session_id"] = session.id
    return result


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
    # пользователя. Дальше — точка с карты, если человек о ней и говорит,
    # иначе координаты пользователя.
    lat, lon = _pick_coords(
        None, None, payload.latitude, payload.longitude,
        payload.map_latitude, payload.map_longitude,
        prefer_map=_prefer_map(payload),
    )
    place = None
    # «этой точкой» — не название места: не шлём фиктивный запрос к резолверу,
    # а берём координаты карты, которые уже подобраны выше
    if payload.query and not _prefer_map(payload):
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
        not_keyword=_not_keyword(payload.query, payload.not_keyword),
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
