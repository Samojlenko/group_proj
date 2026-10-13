"""Локальная диагностика (без сети): расстояния «объект -> кандидат из Вики»."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from etl_lib import haversine_m, name_similarity  # noqa: E402

D = ROOT / "data/raw/wikidata"
objs = json.loads((ROOT / "data/processed/objects.json").read_text(encoding="utf-8"))
search = json.loads((D / "search_cache.json").read_text(encoding="utf-8"))
t2q = json.loads((D / "title2qid_cache.json").read_text(encoding="utf-8"))
links = json.loads((D / "osm_links.json").read_text(encoding="utf-8"))
amap = json.loads((D / "article_map.json").read_text(encoding="utf-8"))
entities = json.loads((D / "entities.json").read_text(encoding="utf-8"))


def coord(ent):
    for c in ent.get("claims", {}).get("P625", []):
        if c.get("mainsnak", {}).get("snaktype") == "value":
            v = c["mainsnak"]["datavalue"]["value"]
            if isinstance(v, dict) and "latitude" in v:
                return float(v["latitude"]), float(v["longitude"])
    return None


targets = []
for o in objs:
    sid = o["source_id"].split(";")[0] if o["source"] != "wikidata" else None
    key = f"wd:{o['source_id']}" if o["source"] == "wikidata" else f"osm:{sid}"
    if key in amap:
        continue
    if o["source"] != "wikidata" and sid in links:
        continue
    targets.append((key, o))

buckets = {"<=150": 0, "150-300": 0, "300-500": 0, "500-1000": 0, ">1000": 0, "нет коорд": 0}
best: list[tuple[float, float, str, str]] = []  # dist, sim, title, objname

for key, o in targets:
    mind, minsim = None, 0.0
    have_coord = False
    for title in search.get(o["name"], []):
        qid = t2q.get(title)
        if not qid or qid not in entities:
            continue
        c = coord(entities[qid])
        if not c:
            continue
        have_coord = True
        d = haversine_m(o["latitude"], o["longitude"], c[0], c[1])
        s = name_similarity(o["name"], title)
        if mind is None or d < mind:
            mind, minsim = d, s
    if not have_coord:
        buckets["нет коорд"] += 1
        continue
    if mind <= 150:
        buckets["<=150"] += 1
    elif mind <= 300:
        buckets["150-300"] += 1
    elif mind <= 500:
        buckets["300-500"] += 1
    elif mind <= 1000:
        buckets["500-1000"] += 1
    else:
        buckets[">1000"] += 1
    best.append((mind, minsim, "", o["name"]))

print("целей:", len(targets))
print("бакеты расстояний до ближайшего кандидата:", buckets)
best.sort()
print("ближайшие кандидаты 150-500 м (dist, sim, объект):")
for d, s, _t, name in best:
    if 150 < d <= 500:
        print(f"   {d:7.1f} м  sim={s:.2f}  {name}")

# сколько связок дадут разные пороги
for radius, sim, rank0 in ((150, 0.4, True), (300, 0.5, True), (300, 0.4, True), (500, 0.4, True)):
    ok = 0
    for key, o in targets:
        for rank, title in enumerate(search.get(o["name"], [])):
            qid = t2q.get(title)
            if not qid or qid not in entities:
                continue
            c = coord(entities[qid])
            if not c:
                continue
            d = haversine_m(o["latitude"], o["longitude"], c[0], c[1])
            if d > radius:
                continue
            s = name_similarity(o["name"], title)
            if s >= sim or (rank0 and rank == 0):
                ok += 1
                break
    print(f"порог radius={radius}, sim={sim}, rank0={rank0} -> связок: {ok}")
