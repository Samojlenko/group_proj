"""Этап 2.5б: справочник улиц из локальной выгрузки OSM (без сети).

Зачем: резолвер понимает только то, что лежит в таблице locations, а в
`fetch_locations.py` описано 11 мест — в итоге в базе 6 улиц. Из-за этого
запрос «ближайшая галерея к пересечению Пестеля и Моховой» не разрешался.

Скрипт читает уже скачанные тайлы `data/raw/osm/tile_*.xml` (это ответы
OSM API /map — они содержат complete ways, т.е. геометрию улиц целиком) и
собирает из них линии улиц Центрального района. Сеть не нужна.

Выход: data/processed/streets.json в том же формате, что и locations.json.
Порядок импорта (`import_data.import_locations`) — locations.json, landmarks.json,
streets.json: уже существующие записи имеют приоритет, скрипт только дополняет.

Запуск (из корня проекта):
    python scripts/build_local_streets.py
    python scripts/import_locations_only.py   # или внутри контейнера backend
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "osm"
OUT = ROOT / "data" / "processed" / "streets.json"

# bbox Петербурга (запас) — чужие города не берём
SPB_BBOX = (59.75, 60.10, 29.50, 31.00)

# только дороги; служебные проезды и тротуары не нужны — они не «улицы»
HIGHWAY_KINDS = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "pedestrian", "living_street",
}

# типичные слова-обозначения вида улицы: остаётся «ядро» имени для синонимов
_TYPE_WORDS = {
    "улица", "ул", "проспект", "пр", "переулок", "пер", "набережная", "наб",
    "проезд", "бульвар", "б-р", "площадь", "пл", "канал", "реки", "река",
    "мост", "линия", "тракт", "шоссе", "дорога", "аллея", "спуск", "ступени",
}


def _in_spb(lon: float, lat: float) -> bool:
    return SPB_BBOX[0] <= lat <= SPB_BBOX[1] and SPB_BBOX[2] <= lon <= SPB_BBOX[3]


def _clean(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Убирает дубли соседних вершин (иначе PostGIS ругается на zero-length edge)."""
    out: list[tuple[float, float]] = []
    for p in points:
        if not out or out[-1] != p:
            out.append(p)
    return out


def _synonyms(name: str) -> list[str]:
    """«улица Пестеля» -> «Пестеля»; «Моховая улица» -> «Моховая»."""
    words = [w.strip(",.") for w in name.split() if w.strip(",.")]
    core = [w for w in words if w.lower() not in _TYPE_WORDS]
    return core if core else words


def collect_ways() -> dict[str, list[list[tuple[float, float]]]]:
    """name -> список линий (way) с этой названием, уже очищенных."""
    nodes: dict[int, tuple[float, float]] = {}
    ways: dict[int, tuple[str, list[int]]] = {}  # id -> (name, refs)

    files = sorted(RAW.glob("tile_*.xml"))
    if not files:
        raise SystemExit(f"Нет {RAW}/tile_*.xml — сначала запустите fetch_osm.py")

    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:  # битый тайл не должен ронять весь прогон
            print(f"  [!] не разобрался {path.name}: {exc}")
            continue
        for n in root.findall("node"):
            nodes[int(n.get("id"))] = (float(n.get("lon")), float(n.get("lat")))
        for w in root.findall("way"):
            tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
            if tags.get("highway") not in HIGHWAY_KINDS:
                continue
            name = tags.get("name") or tags.get("name:ru")
            if not name:
                continue
            refs = [int(nd.get("ref")) for nd in w.findall("nd")]
            ways[int(w.get("id"))] = (name, refs)

    print(f"Тайлов: {len(files)}, узлов: {len(nodes)}, именованных дорог: {len(ways)}")

    grouped: dict[str, list[list[tuple[float, float]]]] = defaultdict(list)
    for name, refs in ways.values():
        pts = [nodes[r] for r in refs if r in nodes]
        pts = _clean(pts)
        if len(pts) < 2:
            continue
        if not all(_in_spb(lon, lat) for lon, lat in pts):
            continue  # геометрия целиком вне Петербурга — не берём
        grouped[name].append(pts)
    return grouped


def build_entry(name: str, lines: list[list[tuple[float, float]]]) -> dict | None:
    # репрезентативная точка — середина самой длинной линии (устойчиво к шуму
    # от разорванных на куски улиц)
    longest = max(lines, key=len)
    mid = longest[len(longest) // 2]
    lon, lat = mid

    if len(lines) == 1:
        pts = lines[0]
        geom = "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in pts) + ")"
    else:
        geom = "MULTILINESTRING(" + ", ".join(
            "(" + ", ".join(f"{x} {y}" for x, y in line) + ")" for line in lines
        ) + ")"

    return {
        "name": name,
        "synonyms": _synonyms(name),
        "kind": "street",
        "latitude": lat,
        "longitude": lon,
        "geometry_wkt": geom,
        "source": "osm-local",
        "source_id": f"osm-local/{len(lines)} ways",
    }


def main() -> None:
    grouped = collect_ways()
    # слишком короткие осколки (остатки разрезанных на тайлы путей) отбрасываем
    entries = []
    for name, lines in sorted(grouped.items()):
        if sum(len(line) for line in lines) < 4:
            continue
        entry = build_entry(name, lines)
        if entry:
            entries.append(entry)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено улиц: {len(entries)} -> {OUT}")
    for e in entries[:15]:
        print(f"  {e['name']} ({len(e['synonyms'])} син.)")
    if len(entries) > 15:
        print(f"  … и ещё {len(entries) - 15}")


if __name__ == "__main__":
    main()
