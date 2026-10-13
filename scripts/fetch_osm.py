"""Этап 2.2: сбор объектов из OpenStreetMap через OSM API (§8).

Overpass-инстансы из этой сети недоступны, поэтому используем официальный
OSM API (/api/0.6/map) сеткой по bbox района. Сначала забирается граница
района (relation 1114902 — id найден через Wikidata P402), затем —
объекты нужных категорий (§6) внутри bbox.

Сырьё: data/raw/central_district_osm.xml (граница),
       data/raw/osm/pois.json (объекты).
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET

from etl_lib import RAW, ensure_dirs, http_get, load_json, save_json

OSM_API = "https://api.openstreetmap.org/api/0.6"
DISTRICT_RELATION_ID = 1114902  # Центральный район (P402 из Wikidata Q271014)

BOUNDARY_XML = RAW / "central_district_osm.xml"
POIS_JSON = RAW / "osm" / "pois.json"
POLYGON_JSON = RAW / "district_polygon.json"

# теги -> категория (§6)
# tourism=attraction в центре Петербурга — это, как правило, памятник/достопримечательность
CATEGORY_TAGS: dict[tuple[str, str], str] = {
    ("tourism", "museum"): "museum",
    ("historic", "monument"): "monument",
    ("historic", "memorial"): "memorial",
    ("tourism", "gallery"): "gallery",
    ("tourism", "art_gallery"): "gallery",
    ("tourism", "attraction"): "monument",
}

TILE_DEG = 0.004  # ~440x270 м; маленькие тайлы, чтобы не упереться в лимиты API


def fetch_boundary() -> str:
    if BOUNDARY_XML.exists() and BOUNDARY_XML.stat().st_size > 1000:
        return BOUNDARY_XML.read_text(encoding="utf-8")
    print(f"Загрузка границы района (relation {DISTRICT_RELATION_ID})...")
    body = http_get(f"{OSM_API}/relation/{DISTRICT_RELATION_ID}/full", timeout=90)
    RAW.mkdir(parents=True, exist_ok=True)
    BOUNDARY_XML.write_bytes(body)
    return body.decode("utf-8")


def build_polygon(xml_text: str) -> dict:
    """Собирает замкнутые внешние кольца ways отношенния в полигоны."""
    root = ET.fromstring(xml_text)
    nodes: dict[int, tuple[float, float]] = {}
    for n in root.findall("node"):
        nodes[int(n.get("id"))] = (float(n.get("lon")), float(n.get("lat")))

    way_coords: dict[int, list[tuple[float, float]]] = {}
    for w in root.findall("way"):
        coords = [nodes[int(nd.get("ref"))] for nd in w.findall("nd") if int(nd.get("ref")) in nodes]
        way_coords[int(w.get("id"))] = coords

    relation = next((r for r in root.findall("relation") if int(r.get("id")) == DISTRICT_RELATION_ID), None)
    if relation is None:
        raise RuntimeError("Relation района не найден в XML")

    outer_ids: list[int] = []
    inner_ids: list[int] = []
    for m in relation.findall("member"):
        if m.get("type") != "way":
            continue
        (outer_ids if m.get("role") in ("outer", "") else inner_ids).append(int(m.get("ref")))

    def assemble(ids: list[int]) -> list[list[tuple[float, float]]]:
        segs = [list(way_coords[i]) for i in ids if i in way_coords and len(way_coords[i]) > 1]
        rings: list[list[tuple[float, float]]] = []
        while segs:
            ring = segs.pop(0)
            changed = True
            while changed and ring[0] != ring[-1]:
                changed = False
                for i, seg in enumerate(segs):
                    if seg[0] == ring[-1]:
                        ring += seg[1:]
                    elif seg[-1] == ring[-1]:
                        ring += list(reversed(seg))[1:]
                    elif seg[-1] == ring[0]:
                        ring = seg[:-1] + ring
                    elif seg[0] == ring[0]:
                        ring = list(reversed(seg))[:-1] + ring
                    else:
                        continue
                    segs.pop(i)
                    changed = True
                    break
            rings.append(ring)
        return rings

    outers = assemble(outer_ids)
    inners = assemble(inner_ids)
    polygons = [[r] for r in outers if len(r) >= 4]
    # внутренние кольца добавляем к первому полигону (в районе их обычно нет)
    if polygons:
        for h in inners:
            if len(h) >= 4:
                polygons[0].append(h)
    return {"type": "MultiPolygon", "polygons": polygons}


def tile_grid(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    tiles = []
    y = min_lat
    while y < max_lat:
        x = min_lon
        while x < max_lon:
            tiles.append((x, y, min(x + TILE_DEG, max_lon), min(y + TILE_DEG, max_lat)))
            x += TILE_DEG
        y += TILE_DEG
    return tiles


def classify(tags: dict) -> str | None:
    for (k, v), cat in CATEGORY_TAGS.items():
        if tags.get(k) == v:
            return cat
    return None


def main() -> None:
    ensure_dirs()
    (RAW / "osm").mkdir(parents=True, exist_ok=True)

    xml_text = fetch_boundary()
    polygon = build_polygon(xml_text)
    save_json(POLYGON_JSON, polygon)

    lons = [p[0] for poly in polygon["polygons"] for ring in poly for p in ring]
    lats = [p[1] for poly in polygon["polygons"] for ring in poly for p in ring]
    bbox = (min(lons), min(lats), max(lons), max(lats))
    print(f"BBox района: {bbox}")

    cached = load_json(POIS_JSON) or []
    known = {(p["element"], p["id"]) for p in cached}
    tiles = tile_grid(bbox)
    print(f"Тайлов: {len(tiles)} (уже собрано объектов: {len(cached)})")

    for idx, (x1, y1, x2, y2) in enumerate(tiles, 1):
        key = f"{x1:.4f},{y1:.4f}"
        cache_file = RAW / "osm" / f"tile_{key}.xml"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            xml_body = cache_file.read_text(encoding="utf-8")
        else:
            url = f"{OSM_API}/map?bbox={x1:.5f},{y1:.5f},{x2:.5f},{y2:.5f}"
            try:
                xml_body = http_get(url, timeout=120, retries=4, backoff=(5, 15, 30, 60)).decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                print(f"  [{idx}/{len(tiles)}] ОШИБКА {key}: {exc}")
                continue
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(xml_body, encoding="utf-8")
            time.sleep(0.5)  # вежливость к публичному API

        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            print(f"  [{idx}/{len(tiles)}] parse error {key}")
            continue

        nodes = {
            int(n.get("id")): (float(n.get("lat")), float(n.get("lon")), dict(
                (t.get("k"), t.get("v")) for t in n.findall("tag")
            ))
            for n in root.findall("node")
        }
        found = 0
        for n_id, (lat, lon, tags) in nodes.items():
            cat = classify(tags)
            if not cat:
                continue
            if ("node", n_id) in known:
                continue
            known.add(("node", n_id))
            cached.append({"element": "node", "id": n_id, "lat": lat, "lon": lon,
                           "category": cat, "tags": tags})
            found += 1

        for w in root.findall("way"):
            tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
            cat = classify(tags)
            if not cat:
                continue
            w_id = int(w.get("id"))
            if ("way", w_id) in known:
                continue
            pts = [nodes[int(nd.get("ref"))] for nd in w.findall("nd") if int(nd.get("ref")) in nodes]
            if not pts:
                continue
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            known.add(("way", w_id))
            cached.append({"element": "way", "id": w_id, "lat": lat, "lon": lon,
                           "category": cat, "tags": tags})
            found += 1

        if idx % 10 == 0 or found:
            print(f"  [{idx}/{len(tiles)}] {key} +{found} (всего {len(cached)})")
        save_json(POIS_JSON, cached)

    by_cat: dict[str, int] = {}
    for p in cached:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
    print("Итого OSM-объектов:", len(cached), by_cat)


if __name__ == "__main__":
    main()
