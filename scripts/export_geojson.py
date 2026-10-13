"""Одноразовый экспорт data/processed/objects.json -> GeoJSON (для передачи/приёмки).

Конвертация локальная, БД не требуется: точки 4326 + свойства объекта.
Запуск: python -X utf8 scripts/export_geojson.py [путь-выйти]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "processed" / "objects.json"
DST = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "processed" / "objects.geojson"

# свойства, которые имеют смысл во внешнем файле (без внутренних полей ETL)
KEEP = (
    "name", "category", "description", "short_description", "address",
    "latitude", "longitude", "website", "phone", "opening_hours",
    "working_hours", "themes", "art_types", "keywords", "metro_stations",
    "year_established", "is_free", "ticket_price", "image_url",
    "source", "source_id",
)


def main() -> None:
    objects = json.loads(SRC.read_text(encoding="utf-8"))

    features = []
    for o in objects:
        props = {k: o[k] for k in KEEP if o.get(k) is not None}
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [o["longitude"], o["latitude"]],  # GeoJSON: [lon, lat]
            },
            "properties": props,
        })

    fc = {
        "type": "FeatureCollection",
        "name": "cultural_objects_central_district_spb",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    DST.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"объектов: {len(features)}")
    print(f"-> {DST} ({DST.stat().st_size / 1024:.0f} КБ)")


if __name__ == "__main__":
    main()
