"""Тесты этапа H (идеи 28–29): заведения из OSM и погода open-meteo.

Разбор фраз — правилами (§53) без модели; SQL — на фейковой БД (как в
test_fun.py); погода — на подменённом HTTP-слое; эндпоинты — с моками
SQL-слоя и внешнего сервиса.
"""
from __future__ import annotations

import pytest

from app.ai import summary as ai_summary
from app.ai import weather as weather_mod
from app.ai.rules import parse_rules, weather_advice_wanted
from app.ai.schemas_ai import StructuredQuery
from app.config import Settings
from app.spatial import amenities as amenities_q


def _settings(**kw) -> Settings:
    kw.setdefault("ai_enabled", True)
    return Settings(_env_file=None, **kw)


def _obj(i: int, name: str, category: str = "museum", **extra) -> dict:
    return {
        "id": i, "name": name, "category": category,
        "latitude": 59.9, "longitude": 30.3, "source": "test", **extra,
    }


class _Result:
    def __init__(self, rows):  # noqa: ANN001
        self._rows = rows

    def mappings(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return self._rows


class _FakeDb:
    """Фейковая сессия: отдаёт заранее заданные ответы по порядку вызовов."""

    def __init__(self, responses: list):  # noqa: ANN001
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(sql), params or {}))
        return _Result(self._responses.pop(0) if self._responses else [])


# --- разбор фраз правилами (идея 28–29) ----------------------------------------


def test_rules_amenities_cafe():
    sq = parse_rules("Где выпить кофе рядом с Эрмитажем?")
    assert sq is not None and sq.action == "amenities"
    assert sq.amenity == "cafe" and sq.place == "Эрмитажем"


def test_rules_amenities_toilet_and_parking():
    toilet = parse_rules("Есть ли туалет рядом с Невским проспектом?")
    assert toilet is not None and toilet.action == "amenities"
    assert toilet.amenity == "toilet" and toilet.place == "Невским проспектом"
    parking = parse_rules("Где припарковаться рядом с Дворцовой площадью?")
    assert parking is not None and parking.action == "amenities"
    assert parking.amenity == "parking"


def test_rules_amenities_radius():
    sq = parse_rules("Кафе в радиусе 300 метров от Дворцовой площади")
    assert sq is not None and sq.action == "amenities"
    assert sq.amenity == "cafe" and sq.radius_meters == 300.0
    assert sq.place == "Дворцовой площади"


def test_rules_amenities_needs_context():
    # голое «Кафе» — это полнотекстовый поиск, а не запрос заведений рядом
    sq = parse_rules("Кафе")
    assert sq is None or sq.action != "amenities"


def test_rules_weather_facts_and_advice():
    facts = parse_rules("Какая сегодня погода в Петербурге?")
    assert facts is not None and facts.action == "weather"
    assert facts.advice is False
    advice = parse_rules("Что посмотреть сегодня, учитывая дождь?")
    assert advice is not None and advice.action == "weather"
    assert advice.advice is True


def test_rules_weather_with_place():
    sq = parse_rules("Что посмотреть в дождь рядом с Эрмитажем")
    assert sq is not None and sq.action == "weather"
    assert sq.advice is True and sq.place == "Эрмитажем"


def test_weather_advice_wanted():
    assert weather_advice_wanted("что посмотреть в дождь")
    assert weather_advice_wanted("куда сходить сегодня")
    assert not weather_advice_wanted("какая погода")


def test_schema_amenity_defaults_and_synonyms():
    sq = StructuredQuery(action="amenities")
    assert sq.radius_meters == 800.0 and sq.amenity is None
    assert StructuredQuery(action="amenities", amenity="Кафе").amenity == "cafe"
    assert StructuredQuery(action="amenities", amenity="WC").amenity == "toilet"
    assert StructuredQuery(action="amenities", amenity="Парковка").amenity == "parking"
    with pytest.raises(Exception):
        StructuredQuery(action="amenities", amenity="ресторан")


def test_schema_advice_boolish():
    assert StructuredQuery(action="weather", advice=None).advice is False
    assert StructuredQuery(action="weather", advice="да").advice is True
    assert StructuredQuery(action="weather", advice="false").advice is False


# --- SQL-слой заведений (идея 28) -----------------------------------------------


def test_amenities_near_sql_and_rounding():
    rows = [{
        "id": 601, "osm_type": "node", "osm_id": 1, "name": "Распутин",
        "category": "cafe", "address": None, "opening_hours": "24/7",
        "latitude": 59.94, "longitude": 30.32, "distance_meters": 210.14,
    }]
    db = _FakeDb([[{"n": 39}], rows])
    items, total = amenities_q.amenities_near(
        db, 59.94, 30.31, 800.0, category="cafe", limit=5
    )
    assert total == 39 and items[0]["distance_meters"] == 210.1
    sql, params = db.calls[1]
    assert "ST_DWithin" in sql and "ORDER BY distance_meters" in sql
    assert params["category"] == "cafe" and params["limit"] == 5
    assert params["point"].startswith("SRID=4326;POINT(30.31")
    # счётчик считает без LIMIT: в запросе total нет limit-параметров
    count_sql, count_params = db.calls[0]
    assert "count(*)" in count_sql and "limit" not in count_params


def test_amenities_geojson_filters_category():
    fc = {"type": "FeatureCollection", "features": []}
    db = _FakeDb([[{"fc": fc}]])
    out = amenities_q.amenities_geojson(db, category="toilet")
    assert out == fc
    sql, params = db.calls[0]
    assert "WHERE a.category = :category" in sql and params["category"] == "toilet"


# --- погода (идея 29) ------------------------------------------------------------


def test_describe_code():
    assert weather_mod.describe_code(61) == "небольшой дождь"
    assert weather_mod.describe_code(None) == "погода без описания"
    assert weather_mod.describe_code(123) == "необычная погода"


def test_is_rainy_variants():
    assert weather_mod.is_rainy({"precipitation": 0.3})
    assert weather_mod.is_rainy({"weather_code": 61})
    assert weather_mod.is_rainy({"weather_code": 71})  # снег — тоже под крышей
    assert weather_mod.is_rainy({"precipitation_probability": 60})
    assert not weather_mod.is_rainy(
        {"precipitation": 0.0, "weather_code": 2, "precipitation_probability": 10}
    )


def test_next_hours_probability():
    hourly = {
        "time": ["2026-10-17T10:00", "2026-10-17T11:00",
                 "2026-10-17T12:00", "2026-10-17T13:00"],
        "precipitation_probability": [10, 70, 40, 20],
    }
    assert weather_mod._next_hours_probability(hourly, "2026-10-17T11:30") == 70
    assert weather_mod._next_hours_probability(hourly, None) == 70
    assert weather_mod._next_hours_probability({}, "2026-10-17T11:30") is None


def test_normalize_weather():
    raw = {
        "current": {
            "time": "2026-10-17T12:15", "temperature_2m": 8.7,
            "apparent_temperature": 5.7, "precipitation": 0.0,
            "weather_code": 2, "wind_speed_10m": 14.0, "is_day": 1,
        },
        "hourly": {"time": ["2026-10-17T12:00"], "precipitation_probability": [5]},
        "daily": {
            "temperature_2m_max": [11.1], "temperature_2m_min": [6.3],
            "precipitation_probability_max": [20],
        },
    }
    w = weather_mod._normalize(raw)
    assert w["temperature"] == 8.7
    assert w["description"] == "переменная облачность"
    assert w["temp_min"] == 6.3 and w["temp_max"] == 11.1
    assert w["is_rainy"] is False and w["precipitation_probability"] == 5


def test_normalize_weather_without_temperature():
    assert weather_mod._normalize({"current": {}}) is None


def test_get_weather_caches(monkeypatch):
    weather_mod.reset_cache()
    calls = {"n": 0}
    fake = {"temperature": 5.0, "description": "ясно", "is_rainy": False}

    def fake_fetch(settings):  # noqa: ANN001
        calls["n"] += 1
        return fake

    monkeypatch.setattr(weather_mod, "_fetch", fake_fetch)
    settings = _settings(weather_cache_seconds=600)
    assert weather_mod.get_weather(settings) == fake
    assert weather_mod.get_weather(settings) == fake
    assert calls["n"] == 1  # второй вызов — из кэша
    weather_mod.get_weather(settings, force=True)
    assert calls["n"] == 2
    weather_mod.reset_cache()


def test_get_weather_stale_cache_on_failure(monkeypatch):
    weather_mod.reset_cache()
    fake = {"temperature": 3.0, "description": "снег", "is_rainy": True}
    monkeypatch.setattr(weather_mod, "_fetch", lambda s: fake)
    settings = _settings(weather_cache_seconds=0)  # кэш сразу «протухает»
    first = weather_mod.get_weather(settings)
    monkeypatch.setattr(weather_mod, "_fetch", lambda s: None)
    assert weather_mod.get_weather(settings) == first  # устаревшее лучше пустоты
    weather_mod.reset_cache()


def test_get_weather_disabled():
    assert weather_mod.get_weather(_settings(weather_enabled=False)) is None


# --- эндпоинт /api/query: заведения и погода ------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.ai import sessions
    from app.config import Settings as _S, get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: _S(
        _env_file=None, ai_enabled=True, ai_agent_enabled=False, ai_summary_enabled=False
    )
    monkeypatch.setattr(ai_api, "available", lambda s: False)
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


def test_endpoint_amenities(client, monkeypatch):
    from app.api import ai as ai_api

    places = {
        "Эрмитажем": {"label": "Эрмитаж", "latitude": 59.9398,
                      "longitude": 30.3146, "object_id": None},
    }
    monkeypatch.setattr(ai_api, "resolve_place", lambda db, s, name: places[name])
    fake_items = [{
        "id": 601, "name": "Распутин", "category": "cafe", "address": None,
        "opening_hours": "24/7", "latitude": 59.94, "longitude": 30.32,
        "distance_meters": 210.1,
    }]
    seen: dict = {}

    def fake_near(db, lat, lon, radius, *, category=None, limit=10, offset=0):  # noqa: ANN001
        seen.update(radius=radius, category=category, lat=lat, lon=lon)
        return fake_items, 39

    monkeypatch.setattr(ai_api.amenity_q, "amenities_near", fake_near)
    resp = client.post("/api/query", json={"query": "Где выпить кофе рядом с Эрмитажем?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "amenities"
    assert data["amenities"][0]["name"] == "Распутин"
    assert data["total"] == 39 and data["items"] == []
    assert seen["category"] == "cafe" and seen["radius"] == 800.0
    assert "Кафе" in data["explanation"] and "Эрмитаж" in data["explanation"]


def test_endpoint_amenities_without_origin(client):
    resp = client.post("/api/query", json={"query": "Где выпить кофе?"})
    assert resp.status_code == 422
    assert "точк" in resp.json()["detail"].lower()


def test_endpoint_weather_advice_indoor(client, monkeypatch):
    from app.api import ai as ai_api

    rainy = {
        "temperature": 7.2, "apparent_temperature": 5.0, "description": "дождь",
        "precipitation": 1.2, "precipitation_probability": 80, "weather_code": 63,
        "wind_speed": 5.0, "is_day": True, "is_rainy": True,
        "temp_min": 5.0, "temp_max": 9.0, "updated_at": "2026-10-17T09:00:00+00:00",
    }
    monkeypatch.setattr(ai_api, "get_weather", lambda s: rainy)
    seen: dict = {}

    def fake_rec(db, settings, **kw):  # noqa: ANN001
        seen.update(kw)
        return {"items": [{**_obj(10, "Эрмитаж"), "score": 0.9,
                           "factors": {"semantic": 0.9, "spatial": 0.0, "category": 0.0}}]}

    monkeypatch.setattr(ai_api, "run_recommend", fake_rec)
    resp = client.post("/api/query", json={"query": "Что посмотреть сегодня, учитывая дождь?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "weather"
    assert data["weather"]["is_rainy"] is True
    assert seen["categories"] == ["museum", "gallery"]
    assert data["items"][0]["name"] == "Эрмитаж"
    assert "под крышей" in data["explanation"]


def test_endpoint_weather_advice_outdoor(client, monkeypatch):
    from app.api import ai as ai_api

    dry = {
        "temperature": 12.0, "apparent_temperature": 11.0, "description": "ясно",
        "precipitation": 0.0, "precipitation_probability": 5, "weather_code": 0,
        "wind_speed": 3.0, "is_day": True, "is_rainy": False,
        "temp_min": 8.0, "temp_max": 14.0, "updated_at": "2026-10-17T09:00:00+00:00",
    }
    monkeypatch.setattr(ai_api, "get_weather", lambda s: dry)
    seen: dict = {}

    def fake_rec(db, settings, **kw):  # noqa: ANN001
        seen.update(kw)
        return {"items": [{**_obj(20, "Памятник Суворову"), "score": 0.8,
                           "factors": {"semantic": 0.8, "spatial": 0.0, "category": 0.0}}]}

    monkeypatch.setattr(ai_api, "run_recommend", fake_rec)
    resp = client.post("/api/query", json={"query": "Что посмотреть сегодня в хорошую погоду?"})
    assert resp.status_code == 200
    data = resp.json()
    assert seen["categories"] == ["monument"]
    assert "гулять" in data["explanation"] or "прогулк" in data["explanation"]


def test_endpoint_weather_facts_only(client, monkeypatch):
    from app.api import ai as ai_api

    dry = {
        "temperature": 8.7, "apparent_temperature": 5.7, "description": "переменная облачность",
        "precipitation": 0.0, "precipitation_probability": 0, "weather_code": 2,
        "wind_speed": 14.0, "is_day": False, "is_rainy": False,
        "temp_min": 6.3, "temp_max": 11.1, "updated_at": "2026-10-17T09:00:00+00:00",
    }
    monkeypatch.setattr(ai_api, "get_weather", lambda s: dry)
    resp = client.post("/api/query", json={"query": "Какая сегодня погода в Петербурге?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "weather"
    assert data["total"] == 0 and data["items"] == []
    assert "Сейчас в Петербурге" in data["explanation"]
    assert "+9" in data["explanation"]  # округление 8.7 до +9°


def test_endpoint_weather_unavailable(client, monkeypatch):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "get_weather", lambda s: None)
    resp = client.post("/api/query", json={"query": "Какая сегодня погода?"})
    assert resp.status_code == 503
    assert "погод" in resp.json()["detail"].lower()


# --- факты резюме (идеи 16, 28, 29) ----------------------------------------------


def test_build_facts_amenities_and_weather():
    result = {
        "action": "amenities",
        "explanation": "Кафе в радиусе 800 м",
        "structured": {"categories": []},
        "total": 39,
        "items": [],
        "amenities": [{
            "id": 601, "name": "Распутин", "category": "cafe",
            "distance_meters": 210.14, "opening_hours": "24/7", "address": None,
        }],
        "weather": {
            "temperature": 7.2, "description": "дождь", "is_rainy": True,
            "temp_min": 5.0, "temp_max": 9.0, "precipitation_probability": 80,
        },
    }
    facts = ai_summary.build_facts(result)
    assert facts["заведения"][0]["название"] == "Распутин"
    assert facts["заведения"][0]["тип"] == "Кафе"
    assert facts["заведения"][0]["расстояние_м"] == 210
    assert facts["заведения"][0]["часы"] == "24/7"
    assert facts["погода"]["дождь"] is True
    assert "+7" in facts["погода"]["сейчас"]
    assert facts["погода"]["днём"] == "+5…+9°"


def test_validate_summary_rejects_cjk_and_json():
    facts = '{"температура": 7}'
    assert not ai_summary.validate_summary(
        "Сейчас +7°, дождь. 預期的 JSON 格式导致信息丢失", facts
    )
    assert not ai_summary.validate_summary("Вот факты в JSON: сейчас +7 градусов", facts)
    assert ai_summary.validate_summary("Сейчас в Петербурге +7 градусов и дождь", facts)


# --- SSE-сериализация: Decimal из SQL (нашлось вживую на ветке погоды) -------------


def test_sse_serializes_decimal_and_dates():
    """Кадр result не должен падать на Decimal/датах: поток обходит Pydantic."""
    import json as _json
    from datetime import date, datetime
    from decimal import Decimal

    from app.api.ai import _sse

    frame = _sse("result", {
        "score": Decimal("0.75"),
        "day": date(2026, 10, 17),
        "at": datetime(2026, 10, 17, 9, 0),
    })
    assert frame.startswith("event: result\n")
    payload = _json.loads(frame.split("data: ", 1)[1])
    assert payload["score"] == 0.75
    assert payload["day"] == "2026-10-17"
    assert payload["at"].startswith("2026-10-17T09:00")


def test_recommend_items_json_safe_with_decimal_scores():
    """sem_score/spatial_score из SQL — Decimal: рекомендации обязаны быть JSON-safe."""
    import json as _json
    from decimal import Decimal

    from app.ai.recommend import recommend as run_recommend

    row = {
        "id": 1, "name": "Музей А", "category": "museum", "latitude": 59.9,
        "longitude": 30.3, "source": "test", "distance_m": Decimal("500.4"),
        "sem_score": Decimal("0.0"), "spatial_score": Decimal("0.5"),
        "score": Decimal("0.7"),
    }
    out = run_recommend(
        _FakeDb([[row]]), _settings(),
        categories=["museum"], latitude=59.9, longitude=30.3, limit=5,
    )
    item = out["items"][0]
    assert item["sem_score"] == 0.0 and item["spatial_score"] == 0.5
    assert item["distance_meters"] == 500.4
    assert _json.dumps(out, ensure_ascii=False)  # SSE: должно сериализоваться
