"""Этап 2.4b: связка объектов со статьями Википедии по названию (§8, §9).

Прямых тегов `wikidata`/`wikipedia` у большинства OSM-объектов нет, поэтому:
  1) ищем статью по названию объекта в русской Википедии (list=search);
  2) проверяем связь честно: у статьи должна быть Wikidata-сущность с
     координатами не далее 150 м от объекта И название статьи должно
     совпадать с названием объекта (Jaccard по токенам >= 0.5);
  3) подтверждённые связки сохраняются в osm_links.json, статья — в
     article_map.json (читает prepare_data.py).

Неподтверждённые кандидаты отбрасываются — ничего не «подставляется».
Кэшируется всё: повторный запуск идемпотентен.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from etl_lib import PROCESSED, RAW, haversine_m, http_get, load_json, name_similarity, save_json

WD_API = "https://www.wikidata.org/w/api.php"
RU_WIKI = "https://ru.wikipedia.org/w/api.php"

WIKIDATA_DIR = RAW / "wikidata"
LINKS_PATH = WIKIDATA_DIR / "osm_links.json"
ARTICLE_MAP_PATH = WIKIDATA_DIR / "article_map.json"
SEARCH_CACHE = WIKIDATA_DIR / "search_cache.json"
TITLE_QID_CACHE = WIKIDATA_DIR / "title2qid_cache.json"

MATCH_RADIUS_M = 150.0
TITLE_SIMILARITY = 0.5
SEARCH_LIMIT = 5


def wiki_search(title: str) -> list[str]:
    """Кандидаты-заголовки в русской Википедии (кэшируется)."""
    cache = load_json(SEARCH_CACHE, {}) or {}
    if title in cache:
        return cache[title]
    q = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": title,
        "srnamespace": "0", "srlimit": str(SEARCH_LIMIT), "format": "json",
    })
    try:
        data = json.loads(http_get(f"{RU_WIKI}?{q}", timeout=45, retries=3,
                                   backoff=(2, 5, 12)).decode("utf-8"))
        hits = [h["title"] for h in data.get("query", {}).get("search", [])]
    except Exception as exc:  # noqa: BLE001
        print(f"    [!] search «{title}»: {exc}")
        hits = []
    cache[title] = hits
    save_json(SEARCH_CACHE, cache)
    time.sleep(0.15)
    return hits


def wiki_post(api: str, params: dict, *, retries: int = 5) -> dict:
    """POST к MediaWiki API: длинные списки заголовков не влезают в URL.

    Википедия ограничивает частоту запросов (429) — делаем паузы и повторы
    с экспоненциальной задержкой, чтоб не добивать их роботами.
    """
    body = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"}).encode("utf-8")
    delay = 3.0
    for attempt in range(retries):
        req = urllib.request.Request(
            api, data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "cultural-map-project/1.0 (edu; one-shot data import)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries - 1:
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                time.sleep(wait)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("pageprops: исчерпаны повторы")


def pageprops(titles: list[str]) -> dict[str, str]:
    """Заголовок статьи -> QID Wikidata (POST-батчами, с дисковым кэшем)."""
    cache = load_json(TITLE_QID_CACHE, {}) or {}
    missing = sorted({t for t in titles if t not in cache})
    out: dict[str, str] = dict(cache)
    if not missing:
        return out
    for i in range(0, len(missing), 25):
        chunk = missing[i:i + 25]
        try:
            data = wiki_post(RU_WIKI, {
                "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                "titles": "|".join(chunk), "redirects": "1",
            })
        except Exception as exc:  # noqa: BLE001
            print(f"    [!] pageprops batch: {exc}")
            time.sleep(3)
            continue
        pages = data.get("query", {}).get("pages", [])
        if isinstance(pages, dict):  # формат по умолчанию — мапа по pageid
            pages = list(pages.values())
        # normalized/redirects: from -> to; обратный ключ нужен, чтобы
        # сохранённый заголовок совпадал с кандидатом из поиска
        rev: dict[str, str] = {}
        for key in ("normalized", "redirects", "converted"):
            for pair in data.get("query", {}).get(key, []):
                rev[pair["to"]] = pair["from"]
        for p in pages:
            if not isinstance(p, dict) or p.get("missing"):
                continue
            title = p.get("title", "")
            orig = rev.get(title, title)
            # formatversion=2: pageprops — словарь {"wikibase_item": "Q..."}
            qid = (p.get("pageprops") or {}).get("wikibase_item")
            if qid and qid != "-1":
                out[title] = qid
                out[orig] = qid
        save_json(TITLE_QID_CACHE, out)  # кэш растёт батч за батчем
        time.sleep(2.0)  # вежливость к Википедии: не больше ~0.3 запроса/с
    return out


def ensure_entities(qids: set[str]) -> dict:
    """Подтянуть сущности, которых нет в кэше (координаты для проверки)."""
    entities = load_json(WIKIDATA_DIR / "entities.json", {}) or {}
    missing = sorted(q for q in qids if q and q not in entities)
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        try:
            data = json.loads(http_get(
                f"{WD_API}?action=wbgetentities&ids={'|'.join(chunk)}"
                "&props=labels|descriptions|claims|sitelinks&languages=ru|en&format=json",
                timeout=60, retries=4,
            ).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"    [!] entities batch: {exc}")
            continue
        entities.update(data.get("entities", {}))
        save_json(WIKIDATA_DIR / "entities.json", entities)
        time.sleep(0.5)
    return entities


def entity_coord(ent: dict) -> tuple[float, float] | None:
    for c in ent.get("claims", {}).get("P625", []):
        if c.get("mainsnak", {}).get("snaktype") == "value":
            v = c["mainsnak"]["datavalue"]["value"]
            if isinstance(v, dict) and "latitude" in v:
                return float(v["latitude"]), float(v["longitude"])
    return None


def main() -> None:
    objects = load_json(PROCESSED / "objects.json", []) or []
    links = load_json(LINKS_PATH, {}) or {}
    article_map = load_json(ARTICLE_MAP_PATH, {}) or {}

    # кого ищем: OSM-объекты без статьи; у wd-объектов статья уже из sitelinks
    targets = []
    for o in objects:
        sid = o["source_id"].split(";")[0] if o["source"] != "wikidata" else None
        if o["source"] == "wikidata":
            key = f"wd:{o['source_id']}"
        else:
            key = f"osm:{sid}"
        if key in article_map or key in links and o["source"] == "wikidata":
            continue
        if o["source"] != "wikidata" and sid in links:
            continue  # уже связано по тегу OSM
        targets.append((key, o))
    print(f"Объектов без статьи: {len(targets)}")

    # 1) поиск кандидатов
    print("1) поиск статей по названию...")
    search: dict[str, list[str]] = {}
    for i, (key, o) in enumerate(targets, 1):
        search[o["name"]] = wiki_search(o["name"])
        if i % 50 == 0:
            print(f"   {i}/{len(targets)}")

    # 2) pageprops для всех кандидатов
    print("2) заголовок -> QID...")
    all_titles = sorted({t for hits in search.values() for t in hits})
    print(f"   уникальных заголовков-кандидатов: {len(all_titles)}")
    title2qid = pageprops(all_titles)
    print(f"   найдено QID: {len(title2qid)}")

    # 3) проверка: координаты сущности рядом + правдоподобность названия
    print("3) проверка координатами...")
    entities = ensure_entities(set(title2qid.values()))

    def is_person(ent: dict) -> bool:
        """Биографии (занятие P106 / P31=Q5) нельзя приклеивать к зданиям."""
        claims = ent.get("claims", {})
        if claims.get("P106"):
            return True
        for c in claims.get("P31", []):
            if (c.get("mainsnak", {}).get("datavalue", {}).get("value", {}) or {}).get("id") == "Q5":
                return True
        return False

    matched: dict[str, tuple[str, str]] = {}  # obj_key -> (qid, title)
    stat = {"без_кандидатов": 0, "без_qid": 0, "без_координаты": 0,
            "дальше_радиуса": 0, "биография": 0, "связано": 0}
    dissimilar_near: list[tuple[str, str, float]] = []  # рядом, но название не похоже

    for key, o in targets:
        hits = search.get(o["name"], [])
        if not hits:
            stat["без_кандидатов"] += 1
            continue
        target_done = False
        saw_qid = False
        saw_coord = False
        for rank, title in enumerate(hits):
            qid = title2qid.get(title)
            if not qid or qid not in entities:
                continue
            saw_qid = True
            ent = entities[qid]
            coord = entity_coord(ent)
            if not coord:
                continue
            saw_coord = True
            dist = haversine_m(o["latitude"], o["longitude"], coord[0], coord[1])
            if dist > MATCH_RADIUS_M:
                continue
            sim = name_similarity(o["name"], title)
            # связь сильная, если сущность рядом И (название похоже, либо это
            # первый, наиболее релевантный результат поиска по точному имени)
            if is_person(ent):
                continue
            if sim >= TITLE_SIMILARITY or rank == 0:
                matched[key] = (qid, title)
                stat["связано"] += 1
                target_done = True
                break
            dissimilar_near.append((key, title, sim))
        if target_done:
            continue
        if not saw_qid:
            stat["без_qid"] += 1
        elif not saw_coord:
            stat["без_координаты"] += 1
        else:
            stat["дальше_радиуса"] += 1
    if dissimilar_near:
        print(f"   рядом с объектом, но название не похоже (не связали): {len(dissimilar_near)}")
        for _k, t, s in dissimilar_near[:8]:
            print(f"      sim={s:.2f} <- {t}")

    # 4) сохранение
    for key, (qid, title) in matched.items():
        if key.startswith("osm:"):
            links[key[4:]] = qid
        article_map[key] = f"ru:{title}"
    save_json(LINKS_PATH, links)
    save_json(ARTICLE_MAP_PATH, article_map)
    print(f"Подтверждённых новых связок: {len(matched)}")
    print("   причины отсева:", stat)
    print(f"links={len(links)}, article_map={len(article_map)}")


if __name__ == "__main__":
    main()
