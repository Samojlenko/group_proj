"""Гибридные рекомендации (§28–§30): семантика + пространство + категория.

score = w_sem * семантика + w_spatial * пространственная близость + w_cat * категория

Веса берутся из конфигурации (§28 — их нельзя зашивать в код).
Всё ранжирование выполняется в SQL (pgvector `<=>` + PostGIS ST_Distance);
в Python не загружаются и не сортируются все объекты (§45).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai.client import AIServiceUnavailable, embed
from app.config import Settings
from app.spatial.queries import OBJ_COLS

logger = logging.getLogger(__name__)


def _weights(settings: Settings, *, has_vector: bool, has_point: bool) -> dict[str, float]:
    """Нормализация весов под фактически доступные компоненты (§28)."""
    w = {
        "sem": settings.score_weight_semantic if has_vector else 0.0,
        "spatial": settings.score_weight_spatial if has_point else 0.0,
        "category": settings.score_weight_category,
    }
    total = sum(w.values())
    if total <= 0:
        return {"sem": 0.0, "spatial": 0.0, "category": 1.0}
    return {k: v / total for k, v in w.items()}


def recommend(
    db: Session,
    settings: Settings,
    *,
    query_text: str | None = None,
    categories: list[str] | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: float | None = None,
    limit: int = 10,
    not_keyword: str | None = None,
) -> dict:
    """Возвращает {"items", "weights", "components"}.

    items       — объекты с полем score (0..1) по убыванию;
    weights     — ФАКТИЧЕСКИ применённые (нормализованные) веса (§28);
    components  — какие компоненты реально участвовали в скоринге.
    """
    has_point = latitude is not None and longitude is not None
    vector: list[float] | None = None
    if query_text and settings.ai_enabled:
        try:
            vector = embed(settings, query_text)
        except AIServiceUnavailable as exc:
            logger.warning("Эмбеддинг недоступен, работаем без семантики: %s", exc)

    weights = _weights(settings, has_vector=vector is not None, has_point=has_point)

    params: dict = {"limit": limit, "w_sem": weights["sem"],
                    "w_spatial": weights["spatial"], "w_cat": weights["category"]}

    select_bits: list[str] = []
    where_bits: list[str] = []

    if vector:
        params["vec"] = str(list(vector))
        # CAST вместо :vec::vector / :point::geography: SQLAlchemy не распознаёт
        # bind-параметр, за которым сразу идёт двойное двоеточие cast'а.
        # COALESCE: объекты без эмбеддинга получают семантику 0.0, а не
        # NULL (иначе при неподготовленной pgvector-выдаче выдача пустела, §31)
        sem_expr = "COALESCE((1.0 - (c.embedding <=> CAST(:vec AS vector))), 0.0)"
    else:
        sem_expr = "0.0"

    if has_point:
        params["point"] = f"SRID=4326;POINT({longitude} {latitude})"
        dist_expr = "ST_Distance(c.geog, CAST(:point AS geography))"
        spatial_expr = f"(1.0 / (1.0 + {dist_expr} / 500.0))"
        select_bits.append(f"{dist_expr} AS distance_m")
    else:
        spatial_expr = "0.0"
        select_bits.append("NULL::double precision AS distance_m")

    if radius_meters and has_point:
        params["radius"] = radius_meters
        where_bits.append("ST_DWithin(c.geog, CAST(:point AS geography), :radius)")

    if not_keyword:
        # доски и прочие служебные подтипы не должны попадать в рекомендации,
        # если скрыты на карте (счётчики и списки сходятся)
        params["not_keyword"] = not_keyword
        where_bits.append("NOT (coalesce(c.keywords, '{}') @> ARRAY[:not_keyword]::text[])")

    if categories:
        names = [f"cat_{i}" for i in range(len(categories))]
        where_bits.append(f"c.category IN ({', '.join(':' + n for n in names)})")
        params.update(dict(zip(names, categories)))
        cat_expr = "1.0"
    else:
        cat_expr = "0.0"

    select_bits.append(f"({sem_expr}) AS sem_score")
    select_bits.append(f"({spatial_expr}) AS spatial_score")
    score_expr = (
        f":w_sem * ({sem_expr}) + :w_spatial * ({spatial_expr}) + :w_cat * ({cat_expr})"
    )
    select_bits.append(f"{score_expr} AS score")

    where_sql = f"WHERE {' AND '.join(where_bits)}" if where_bits else ""
    sql = f"""
    SELECT {OBJ_COLS}, {', '.join(select_bits)}
    FROM cultural_objects c
    {where_sql}
    ORDER BY score DESC, c.name
    LIMIT :limit
    """
    rows = db.execute(text(sql), params).mappings().all()
    items = []
    for r in rows:
        # «Почему эти?» (идея 17): вклад каждого компонента в балл, чтобы
        # интерфейс мог показать «рядом / похоже по описанию / нужная категория»
        factors = {
            "semantic": round(weights["sem"] * float(r["sem_score"] or 0.0), 3),
            "spatial": round(weights["spatial"] * float(r["spatial_score"] or 0.0), 3),
            "category": round(weights["category"] * (1.0 if categories else 0.0), 3),
        }
        items.append({
            **dict(r),
            "distance_meters": round(r["distance_m"], 1) if r["distance_m"] is not None else None,
            "score": round(float(r["score"]), 4),
            "factors": factors,
        })
    return {
        "items": items,
        "weights": {k: round(v, 3) for k, v in weights.items()},
        "components": {
            "semantic": vector is not None,
            "spatial": has_point,
            "category": bool(categories),
        },
    }
