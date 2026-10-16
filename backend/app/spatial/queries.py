"""Пространственные запросы к PostGIS (§11–§16, §45).

Правило проекта: все расстояния и пространственные отношения считаются
в PostgreSQL/PostGIS. Python только собирает параметры и отдаёт ответ.
Используются GiST-индексы (geometry/geog) и KNN-оператор `<->`.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# --- фрагменты SQL -----------------------------------------------------------
OBJ_COLS = """
    c.id, c.name, c.category, c.description, c.short_description, c.address,
    c.latitude, c.longitude, c.website, c.phone, c.opening_hours, c.working_hours,
    c.themes, c.art_types, c.keywords, c.metro_stations, c.year_established,
    c.is_free, c.ticket_price, c.image_url, c.source, c.source_id
"""

DIST_COL = ", ST_Distance(c.geog, CAST(:point AS geography)) AS distance_meters"


def _point_params(lat: float, lon: float) -> dict:
    # точка задаётся строкой WKT -> одна параметризованная позиция, без конкатенации
    return {"point": f"SRID=4326;POINT({lon} {lat})"}


def _filters_sql(
    *,
    categories: list[str] | None,
    q: str | None,
    theme: str | None,
    keyword: str | None,
    has_website: bool | None,
    has_description: bool | None,
    within_district: bool,
    not_keyword: str | None = None,
) -> tuple[str, dict]:
    where: list[str] = []
    params: dict = {}
    if categories:
        # именованные параметры по одному: работает одинаково на psycopg2/psycopg3
        names = [f"cat_{i}" for i in range(len(categories))]
        where.append(f"c.category IN ({', '.join(':' + n for n in names)})")
        params.update(dict(zip(names, categories)))
    if q:
        where.append("(c.name ILIKE :like_q OR coalesce(c.short_description,'') ILIKE :like_q)")
        params["like_q"] = f"%{q}%"
    if theme:
        where.append("(c.themes && ARRAY[:theme]::text[] OR c.keywords && ARRAY[:theme]::text[])")
        params["theme"] = theme
    if keyword:
        where.append("(c.keywords && ARRAY[:keyword]::text[] OR c.name ILIKE :kw_like)")
        params["keyword"] = keyword
        params["kw_like"] = f"%{keyword}%"
    if not_keyword:
        # исключение объектов с ключевым словом (например, мемориальных досок)
        where.append("NOT (coalesce(c.keywords, '{}') @> ARRAY[:not_keyword]::text[])")
        params["not_keyword"] = not_keyword
    if has_website is True:
        where.append("c.website IS NOT NULL AND c.website <> ''")
    elif has_website is False:
        where.append("c.website IS NULL OR c.website = ''")
    if has_description is True:
        where.append("c.description IS NOT NULL")
    elif has_description is False:
        where.append("c.description IS NULL")
    if within_district:
        # выбираем Центральный район по имени (иначе первый попавшийся, §10)
        where.append(
            "ST_Within(c.geometry, (SELECT geometry FROM districts "
            "ORDER BY (name ILIKE '%ентральн%') DESC, id LIMIT 1))"
        )
    sql = ("WHERE " + " AND ".join(where)) if where else ""
    return sql, params


def _run(db: Session, sql: str, params: dict) -> list:
    return db.execute(text(sql), params).mappings().all()


def _with_rounded_distance(row: dict) -> dict:
    """Округляет метры до 0.1 — единый вид distance_meters во всех ответах."""
    if row.get("distance_meters") is not None:
        row["distance_meters"] = round(row["distance_meters"], 1)
    return row


def _plain_filters(**overrides) -> tuple[str, dict]:
    """WHERE без текстовых/категорийных условий — для агрегатов и radius.

    Нужен, чтобы один и тот же набор фильтров (например, скрытые мемориальные
    доски) применялся и к выборке, и к счётчикам: иначе в сайдбаре 474 объекта,
    а на карте 281.
    """
    base: dict = dict(
        categories=None, q=None, theme=None, keyword=None,
        has_website=None, has_description=None, within_district=False,
    )
    base.update(overrides)
    return _filters_sql(**base)


def get_object(db: Session, object_id: int) -> dict | None:
    """Карточка объекта (§33) — точный выбор по id."""
    rows = _run(
        db,
        f"SELECT {OBJ_COLS} FROM cultural_objects c WHERE c.id = :id",
        {"id": object_id},
    )
    return dict(rows[0]) if rows else None


def category_counts(db: Session, *, not_keyword: str | None = None) -> dict[str, int]:
    """Счётчики по категориям (§6, §36) — с учётом тех же фильтров, что на карте."""
    where, params = _plain_filters(not_keyword=not_keyword)
    rows = _run(
        db,
        f"SELECT c.category, count(*) AS n FROM cultural_objects c {where} GROUP BY c.category",
        params,
    )
    return {r["category"]: r["n"] for r in rows}


# --- публичные функции -------------------------------------------------------

def list_objects(
    db: Session,
    *,
    categories: list[str] | None = None,
    q: str | None = None,
    theme: str | None = None,
    keyword: str | None = None,
    not_keyword: str | None = None,
    has_website: bool | None = None,
    has_description: bool | None = None,
    within_district: bool = False,
    limit: int = 50,
    offset: int = 0,
    lat: float | None = None,
    lon: float | None = None,
) -> tuple[list[dict], int]:
    """GET /api/objects (§17) — фильтрация (§15) + сортировка (§11)."""
    where, params = _filters_sql(
        categories=categories, q=q, theme=theme, keyword=keyword,
        not_keyword=not_keyword,
        has_website=has_website, has_description=has_description,
        within_district=within_district,
    )
    total = _run(db, f"SELECT count(*) AS n FROM cultural_objects c {where}", params)[0]["n"]

    dist = ""
    order = "ORDER BY c.name"
    if lat is not None and lon is not None:
        params.update(_point_params(lat, lon))
        dist = DIST_COL
        order = "ORDER BY distance_meters"
    rows = _run(
        db,
        f"SELECT {OBJ_COLS}{dist} FROM cultural_objects c {where} {order} LIMIT :limit OFFSET :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return [dict(r) for r in rows], total


def geojson_objects(db: Session, **filters) -> dict:
    """GET /api/objects/geojson (§16) — FeatureCollection."""
    cats = filters.get("categories")
    limit = filters.pop("limit", 5000)
    where, params = _filters_sql(
        categories=cats,
        q=filters.get("q"),
        theme=filters.get("theme"),
        keyword=filters.get("keyword"),
        not_keyword=filters.get("not_keyword"),
        has_website=filters.get("has_website"),
        has_description=filters.get("has_description"),
        within_district=filters.get("within_district", False),
    )
    # LIMIT применяется во вложенном SELECT: json_agg — агрегат, и на внешнем
    # уровне строка всегда одна (иначе limit фактически не действовал)
    sql = f"""
    SELECT COALESCE(json_build_object(
        'type', 'FeatureCollection',
        'features', json_agg(json_build_object(
            'type', 'Feature',
            'id', c.id,
            'geometry', ST_AsGeoJSON(c.geometry)::json,
            'properties', json_build_object(
                'name', c.name,
                'category', c.category,
                'address', c.address,
                'short_description', c.short_description,
                'image_url', c.image_url,
                'is_plaque', COALESCE(c.keywords, '{{}}') @> ARRAY['мемориальная доска']::text[]
            )
        ))
    ), '{{"type":"FeatureCollection","features":[]}}'::json) AS fc
    FROM (
        SELECT * FROM cultural_objects c
        {where}
        ORDER BY c.id
        LIMIT :limit
    ) c
    """
    return _run(db, sql, {**params, "limit": limit})[0]["fc"]


def nearby_objects(
    db: Session, lat: float, lon: float, *, categories: list[str] | None = None,
    not_keyword: str | None = None, limit: int = 10
) -> list[dict]:
    """§11.1: ближайшие объекты — KNN-сортировка по GiST-индексу (метры)."""
    where, params = _filters_sql(
        categories=categories, q=None, theme=None, keyword=None,
        not_keyword=not_keyword,
        has_website=None, has_description=None, within_district=False,
    )
    params.update(_point_params(lat, lon))
    sql = f"""
    SELECT {OBJ_COLS}{DIST_COL}
    FROM cultural_objects c
    {where}
    ORDER BY c.geog <-> CAST(:point AS geography)
    LIMIT :limit
    """
    return [dict(r) for r in _run(db, sql, {**params, "limit": limit})]


def radius_objects(
    db: Session,
    lat: float,
    lon: float,
    radius_m: float,
    *,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int, list[dict]]:
    """§12: поиск в радиусе (ST_DWithin по индексу geog, затем точная метрика).

    Возвращает ``(страница, total, in_radius)``:

    * ``items`` — ``limit`` строк с ``offset`` (выдача ограничена MAX_LIMIT);
    * ``total`` — сколько объектов реально попало в радиус;
    * ``in_radius`` — id и расстояние **всех** объектов в радиусе, без LIMIT.

    ``in_radius`` нужен карте: подсветка маркеров должна соответствовать кругу
    на карте, а не первым 100 строкам выдачи (иначе при большом радиусе часть
    объектов внутри круга остаётся неакцентированной). total == len(in_radius):
    тот же WHERE, только без LIMIT.
    """
    where, params = _plain_filters(categories=categories, not_keyword=not_keyword)
    params.update(_point_params(lat, lon))
    params["radius"] = radius_m
    within = (
        f"{where} {('AND' if where else 'WHERE')} "
        f"ST_DWithin(c.geog, CAST(:point AS geography), :radius)"
    )

    in_rows = _run(
        db,
        f"SELECT c.id{DIST_COL} FROM cultural_objects c {within} ORDER BY distance_meters",
        params,
    )
    in_radius = [_with_rounded_distance(dict(r)) for r in in_rows]
    total = len(in_radius)

    rows = _run(
        db,
        f"SELECT {OBJ_COLS}{DIST_COL} FROM cultural_objects c {within} "
        f"ORDER BY distance_meters LIMIT :limit OFFSET :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return [dict(r) for r in rows], total, in_radius


def distance_between(db: Session, id_a: int, id_b: int) -> dict | None:
    """§13: расстояние между двумя объектами."""
    sql = """
    SELECT a.id AS id_from, b.id AS id_to,
           ST_Distance(a.geog, b.geog) AS distance_m
    FROM cultural_objects a, cultural_objects b
    WHERE a.id = :id_a AND b.id = :id_b
    """
    rows = _run(db, sql, {"id_a": id_a, "id_b": id_b})
    if not rows:
        return None
    r = dict(rows[0])
    return {"id_from": r["id_from"], "id_to": r["id_to"],
            "distance_meters": round(r["distance_m"], 1),
            "distance_km": round(r["distance_m"] / 1000, 3)}


def near_object(
    db: Session,
    object_id: int,
    radius_m: float,
    *,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """§14: объекты рядом с другим объектом («Что рядом?», §34)."""
    where, params = _filters_sql(
        categories=categories, q=None, theme=None, keyword=None,
        not_keyword=not_keyword,
        has_website=None, has_description=None, within_district=False,
    )
    params.update({"base": object_id, "radius": radius_m, "limit": limit})
    and_sql = where[6:] if where.startswith("WHERE ") else ""
    sql = f"""
    WITH base AS (SELECT geog FROM cultural_objects WHERE id = :base)
    SELECT {OBJ_COLS}, ST_Distance(c.geog, base.geog) AS distance_meters
    FROM cultural_objects c CROSS JOIN base
    WHERE c.id <> :base
      {('AND ' + and_sql) if and_sql else ''}
      AND ST_DWithin(c.geog, base.geog, :radius)
    ORDER BY distance_meters
    LIMIT :limit
    """
    return [dict(r) for r in _run(db, sql, params)]


def counts_near_object(
    db: Session, object_id: int, radius_m: float, *, not_keyword: str | None = None
) -> dict[str, int]:
    """§34: группировка «что рядом» по категориям (Музеи — 7, ...).

    ``not_keyword`` — тот же фильтр, что на карте: счётчики должны сходиться
    с видимыми маркерами (по умолчанию скрыты мемориальные доски).
    """
    params: dict = {"base": object_id, "radius": radius_m}
    extra = ""
    if not_keyword:
        extra = "AND NOT (coalesce(c.keywords, '{}') @> ARRAY[:not_keyword]::text[])"
        params["not_keyword"] = not_keyword
    sql = f"""
    WITH base AS (SELECT geog FROM cultural_objects WHERE id = :base)
    SELECT c.category, count(*) AS n
    FROM cultural_objects c CROSS JOIN base
    WHERE c.id <> :base AND ST_DWithin(c.geog, base.geog, :radius) {extra}
    GROUP BY c.category
    ORDER BY n DESC
    """
    return {r["category"]: r["n"] for r in _run(db, sql, params)}


def text_search(
    db: Session,
    query: str,
    *,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
    limit: int = 20,
    lat: float | None = None,
    lon: float | None = None,
) -> tuple[list[dict], int]:
    """§15: полнотекстовый поиск (ru-стемминг) + ILIKE, опционально — по расстоянию.

    Возвращает (строки, total) — total считается отдельным count-запросом,
    чтобы не врать «всего найдено» через len(ограниченной выдачи).
    """
    where, params = _filters_sql(
        categories=categories, q=None, theme=None, keyword=None,
        not_keyword=not_keyword,
        has_website=None, has_description=None, within_district=False,
    )
    params.update({"tsq": query, "like": f"%{query}%", "limit": limit})
    match = (
        "(c.search_vector @@ plainto_tsquery('russian', :tsq) OR c.name ILIKE :like)"
    )
    total = _run(
        db,
        f"SELECT count(*) AS n FROM cultural_objects c {where} "
        f"{('AND' if where else 'WHERE')} {match}",
        params,
    )[0]["n"]
    dist = ""
    order = "ORDER BY rank DESC, c.name"
    if lat is not None and lon is not None:
        params.update(_point_params(lat, lon))
        dist = ", ST_Distance(c.geog, CAST(:point AS geography)) AS distance_meters"
        order = "ORDER BY distance_meters"
    sql = f"""
    SELECT {OBJ_COLS}{dist},
           greatest(
             ts_rank(c.search_vector, plainto_tsquery('russian', :tsq)),
             CASE WHEN c.name ILIKE :like THEN 0.5 ELSE 0 END
           ) AS rank
    FROM cultural_objects c
    {where} {('AND' if where else 'WHERE')} {match}
    {order}
    LIMIT :limit
    """
    return [dict(r) for r in _run(db, sql, params)], total


def statistics(db: Session, *, not_keyword: str | None = None) -> dict:
    """§36: статистика по объектам.

    ``not_keyword`` — тот же фильтр, что на карте (по умолчанию статистика
    глобальная): без него счётчики в сайдбаре не сходятся с видимыми объектами.
    """
    where, params = _plain_filters(not_keyword=not_keyword)
    rows = _run(db, f"""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE c.website IS NOT NULL AND c.website <> '') AS with_website,
            count(*) FILTER (WHERE c.description IS NOT NULL) AS with_description
        FROM cultural_objects c
        {where}
    """, params)[0]
    by_cat = {r["category"]: r["n"] for r in _run(db, f"""
        SELECT c.category, count(*) AS n FROM cultural_objects c {where}
        GROUP BY c.category ORDER BY n DESC
    """, params)}
    extent = _run(db, f"""
        SELECT ST_XMin(ST_Extent(c.geometry)) AS min_lon, ST_YMin(ST_Extent(c.geometry)) AS min_lat,
               ST_XMax(ST_Extent(c.geometry)) AS max_lon, ST_YMax(ST_Extent(c.geometry)) AS max_lat
        FROM cultural_objects c
        {where}
    """, params)[0]
    updated = _run(db, "SELECT max(updated_at) AS updated_at FROM cultural_objects", {})[0]
    return {
        "total": rows["total"],
        "by_category": by_cat,
        "with_website": rows["with_website"],
        "with_description": rows["with_description"],
        "bbox": dict(extent) if extent and extent["min_lon"] is not None else None,
        "updated_at": updated["updated_at"],
    }
