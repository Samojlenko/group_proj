"""Фан-режим (идеи 24–26): «удиви меня», профиль вкуса, подборки.

Все выборки идут в SQL: случайный «эффектный» объект — из пула лучших по
описанию и фото, подборка — те же критерии, профиль вкуса — среднее
эмбеддингов избранного и KNN-поиск по pgvector. Python только выбирает
случайный элемент из готового пула и усредняет векторы (§45).
"""

from __future__ import annotations

import json
import logging
import random

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.spatial.queries import OBJ_COLS

logger = logging.getLogger(__name__)

# из скольких «интересных» объектов выбираем случайный («удиви меня»)
SURPRISE_POOL = 40
# описание короче этого не годится для подборок и квестов: вопрос не из чего
# составить, а «эффектная подача» без текста пустая (в базе 474 объекта,
# описаний длиннее 40 символов — около четырёх десятков)
DESC_MIN = 40
# сколько избранных учитываем в профиле вкуса (лишние только размывают вектор)
MAX_FAVORITES = 50

# «топ-7 музеев» — родительный падеж для названия подборки
_COLLECTION_WORDS: dict[str, str] = {
    "museum": "музеев",
    "monument": "памятников",
    "memorial": "мемориалов",
    "gallery": "галерей",
}


def _desc_expr() -> str:
    """Описание для фильтров и сортировки: короткое предпочтительнее полного."""
    return "coalesce(nullif(c.short_description, ''), nullif(c.description, ''))"


def _filters(*, categories: list[str] | None, not_keyword: str | None,
             params: dict) -> list[str]:
    """Общие условия выборок: категории и скрытые подтипы (доски, §8)."""
    where: list[str] = []
    if categories:
        names = [f"cat_{i}" for i in range(len(categories))]
        where.append(f"c.category IN ({', '.join(':' + n for n in names)})")
        params.update(dict(zip(names, categories)))
    if not_keyword:
        params["not_keyword"] = not_keyword
        where.append("NOT (coalesce(c.keywords, '{}') @> ARRAY[:not_keyword]::text[])")
    return where


def surprise_pick(
    db: Session,
    *,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
    exclude: list[int] | None = None,
) -> dict | None:
    """Случайный «эффектный» объект для «удиви меня» (идея 24).

    Пул — объекты с фотографией и содержательным описанием (сортировка по
    длине текста: в пул попадают самые «рассказывающие»), из пула берём
    случайный. ``exclude`` — id недавно показанных: «ещё раз» не повторяется.
    Если с фото и описанием пусто — подставляем объект просто с описанием,
    а затем — любой объект: «удиви меня» не должен упираться в пустоту.
    """
    exclude_ids = list(exclude or [])
    base_params: dict = {"pool": SURPRISE_POOL, "desc_min": DESC_MIN}
    base = _filters(categories=categories, not_keyword=not_keyword, params=base_params)

    # ступени всё мягче: фото + рассказ → рассказ → хоть что-то без повтора →
    # повтор разрешён. «Удиви меня» не должен упираться в пустоту.
    stages = ((True, True, True), (False, True, True), (False, False, True), (False, False, False))
    for with_image, with_desc, use_exclude in stages:
        params = dict(base_params)
        where = list(base)
        if with_image:
            where.append("c.image_url IS NOT NULL")
        if with_desc:
            where.append(f"length({_desc_expr()}) >= :desc_min")
        if use_exclude and exclude_ids:
            params["exclude"] = exclude_ids
            where.append("NOT (c.id = ANY(:exclude))")
        order = (
            "(c.image_url IS NOT NULL) DESC, "
            f"length({_desc_expr()}) DESC NULLS LAST, c.name"
        )
        sql = f"""
        SELECT {OBJ_COLS} FROM cultural_objects c
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT :pool
        """
        rows = db.execute(text(sql), params).mappings().all()
        if rows:
            return dict(random.choice(rows))
    return None


def build_collection(
    db: Session,
    *,
    categories: list[str] | None = None,
    count: int = 5,
    not_keyword: str | None = None,
) -> list[dict]:
    """«Топ-N» (идея 26): объекты с фото и самыми содержательными описаниями.

    Рейтингов у объектов нет, поэтому «топ» — честная прокси-метрика: есть
    фотография, длинное описание (главный источник впечатления и подсказок).
    """
    params: dict = {"count": count, "desc_min": DESC_MIN}
    where = _filters(categories=categories, not_keyword=not_keyword, params=params)
    where.append(f"length({_desc_expr()}) >= :desc_min")
    sql = f"""
    SELECT {OBJ_COLS} FROM cultural_objects c
    WHERE {' AND '.join(where)}
    ORDER BY (c.image_url IS NOT NULL) DESC,
             length({_desc_expr()}) DESC, c.name
    LIMIT :count
    """
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


def collection_name(categories: list[str] | None, count: int) -> str:
    """«Топ-7 музеев», «Топ-5 объектов» — человеческое имя подборки."""
    words = [_COLLECTION_WORDS[c] for c in (categories or []) if c in _COLLECTION_WORDS]
    tail = " и ".join(words) if words else "объектов"
    return f"Топ-{count} {tail}"


def taste_recommend(
    db: Session,
    *,
    favorite_ids: list[int] | None,
    limit: int = 10,
    not_keyword: str | None = None,
) -> dict:
    """Профиль вкуса (идея 25): усредняем эмбеддинги избранного, ищем похожее.

    Возвращает ``{"items", "used", "favorites"}``: ``used`` — сколько
    избранных реально дали вектор (у остальных нет описания). Пустое
    ``used`` — не ошибка на этом уровне: эндпоинт сам решает, что сказать.
    """
    ids = [int(i) for i in (favorite_ids or [])][:MAX_FAVORITES]
    result: dict = {"items": [], "used": 0, "favorites": len(ids)}
    if not ids:
        return result

    rows = db.execute(
        text(
            "SELECT c.embedding::text AS emb FROM cultural_objects c "
            "WHERE c.id = ANY(:ids) AND c.embedding IS NOT NULL"
        ),
        {"ids": ids},
    ).all()
    vectors: list[list[float]] = []
    for row in rows:
        try:
            vectors.append([float(x) for x in json.loads(row[0])])
        except (TypeError, ValueError):  # pragma: no cover - битый вектор в БД
            logger.warning("Профиль вкуса: пропущен нечитаемый эмбеддинг")
    if not vectors:
        return result

    # средний вектор избранного — «профиль вкуса»; размерность у всех одинакова
    avg = [sum(col) / len(vectors) for col in zip(*vectors)]
    result["used"] = len(vectors)

    params: dict = {"vec": str(avg), "ids": ids, "limit": limit}
    where = ["c.embedding IS NOT NULL", "NOT (c.id = ANY(:ids))"]
    if not_keyword:
        params["not_keyword"] = not_keyword
        where.append("NOT (coalesce(c.keywords, '{}') @> ARRAY[:not_keyword]::text[])")
    sql = f"""
    SELECT {OBJ_COLS}, (1.0 - (c.embedding <=> CAST(:vec AS vector))) AS score
    FROM cultural_objects c
    WHERE {' AND '.join(where)}
    ORDER BY c.embedding <=> CAST(:vec AS vector)
    LIMIT :limit
    """
    items = []
    for r in db.execute(text(sql), params).mappings().all():
        score = round(float(r["score"]), 4)
        items.append({
            **dict(r),
            "distance_meters": None,
            "score": score,
            # в профиле вкуса участвует только семантика — «почему эти» (17)
            # покажет одну полоску «похоже по описанию»
            "factors": {"semantic": score, "spatial": 0.0, "category": 0.0},
        })
    result["items"] = items
    return result
