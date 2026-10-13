# Данные проекта

## Структура

- `raw/` — сырой кэш ответов источников (OSM API, Wikidata API). Не коммитится.
- `processed/` — нормализованный единый формат (`objects.json`, `locations.json`,
  `district.geojson`), который загружается в PostGIS через `scripts/import_data.py`.

## Источники (§8)

| Источник | Что берём | Идентификатор |
|---|---|---|
| OpenStreetMap (`api.openstreetmap.org`) | координаты, категории, адреса, теги | `osm:<node_id>` |
| Wikidata (`query.wikidata.org`) | описания на русском, сайты, годы основания, изображения | `wikidata:Q...` |

Каждая запись в БД содержит поля `source` и `source_id` — происхождение
записи можно определить в любой момент (§8).

Данные распространяются по лицензиями ODbL (OSM) и CC BY-SA (Wikidata),
атрибуция сохранена в `source`.

## Воспроизводимость

```bash
python scripts/fetch_osm.py      # этап 1: сбор из OSM API -> data/raw/
python scripts/fetch_wikidata.py # этап 2: обогащение из Wikidata
python scripts/prepare_data.py   # этап 3: очистка/нормализация/дедупликация -> data/processed/
python scripts/import_data.py    # этап 4: загрузка в PostGIS
```
