"""Этап 2.4: загрузка нормализованных данных в PostgreSQL + PostGIS (§9).

Запуск (из корня проекта):
    # внутри контейнера backend:
    docker compose run --rm -v ${PWD}/scripts:/app/scripts backend \
        python /app/scripts/import_data.py
    # либо в venv на хосте при поднятой БД:
    python scripts/import_data.py

Идемпотентность: таблицы districts/locations перезаписываются полностью,
cultural_objects — полная перезагрузка (эмбеддинги пересчитываются
скриптом generate_embeddings.py сразу после импорта).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"

FIELDS = [
    "name", "category", "description", "short_description", "long_description",
    "address", "latitude", "longitude", "website", "phone", "opening_hours",
    "working_hours", "themes", "period", "art_types", "keywords", "metro_stations",
    "year_established", "is_free", "ticket_price", "image_url", "source", "source_id",
]


def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    user = os.environ.get("POSTGRES_USER", "culturalfind")
    pwd = os.environ.get("POSTGRES_PASSWORD", "culturalfind")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "culturalfind")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def ring_to_multipolygon(polygons: list[list[list[tuple[float, float]]]]) -> str:
    """Polygons -> WKT MULTIPOLYGON."""
    polys = []
    for poly in polygons:
        rings = []
        for ring in poly:
            pts = ", ".join(f"{lon} {lat}" for lon, lat in ring)
            rings.append(f"({pts})")
        polys.append("(" + ", ".join(rings) + ")")
    return "MULTIPOLYGON(" + ", ".join(polys) + ")"


def import_district(cur) -> int:
    polygon_path = ROOT / "data" / "raw" / "district_polygon.json"
    if not polygon_path.exists():
        print("Нет data/raw/district_polygon.json — сначала запустите fetch_osm.py")
        return 0
    poly = json.loads(polygon_path.read_text(encoding="utf-8"))
    wkt = ring_to_multipolygon(poly["polygons"])
    cur.execute("DELETE FROM districts")
    cur.execute(
        """
        INSERT INTO districts (name, name_en, geometry, source)
        VALUES (%s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326), %s)
        """,
        ("Центральный район", "Central District", wkt, "osm:relation/1114902"),
    )
    return 1


def import_locations(cur) -> int:
    """locations.json (улицы/площади из OSM/Wikidata) + landmarks.json (ориентиры).

    Дубликаты по имени пропускаются: landmarks дополняет, а не заменяет.
    """
    seen: set[str] = set()
    count = 0
    cur.execute("DELETE FROM locations")
    for fname in ("locations.json", "landmarks.json"):
        path = PROCESSED / fname
        if not path.exists():
            print(f"Нет data/processed/{fname} — пропускаем")
            continue
        for loc in json.loads(path.read_text(encoding="utf-8")):
            geom = loc.get("geometry_wkt")
            if not geom or loc["name"] in seen:
                continue
            seen.add(loc["name"])
            cur.execute(
                """
                INSERT INTO locations (name, synonyms, kind, latitude, longitude, geometry, source, source_id)
                VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326), %s, %s)
                """,
                (
                    loc["name"],
                    loc.get("synonyms"),
                    loc.get("kind"),
                    loc["latitude"],
                    loc["longitude"],
                    geom,
                    loc.get("source"),
                    loc.get("source_id"),
                ),
            )
            count += 1
    if count == 0:
        print("Нет data/processed/locations.json — запустите fetch_locations.py (необязательно)")
    return count


def import_objects(cur) -> int:
    path = PROCESSED / "objects.json"
    if not path.exists():
        raise SystemExit("Нет data/processed/objects.json — сначала запустите prepare_data.py")
    objects = json.loads(path.read_text(encoding="utf-8"))

    cur.execute("TRUNCATE cultural_objects RESTART IDENTITY")
    count = 0
    for obj in objects:
        cols = FIELDS
        values = [obj.get(f) for f in cols]
        placeholders = ", ".join(["%s"] * len(cols))
        col_sql = ", ".join(cols)
        # geometry не передаём: триггер trg_cultural_objects_sync вычисляет её
        # из latitude/longitude (единый источник истины)
        cur.execute(f"INSERT INTO cultural_objects ({col_sql}) VALUES ({placeholders})", values)
        count += 1
    return count


def main() -> None:
    try:
        import psycopg
    except ImportError:
        raise SystemExit("Нужен psycopg: pip install -r backend/requirements.txt (или запуск в контейнере)")

    url = db_url()
    print(f"Подключение: {url.split('@')[-1]}")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            n_d = import_district(cur)
            n_l = import_locations(cur)
            n_o = import_objects(cur)
        conn.commit()

    print(f"Импортировано: districts={n_d}, locations={n_l}, cultural_objects={n_o}")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT category, count(*) FROM cultural_objects GROUP BY category ORDER BY 2 DESC")
            for cat, cnt in cur.fetchall():
                print(f"  {cat}: {cnt}")
            cur.execute(
                "SELECT count(*) FROM cultural_objects c JOIN districts d ON ST_Within(c.geometry, d.geometry)"
            )
            print("внутри полигона района:", cur.fetchone()[0])


if __name__ == "__main__":
    sys.exit(main())
