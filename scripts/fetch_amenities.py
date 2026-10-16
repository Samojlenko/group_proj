"""Этап H (идея 28): кафе, туалеты и парковки из OSM — слой «вокруг объекта».

Сеть не нужна: тайлы района уже скачаны на этапе 2 (`data/raw/osm/tile_*.xml`),
в них лежат ВСЕ элементы bbox, а не только культурные объекты. Скрипт заново
читает кэш и вытаскивает `amenity=cafe|toilets|parking`.

Правила отбора:
* только внутри полигона района (bbox шире границы);
* `access=private|no` пропускается (чужие дворовые стоянки и служебные уборные);
* way берётся, только если в тайле есть все его точки, — иначе центроид
  сместится к краю тайла;
* дубликаты (элемент встречается в соседних тайлах) схлопываются по id.

Результат: data/processed/amenities.json
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from etl_lib import PROCESSED, RAW, ensure_dirs, load_json, save_json

TILES_DIR = RAW / "osm"
POLYGON_JSON = RAW / "district_polygon.json"
OUT_JSON = PROCESSED / "amenities.json"

# amenity -> наша категория (§6: слои «кафе», «туалеты», «парковки»)
AMENITY_TAGS: dict[str, str] = {
    "cafe": "cafe",
    "toilets": "toilet",
    "parking": "parking",
}

FALLBACK_NAMES = {"cafe": "Кафе", "toilet": "Туалет", "parking": "Парковка"}


def point_in_polygon(lon: float, lat: float, polygons: list) -> bool:
    """Луч-кастинг: внутри внешнего кольца и вне внутренних (дыр)."""
    for rings in polygons:
        if not rings:
            continue
        if not _in_ring(lon, lat, rings[0]):
            continue
        if any(_in_ring(lon, lat, hole) for hole in rings[1:]):
            continue
        return True
    return False


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def amenity_of(tags: dict) -> str | None:
    cat = AMENITY_TAGS.get(tags.get("amenity", ""))
    if not cat:
        return None
    if tags.get("access") in ("private", "no"):
        return None
    # безымянное кафе в ответе «где выпить кофе» бесполезно: это, как правило,
    # кофейный автомат или точка без данных; у туалетов/парковок имя не нужно
    if cat == "cafe" and not (tags.get("name") or "").strip():
        return None
    return cat


def build_record(element: str, osm_id: int, category: str, tags: dict,
                 lat: float, lon: float) -> dict:
    address = " ".join(
        part for part in (tags.get("addr:street"), tags.get("addr:housenumber")) if part
    )
    return {
        "element": element,
        "id": osm_id,
        "category": category,
        "name": (tags.get("name") or "").strip() or FALLBACK_NAMES[category],
        "address": address or None,
        "opening_hours": tags.get("opening_hours"),
        "latitude": round(lat, 7),
        "longitude": round(lon, 7),
        "source": "osm",
        "source_id": f"{element}/{osm_id}",
    }


def parse_tile(path, polygons: list, found: dict[tuple[str, int], dict]) -> int:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        print(f"  битый тайл: {path.name}")
        return 0

    nodes: dict[int, tuple[float, float, dict]] = {}
    for n in root.findall("node"):
        nodes[int(n.get("id"))] = (
            float(n.get("lat")),
            float(n.get("lon")),
            {t.get("k"): t.get("v") for t in n.findall("tag")},
        )

    added = 0
    for n_id, (lat, lon, tags) in nodes.items():
        cat = amenity_of(tags)
        if not cat or ("node", n_id) in found:
            continue
        if not point_in_polygon(lon, lat, polygons):
            continue
        found[("node", n_id)] = build_record("node", n_id, cat, tags, lat, lon)
        added += 1

    for w in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        cat = amenity_of(tags)
        w_id = int(w.get("id"))
        if not cat or ("way", w_id) in found:
            continue
        refs = [int(nd.get("ref")) for nd in w.findall("nd")]
        if not refs or any(r not in nodes for r in refs):
            continue  # геометрия обрезана тайлом — центроид был бы смещён
        lat = sum(nodes[r][0] for r in refs) / len(refs)
        lon = sum(nodes[r][1] for r in refs) / len(refs)
        if not point_in_polygon(lon, lat, polygons):
            continue
        found[("way", w_id)] = build_record("way", w_id, cat, tags, lat, lon)
        added += 1

    return added


def main() -> None:
    ensure_dirs()
    polygon = load_json(POLYGON_JSON)
    if not polygon:
        raise SystemExit("Нет data/raw/district_polygon.json — сначала fetch_osm.py")
    polygons = polygon["polygons"]

    tiles = sorted(TILES_DIR.glob("tile_*.xml"))
    if not tiles:
        raise SystemExit("Нет тайлов data/raw/osm/tile_*.xml — сначала fetch_osm.py")
    print(f"Тайлов: {len(tiles)}")

    found: dict[tuple[str, int], dict] = {}
    for idx, tile in enumerate(tiles, 1):
        added = parse_tile(tile, polygons, found)
        if added and idx % 20 == 0:
            print(f"  [{idx}/{len(tiles)}] {tile.name}: +{added} (всего {len(found)})")

    records = sorted(found.values(), key=lambda r: (r["category"], r["name"]))
    save_json(OUT_JSON, records)

    by_cat: dict[str, int] = {}
    for r in records:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    named = sum(1 for r in records if r["name"] != FALLBACK_NAMES[r["category"]])
    print(f"Итого: {len(records)} {by_cat}; с названием: {named}")


if __name__ == "__main__":
    main()
