"""Этап 2.1: сбор списка объектов из Wikidata (§8, §9).

Стратегия:
  1) SPARQL-запросы: предметы, связанные с Центральным районом (Q271014)
     или Санкт-Петербургом (Q655) через P131 (административная единица)
     или P276 (местоположение), чей класс входит в 4 категории (§6).
  2) wbgetentities батчами по 50 — детали (метки, описания, координаты,
     сайт, адрес, изображение, ссылка на статью).
  3) Принадлежность району проверяется позже по полигону (prepare_data.py).

Сырьё кэшируется в data/raw/wikidata/ — повторный запуск не ходит в сеть
за уже полученными батчами (воспроизводимость, §9).
"""

from __future__ import annotations

import json
import time

from etl_lib import (CATEGORY_ROOTS, RAW, ensure_dirs, http_get, http_post_form,
                     load_json, save_json)

WD_SPARQL = "https://query.wikidata.org/sparql"
WD_API = "https://www.wikidata.org/w/api.php"

DISTRICT_QID = "Q271014"   # Центральный район Санкт-Петербурга
CITY_QID = "Q655"          # Санкт-Петербург

TARGETS = [
    ("P131", DISTRICT_QID),
    ("P276", DISTRICT_QID),
    ("P131", CITY_QID),
    ("P276", CITY_QID),
]

CACHE_DIR = RAW / "wikidata"


def sparql_list(prop: str, target: str) -> list[dict]:
    """Список QID + корневой класс категории. Кэшируется в файл."""
    cache = CACHE_DIR / f"list_{prop}_{target}.json"
    cached = load_json(cache)
    if cached is not None:
        print(f"  [cache] {prop}->{target}: {len(cached)} rows")
        return cached

    values = " ".join(f"wd:{qid}" for qid in CATEGORY_ROOTS)
    query = f"""
SELECT ?root ?item WHERE {{
  ?item wdt:{prop}* wd:{target}.
  ?item wdt:P625 ?coord.
  ?item wdt:P31/wdt:P279* ?root.
  VALUES ?root {{ {values} }}
}} LIMIT 20000
"""
    print(f"  [query] {prop}->{target} ...")
    url = f"{WD_SPARQL}?query={__import__('urllib.parse', fromlist=['quote']).quote(query)}&format=json"
    body = http_get(url, timeout=150)
    rows = json.loads(body.decode("utf-8"))["results"]["bindings"]
    out = [
        {"item": r["item"]["value"].rsplit("/", 1)[-1], "root": r["root"]["value"].rsplit("/", 1)[-1]}
        for r in rows
    ]
    save_json(cache, out)
    print(f"  [done] {prop}->{target}: {len(out)} rows")
    time.sleep(2)
    return out


def fetch_entities(qids: list[str]) -> dict:
    """wbgetentities батчами по 50. Существующие батчи не перезапрашиваем."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    all_ids = sorted(set(qids))
    result: dict = {}

    # подтягиваем уже сохранённые батчи
    for i in range(0, len(all_ids), 50):
        batch = all_ids[i : i + 50]
        cache = CACHE_DIR / f"batch_{batch[0]}.json"
        data = load_json(cache)
        if data is None:
            joined = "|".join(batch)
            params = (
                f"action=wbgetentities&ids={joined}"
                "&props=labels|descriptions|claims|sitelinks|aliases&languages=ru|en&format=json"
            )
            print(f"  [entities] {batch[0]}..{batch[-1]} ({len(batch)} ids)")
            data = json.loads(http_get(f"{WD_API}?{params}", timeout=90).decode("utf-8"))
            save_json(cache, data)
            time.sleep(0.7)
        result.update(data.get("entities", {}))
    return result


def main() -> None:
    ensure_dirs()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("Этап 2.1: SPARQL-списки объектов")
    qid_roots: dict[str, set[str]] = {}
    for prop, target in TARGETS:
        for row in sparql_list(prop, target):
            qid_roots.setdefault(row["item"], set()).add(row["root"])

    print(f"Уникальных QID: {len(qid_roots)}")
    print("Этап 2.1: детали объектов (wbgetentities)")
    entities = fetch_entities(list(qid_roots))
    save_json(CACHE_DIR / "entities.json", entities)

    summary = {
        "qids": len(qid_roots),
        "entities": len(entities),
        "by_root": {},
    }
    for roots in qid_roots.values():
        for r in roots:
            summary["by_root"][r] = summary["by_root"].get(r, 0) + 1
    save_json(CACHE_DIR / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
