"""Резолвер имён мест (§26): объект -> locations -> пересечения улиц -> геокодер.

Порядок обязателен по ТЗ: сначала ищем в собственной БД, внешний геокодер
используется только как последний вариант и по умолчанию отключён (§48).

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
_ENDINGS = (
    "иями", "ями", "ами", "ием", "ьем", "ем", "ом", "ой", "ей", "ах", "ях",
    "ов", "ев", "ин", "ый", "ий", "ы", "и", "а", "я", "о", "е", "у", "ю", "ь", "й",
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


def _rank(needle: str, name: str) -> int:
    """Чем меньше, тем лучше совпадение; 99 — не совпало."""
    n = _normalize(needle)
    c = _normalize(name)
    if n == c:
        return 0
    if c.startswith(n) or n.startswith(c):
        return 1
    # сравнение по стемам слов: «Эрмитажем» ~ «Государственный Эрмитаж»,
    # «на Невском» ~ «Невский проспект»
    ns = {_stem(w) for w in n.split()}
    cs = {_stem(w) for w in c.split()}
    if ns & cs:
        return 2
    if n in c or c in n:
        return 3
    return 99


def _lookup(db: Session, sql: str, needle: str) -> dict | None:
    """Ищет лучшее совпадение имени в заданной таблице (SQL остаётся без параметров AI, §23).

    Кандидаты фильтруются по префиксу основы в SQL (дёшево), точный ранжиринг
    выполняется в Python: точное совпадение -> начало слова -> стем -> подстрока.
    """
    first = needle.split()[0] if needle.split() else needle
    prefix = _stem(first)[:4] or _normalize(first)[:4]
    rows = db.execute(
        text(f"{sql} WHERE lower(replace(name, 'ё', 'е')) LIKE :p"),
        {"p": f"{prefix}%"},
    ).mappings().all()
    if not rows:
        # запасной вариант: основа встречается в любом месте имени
        # («Государственный Эрмитаж» на префикс «эрм» не попадает)
        rows = db.execute(
            text(f"{sql} WHERE lower(replace(name, 'ё', 'е')) LIKE :p"),
            {"p": f"%{prefix}%"},
        ).mappings().all()

    best: tuple[int, int, dict] | None = None
    for row in rows:
        r = _rank(needle, row["name"])
        if r == 99:
            continue
        key = (r, len(row["name"]))
        if best is None or key < best[:2]:
            best = (r, len(row["name"]), dict(row))
    return best[2] if best else None


_LOCATION_SELECT = (
    "SELECT id, name, latitude, longitude, kind, source, "
    "ST_AsText(geometry) AS wkt FROM locations"
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
    row_a = _lookup(db, _LOCATION_SELECT, a)
    row_b = _lookup(db, _LOCATION_SELECT, b)
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


def resolve_place(db: Session, settings: Settings, name: str) -> dict:
    """Имя места -> {latitude, longitude, label, source}.

    Кандидаты ранжируются: точное совпадение -> совпадение по подстроке.
    Внешний геокодер подключается только при заданном geocoder_url (TODO §26).
    """
    needle = name.strip().strip("«»\"'(),.")
    if not needle:
        raise PlaceNotFound("пустое название места")

    # 1) культурные объекты
    row = _lookup(
        db,
        "SELECT name, latitude, longitude, 'cultural_object' AS source "
        "FROM cultural_objects",
        needle,
    )
    if row:
        return {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "label": row["name"],
            "source": row["source"],
        }

    # 2) пересечения улиц («пересечение Пестеля и Моховой») — PostGIS (§45).
    #    Раньше обычного поиска по locations: иначе «Пестеля и Моховой»
    #    свелось бы к одной улице, а нужна точка их пересечения.
    pair = _split_pair(needle)
    if pair:
        hit = _intersection(db, *pair)
        if hit:
            return hit

    # 3) именованные локации (улицы, площади, ориентиры)
    row = _lookup(
        db,
        "SELECT name, latitude, longitude, kind, source FROM locations",
        needle,
    )
    if row:
        return {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "label": row["name"],
            "source": row["source"] or row["kind"] or "location",
        }

    # 4) внешний геокодер — только fallback (§26), по умолчанию выключен
    if settings.geocoder_url:
        # TODO(§26): интеграция с внешним геокодером — endpoint не определён в ТЗ,
        # поэтому автоматически не используется, чтобы не подставлять сомнительные координаты.
        logger.warning("Внешний геокодер настроен, но интеграция не реализована: %s", name)

    raise PlaceNotFound(f"не удалось определить место «{name}»")
