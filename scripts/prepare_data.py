"""Этап 2.3: очистка, нормализация, дедупликация, фильтр по району (§9, §10).

Вход:  data/raw/wikidata/{entities,list_*,osm_links,street_labels,
       article_map,wiki_cache}.json, data/raw/osm/pois.json,
       data/raw/district_polygon.json   (запустите enrich.py до этого скрипта)
Выход: data/processed/objects.json — единый формат для импорта в PostGIS.

Правила (§7, §8):
  * отсутствующие данные -> null, ничего не выдумываем;
  * каждый объект знает свой источник (wikidata / openstreetmap / их связка);
  * дубликаты (один объект в двух источниках) сливаются: приоритет Wikidata
    для описаний, OSM — для эксплуатационных тегов (часы, телефон, адрес);
  * точка должна лежать внутри полигона Центрального района.

Уровни описания: short_description — метка Wikidata; description — первое
предложение википедийского лида; long_description — весь лид статьи.
"""

from __future__ import annotations

import re

from etl_lib import (
    CATEGORY_PRIORITY,
    CATEGORY_ROOTS,
    PROCESSED,
    RAW,
    haversine_m,
    load_json,
    name_similarity,
    normalize_name,
    point_in_polygon,
    save_json,
)

WIKIDATA_DIR = RAW / "wikidata"
POLYGON = RAW / "district_polygon.json"
OUT = PROCESSED / "objects.json"

MERGE_RADIUS_M = 60.0  # радиус сопоставления дубликатов между источниками
MAX_DESCRIPTION = 1200  # длина long_description (лид статьи режем аккуратно)
MAX_SHORT = 300


# --- вспомогательные -----------------------------------------------------------

def claim_value(entity: dict, prop: str):
    claims = entity.get("claims", {}).get(prop, [])
    for c in claims:
        main = c.get("mainsnak", {})
        if main.get("snaktype") != "value":
            return None
        return main.get("datavalue", {}).get("value")
    return None


def pick_label(entity: dict, lang: str = "ru") -> str | None:
    labels = entity.get("labels", {})
    for code in (lang, "en"):
        if code in labels:
            return labels[code]["value"]
    return None


def pick_description(entity: dict) -> str | None:
    for code in ("ru", "en"):
        d = entity.get("descriptions", {}).get(code)
        if d:
            return d["value"]
    return None


def parse_year(value) -> int | None:
    if not value:
        return None
    if isinstance(value, dict) and "time" in value:
        try:
            year = int(value["time"][1:5])
        except (ValueError, IndexError):
            return None
        return year if value.get("precision", 11) >= 11 else None  # precision < 11 — век
    if isinstance(value, dict) and "year" in value:
        return int(value["year"])
    return None


def split_first_sentence(text: str) -> str:
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    first = m.group(1) if m else text
    if len(first) > MAX_SHORT:
        return first[:MAX_SHORT].rstrip() + "…"
    return first.strip()


def build_qid_roots() -> dict[str, set[str]]:
    qid_roots: dict[str, set[str]] = {}
    for f in sorted(WIKIDATA_DIR.glob("list_*.json")):
        for row in load_json(f, []):
            qid_roots.setdefault(row["item"], set()).add(row["root"])
    return qid_roots


def category_of(roots: set[str]) -> str:
    codes = [CATEGORY_ROOTS[r] for r in roots if r in CATEGORY_ROOTS]
    for cand in CATEGORY_PRIORITY:
        if cand in codes:
            return cand
    return codes[0] if codes else "monument"


def article_for(obj_key: str) -> dict | None:
    """wiki_cache по карте объект -> 'lang:Заголовок'."""
    article_map = article_for._map  # type: ignore[attr-defined]
    cache = article_for._cache  # type: ignore[attr-defined]
    ck = article_map.get(obj_key)
    return cache.get(ck) if ck else None


def load_support() -> None:
    """Загрузка справочных данных enrich.py (кэшируется в атрибутах функции)."""
    article_for._map = load_json(WIKIDATA_DIR / "article_map.json", {}) or {}  # type: ignore[attr-defined]
    raw_cache = load_json(WIKIDATA_DIR / "wiki_cache.json", {}) or {}  # type: ignore[attr-defined]
    # старый формат кэша (без ключа 'lang:') тоже принимаем
    article_for._cache = raw_cache  # type: ignore[attr-defined]


# --- Wikidata-объекты ----------------------------------------------------------

def wikidata_objects() -> list[dict]:
    entities = load_json(WIKIDATA_DIR / "entities.json", {}) or {}
    street_labels = load_json(WIKIDATA_DIR / "street_labels.json", {}) or {}
    qid_roots = build_qid_roots()
    out: list[dict] = []

    for qid, roots in qid_roots.items():
        ent = entities.get(qid)
        if not ent:
            continue
        coord = claim_value(ent, "P625")
        if not coord:
            continue
        lat, lon = float(coord["latitude"]), float(coord["longitude"])
        name = pick_label(ent)
        if not name:
            continue

        # адрес: P669 (улица, подпись подтягивает enrich.py) + P670 (дом)
        street = claim_value(ent, "P669")
        house = claim_value(ent, "P670")
        address = None
        if street and isinstance(street, dict) and street.get("id"):
            street_name = street_labels.get(street["id"])
            if street_name:
                address = f"{street_name}, {house}" if house else street_name
        if address is None and house:
            address = f"д. {house}"

        website = claim_value(ent, "P856")
        if isinstance(website, dict):
            website = website.get("text") or website.get("id")
        image = claim_value(ent, "P18")

        art = article_for(f"wd:{qid}")
        extract = (art or {}).get("extract")
        categories = clean_kw((art or {}).get("categories") or [])
        sl = ent.get("sitelinks", {})
        wiki_title = (sl.get("ruwiki") or sl.get("enwiki") or {}).get("title")

        out.append({
            "name": name,
            "category": category_of(roots),
            "description": split_first_sentence(extract) if extract else None,
            "long_description": (extract or None) and extract[:MAX_DESCRIPTION],
            "short_description": pick_description(ent),
            "address": address,
            "latitude": lat,
            "longitude": lon,
            "website": website or None,
            "phone": None,
            "opening_hours": None,
            "working_hours": None,
            "themes": themes_from(categories),
            "period": None,
            "art_types": None,
            "keywords": categories or None,
            "metro_stations": None,
            "year_established": parse_year(claim_value(ent, "P571")),
            "is_free": None,
            "ticket_price": None,
            "image_url": (f"https://commons.wikimedia.org/wiki/Special:FilePath/"
                          f"{image.replace(' ', '_')}?width=900") if image else None,
            "source": "wikidata",
            "source_id": qid,
            "wikipedia_title": wiki_title,
        })
    return out


def themes_from(categories: list[str]) -> list[str] | None:
    from enrich import themes_from_categories  # те же правила, один источник

    t = themes_from_categories(categories)
    return t or None


def clean_kw(categories: list[str]) -> list[str]:
    """Очистка категорий Википедии от шума перед keywords/themes.

    enrich.clean_categories применяется здесь (а не при сборе кэша), чтобы
    одинаково чистить и wikidata-, и OSM-ветки (чёрный список — enrich.CATEGORY_BLACKLIST).
    Префикс «Категория:» убираем: он служебный и только засоряет выдачу.
    """
    from enrich import clean_categories  # тот же источник правил

    return [c.removeprefix("Категория:").strip() for c in clean_categories(categories)]


# --- OSM-объекты ---------------------------------------------------------------

def osm_objects() -> list[dict]:
    pois = load_json(RAW / "osm" / "pois.json", []) or []
    links = load_json(WIKIDATA_DIR / "osm_links.json", {}) or {}
    entities = load_json(WIKIDATA_DIR / "entities.json", {}) or {}
    street_labels = load_json(WIKIDATA_DIR / "street_labels.json", {}) or {}
    out: list[dict] = []

    for p in pois:
        tags = p.get("tags", {})
        name = tags.get("name") or tags.get("name:ru") or tags.get("name:en")
        if not name:
            continue
        sid = f"{p['element']}/{p['id']}"

        street = tags.get("addr:street")
        house = tags.get("addr:housenumber")
        address = ", ".join(x for x in (street, house) if x) or None

        fee = tags.get("fee")
        is_free = True if fee == "no" else (False if fee == "yes" else None)

        year = None
        if tags.get("start_date"):
            m = re.search(r"(1[6-9]\d{2}|20\d{2})", tags["start_date"])
            year = int(m.group(1)) if m else None

        description = tags.get("description") or None
        long_description = None
        short_description = None
        website = tags.get("website") or tags.get("contact:website") or None
        image_url = (tags.get("image") if str(tags.get("image", "")).startswith("http") else None)
        keywords = themes = None

        # обогащение по явной ссылке OSM -> Wikidata (enrich.py)
        qid = links.get(sid)
        source, source_id = "openstreetmap", sid
        if qid and qid in entities:
            ent = entities[qid]
            art = article_for(f"osm:{sid}")
            extract = (art or {}).get("extract")
            categories = clean_kw((art or {}).get("categories") or [])

            if extract:
                long_description = extract[:MAX_DESCRIPTION]
                if not description:
                    description = split_first_sentence(extract)
            if not short_description:
                short_description = pick_description(ent)
            if not website:
                w = claim_value(ent, "P856")
                website = (w.get("text") if isinstance(w, dict) else w) or None
            if not address:
                st = claim_value(ent, "P669")
                hn = claim_value(ent, "P670")
                if st and isinstance(st, dict) and street_labels.get(st.get("id")):
                    sn = street_labels[st["id"]]
                    address = f"{sn}, {hn}" if hn else sn
            if not image_url:
                img = claim_value(ent, "P18")
                if img:
                    image_url = (f"https://commons.wikimedia.org/wiki/Special:FilePath/"
                                 f"{img.replace(' ', '_')}?width=900")
            if year is None:
                year = parse_year(claim_value(ent, "P571"))
            if categories:
                keywords = categories
                themes = themes_from(categories)
            source = "openstreetmap+wikidata"
            source_id = f"{sid};{qid}"

        # Мемориальные доски (memorial=plaque) — узкий подтип: помечаем ключевым
        # словом, чтобы UI мог скрывать их по умолчанию (баланс категорий, §8).
        if tags.get("memorial") == "plaque":
            keywords = list(keywords or [])
            if "мемориальная доска" not in keywords:
                keywords.append("мемориальная доска")

        out.append({
            "name": name,
            "category": p.get("category") or "monument",
            "description": description,
            "long_description": long_description,
            "short_description": short_description,
            "address": address,
            "latitude": p["lat"],
            "longitude": p["lon"],
            "website": website,
            "phone": tags.get("phone") or tags.get("contact:phone") or None,
            "opening_hours": tags.get("opening_hours") or None,
            "working_hours": None,
            "themes": themes,
            "period": None,
            "art_types": None,
            "keywords": keywords,
            "metro_stations": None,
            "year_established": year,
            "is_free": is_free,
            "ticket_price": tags.get("charge") or None,
            "image_url": image_url,
            "source": source,
            "source_id": source_id,
            "wikipedia_title": None,
        })
    return out


# --- фильтр и дедупликация ------------------------------------------------------

def in_district(lon: float, lat: float) -> bool:
    poly = load_json(POLYGON)
    if not poly:
        return True
    return any(point_in_polygon(lon, lat, pl) for pl in poly["polygons"])


def merge(primary: dict, secondary: dict) -> dict:
    """Слияние дубликата: primary — Wikidata, secondary — OSM."""
    for field in ("address", "website", "phone", "opening_hours", "ticket_price",
                  "is_free", "year_established", "image_url", "short_description",
                  "description", "long_description", "themes", "keywords"):
        if primary.get(field) in (None, "") and secondary.get(field) not in (None, ""):
            primary[field] = secondary[field]
    if secondary.get("source") and secondary["source"] not in primary.get("source", ""):
        primary["source"] = f"{primary['source']}+{secondary['source']}"
        primary["source_id"] = f"{primary['source_id']};{secondary['source_id']}"
    return primary


def dedupe(objects: list[dict]) -> tuple[list[dict], int]:
    """Удаление дубликатов внутри и между источниками (§8)."""
    kept: list[dict] = []
    removed = 0
    for obj in sorted(objects, key=lambda o: (o["source"] != "wikidata",
                                              -(o.get("year_established") or 0))):
        dup = None
        for k in kept:
            if k["category"] != obj["category"]:
                continue
            dist = haversine_m(k["latitude"], k["longitude"], obj["latitude"], obj["longitude"])
            if dist > MERGE_RADIUS_M:
                continue
            if (name_similarity(k["name"], obj["name"]) >= 0.5
                    or normalize_name(k["name"]) == normalize_name(obj["name"])):
                dup = k
                break
        if dup is None:
            kept.append(obj)
            continue
        removed += 1
        if dup["source"] == "wikidata" and obj["source"] == "openstreetmap":
            merge(dup, obj)
        elif dup["source"] == "openstreetmap" and obj["source"] == "wikidata":
            merge(obj, dup)
            kept[kept.index(dup)] = obj
        else:
            # внутри одного источника (или уже связанных записей) — оставляем более полную
            richer = obj if sum(v not in (None, "") for v in obj.values()) > \
                           sum(v not in (None, "") for v in dup.values()) else dup
            kept[kept.index(dup)] = richer
    return kept, removed


def main() -> None:
    load_support()
    wd = wikidata_objects()
    osm = osm_objects()
    print(f"Загружено: wikidata={len(wd)}, osm={len(osm)}")

    before = len(wd) + len(osm)
    both = [o for o in wd + osm if in_district(o["longitude"], o["latitude"])]
    print(f"Фильтр по полигону района (§10): {before} -> {len(both)}")

    merged, removed = dedupe(both)
    print(f"Дедупликация: -{removed} -> {len(merged)}")

    merged.sort(key=lambda o: (o["category"], normalize_name(o["name"])))
    for i, o in enumerate(merged, 1):
        o["id"] = i

    by_cat: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for o in merged:
        by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1
        by_src[o["source"]] = by_src.get(o["source"], 0) + 1

    save_json(OUT, merged)
    filled = {}
    for f in ("description", "long_description", "short_description", "address",
              "website", "phone", "opening_hours", "image_url", "year_established",
              "keywords", "themes"):
        filled[f] = sum(1 for o in merged if o.get(f))

    print(f"Итого: {len(merged)} объектов")
    print("по категориям:", by_cat)
    print("по источникам:", by_src)
    print("заполненность:", {k: f"{v} ({v * 100 // max(len(merged), 1)}%)"
                             for k, v in filled.items()})
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
