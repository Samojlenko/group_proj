"""Этап 2.5: именованные географические места для Location Resolver (§26).

Резолвер сначала ищет совпадение в cultural_objects, затем в locations
(этот скрипт), внешний геокодер — только fallback.

Проверки достоверности (§53, ничего не выдумываем):
  * точная русская метка существа в Wikidata (rdfs:label);
  * координаты попадают в bbox Санкт-Петербурга;
  * предпочтение — сущности с P131 = Q271014 (Центральный район);
  * если у существа есть P402 (ссылка на OSM), берётся реальная линия улицы
    из OSM, и она тоже обязана проложена внутри bbox Петербурга.
"""

from __future__ import annotations

import json
import urllib.parse
import xml.etree.ElementTree as ET

from etl_lib import PROCESSED, RAW, http_get, save_json

WD_SPARQL = "https://query.wikidata.org/sparql"
OSM_API = "https://api.openstreetmap.org/api/0.6"

# bbox Петербурга (lat_min, lat_max, lon_min, lon_max) с запасом
SPB_BBOX = (59.75, 60.10, 29.50, 31.00)
CENTRAL_DISTRICT_QID = "Q271014"

# (точная метка @ru, синонимы для резолвера, тип)
PLACES: list[tuple[str, list[str], str]] = [
    ("Невский проспект", ["Невский"], "street"),
    ("Дворцовая площадь", ["Дворцовая"], "square"),
    ("Литейный проспект", ["Литейный"], "street"),
    ("Владимирский проспект", ["Владимирский"], "street"),
    ("Витебский проспект", ["Витебский"], "street"),
    ("Садовая улица", ["Садовая"], "street"),
    ("набережная реки Мойки", ["Мойка"], "street"),
    ("Набережная канала Грибоедова", ["Грибоедова"], "street"),
    ("Сенатская площадь", ["Сенатская"], "square"),
    ("Марсово поле", [], "square"),
    ("Исаакиевская площадь", ["Исаакиевская"], "square"),
]

OUT = PROCESSED / "locations.json"
OSM_CACHE_DIR = RAW / "osm" / "locations"


def _in_spb(lat: float, lon: float) -> bool:
    return SPB_BBOX[0] <= lat <= SPB_BBOX[1] and SPB_BBOX[2] <= lon <= SPB_BBOX[3]


def find_place(label: str) -> dict | None:
    """Точная метка @ru + проверка bbox; предпочтение — привязке к Центральному району."""
    query = f"""
SELECT ?item ?coord ?osm ?p131 WHERE {{
  ?item rdfs:label "{label}"@ru.
  ?item wdt:P625 ?coord.
  OPTIONAL {{ ?item wdt:P402 ?osm. }}
  OPTIONAL {{ ?item wdt:P131 ?p131. }}
}} LIMIT 10
"""
    url = f"{WD_SPARQL}?query={urllib.parse.quote(query)}&format=json"
    try:
        data = json.loads(http_get(url, timeout=90).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] SPARQL error для «{label}»: {exc}")
        return None

    candidates: list[dict] = []
    for row in data["results"]["bindings"]:
        coord = row["coord"]["value"]
        body = coord[coord.index("(") + 1: coord.index(")")]
        lon_s, lat_s = body.split()
        lat, lon = float(lat_s), float(lon_s)
        if not _in_spb(lat, lon):
            continue  # чужой город — не берём
        p131 = row.get("p131", {}).get("value", "").rsplit("/", 1)[-1]
        candidates.append({
            "qid": row["item"]["value"].rsplit("/", 1)[-1],
            "lat": lat,
            "lon": lon,
            "osm": int(row["osm"]["value"]) if "osm" in row else None,
            "district": p131,
        })

    if not candidates:
        return None
    for c in candidates:  # сначала — точно Центральный район
        if c["district"] == CENTRAL_DISTRICT_QID:
            return c
    return candidates[0]


def osm_geometry(osm_id: int) -> tuple[str, float, float] | None:
    """Геометрия OSM way/relation -> (WKT, lat, lon); все точки обязаны быть в СПб."""
    OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for otype in ("way", "relation"):
        cache = OSM_CACHE_DIR / f"{otype}_{osm_id}.xml"
        if cache.exists():
            xml_text = cache.read_text(encoding="utf-8")
        else:
            try:
                xml_text = http_get(f"{OSM_API}/{otype}/{osm_id}/full", timeout=60).decode("utf-8")
            except Exception:  # noqa: BLE001
                continue
            cache.write_text(xml_text, encoding="utf-8")
        try:
            root = ET.fromstring(xml_text)
        except Exception:  # noqa: BLE001
            continue

        nodes = {int(n.get("id")): (float(n.get("lon")), float(n.get("lat")))
                 for n in root.findall("node")}
        ways = []
        for w in root.findall("way"):
            pts = [nodes[int(nd.get("ref"))] for nd in w.findall("nd") if int(nd.get("ref")) in nodes]
            if len(pts) >= 2:
                ways.append(pts)
        if not ways:
            continue

        # сборка цепочки из отрезков (улица может состоять из нескольких way)
        chain = list(ways[0])
        rest = ways[1:]
        while rest:
            for i, seg in enumerate(rest):
                if seg[0] == chain[-1]:
                    chain += seg[1:]
                    break
                if seg[-1] == chain[-1]:
                    chain += list(reversed(seg))[1:]
                    break
                if seg[-1] == chain[0]:
                    chain = seg[:-1] + chain
                    break
                if seg[0] == chain[0]:
                    chain = list(reversed(seg))[:-1] + chain
                    break
            else:
                break
            rest.pop(i)

        if len(chain) < 2:
            continue
        if not all(_in_spb(y, x) for x, y in chain):
            continue  # геометрия вне Петербурга — не используем

        closed = chain[0] == chain[-1] and len(chain) >= 4
        if closed:
            wkt = "POLYGON((" + ", ".join(f"{x} {y}" for x, y in chain) + "))"
        else:
            wkt = "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in chain) + ")"
        lat = sum(p[1] for p in chain) / len(chain)
        lon = sum(p[0] for p in chain) / len(chain)
        return wkt, lat, lon
    return None


def main() -> None:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    results = []
    for label, synonyms, kind in PLACES:
        place = find_place(label)
        if not place:
            print(f"  [!] не найдено/не проверено: {label}")
            continue

        wkt = lat = lon = None
        if place["osm"]:
            geo = osm_geometry(place["osm"])
            if geo:
                wkt, lat, lon = geo
        if wkt is None:
            lat, lon = place["lat"], place["lon"]
            wkt = f"POINT({lon} {lat})"

        results.append({
            "name": label,
            "synonyms": synonyms,
            "kind": kind,
            "latitude": lat,
            "longitude": lon,
            "geometry_wkt": wkt,
            "source": "wikidata+osm" if place["osm"] else "wikidata",
            "source_id": place["qid"] if not place["osm"] else f"{place['qid']};osm/{place['osm']}",
        })
        print(f"  [ok] {label} ({kind}) -> {wkt[:52]}...")

    save_json(OUT, results)
    print(f"Сохранено локаций: {len(results)} -> {OUT}")


if __name__ == "__main__":
    main()
