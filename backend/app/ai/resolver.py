"""Резолвер имён мест (§26): объект -> locations -> внешний геокодер (fallback).

Порядок обязателен по ТЗ: сначала ищем в собственной БД, внешний геокодер
используется только как последний вариант и по умолчанию отключён (§48).
"""

from __future__ import annotations

import logging

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

    # 2) именованные локации (улицы, площади, ориентиры)
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

    # 3) внешний геокодер — только fallback (§26), по умолчанию выключен
    if settings.geocoder_url:
        # TODO(§26): интеграция с внешним геокодером — endpoint не определён в ТЗ,
        # поэтому автоматически не используется, чтобы не подставлять сомнительные координаты.
        logger.warning("Внешний геокодер настроен, но интеграция не реализована: %s", name)

    raise PlaceNotFound(f"не удалось определить место «{name}»")
