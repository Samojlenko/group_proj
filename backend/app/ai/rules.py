"""Rule-based разбор запросов — fallback при недоступной LLM (§31, §53).

Требование ТЗ: «Система должна продолжать работать в обычном GIS-режиме даже
при отключенном AI». Модель может быть не загружена или отвечать медленно, но
типовые запросы (категории, радиус, координаты, место) распознаются и без неё.

Никаких вычислений расстояний здесь нет: модуль только возвращает структуру,
которую валидирует Pydantic (§25), а геометрию считает PostGIS (§45).
"""

from __future__ import annotations

import re

from app.ai.schemas_ai import StructuredQuery

# коды категорий → словоформы, встречающиеся в запросах (§6)
_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "museum": ("музе", "музе́й", "museum"),
    "monument": ("памятник", "monument"),
    "memorial": ("мемориал", "мемориальн", "memorial"),
    "gallery": ("галере", "gallery"),
}

_RADIUS_RE = re.compile(
    r"(?:в\s+радиусе|в\s+пределах|не\s+дальше\s+чем\s+на|до)\s+"
    r"(\d+(?:[.,]\d+)?)\s*(км|километр\w*|м|метр\w*|метра\w*)",
    re.IGNORECASE,
)
_RADIUS_KM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(км|километр\w*)", re.IGNORECASE)
# координаты Центрального района СПб: 59.xx, 30.xx (обе в одном запросе)
_COORDS_RE = re.compile(r"(-?\d{1,3}[.,]\d+)\s*[,;]\s*(-?\d{1,3}[.,]\d+)")
_LIMIT_RE = re.compile(r"(?:первые|ближайшие|топ|найди|покажи)\s+(\d{1,3})\b", re.IGNORECASE)

_NEARBY_RE = re.compile(
    r"рядом\s+с|возле|вокруг|около|недалеко\s+от|ближе\s+всего\s+к|"
    r"ближайш\w*|по\s+близости\s+от|рядом|по\s+близости",
    re.IGNORECASE,
)
# «пересечение Пестеля и Моховой», «пересечению улиц Пестеля и Моховой», «угол A и B»
_INTERSECTION_RE = re.compile(
    r"(?:пересечени\w*|угл\w*)\s+(?:улиц\w*\s+)?(.+)$", re.IGNORECASE
)
_PAIR_SPLIT_RE = re.compile(r"\s+и\s+", re.IGNORECASE)
_RECOMMEND_RE = re.compile(
    r"рекоменд|посовет|подбер|что\s+посмотр|что\s+интересн|хочу\s+посмотр|по\s+совету",
    re.IGNORECASE,
)
_STAT_RE = re.compile(r"сколько\s+(?:всего\s+)?(объект|музе|памятник)|статистик|распределени|категори", re.IGNORECASE)
_SEARCH_RE = re.compile(r"^\s*(?:найди|поищи|где|выдай|ищи)\b", re.IGNORECASE)

# предлоги, после которых начинается название места
_PLACE_RE = re.compile(
    r"(?:рядом\s+с|возле|вокруг|около|недалеко\s+от|ближе\s+всего\s+к|от|к|по)\s+"
    r"(?:того\s+|этого\s+)?(.+?)[?!.]*\s*$",
    re.IGNORECASE,
)

# русские словоформы → начало названия категории; также служебные слова,
# которые не должны попадать в place/text
_STOP_WORDS = frozenset(
    """и или в во на над под от до для с со у к ко по за между через что где
    какой какая какие который которая этот эта это здесь там мой моя моё меня
    этой этого тут не мне нас наш хочу можно пожалуйста
    точке точка точки пункте центре центр""".split()
)


def _to_number(raw: str) -> float:
    return float(raw.replace(",", "."))


def _find_categories(text: str) -> list[str]:
    low = text.lower()
    found = [code for code, words in _CATEGORY_WORDS.items() if any(w in low for w in words)]
    # «мемориальная доска» — не мемориал как категория, но и не категория «мемориал»
    # здесь не трогаем: доски относятся к memorial и фильтруются отдельно (§8)
    return found


def _find_radius(text: str) -> float | None:
    m = _RADIUS_RE.search(text)
    if not m:
        return None
    value = _to_number(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("км") or unit.startswith("километр"):
        value *= 1000
    return value


def _find_coords(text: str) -> tuple[float, float] | None:
    m = _COORDS_RE.search(text)
    if not m:
        return None
    lat, lon = _to_number(m.group(1)), _to_number(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    # координаты могли быть переставлены местами
    if -90 <= lon <= 90 and -180 <= lat <= 180:
        return lon, lat
    return None


def _find_place(text: str) -> str | None:
    """Достаёт название места после предлога («рядом с Эрмитажем» → «Эрмитаж»)."""
    m = _PLACE_RE.search(text)
    if not m:
        return None
    place = m.group(1).strip().strip("«»\"'(),.")
    if not place or len(place) < 3:
        return None
    words = [w for w in re.split(r"\s+", place.lower()) if w not in _STOP_WORDS]
    if not words:
        return None
    return place


# «здесь», «эта точка», «выбранная точка» — точка отсчёта пришла с карты
_DEICTIC_RE = re.compile(
    r"здесь|тут|отсюда|туда|сюда|"
    r"эта\s+точк\w*|этой\s+точк\w*|эту\s+точк\w*|этой\s+метк\w*|"
    r"у\s+точки|от\s+точки|у\s+метки|моей\s+точке|поставленн\w*\s+точк\w*|"
    r"выбранн\w*\s+точк\w*|выбранн\w*\s+мест\w*|указанн\w*\s+точк\w*|"
    r"центре\s+карты|по\s+центру\s+карты|той\s+точке",
    re.IGNORECASE,
)


def is_deictic(text: str) -> bool:
    """Пользователь ссылается на точку, которую сам поставил на карте.

    Нужно API (app/api/ai.py): при таком запросе точка с карты важнее
    геолокации браузера — иначе «что здесь?» всегда считается от GPS-координат,
    а не от того места, куда человек ткнул мышкой.
    """
    return bool(_DEICTIC_RE.search(text or ""))


def _find_intersection(text: str) -> str | None:
    """«пересечению улиц Пестеля и Моховой» → «пересечение Пестеля и Моховой».

    Канонический префикс «пересечение» нужен резолверу: он вычисляет точку по
    геометрии двух улиц в PostGIS (§45), а не ищет её по имени.
    """
    m = _INTERSECTION_RE.search(text)
    if not m:
        return None
    body = m.group(1).strip().strip("«»\"'(),.?!")
    parts = [p.strip(" «»\"'(),.?!") for p in _PAIR_SPLIT_RE.split(body)]
    parts = [p for p in parts if len(p) >= 3]
    if len(parts) != 2:
        return None
    return f"пересечение {parts[0]} и {parts[1]}"


def _find_limit(text: str) -> int | None:
    m = _LIMIT_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 100 else None


def parse_rules(query: str, user_lat: float | None = None,
                user_lon: float | None = None,
                map_lat: float | None = None,
                map_lon: float | None = None) -> StructuredQuery | None:
    """Простые шаблоны русского языка → StructuredQuery.

    Возвращает None, если запрос не распознан (тогда вызывающая сторона
    отдаёт контролируемую ошибку 422 с просьбой уточнить, §31).

    ``map_lat/map_lon`` — точка, выбранная пользователем на карте: она
    используется для указательных форм («здесь», «эта точка»).
    """
    text = query.strip()
    categories = _find_categories(text)
    radius = _find_radius(text)
    limit = _find_limit(text) or 20
    coords = _find_coords(text)
    deictic = is_deictic(text)

    base: dict = {"limit": limit}

    # «здесь»/«эта точка» — карта важнее геолокации: пользователь явно ткнул
    if deictic and map_lat is not None and map_lon is not None:
        coords = (map_lat, map_lon)

    # статистика
    if _STAT_RE.search(text) and not _RECOMMEND_RE.search(text):
        base.update({"action": "statistics", "categories": categories or [],
                     "within_district": bool(_find_categories(text)) and "район" in text.lower()})
        return _validate(base)

    # рекомендации
    if _RECOMMEND_RE.search(text):
        place = _find_place(text)
        base.update({
            "action": "recommend",
            "categories": categories or [],
            "text": _strip_boilerplate(text),
            "place": place,
            "radius_meters": radius,
        })
        if coords:
            base.update({"latitude": coords[0], "longitude": coords[1], "place": None})
        return _validate(base)

    # координаты пользователя имеют приоритет над «эта точка»
    if coords:
        base.update({"latitude": coords[0], "longitude": coords[1]})

    # пересечение улиц: место, которое считает PostGIS по геометрии (§45)
    intersection = _find_intersection(text)
    if intersection:
        base.update({
            "action": "radius_search" if radius else "nearby",
            "categories": categories or [],
            "place": intersection,
            "radius_meters": radius or 1500.0,
        })
        return _validate(base)

    # радиусный поиск (само наличие радиуса — достаточный признак)
    if radius is not None:
        place = _find_place(text)
        base.update({
            "action": "radius_search",
            "categories": categories or [],
            "radius_meters": radius,
            "place": None if coords else place,
        })
        return _validate(base)

    # поиск рядом с местом / точкой
    if _NEARBY_RE.search(text):
        place = _find_place(text)
        base.update({
            "action": "nearby",
            "categories": categories or [],
            "radius_meters": radius or 1000.0,
            "place": None if coords else place,
        })
        if not place and not coords:
            # нет точки отсчёта — просим уточнить (§31), но возвращаем структуру
            base["place"] = None
        return _validate(base)

    # просто перечисление категорий («музеи», «покажи галереи»)
    if categories and not _SEARCH_RE.search(text):
        base.update({"action": "list", "categories": categories})
        return _validate(base)

    # «найди X» → полнотекстовый поиск
    stripped = _SEARCH_RE.sub("", text).strip()
    if stripped and len(stripped) >= 2:
        base.update({"action": "search", "text": stripped,
                     "categories": categories or []})
        return _validate(base)

    return None


def _strip_boilerplate(text: str) -> str:
    """Убирает служебные слова из текста для семантического поиска."""
    words = [w for w in re.split(r"\s+", text) if w.lower() not in _STOP_WORDS]
    out = " ".join(words)
    return out[:200]


def _validate(data: dict) -> StructuredQuery | None:
    """Та же Pydantic-валидация, что и у ответа LLM (§25)."""
    try:
        return StructuredQuery.model_validate(data)
    except Exception:  # noqa: BLE001 - любой отказ валидации → не распознано
        return None
