"""Пространственные запросы по заведениям из OSM (идея 28, этап H).

Кафе, туалеты и парковки живут в отдельной таблице `amenities` (миграция
0003): это не объекты культурной карты, у них другой набор полей. Правило
проекта не меняется — все расстояния и пространственные отношения считает
PostGIS по колонке `geog` (GiST-индекс, метры), Python лишь собирает ответ
(§45).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.constants import AMENITY_LABELS, AMENITY_SINGULAR

__all__ = [
    "AMENITY_LABELS", "AMENITY_SINGULAR",
    "amenities_near", "amenities_geojson",
]

_AMENITY_COLS = """
    a.id, a.osm_type, a.osm_id, a.category, a.name, a.address,
    a.opening_hours, a.latitude, a.longitude
"""


def _run(db: Session, sql: str, params: dict) -> list:
    return db.execute(text(sql), params).mappings().all()


def _point_params(lat: float, lon: float) -> dict:
    # точка задаётся строкой WKT -> одна параметризованная позиция (как в queries.py)
    return {"point": f"SRID=4326;POINT({lon} {lat})"}


def amenities_near(
    db: Session,
    lat: float,
    lon: float,
    radius_m: float,
    *,
    category: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Заведения в радиусе от точки: ``(страница, всего в радиусе)``.

    Сортировка — по расстоянию (KNN-оператор отработал при фильтре ST_DWithin,
    точные метры считает ST_Distance по geog).
    """
    where = "ST_DWithin(a.geog, CAST(:point AS geography), :radius)"
    params: dict = {**_point_params(lat, lon), "radius": radius_m}
    if category:
        where += " AND a.category = :category"
        params["category"] = category

    total = int(_run(db, f"SELECT count(*) AS n FROM amenities a WHERE {where}", params)[0]["n"])

    rows = _run(
        db,
        f"""
        SELECT {_AMENITY_COLS},
               ST_Distance(a.geog, CAST(:point AS geography)) AS distance_meters
        FROM amenities a
        WHERE {where}
        ORDER BY distance_meters
        LIMIT :limit OFFSET :offset
        """,
        {**params, "limit": limit, "offset": offset},
    )
    items: list[dict] = []
    for row in rows:
        item = dict(row)
        if item.get("distance_meters") is not None:
            item["distance_meters"] = round(float(item["distance_meters"]), 1)
        items.append(item)
    return items, total


def amenities_geojson(
    db: Session, *, category: str | None = None, limit: int = 5000
) -> dict:
    """Слой карты: FeatureCollection с заведениями (кафе/туалеты/парковки)."""
    where = ""
    params: dict = {"limit": limit}
    if category:
        where = "WHERE a.category = :category"
        params["category"] = category
    sql = f"""
    SELECT COALESCE(json_build_object(
        'type', 'FeatureCollection',
        'features', json_agg(json_build_object(
            'type', 'Feature',
            'id', a.id,
            'geometry', ST_AsGeoJSON(a.geometry)::json,
            'properties', json_build_object(
                'name', a.name,
                'category', a.category,
                'address', a.address,
                'opening_hours', a.opening_hours
            )
        ))
    ), '{{"type":"FeatureCollection","features":[]}}'::json) AS fc
    FROM (
        SELECT * FROM amenities a
        {where}
        ORDER BY a.id
        LIMIT :limit
    ) a
    """
    return _run(db, sql, params)[0]["fc"]
