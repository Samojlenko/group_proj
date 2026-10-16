"""Резолвер имён мест (§26): объект -> locations -> пересечения улиц -> геокодер.

Порядок обязателен по ТЗ: сначала ищем в собственной БД, внешний геокодер
используется только как последний вариант и по умолчанию отключён (§48).

Возможности этапа D: латиница переводится в кириллицу (словарь популярных
названий + транслитерация, §13); у locations учитываются синонимы из колонки
synonyms («Русский музей» → «Михайловский дворец», §15); при полном промахе
LIKE-проходов работает триграммная схожесть pg_trgm — опечатки (§12).

Пересечения улиц считаются в PostGIS по реальной геометрии из locations:
`ST_ClosestPoint(A, B)` и `ST_ClosestPoint(B, A)` дают точку пересечения двух
линий (а если линии не пересекаются — ближайшие точки, радиус поиска всё
равно ограничен). Внешний геокодер для этого не нужен.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings

logger = logging.getLogger(__name__)


class PlaceNotFound(RuntimeError):
    """Место не удалось определить (§26)."""


# типичные русские падежные окончания — чтобы «Эрмитажем» находило «Эрмитаж»
# (прилагательные «-ая/-ой/-ые» проверяются до односложных: «Моховой» ~ «Моховая»)
_ENDINGS = (
    "иями", "ями", "ами", "ием", "ьем", "ем", "ом", "ой", "ей", "ах", "ях",
    "ов", "ев", "ин", "ый", "ий", "ая", "яя", "ые", "ие", "ое", "ее",
    "ую", "юю", "ого", "его", "ому", "ему", "ых", "их",
    "ы", "и", "а", "я", "о", "е", "у", "ю", "ь", "й",
)

# «пересечение Пестеля и Моховой», «пересечению улиц Пестеля и Моховой», «угол X и Y»
_INTERSECTION_RE = re.compile(
    r"(?:пересечени\w*|угл\w*)\s+(?:улиц\w*\s+)?(.+)$", re.IGNORECASE
)
_PAIR_SPLIT_RE = re.compile(r"\s+и\s+", re.IGNORECASE)


def _normalize(word: str) -> str:
    return word.lower().replace("ё", "е")


def _stem(word: str) -> str:
    """Грубый стем: срезает падежное окончание, оставляя основу (минимум 4 символа)."""
    w = _normalize(word)
    for end in _ENDINGS:
        if len(w) - len(end) >= 4 and w.endswith(end):
            return w[: -len(end)]
    return w


def _word_hit(a: str, b: str) -> bool:
    """Слова совпали, если совпали основы или одна — префикс другой.

    «Русского» ~ «русский» (общая основа «русск»), «музея» ~ «музей»
    (префикс «музе»): так двухсловные названия требуют совпадения всей
    фразы, а не одного слова — иначе «Русского музея» находит «Русский
    Левша» и подобных однофамильцев.
    """
    sa, sb = _stem(a), _stem(b)
    if sa == sb:
        return True
    return len(sa) >= 4 and len(sb) >= 4 and (sa.startswith(sb) or sb.startswith(sa))


def _rank(needle: str, name: str) -> int:
    """Чем меньше, тем лучше совпадение; 99 — не совпало."""
    n = _normalize(needle)
    c = _normalize(name)
    if n == c:
        return 0
    if c.startswith(n) or n.startswith(c):
        return 1
    # сравнение по словам: «Эрмитажем» ~ «Государственный Эрмитаж»
    nw, cw = n.split(), c.split()
    matched = sum(1 for w in nw if any(_word_hit(w, x) for x in cw))
    if matched:
        # совпали все слова запроса — крепкое совпадение; только часть —
        # слабое («Русского музея» и «Русский Левша»: общее лишь «русск»)
        return 2 if matched == len(nw) else 4
    if n in c or c in n:
        return 3
    return 99


def _rank_row(needle: str, row, with_synonyms: bool) -> tuple[int, str | None]:
    """Лучший ранг строки: по имени и, если есть, по синонимам (§15).

    Второй элемент — синоним, совпавший лучше имени: подпись ответа берётся
    из него («Русского музея» → «Русский музей», а не «Михайловский дворец»).
    """
    best = _rank(needle, row["name"])
    matched: str | None = None
    if with_synonyms:
        for synonym in row.get("synonyms") or []:
            rank = _rank(needle, synonym)
            if rank < best:
                best, matched = rank, synonym
    return best, matched


# порог триграммного прохода: «эрмидаж» ~ «эрмитаж» (0.455) проходит,
# мусорные слова («атлантида», «погода») остаются ниже 0.36
_TRGM_THRESHOLD = 0.42


def _trigram_lookup(db: Session, sql: str, table: str, needle: str,
                    synonyms: bool) -> tuple[dict, float] | None:
    """Запасной проход по триграммной схожести pg_trgm: опечатки (§12).

    Схожесть считается и по имени, и (для locations) по синонимам: «эрмидаж»
    находит «Государственный Эрмитаж» через синоним «Эрмитаж». Берётся лучший
    кандидат выше порога, строку целиком добирает прежний SELECT.
    """
    n = _normalize(needle)
    if synonyms:
        sim = (
            "greatest(similarity(lower(replace(name, 'ё', 'е')), :n), "
            "coalesce((SELECT max(similarity(lower(replace(s, 'ё', 'е')), :n)) "
            "FROM unnest(synonyms) s), 0))"
        )
    else:
        sim = "similarity(lower(replace(name, 'ё', 'е')), :n)"
    rows = db.execute(
        text(
            f"SELECT id, sim FROM (SELECT id, {sim} AS sim FROM {table}) t "
            f"WHERE sim >= :thr ORDER BY sim DESC LIMIT 3"
        ),
        {"n": n, "thr": _TRGM_THRESHOLD},
    ).mappings().all()
    if not rows:
        return None
    ids = [row["id"] for row in rows]
    found = db.execute(
        text(f"{sql} WHERE id = ANY(:ids)"), {"ids": ids}
    ).mappings().all()
    by_id = {row["id"]: dict(row) for row in found}
    for row in rows:  # порядок по схожести сохраняем
        if row["id"] in by_id:
            return by_id[row["id"]], float(row["sim"])
    return None


def _lookup(db: Session, sql: str, needle: str, *,
            table: str, synonyms: bool = False, fuzzy: bool = True) -> dict | None:
    """Ищет лучшее совпадение имени в заданной таблице (SQL остаётся без параметров AI, §23).

    Кандидаты фильтруются по префиксу основы в SQL (дёшево), точный ранжиринг
    выполняется в Python: точное совпадение -> начало слова -> стем -> подстрока.
    У locations учитываются жаргонные синонимы из колонки synonyms (§15):
    «Русский музей» находит «Михайловский дворец». Если ничего не совпало —
    запасной проход pg_trgm по схожести (опечатки, §12); ``fuzzy=False``
    отключает его, когда поправку опечаток решает вызывающая сторона.
    """
    first = needle.split()[0] if needle.split() else needle
    prefix = _stem(first)[:4] or _normalize(first)[:4]
    where = "lower(replace(name, 'ё', 'е')) LIKE :p"
    if synonyms:
        where = (
            f"({where} OR EXISTS (SELECT 1 FROM unnest(synonyms) s "
            f"WHERE lower(replace(s, 'ё', 'е')) LIKE :p))"
        )
    rows = db.execute(
        text(f"{sql} WHERE {where}"),
        {"p": f"{prefix}%"},
    ).mappings().all()
    if not rows:
        # запасной вариант: основа встречается в любом месте имени
        # («Государственный Эрмитаж» на префикс «эрм» не попадает)
        rows = db.execute(
            text(f"{sql} WHERE {where}"),
            {"p": f"%{prefix}%"},
        ).mappings().all()

    best: tuple[int, int, dict] | None = None
    multiword = len(needle.split()) > 1
    for row in rows:
        r, matched = _rank_row(needle, row, synonyms)
        if r == 99:
            continue
        if r == 4 and multiword:
            # многословный запрос, совпало лишь одно слово: это, скорее всего,
            # не то место («Русского музея» → «Русский Левша») — лучше поискать
            # дальше (улицы, ориентиры) или честно не найти
            continue
        key = (r, len(row["name"]))
        if best is None or key < best[:2]:
            candidate = dict(row)
            if matched is not None:
                candidate["matched_label"] = matched
            candidate["match_rank"] = r
            best = (r, len(row["name"]), candidate)
    if best:
        return best[2]

    if not fuzzy:
        return None
    hit = _trigram_lookup(db, sql, table, needle, synonyms)
    if hit is None:
        return None
    row, sim = hit
    row["match_rank"] = 4  # кандидат «на исправление опечатки», слабее точных
    row["fuzzy_sim"] = sim
    return row


_OBJECT_SELECT = (
    "SELECT id, name, latitude, longitude, 'cultural_object' AS source "
    "FROM cultural_objects"
)
_LOCATION_SELECT = (
    "SELECT id, name, latitude, longitude, kind, source, synonyms, "
    "ST_AsText(geometry) AS wkt FROM locations"
)
_LOCATIONS_SELECT = (
    "SELECT id, name, latitude, longitude, kind, source, synonyms FROM locations"
)

# bbox Санкт-Петербурга — защита от бессмысленных координат
_SPB_LAT = (59.75, 60.10)
_SPB_LON = (29.50, 31.00)


def _split_pair(name: str) -> tuple[str, str] | None:
    """Достаёт два названия улиц из «пересечения A и B» / «угол A и B».

    Без явного маркера берёт любые «A и B»: вернётся пара только если обе
    части реально нашлись в locations, иначе резолвер уйдёт к обычному поиску.
    """
    m = _INTERSECTION_RE.search(name)
    body = m.group(1) if m else (name if _PAIR_SPLIT_RE.search(name) else None)
    if not body:
        return None
    parts = [p.strip(" «»\"'(),.") for p in _PAIR_SPLIT_RE.split(body)]
    parts = [p for p in parts if len(p) >= 3]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _intersection(db: Session, a: str, b: str) -> dict | None:
    """Точка пересечения двух улиц: геометрия берётся из locations, считает PostGIS.

    ``ST_ClosestPoint(A, B)`` и ``ST_ClosestPoint(B, A)`` — ближайшие точки двух
    линий; для пересекающихся линий это одна и та же точка пересечения.
    """
    row_a = _lookup(db, _LOCATION_SELECT, a, table="locations")
    row_b = _lookup(db, _LOCATION_SELECT, b, table="locations")
    if not row_a or not row_b or row_a["id"] == row_b["id"]:
        return None

    rows = db.execute(
        text(
            """
            SELECT ST_Y(pt) AS latitude, ST_X(pt) AS longitude
            FROM (
                SELECT ST_Centroid(ST_Union(
                    ST_ClosestPoint(a.geometry, b.geometry),
                    ST_ClosestPoint(b.geometry, a.geometry)
                )) AS pt
                FROM locations a, locations b
                WHERE a.id = :a AND b.id = :b
            ) s
            """
        ),
        {"a": row_a["id"], "b": row_b["id"]},
    ).mappings().all()
    if not rows:
        return None
    lat, lon = rows[0]["latitude"], rows[0]["longitude"]
    if lat is None or lon is None:
        return None
    if not (_SPB_LAT[0] <= lat <= _SPB_LAT[1] and _SPB_LON[0] <= lon <= _SPB_LON[1]):
        logger.info("Пересечение вне СПб, отклонено: %s x %s -> %s, %s", a, b, lat, lon)
        return None
    return {
        "latitude": lat,
        "longitude": lon,
        "label": f"пересечение {a} и {b}",
        "source": "location:intersection",
    }


# --- латиница и транслитерация (§13) ------------------------------------------

_LATIN_TOKEN_RE = re.compile(r"[A-Za-z'’]+")

# частые слова латинских названий: словарь точнее транслитерации
# («hermitage» → «эрмитаж», а не «хермитаже»)
_LATIN_WORDS: dict[str, str] = {
    "hermitage": "эрмитаж",
    "winter": "зимний", "palace": "дворец",
    "nevsky": "невский", "nevskiy": "невский", "nevski": "невский",
    "prospect": "проспект", "prospekt": "проспект", "avenue": "проспект",
    "street": "улица", "ulitsa": "улица", "square": "площадь",
    "ploshchad": "площадь", "dvortsovaya": "дворцовая",
    "embankment": "набережная", "bridge": "мост",
    "garden": "сад", "park": "парк",
    "museum": "музей", "cathedral": "собор", "church": "церковь",
    "fortress": "крепость",
    "isaac": "исаакиевский", "isaacs": "исаакиевский",
    "kazan": "казанский", "kazansky": "казанский",
    "petropavlovsky": "петропавловский",
    "savior": "спас", "saviour": "спас", "blood": "крови",
    "russian": "русский", "summer": "летний",
    "mikhailovsky": "михайловский", "mikhaylovsky": "михайловский",
    "smolny": "смольный", "aurora": "аврора",
    "gostiny": "гостиный", "dvor": "двор",
    "vasilievsky": "васильевского", "vasilyevsky": "васильевского",
    "spit": "стрелка", "tavrichesky": "таврический",
    "st": "", "of": "", "the": "", "on": "на", "and": "и",
}

# популярные названия целиком — точнее поэлементного перевода
_LATIN_PHRASES: dict[str, str] = {
    "winter palace": "зимний дворец",
    "palace square": "дворцовая площадь",
    "russian museum": "русский музей",
    "summer garden": "летний сад",
    "peter and paul fortress": "петропавловская крепость",
    "savior on blood": "спас на крови",
    "church of the savior on blood": "спас на крови",
    "bronze horseman": "медный всадник",
    "gostiny dvor": "гостиный двор",
    "vasilievsky island": "васильевский остров",
}

# диграфы — раньше одиночных букв: «shch» → «щ», «ya» → «я»
_TRANSLIT_DIGRAPHS: tuple[tuple[str, str], ...] = (
    ("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("zh", "ж"), ("kh", "х"),
    ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
    ("ye", "е"), ("yi", "ий"), ("iy", "ий"), ("ju", "ю"), ("ja", "я"),
    ("je", "е"),
)
_TRANSLIT_LETTERS: dict[str, str] = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "ы", "z": "з",
}


def _translit_word(word: str) -> str:
    """Практическая транслитерация: «Pestelya» → «пестеля», «Nevskiy» → «невский»."""
    w = word.lower()
    out: list[str] = []
    i = 0
    while i < len(w):
        for latin, cyr in _TRANSLIT_DIGRAPHS:
            if w.startswith(latin, i):
                out.append(cyr)
                i += len(latin)
                break
        else:
            out.append(_TRANSLIT_LETTERS.get(w[i], w[i]))
            i += 1
    return "".join(out)


def _latin_to_cyrillic(text: str) -> str:
    """Латинские слова в тексте → кириллица: словарь, для остальных — транслит."""
    def repl(match: re.Match) -> str:
        token = match.group(0)
        key = token.lower().strip("'’")
        if key.endswith("'s"):
            key = key[:-2]
        if key in _LATIN_WORDS:
            return _LATIN_WORDS[key]
        return _translit_word(key)

    return re.sub(r"\s+", " ", _LATIN_TOKEN_RE.sub(repl, text)).strip(" .,")


def _name_variants(needle: str) -> list[str]:
    """Кириллические варианты названия, если в нём есть латиница (§13)."""
    if not re.search(r"[A-Za-z]", needle):
        return []
    variants: list[str] = []
    low = re.sub(r"\s+", " ", needle.lower().strip(" «»\"'(),."))
    phrase = _LATIN_PHRASES.get(low)
    if phrase:
        variants.append(phrase)
    cyr = _latin_to_cyrillic(needle)
    if cyr and cyr != needle:
        variants.append(cyr)
    return list(dict.fromkeys(v for v in variants if v != needle))


# --- сборка ответа и точки входа ----------------------------------------------

def _as_object(row: dict) -> dict:
    return {
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "label": row["name"],
        "source": row["source"],
        # id нужен интентам «расстояние» и «сравнение»: им требуются карточки
        "object_id": row["id"],
    }


def _as_location(row: dict) -> dict:
    return {
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        # совпадение по синониму отвечаем тем же словом, что ввёл человек
        "label": row.get("matched_label") or row["name"],
        "source": row["source"] or row["kind"] or "location",
    }


def _fuzzy_place(db: Session, needle: str) -> dict | None:
    """Поправка опечаток по обеим таблицам: побеждает большая схожесть (§12).

    Объекты чуть впереди при равной схожести — как и в основном порядке поиска.
    """
    best: tuple[float, bool, dict] | None = None
    for table, sql, synonyms in (
        ("cultural_objects", _OBJECT_SELECT, False),
        ("locations", _LOCATIONS_SELECT, True),
    ):
        hit = _trigram_lookup(db, sql, table, needle, synonyms)
        if hit is None:
            continue
        row, sim = hit
        place = _as_location(row) if table == "locations" else _as_object(row)
        if best is None or (sim, table == "cultural_objects") > (best[0], best[1]):
            best = (sim, table == "cultural_objects", place)
    return best[2] if best else None


def _resolve_once(db: Session, settings: Settings, needle: str) -> dict:
    """Один проход по базе: объекты -> пересечения -> локации -> опечатки."""
    # 1) культурные объекты: точное совпадение решает сразу, частичное —
    #    придерживаем, вдруг у локации есть точный синоним («Зимний дворец»)
    row = _lookup(db, _OBJECT_SELECT, needle, table="cultural_objects", fuzzy=False)
    if row and row.get("match_rank") == 0:
        return _as_object(row)

    # 2) пересечения улиц («пересечение Пестеля и Моховой») — PostGIS (§45).
    #    Раньше обычного поиска по locations: иначе «Пестеля и Моховой»
    #    свелось бы к одной улице, а нужна точка их пересечения.
    pair = _split_pair(needle)
    if pair:
        hit = _intersection(db, *pair)
        if hit:
            return hit

    # 3) именованные локации (улицы, площади, ориентиры) с синонимами (§15)
    loc = _lookup(db, _LOCATIONS_SELECT, needle, table="locations",
                  synonyms=True, fuzzy=False)
    if loc and (row is None or loc.get("match_rank", 99) < row.get("match_rank", 99)):
        return _as_location(loc)
    if row:
        return _as_object(row)

    # 4) опечатки: запасной проход pg_trgm по обеим таблицам (§12)
    fuzzy = _fuzzy_place(db, needle)
    if fuzzy:
        return fuzzy

    # 5) внешний геокодер — только fallback (§26), по умолчанию выключен
    if settings.geocoder_url:
        # TODO(§26): интеграция с внешним геокодером — endpoint не определён в ТЗ,
        # поэтому автоматически не используется, чтобы не подставлять сомнительные координаты.
        logger.warning("Внешний геокодер настроен, но интеграция не реализована: %s", needle)

    raise PlaceNotFound(f"не удалось определить место «{needle}»")


def resolve_place(db: Session, settings: Settings, name: str) -> dict:
    """Имя места -> {latitude, longitude, label, source}.

    Кандидаты ранжируются: точное совпадение -> совпадение по подстроке.
    Латинские названия переводятся в кириллицу (словарь и транслитерация, §13);
    для смешанных («Nevsky проспект») сначала пробуется исходное написание.
    Синонимы и опечатки разбираются внутри `_lookup`.
    Внешний геокодер подключается только при заданном geocoder_url (TODO §26).
    """
    needle = name.strip().strip("«»\"'(),.")
    if not needle:
        raise PlaceNotFound("пустое название места")

    has_cyrillic = bool(re.search(r"[а-яё]", needle, re.IGNORECASE))
    variants = _name_variants(needle)
    order = [needle, *variants] if has_cyrillic or not variants else [*variants, needle]
    for candidate in dict.fromkeys(order):  # от исходного к переведённому
        try:
            return _resolve_once(db, settings, candidate)
        except PlaceNotFound:
            continue
    raise PlaceNotFound(f"не удалось определить место «{name}»")
