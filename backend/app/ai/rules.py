"""Rule-based разбор запросов — fallback при недоступной LLM (§31, §53).

Требование ТЗ: «Система должна продолжать работать в обычном GIS-режиме даже
при отключенном AI». Модель может быть не загружена или отвечать медленно, но
типовые запросы (категории, радиус, координаты, место) распознаются и без неё.

Никаких вычислений расстояний здесь нет: модуль только возвращает структуру,
которую валидирует Pydantic (§25), а геометрию считает PostGIS (§45).
"""

from __future__ import annotations

import re

from app.ai.schemas_ai import MAX_RADIUS_M, StructuredQuery
from app.spatial.routes import WALK_SPEED_M_PER_MIN

# коды категорий → словоформы, встречающиеся в запросах (§6)
_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "museum": ("музе", "музе́й", "museum"),
    "monument": ("памятник", "monument"),
    "memorial": ("мемориал", "мемориальн", "memorial"),
    "gallery": ("галере", "gallery"),
}

_RADIUS_RE = re.compile(
    r"(?:в\s+радиусе|в\s+пределах|радиус\w*|не\s+дальше\s+чем\s+на|до)\s+"
    r"(\d+(?:[.,]\d+)?)\s*(км|километр\w*|м|метр\w*|метра\w*)",
    re.IGNORECASE,
)
_RADIUS_KM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(км|километр\w*)", re.IGNORECASE)
# «в 10 минутах ходьбы», «5 минут пешком», «10 мин шагом» — радиус через время (§14):
# N × 80 м/мин. Без слов «ходьбы/пешком/шагом» это НЕ радиус: «погулка на 30 минут».
_WALK_MINUTES_RE = re.compile(
    r"(?:в\s+|за\s+)?(\d{1,3})\s*(?:минут\w*|мин\b)[\s,]+"
    r"(?:ходьб\w+|пешком|пеш\w+|шагом|неспешн\w+\s+шагом)",
    re.IGNORECASE,
)
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

# --- интенты этапа B: расстояние, сравнение, прогулка, аналитика -----------

# «сколько идти от A до B», «расстояние между A и B», «как далеко от A до B»
_DISTANCE_RE = re.compile(
    r"расстоян\w*|как\s+далеко|(?<![а-яё])далеко\s+ли|"
    r"сколько\s+(?:идти|ехать|метров|километров|км)",
    re.IGNORECASE,
)
# «сравни A и B», «чем отличается A от B», «разница между A и B»
_COMPARE_RE = re.compile(r"сравни\w*|сравнени\w*|отлича\w*|разниц\w*", re.IGNORECASE)
# «прогулка», «погулять час», «маршрут пешком», «что успеть за два часа»
_WALK_RE = re.compile(
    r"прогулк\w*|погулк\w*|погуля\w*|пойти\s+пешком|пройтись|"
    r"маршрут\s+(?:пешком|для\s+прогулки|прогулки)|"
    r"что\s+успеть|успею\s+за",
    re.IGNORECASE,
)
# «самый старый памятник», «старейший музей»
_OLDEST_RE = re.compile(r"сам\w+\s+(?:стары|древн)\w*|старейш\w*|древнейш\w*", re.IGNORECASE)
# «самый новый памятник», «самый молодой музей»
_NEWEST_RE = re.compile(r"сам\w+\s+(?:нов|молод)\w*|новейш\w*", re.IGNORECASE)
# «открыто сейчас», «какие музеи сейчас открыты»
_OPEN_RE = re.compile(
    r"открыт\w*|работающ\w*|работает\s+сейчас|работают\s+сейчас", re.IGNORECASE
)

# --- идеи этапа G: «удиви меня», вкус, подборки, квесты ----------------------

# «удиви меня», «что-нибудь необычное», «случайное место»
_SURPRISE_RE = re.compile(
    r"удиви|порази|что-нибудь\s+(?:необычн|интересн|эффектн)|"
    r"случайн\w*\s+(?:объект|место)",
    re.IGNORECASE,
)
# «похожее на моё избранное», «в моём вкусе», «как то, что мне нравится»
# (просто «покажи избранные музеи» — не вкус: избранное фильтруется на клиенте)
_TASTE_RE = re.compile(
    r"(?:похож|подбер|подскаж|что-то|что-нибудь)[^.!?]{0,40}избранн|"
    r"мо[её]\w*\s+избранн|в\s+мо[её]м\s+вкусе|мо[её]м\s+вкус|"
    r"как\s+то,?\s*что\s+мне\s+нравится",
    re.IGNORECASE,
)
# «собери подборку», «топ-7 ... и сохрани», «сохрани коллекцию»
_COLLECTION_RE = re.compile(r"подборк|собери|сохрани|коллекци", re.IGNORECASE)
_TOP_RE = re.compile(r"топ[-\s]?(\d{1,2})", re.IGNORECASE)
# «сделай квест по истории на 5 точек», «квест»
_QUEST_RE = re.compile(r"квест", re.IGNORECASE)
_QUEST_POINTS_RE = re.compile(r"на\s+(\d{1,2})\s+точек", re.IGNORECASE)
# тема квеста: «по истории», «про архитектуру» — из неё строится название
_THEME_WORDS: tuple[tuple[str, str], ...] = (
    ("истори", "история"),
    ("архитектур", "архитектура"),
    ("литератур", "литература"),
    ("искусств", "искусство"),
)


def _find_theme(text: str) -> str | None:
    low = text.lower()
    for stem, label in _THEME_WORDS:
        if stem in low:
            return label
    return None


def _quest_place(text: str) -> str | None:
    """Место старта квеста: тема и число точек в название не попадают.

    «Сделай квест по истории на 5 точек» — это не место «истории на 5 точек»:
    убираем тему и счёт, затем ищем название как обычно.
    """
    cleaned = _QUEST_POINTS_RE.sub(" ", text)
    stems = "|".join(stem for stem, _ in _THEME_WORDS)
    cleaned = re.sub(
        rf"(?:по|про|о)\s+\S*(?:{stems})\S*", " ", cleaned, flags=re.IGNORECASE
    )
    return _find_place(cleaned)

# «от A до B», «между A и B» — пара мест для расстояния
_PAIR_UNTIL_RE = re.compile(r"\b(?:от|между)\s+(.+?)\s+(?:до|и)\s+(.+)", re.IGNORECASE)
# «сравни A и B», «чем отличается A от B»
_PAIR_COMPARE_RE = re.compile(
    r"(?:сравни\w*|сравнени\w*|отлич\w*|разниц\w*)\s+(?:между\s+)?(.+?)\s+и\s+(.+)",
    re.IGNORECASE,
)

# «на час», «30 минут», «2 часа», «полтора часа»
_DURATION_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(минут\w*|мин\b|час\w*|ч\b)", re.IGNORECASE
)
_NUMBER_WORDS = {"два": 120, "две": 120, "три": 180, "четыре": 240}


def _clean_place(raw: str) -> str | None:
    """Название места из пары: без кавычек и хвостовой пунктуации."""
    s = raw.strip().strip("«»\"'(),.!?;:").strip()
    return s if len(s) >= 3 else None


def _find_pair(text: str) -> tuple[str, str] | None:
    """Два места из фразы: «от A до B», «между A и B», «сравни A и B»."""
    for pattern in (_PAIR_UNTIL_RE, _PAIR_COMPARE_RE):
        m = pattern.search(text)
        if m:
            a, b = _clean_place(m.group(1)), _clean_place(m.group(2))
            if a and b and a.lower() != b.lower():
                return a, b
    return None


def _find_duration(text: str) -> int | None:
    """Время прогулки в минутах: «на час», «30 минут», «полтора часа», «два часа»."""
    low = text.lower().replace("ё", "е")
    if "полтора часа" in low:
        return 90
    if "полчаса" in low or "пол часа" in low:
        return 30
    m = _DURATION_RE.search(low)
    if m:
        value = _to_number(m.group(1))
        unit = m.group(2)
        minutes = value * 60 if unit.startswith(("час", "ч")) else value
        return min(max(round(minutes), 5), 240)
    m = re.search(r"(два|две|три|четыре)\s+часа", low)
    if m:
        minutes = _NUMBER_WORDS[m.group(1)]
        if "с половиной" in m.group(0):
            minutes += 30
        return min(minutes, 240)
    if re.search(r"(?<![а-яё])час(?:а|ов)?(?![а-яё])", low):
        return 60
    return None

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
    if m:
        value = _to_number(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("км") or unit.startswith("километр"):
            value *= 1000
        return value
    # «в 10 минутах ходьбы» → 10 × 80 = 800 м (§14)
    m = _WALK_MINUTES_RE.search(text)
    if m:
        minutes = int(m.group(1))
        value = minutes * WALK_SPEED_M_PER_MIN
        if 1 <= minutes and value <= MAX_RADIUS_M:
            return value
    return None


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
    # «открыто сейчас» — общий фильтр к пространственным и списковым веткам
    open_now = True if _OPEN_RE.search(text) else None

    # «здесь»/«эта точка» — карта важнее геолокации: пользователь явно ткнул
    if deictic and map_lat is not None and map_lon is not None:
        coords = (map_lat, map_lon)

    # статистика
    if _STAT_RE.search(text) and not _RECOMMEND_RE.search(text):
        base.update({"action": "statistics", "categories": categories or [],
                     "within_district": bool(_find_categories(text)) and "район" in text.lower()})
        return _validate(base)

    # расстояние между двумя местами («сколько от Эрмитажа до Русского музея»)
    if _DISTANCE_RE.search(text):
        pair = _find_pair(text)
        if pair:
            base.update({"action": "distance", "place": pair[0], "place2": pair[1],
                         "categories": categories or []})
            return _validate(base)

    # сравнение двух объектов («сравни Эрмитаж и Русский музей»)
    if _COMPARE_RE.search(text):
        pair = _find_pair(text)
        if pair:
            base.update({"action": "compare", "place": pair[0], "place2": pair[1],
                         "categories": categories or []})
            return _validate(base)

    # прогулка («погулять час от Дворцовой площади», «что успеть за два часа»)
    if _WALK_RE.search(text):
        base.update({
            "action": "walk",
            "categories": categories or [],
            "place": _find_place(text),
            "radius_meters": radius,
            "duration_minutes": _find_duration(text),
        })
        if coords:
            base.update({"latitude": coords[0], "longitude": coords[1], "place": None})
        return _validate(base)

    # аналитика: «самый старый/новый ...» по году основания
    if _OLDEST_RE.search(text) or _NEWEST_RE.search(text):
        metric = "newest" if _NEWEST_RE.search(text) and not _OLDEST_RE.search(text) else "oldest"
        base.update({"action": "analytics", "metric": metric,
                     "categories": categories or [], "limit": min(limit, 10)})
        return _validate(base)

    # --- идеи этапа G: «удиви меня», вкус, подборки, квесты ---------------
    # (проверяются до recommend: «собери подборку» важнее общего «подбери»)

    # «удиви меня» — случайный объект с эффектной подачей
    if _SURPRISE_RE.search(text):
        base.update({"action": "surprise", "categories": categories or []})
        return _validate(base)

    # «похожее на избранное» — профиль вкуса по эмбеддингам избранного
    if _TASTE_RE.search(text):
        base.update({"action": "taste", "categories": categories or []})
        return _validate(base)

    # «сделай квест по истории на 5 точек»
    if _QUEST_RE.search(text):
        points = _QUEST_POINTS_RE.search(text)
        base.update({
            "action": "quest",
            "categories": categories or [],
            "count": int(points.group(1)) if points else None,
            "radius_meters": radius,
            "place": _quest_place(text),
            "theme": _find_theme(text),
        })
        if coords:
            base.update({"latitude": coords[0], "longitude": coords[1], "place": None})
        return _validate(base)

    # «собери топ-7 музеев и сохрани»
    if _COLLECTION_RE.search(text):
        top = _TOP_RE.search(text)
        base.update({
            "action": "collection",
            "categories": categories or [],
            "count": int(top.group(1)) if top else None,
        })
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
            "open_now": open_now,
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
            "open_now": open_now,
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
            "open_now": open_now,
        })
        if not place and not coords:
            # нет точки отсчёта — просим уточнить (§31), но возвращаем структуру
            base["place"] = None
        return _validate(base)

    # просто перечисление категорий («музеи», «покажи галереи»)
    if categories and not _SEARCH_RE.search(text):
        base.update({"action": "list", "categories": categories, "open_now": open_now})
        return _validate(base)

    # «найди X» → полнотекстовый поиск
    stripped = _SEARCH_RE.sub("", text).strip()
    if stripped and len(stripped) >= 2:
        base.update({"action": "search", "text": stripped,
                     "categories": categories or [], "open_now": open_now})
        return _validate(base)

    return None


def _strip_boilerplate(text: str) -> str:
    """Убирает служебные слова из текста для семантического поиска."""
    words = [w for w in re.split(r"\s+", text) if w.lower() not in _STOP_WORDS]
    out = " ".join(words)
    return out[:200]


# --- уточнения к прошлому ответу (сессии диалога, §40) ------------------------

# «ещё», «дальше», «продолжи», «ещё 5» — продолжить прошлую выдачу
_MORE_RE = re.compile(
    r"^\s*(?:а\s+|ну\s+|давай\s+|и\s+|покажи\s+|дай\s+)*"
    r"(?:ещё|еще|дальше|продолж\w*|следующ\w+)\s*"
    r"(\d{1,3})?\s*[?!.]*\s*$",
    re.IGNORECASE,
)

# порядковые: «покажи 2-й», «второй объект», «про третий», просто «3»
_ORDINALS = {
    "перв": 1, "втор": 2, "трет": 3, "четвёрт": 4, "четверт": 4,
    "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
}
_NTH_RE = re.compile(
    r"^\s*(?:а\s+)?(?:покажи\s+|открой\s+|дай\s+|выбери\s+|про\s+)?"
    r"(?:(\d{1,2})(?:-?(?:й|ый|ой|го|му))?"
    r"|(перв|втор|трет|четвёрт|четверт|пят|шест|седьм|восьм|девят|десят)\w*)"
    r"\s*(?:объект\w*|мест\w*|точк\w*|его|её|ее|из\s+них)?\s*[?!.]*\s*$",
    re.IGNORECASE,
)

# слова, которые могут сопровождать уточнение и не несут нового смысла
_REFINE_FILLERS = frozenset(
    """а и ну да давай только теперь ещё еще же мне пожалуйста покажи оставь
    объект объекты объектов место места из них их все всё хочу к""".split()
)


def _categories_only(text: str) -> list[str] | None:
    """«а теперь только музеи» → ["museum"]; иначе None (это не уточнение)."""
    cats = _find_categories(text)
    if not cats:
        return None
    t = text.lower()
    for words in _CATEGORY_WORDS.values():
        for w in sorted(words, key=len, reverse=True):
            t = re.sub(rf"{re.escape(w)}\w*", " ", t)
    # «доски» — уточнение к мемориалам, а не новое слово запроса
    t = re.sub(r"доск\w*", " ", t)
    rest = [w.strip("?!.,«»\"'()") for w in t.split()]
    rest = [w for w in rest if w]
    if all(w in _REFINE_FILLERS for w in rest):
        return cats
    return None


def _find_nth(text: str) -> int | None:
    """«покажи 2-й», «второй», «3» → номер объекта из прошлого ответа."""
    t = text.strip().lower()
    if len(t.split()) > 4:
        return None
    m = _NTH_RE.match(t)
    if not m:
        return None
    if m.group(1):
        n = int(m.group(1))
    else:
        n = _ORDINALS.get(m.group(2) or "", 0)
    return n if 1 <= n <= 50 else None


def _radius_only(text: str) -> float | None:
    """«в радиусе 500 метров» → 500.0; запрос с местом/категорией — не уточнение."""
    radius = _find_radius(text)
    if radius is None:
        return None
    if _NEARBY_RE.search(text) or _find_place(text) or _find_categories(text):
        return None
    if len(text.split()) > 6:
        return None
    return radius


# «а теперь открытые», «только открытые сейчас» — фильтр к прошлой выдаче
_OPEN_ONLY_RE = re.compile(
    r"^\s*(?:а\s+)?(?:теперь\s+|только\s+|оставь\s+)?"
    r"(?:открыт\w*(?:\s+сейчас)?|сейчас\s+открыт\w*|работающ\w+)\s*[?!.]*\s*$",
    re.IGNORECASE,
)


def detect_refinement(text: str) -> dict | None:
    """Уточнение к прошлому ответу или None, если запрос самостоятельный.

    Вызывается только при непустой сессии: без прошлого разбора «только
    музеи» и «ещё» — обычные запросы, их разбирает общий конвейер.
    """
    t = (text or "").strip()
    if not t:
        return None

    m = _MORE_RE.match(t)
    if m:
        count = int(m.group(1)) if m.group(1) else None
        if count is not None and not 1 <= count <= 100:
            count = None
        return {"kind": "more", "count": count}

    n = _find_nth(t)
    if n:
        return {"kind": "nth", "n": n}

    cats = _categories_only(t)
    if cats:
        return {"kind": "categories", "categories": cats}

    # «покажи доски» без слова «мемориальные» — тоже уточнение категории
    if "доск" in t.lower():
        return {"kind": "categories", "categories": ["memorial"]}

    radius = _radius_only(t)
    if radius:
        return {"kind": "radius", "radius": radius}

    if _OPEN_ONLY_RE.match(t):
        return {"kind": "open_now"}

    return None


def merge_refinement(previous: dict, refinement: dict) -> dict:
    """Накладывает уточнение на прошлый разбор: неуказанное наследуется.

    «а теперь только музеи» после «галереи рядом с Эрмитажем» даст
    «музеи рядом с Эрмитажем» — место и радиус остаются прежними.
    """
    data = dict(previous)
    kind = refinement.get("kind")

    if kind == "categories":
        data["categories"] = refinement["categories"]
        # полнотекстовый поиск продолжает работать, но с новыми категориями
    elif kind == "radius":
        data["radius_meters"] = refinement["radius"]
        # радиус осмыслен только для пространственных действий
        if data.get("action") in ("list", "search", "recommend"):
            has_origin = bool(data.get("place")) or data.get("latitude") is not None
            data["action"] = "radius_search" if has_origin else "nearby"
    elif kind == "open_now":
        # «а теперь открытые»: фильтр часов к прошлой выдаче
        data["open_now"] = True
    # «more» и «nth» структуру не меняют: ими управляет API-слой

    return data



def _validate(data: dict) -> StructuredQuery | None:
    """Та же Pydantic-валидация, что и у ответа LLM (§25)."""
    try:
        return StructuredQuery.model_validate(data)
    except Exception:  # noqa: BLE001 - любой отказ валидации → не распознано
        return None
