"""Аналитика и агрегаты (§36–§38, §45): всё считается в PostGIS."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def heatmap(db: Session, *, cell_m: float = 100.0, categories: list[str] | None = None) -> dict:
    """Плотность объектов: точки сетки с весом = число объектов в ячейке (§37)."""
    cell_deg = cell_m / 111320.0  # градусы широты ~ метры
    where = ""
    params: dict = {"cell": cell_deg}
    if categories:
        names = [f"cat_{i}" for i in range(len(categories))]
        where = f"WHERE c.category IN ({', '.join(':' + n for n in names)})"
        params.update(dict(zip(names, categories)))
    rows = db.execute(text(f"""
        SELECT ST_Y(ST_Centroid(cell)) AS lat, ST_X(ST_Centroid(cell)) AS lon, n
        FROM (
            SELECT ST_SnapToGrid(c.geometry, :cell) AS cell, count(*) AS n
            FROM cultural_objects c
            {where}
            GROUP BY cell
        ) g
    """), params).mappings().all()
    if not rows:
        return {"points": [], "max": 0}
    peak = max(r["n"] for r in rows)
    return {
        "points": [{"latitude": r["lat"], "longitude": r["lon"], "weight": r["n"]} for r in rows],
        "max": peak,
        "cell_meters": cell_m,
    }


def density_grid(db: Session, *, cell_m: float = 250.0, top: int = 10) -> dict:
    """Самые насыщенные зоны района (§36): топ ячейек сетки + центр (§37)."""
    cell_deg = cell_m / 111320.0
    rows = db.execute(text("""
        SELECT ST_Y(ST_Centroid(cell)) AS lat, ST_X(ST_Centroid(cell)) AS lon, n
        FROM (
            SELECT ST_SnapToGrid(c.geometry, :cell) AS cell, count(*) AS n
            FROM cultural_objects c
            GROUP BY cell
        ) g
        ORDER BY n DESC
        LIMIT :top
    """), {"cell": cell_deg, "top": top}).mappings().all()
    return {
        "cell_meters": cell_m,
        "zones": [
            {"latitude": r["lat"], "longitude": r["lon"], "count": r["n"]} for r in rows
        ],
    }


def nearest_neighbour_stats(db: Session, *, categories: list[str] | None = None) -> dict:
    """Среднее/медианное расстояние до ближайшего соседа (§36)."""
    where = ""
    params: dict = {}
    if categories:
        names = [f"cat_{i}" for i in range(len(categories))]
        where = f"WHERE c.category IN ({', '.join(':' + n for n in names)})"
        params.update(dict(zip(names, categories)))
    row = db.execute(text(f"""
        SELECT avg(d) AS avg_m,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS median_m,
               count(*) AS objects
        FROM (
            SELECT (
                SELECT min(ST_Distance(c1.geog, c2.geog))
                FROM cultural_objects c2
                WHERE c2.id <> c1.id
            ) AS d
            FROM cultural_objects c1
            {where.replace('c.category', 'c1.category') if where else ''}
        ) t
        WHERE d IS NOT NULL
    """), params).mappings().first()
    return {
        "avg_nearest_m": round(row["avg_m"], 1) if row and row["avg_m"] else None,
        "median_nearest_m": round(row["median_m"], 1) if row and row["median_m"] else None,
        "objects": row["objects"] if row else 0,
    }


def district_geojson(db: Session) -> dict:
    """Граница Центрального района (§10) для отрисовки на карте."""
    row = db.execute(text("""
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(json_agg(json_build_object(
                'type', 'Feature',
                'id', d.id,
                'geometry', ST_AsGeoJSON(d.geometry)::json,
                'properties', json_build_object('name', d.name, 'name_en', d.name_en)
            )), '[]'::json)
        ) AS fc
        FROM districts d
    """)).mappings().first()
    return row["fc"] if row else {"type": "FeatureCollection", "features": []}
