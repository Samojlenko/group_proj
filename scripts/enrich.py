"""Этап 2.4a: обогащение данных (§7, §8).

Что делает:
  1) связывает OSM-объекты с Wikidata по тегам `wikidata` и `wikipedia`
     (без эвристик «по названию» — только явные ссылки OSM);
  2) подтягивает подписи улиц (P669) для построения адресов;
  3) забирает из Википедии лид статьи и категории: описание (long description)
     и ключевые слова для семантического поиска (§27).

Всё кэшируется в data/raw/wikidata/ — повторный запуск идемпотентен.
Ничего не выдумывается: отсутствующие данные остаются None (§7).
"""

from __future__ import annotations

import json
import time
import urllib.parse

from etl_lib import PROCESSED, RAW, http_get, load_json, save_json

WD_API = "https://www.wikidata.org/w/api.php"
WIKI_API = {
    "ru": "https://ru.wikipedia.org/w/api.php",
    "en": "https://en.wikipedia.org/w/api.php",
}

WIKIDATA_DIR = RAW / "wikidata"
LINKS_PATH = WIKIDATA_DIR / "osm_links.json"
STREET_LABELS_PATH = WIKIDATA_DIR / "street_labels.json"
WIKI_CACHE_PATH = WIKIDATA_DIR / "wiki_cache.json"
ARTICLE_MAP_PATH = WIKIDATA_DIR / "article_map.json"

# категории, бесполезные для тематики/поиска (шум Википедии)
CATEGORY_BLACKLIST = (
    "википедия:", "wikipedia:", "cs1", "непроверен", "статьи с", "страницы с",
    "инфобокс", "иконки", "commons", "wikidata", "автозамены", "проекты",
    "битые", "требуют", "со источников", "локализованные", "статьи ",
    # индексы/служебные пометки ботов: «Категория:Музеи по алфавиту»,
    # «Категория:ПРО:АГЕО:Последняя правка…», «Страницы, использующие…»
    "по алфавиту", "про:агео", "последняя правка", "страницы, использующие",
    "использующие волшебные", "нумерация",
)

# подстроки тем (§7) — фильтрация реальных категорий, ничего не выдумывается
THEME_SUBSTRINGS = (
    "музе", "памятник", "мемориал", "искусств", "архитектур", "истори",
    "выставк", "галере", "скульпт", "живопис", "наследи", "культур", "зод",
)


def wiki_api(lang: str, params: dict) -> dict | None:
    q = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    url = f"{WIKI_API[lang]}?{q}"
    try:
        return json.loads(http_get(url, timeout=45, retries=3, backoff=(2, 5, 12)).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"    [!] wiki {lang} api: {exc}")
        return None


# --- 1. связка OSM -> Wikidata --------------------------------------------------

def resolve_wikipedia_tag(site_title: str, cache: dict) -> str | None:
    """'ru:Название' -> QID через wbgetentities(sites, titles)."""
    if site_title in cache:
        return cache[site_title]
    if ":" not in site_title:
        cache[site_title] = None
        return None
    site, title = site_title.split(":", 1)
    site_key = f"{site}wiki"
    data = wiki_api("ru", {
        "action": "wbgetentities", "sites": site_key, "titles": title,
        "props": "labels", "languages": "ru|en",
    })
    qid = None
    ents = (data or {}).get("entities", {})
    for ent in ents.values():
        if ent.get("id") and ent.get("id") != "-1":
            qid = ent["id"]
            break
    cache[site_title] = qid
    time.sleep(0.2)
    return qid


def build_links() -> dict[str, str]:
    """osm source_id ('node/123') -> QID Wikidata по явным тегам OSM."""
    existing = load_json(LINKS_PATH, {}) or {}
    pois = load_json(RAW / "osm" / "pois.json", []) or []
    wp_cache = load_json(WIKIDATA_DIR / "wikipedia_tag_cache.json", {}) or {}
    links = dict(existing)
    added = 0
    for p in pois:
        sid = f"{p['element']}/{p['id']}"
        tags = p.get("tags", {})
        qid = tags.get("wikidata")
        if not qid and tags.get("wikipedia"):
            qid = resolve_wikipedia_tag(tags["wikipedia"], wp_cache)
            save_json(WIKIDATA_DIR / "wikipedia_tag_cache.json", wp_cache)
        if qid and sid not in links:
            links[sid] = qid
            added += 1
    save_json(LINKS_PATH, links)
    save_json(WIKIDATA_DIR / "wikipedia_tag_cache.json", wp_cache)
    print(f"Связок OSM->Wikidata: {len(links)} (новых: {added})")
    fetch_linked_entities(links)
    return links


def fetch_linked_entities(links: dict[str, str]) -> None:
    """Детали Wikidata-сущностей, на которые ссылаются OSM-объекты."""
    entities = load_json(WIKIDATA_DIR / "entities.json", {}) or {}
    missing = sorted({q for q in links.values() if q and q not in entities})
    if not missing:
        return
    print(f"Загрузка {len(missing)} связанных сущностей Wikidata...")
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        data = http_get(
            f"{WD_API}?action=wbgetentities&ids={'|'.join(chunk)}"
            "&props=labels|descriptions|claims|sitelinks&languages=ru|en&format=json",
            timeout=60,
        ).decode("utf-8")
        entities.update(json.loads(data).get("entities", {}))
        save_json(WIKIDATA_DIR / "entities.json", entities)
        time.sleep(0.5)


# --- 2. подписи улиц для адресов -------------------------------------------------

def fetch_street_labels() -> dict[str, str]:
    """P669 (улица) -> русская подпись. Без подписи адрес остаётся None (§7)."""
    labels = load_json(STREET_LABELS_PATH, {}) or {}
    entities = load_json(WIKIDATA_DIR / "entities.json", {}) or {}
    wanted: set[str] = set()
    for ent in entities.values():
        for c in ent.get("claims", {}).get("P669", []):
            if c.get("mainsnak", {}).get("snaktype") == "value":
                v = c["mainsnak"]["datavalue"]["value"]
                if isinstance(v, dict) and v.get("id"):
                    wanted.add(v["id"])
    missing = sorted(w for w in wanted if w not in labels)
    if missing:
        print(f"Подписей улиц: нужно {len(wanted)}, загружаем {len(missing)}...")
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            data = http_get(
                f"{WD_API}?action=wbgetentities&ids={'|'.join(chunk)}"
                "&props=labels&languages=ru|en&format=json",
                timeout=60,
            ).decode("utf-8")
            for qid, ent in json.loads(data).get("entities", {}).items():
                lab = ent.get("labels", {}).get("ru") or ent.get("labels", {}).get("en")
                labels[qid] = lab["value"] if lab else None
            save_json(STREET_LABELS_PATH, labels)
            time.sleep(0.5)
    return labels


# --- 3. википедийские лиды и категории ------------------------------------------

def fetch_wiki(article_title: str, lang: str) -> dict | None:
    """Лид статьи (exintro) и категории одной страницей; кэш общий."""
    cache = load_json(WIKI_CACHE_PATH, {}) or {}
    key = f"{lang}:{article_title}"
    if key in cache:
        return cache[key]
    data = wiki_api(lang, {
        "action": "query", "prop": "extracts|categories",
        "explaintext": "1", "exintro": "1", "exsectionformat": "plain",
        "cllimit": "max", "redirects": "1", "titles": article_title,
    })
    result = None
    if data and data.get("query", {}).get("pages"):
        page = data["query"]["pages"][0]
        if not page.get("missing"):
            extract = (page.get("extract") or "").strip() or None
            cats = [c["title"] for c in page.get("categories", [])]
            result = {"extract": extract, "categories": cats, "lang": lang}
    cache[key] = result
    save_json(WIKI_CACHE_PATH, cache)
    time.sleep(0.2)
    return result


def clean_categories(cats: list[str]) -> list[str]:
    out = []
    for c in cats:
        low = c.lower()
        if any(b in low for b in CATEGORY_BLACKLIST):
            continue
        out.append(c)
        if len(out) >= 15:
            break
    return out


def themes_from_categories(cats: list[str]) -> list[str]:
    themes: list[str] = []
    for c in cats:
        low = c.lower()
        if any(s in low for s in THEME_SUBSTRINGS):
            themes.append(c)
        if len(themes) >= 5:
            break
    return themes


def collect_articles(links: dict[str, str]) -> list[tuple[str, str, str]]:
    """Список (obj_key, title, lang) для всех объектов со статьёй.

    obj_key: 'wd:Q...' для wikidata-объектов, 'osm:node/1' для OSM.
    """
    entities = load_json(WIKIDATA_DIR / "entities.json", {}) or {}
    articles: list[tuple[str, str, str]] = []

    def entity_article(qid: str) -> tuple[str, str] | None:
        sl = entities.get(qid, {}).get("sitelinks", {})
        if sl.get("ruwiki", {}).get("title"):
            return sl["ruwiki"]["title"], "ru"
        if sl.get("enwiki", {}).get("title"):
            return sl["enwiki"]["title"], "en"
        return None

    # объекты из Wikidata-списков
    for qid, ent in entities.items():
        if not ent.get("claims", {}).get("P625"):
            continue
        art = entity_article(qid)
        if art:
            articles.append((f"wd:{qid}", art[0], art[1]))

    # OSM-объекты: заголовок из тега wikipedia либо из связанной сущности
    pois = load_json(RAW / "osm" / "pois.json", []) or []
    for p in pois:
        sid = f"{p['element']}/{p['id']}"
        wp = p.get("tags", {}).get("wikipedia")
        if wp and ":" in wp:
            lang, title = wp.split(":", 1)
            if lang in WIKI_API:
                articles.append((f"osm:{sid}", title, lang))
                continue
        qid = links.get(sid)
        if qid:
            art = entity_article(qid)
            if art:
                articles.append((f"osm:{sid}", art[0], art[1]))

    # дедупликация по ключу
    seen: set[str] = set()
    uniq = []
    for key, title, lang in articles:
        if key in seen:
            continue
        seen.add(key)
        uniq.append((key, title, lang))
    return uniq


def main() -> None:
    print("1) связка OSM <-> Wikidata по тегам")
    links = build_links()

    print("2) подписи улиц (адреса)")
    fetch_street_labels()

    print("3) википедийские лиды и категории")
    articles = collect_articles(links)
    print(f"   статей к проверке: {len(articles)}")
    article_map: dict[str, str] = {}
    cache = load_json(WIKI_CACHE_PATH, {}) or {}
    fetched = 0
    for key, title, lang in articles:
        cache_key = f"{lang}:{title}"
        article_map[key] = cache_key
        if cache_key not in cache:
            fetch_wiki(title, lang)
            fetched += 1
            cache = load_json(WIKI_CACHE_PATH, {}) or {}
    save_json(ARTICLE_MAP_PATH, article_map)
    cache = load_json(WIKI_CACHE_PATH, {}) or {}
    ok = sum(1 for v in cache.values() if v)
    print(f"   кэш: {len(cache)} записей, со статьёй: {ok} (загружено новых: {fetched})")
    print(f"   карта объект->статья: {len(article_map)} -> {ARTICLE_MAP_PATH}")


if __name__ == "__main__":
    main()
