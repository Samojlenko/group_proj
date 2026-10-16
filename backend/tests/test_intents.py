"""Тесты интентов этапа B: расстояние, сравнение, прогулка, «открыто сейчас», аналитика.

Часы работы — чистые функции (`app/ai/hours.py`), интенты — через правила (§53)
и эндпоинт `/api/query` с моками SQL-слоя: тесты не зависят от БД и модели.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.ai.hours import MSK, is_open_now
from app.ai.rules import parse_rules
from app.ai.schemas_ai import StructuredQuery


def _msk(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    """Московское время; проверочные даты: 2026-07-06 — понедельник,
    2026-07-11 — суббота, 2026-07-12 — воскресенье."""
    return datetime(y, mo, d, h, mi, tzinfo=MSK)


# --- часы работы --------------------------------------------------------------

def test_sanity_check_weekdays():
    assert _msk(2026, 7, 6, 12, 0).weekday() == 0  # Пн
    assert _msk(2026, 7, 11, 12, 0).weekday() == 5  # Сб


def test_always_open():
    assert is_open_now("Круглосуточно") is True
    assert is_open_now("24/7") is True


def test_daily_window_boundaries():
    text = "Ежедневно 10:00-18:00"
    assert is_open_now(text, _msk(2026, 7, 6, 10, 0)) is True
    assert is_open_now(text, _msk(2026, 7, 6, 12, 0)) is True
    assert is_open_now(text, _msk(2026, 7, 6, 18, 0)) is False  # закрытие в 18:00
    assert is_open_now(text, _msk(2026, 7, 6, 19, 0)) is False


def test_weekday_range():
    text = "Вт-Вс 11:00-20:00"
    assert is_open_now(text, _msk(2026, 7, 6, 12, 0)) is False  # понедельник закрыт
    assert is_open_now(text, _msk(2026, 7, 12, 12, 0)) is True  # воскресенье открыто


def test_exception_day():
    text = "Ежедневно 10:00-18:00, кроме понедельника"
    assert is_open_now(text, _msk(2026, 7, 6, 12, 0)) is False
    assert is_open_now(text, _msk(2026, 7, 7, 12, 0)) is True


def test_segments_weekday_weekend():
    text = "Пн-Пт 9:00-19:00; Сб-Вс 11:00-17:00"
    assert is_open_now(text, _msk(2026, 7, 6, 9, 30)) is True
    assert is_open_now(text, _msk(2026, 7, 11, 12, 0)) is True
    assert is_open_now(text, _msk(2026, 7, 11, 18, 0)) is False


def test_english_osm_notation():
    assert is_open_now("Mo-Su 10:30-18:00", _msk(2026, 7, 6, 12, 0)) is True
    assert is_open_now("Mo-Fr 09:00-18:00", _msk(2026, 7, 11, 12, 0)) is False


def test_osm_off_days():
    # OSM-нотация «Su,Mo off» и «We[3] off» — закрытые дни
    text = "Mo-Su 10:00-20:00; Mo off"
    assert is_open_now(text, _msk(2026, 7, 6, 12, 0)) is False  # Пн закрыт
    assert is_open_now(text, _msk(2026, 7, 7, 12, 0)) is True   # Вт открыт
    assert is_open_now("10:00-17:30; We[3] off", _msk(2026, 7, 8, 12, 0)) is False  # Ср
    assert is_open_now("10:00-17:30; We[3] off", _msk(2026, 7, 9, 12, 0)) is True   # Чт


def test_russian_s_do_and_lunch_break():
    assert is_open_now("с 11:00 до 20:00", _msk(2026, 7, 6, 12, 0)) is True
    text = "10:00-13:00, 14:00-18:00"
    assert is_open_now(text, _msk(2026, 7, 6, 13, 30)) is False  # перерыв
    assert is_open_now(text, _msk(2026, 7, 6, 14, 30)) is True


def test_overnight_range():
    text = "20:00-02:00"
    assert is_open_now(text, _msk(2026, 7, 6, 23, 0)) is True
    assert is_open_now(text, _msk(2026, 7, 6, 1, 0)) is True
    assert is_open_now(text, _msk(2026, 7, 6, 12, 0)) is False


def test_unparsed_is_unknown():
    assert is_open_now(None) is None
    assert is_open_now("") is None
    assert is_open_now("по предварительной записи") is None
    assert is_open_now("по расписанию музея") is None


# --- схема: новые действия и поля ---------------------------------------------

def test_distance_requires_two_places():
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({"action": "distance", "place": "Эрмитаж"})
    sq = StructuredQuery.model_validate(
        {"action": "distance", "place": "Эрмитаж", "place2": "Русский музей"}
    )
    assert sq.place2 == "Русский музей"


def test_walk_defaults():
    sq = StructuredQuery.model_validate({"action": "walk", "duration_minutes": 60})
    assert sq.radius_meters == 3000.0  # 60 мин × 80 м/мин = 4800 → ограничение 3000
    assert sq.count == 4
    sq = StructuredQuery.model_validate({"action": "walk"})
    assert sq.radius_meters == 1500.0
    assert sq.count == 6


def test_walk_keeps_explicit_radius_and_count():
    sq = StructuredQuery.model_validate(
        {"action": "walk", "duration_minutes": 30, "radius_meters": 800, "count": 5}
    )
    assert sq.radius_meters == 800.0
    assert sq.count == 5


def test_analytics_metric_default():
    sq = StructuredQuery.model_validate({"action": "analytics"})
    assert sq.metric == "oldest"


def test_open_now_coerced_from_string():
    sq = StructuredQuery.model_validate({"action": "list", "open_now": "true"})
    assert sq.open_now is True
    sq = StructuredQuery.model_validate({"action": "list", "open_now": None})
    assert sq.open_now is None


# --- правила: разбор новых интентов -------------------------------------------

def test_rules_distance():
    sq = parse_rules("сколько идти от Эрмитажа до Русского музея")
    assert sq is not None
    assert sq.action == "distance"
    assert sq.place == "Эрмитажа"
    assert sq.place2 == "Русского музея"


def test_rules_compare():
    sq = parse_rules("сравни Эрмитаж и Русский музей")
    assert sq is not None
    assert sq.action == "compare"
    assert sq.place == "Эрмитаж"
    assert sq.place2 == "Русский музей"


def test_rules_walk_with_duration_and_place():
    sq = parse_rules("погулять час от Дворцовой площади")
    assert sq is not None
    assert sq.action == "walk"
    assert sq.duration_minutes == 60
    assert sq.place == "Дворцовой площади"
    assert sq.radius_meters == 3000.0  # 60 × 80 = 4800 → предел


def test_rules_walk_two_hours_word_number():
    sq = parse_rules("что успеть за два часа")
    assert sq is not None
    assert sq.action == "walk"
    assert sq.duration_minutes == 120


def test_rules_analytics_oldest():
    sq = parse_rules("самый старый памятник")
    assert sq is not None
    assert sq.action == "analytics"
    assert sq.metric == "oldest"
    assert sq.categories == ["monument"]


def test_rules_open_now_flag():
    sq = parse_rules("какие музеи открыты сейчас рядом с Эрмитажем")
    assert sq is not None
    assert sq.action == "nearby"
    assert sq.open_now is True


def test_rules_regular_queries_do_not_get_open_flag():
    assert parse_rules("музеи рядом с Эрмитажем").open_now is None
    assert parse_rules("покажи галереи").open_now is None


# --- эндпоинт: новые интенты --------------------------------------------------

def _obj(i: int, name: str, category: str = "museum", **extra) -> dict:
    return {
        "id": i, "name": name, "category": category,
        "latitude": 59.9, "longitude": 30.3, "source": "test", **extra,
    }


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.ai import sessions
    from app.config import Settings, get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    # агент выключен: классические пути должны быть детерминированными,
    # у агента — свои тесты в test_agent.py (LLM там подменяется)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, ai_enabled=True, ai_agent_enabled=False
    )
    # по умолчанию — путь правил (LLM недоступен): интенты не зависят от модели
    monkeypatch.setattr(ai_api, "available", lambda s: False)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_settings, None)
    get_settings.cache_clear()
    sessions.store.clear_all()


def _no_llm(monkeypatch):
    from app.api import ai as ai_api

    def boom(*args, **kwargs):
        raise AssertionError("интент должен разбираться без LLM")

    monkeypatch.setattr(ai_api, "parse_with_fallback", boom)


def test_endpoint_distance(monkeypatch, client):
    from app.api import ai as ai_api

    places = {
        "Эрмитажа": {"latitude": 59.9398, "longitude": 30.3146,
                     "label": "Государственный Эрмитаж", "source": "cultural_object",
                     "object_id": 1},
        "Русского музея": {"latitude": 59.9386, "longitude": 30.3324,
                           "label": "Русский музей", "source": "cultural_object",
                           "object_id": 2},
    }
    monkeypatch.setattr(ai_api, "resolve_place", lambda db, s, name: places[name])
    monkeypatch.setattr(ai_api.q, "distance_between_points",
                        lambda db, la1, lo1, la2, lo2: 1500.0)
    monkeypatch.setattr(ai_api.q, "get_object",
                        lambda db, oid: _obj(oid, f"Объект {oid}"))
    _no_llm(monkeypatch)

    res = client.post("/api/query", json={"query": "сколько идти от Эрмитажа до Русского музея"})
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "distance"
    assert "1,5 км" in body["explanation"]
    assert "19 мин" in body["explanation"]  # 1500 м / 80 м/мин
    assert [i["id"] for i in body["items"]] == [1, 2]


def test_endpoint_compare(monkeypatch, client):
    from app.api import ai as ai_api

    places = {
        "Эрмитаж": {"latitude": 59.9398, "longitude": 30.3146, "label": "Эрмитаж",
                    "source": "cultural_object", "object_id": 1},
        "Русский музей": {"latitude": 59.9386, "longitude": 30.3324, "label": "Русский музей",
                          "source": "cultural_object", "object_id": 2},
    }
    objs = {
        1: _obj(1, "Эрмитаж", year_established=1764, is_free=False, ticket_price="500 ₽",
                address="Дворцовая пл., 2"),
        2: _obj(2, "Русский музей", year_established=1898, is_free=False,
                ticket_price="450 ₽", address="Инженерная ул., 4"),
    }
    monkeypatch.setattr(ai_api, "resolve_place", lambda db, s, name: places[name])
    monkeypatch.setattr(ai_api.q, "distance_between_points",
                        lambda db, la1, lo1, la2, lo2: 1100.0)
    monkeypatch.setattr(ai_api.q, "get_object", lambda db, oid: objs.get(oid))
    _no_llm(monkeypatch)

    res = client.post("/api/query", json={"query": "сравни Эрмитаж и Русский музей"})
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "compare"
    assert body["total"] == 2
    assert "старше на 134 года" in body["explanation"]
    assert "1,1 км" in body["explanation"]
    assert [i["id"] for i in body["items"]] == [1, 2]


def test_endpoint_compare_requires_objects(monkeypatch, client):
    from app.api import ai as ai_api

    places = {
        "Эрмитаж": {"latitude": 59.9398, "longitude": 30.3146, "label": "Эрмитаж",
                    "source": "cultural_object", "object_id": 1},
        # «сравни Эрмитаж и Дворцовую площадь» — резолвер в реальности разруливает
        # падежи сам; здесь карта форм задана явно
        "Дворцовую площадь": {"latitude": 59.9390, "longitude": 30.3150,
                              "label": "Дворцовая площадь", "source": "location"},
        "Дворцовая площадь": {"latitude": 59.9390, "longitude": 30.3150,
                              "label": "Дворцовая площадь", "source": "location"},
    }
    monkeypatch.setattr(ai_api, "resolve_place", lambda db, s, name: places[name])
    monkeypatch.setattr(ai_api.q, "distance_between_points", lambda *a, **kw: 100.0)
    monkeypatch.setattr(ai_api.q, "get_object", lambda db, oid: _obj(oid, "Эрмитаж"))
    _no_llm(monkeypatch)

    res = client.post("/api/query", json={"query": "сравни Эрмитаж и Дворцовую площадь"})
    assert res.status_code == 422
    assert "не объект сервиса" in res.json()["detail"]


def test_endpoint_walk(monkeypatch, client):
    from app.api import ai as ai_api

    route = {
        "start": {"latitude": 59.94, "longitude": 30.32},
        "radius_meters": 3000.0,
        "count": 2,
        "total_meters": 700.0,
        "walk_minutes": 9,
        "items": [
            {"id": 1, "name": "А", "category": "museum", "latitude": 59.941,
             "longitude": 30.321, "leg_meters": 320.0, "cumulative_meters": 320.0,
             "distance_from_start_meters": None},
            {"id": 2, "name": "Б", "category": "monument", "latitude": 59.942,
             "longitude": 30.322, "leg_meters": 380.0, "cumulative_meters": 700.0,
             "distance_from_start_meters": None},
        ],
        "polyline": [[59.94, 30.32], [59.941, 30.321], [59.942, 30.322]],
    }
    monkeypatch.setattr(ai_api, "resolve_place", lambda db, s, name: {
        "latitude": 59.9388, "longitude": 30.3162, "label": "Дворцовая площадь",
        "source": "location",
    })
    seen: dict = {}

    def fake_route(db, lat, lon, **kw):
        seen.update(kw)
        return route

    monkeypatch.setattr(ai_api, "build_walk_route", fake_route)
    monkeypatch.setattr(ai_api.q, "objects_by_ids", lambda db, ids: {
        1: _obj(1, "А"), 2: _obj(2, "Б", "monument"),
    })
    _no_llm(monkeypatch)

    res = client.post("/api/query", json={"query": "погулять час от Дворцовой площади"})
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "walk"
    assert body["route"]["total_meters"] == 700.0
    assert body["route"]["count"] == 2
    assert "2 остановки" in body["explanation"]
    assert "9 мин" in body["explanation"]
    # «час» из запроса дошёл до построителя: радиус посчитан от 60 минут
    assert seen["radius_m"] == 3000.0
    # остановки — в порядке обхода, с пройденными метрами
    assert [i["id"] for i in body["items"]] == [1, 2]
    assert body["items"][1]["distance_meters"] == 700.0


def test_endpoint_walk_needs_origin(monkeypatch, client):
    from app.api import ai as ai_api

    _no_llm(monkeypatch)
    res = client.post("/api/query", json={"query": "погулять час"})
    assert res.status_code == 422
    assert "от какой точки" in res.json()["detail"]


def test_endpoint_analytics_oldest(monkeypatch, client):
    from app.api import ai as ai_api

    seen: list[dict] = []

    def fake_by_year(db, **kw):
        seen.append(kw)
        return [
            _obj(10, "Медный всадник", "monument", year_established=1782),
            _obj(11, "Памятник Суворову", "monument", year_established=1801),
        ]

    monkeypatch.setattr(ai_api.q, "objects_by_year", fake_by_year)
    _no_llm(monkeypatch)

    res = client.post("/api/query", json={"query": "самый старый памятник"})
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "analytics"
    assert "«Медный всадник» (1782 г.)" in body["explanation"]
    assert seen[-1]["newest"] is False
    assert seen[-1]["categories"] == ["monument"]


def test_endpoint_open_now_filter(monkeypatch, client):
    from app.api import ai as ai_api
    import app.ai.hours as hours_mod

    # фиксируем «сейчас»: понедельник 12:00 — результат детерминирован
    monkeypatch.setattr(hours_mod, "_moscow_now", lambda: _msk(2026, 7, 6, 12, 0))
    items = [
        _obj(1, "Открытый", opening_hours="Ежедневно 10:00-18:00"),
        _obj(2, "Закрытый", opening_hours="Ежедневно 15:00-18:00"),
        _obj(3, "Без данных", opening_hours="по записи"),
    ]
    monkeypatch.setattr(ai_api.q, "list_objects", lambda db, **kw: (items, 3))
    _no_llm(monkeypatch)

    res = client.post("/api/query", json={"query": "музеи, открытые сейчас"})
    assert res.status_code == 200
    body = res.json()
    assert [i["id"] for i in body["items"]] == [1]
    assert body["total"] == 1
    assert "открыто сейчас: 1 из 3" in body["explanation"]
    assert "без распознанных часов: 1" in body["explanation"]
    assert body["structured"]["open_now"] is True


def test_endpoint_open_now_refinement(monkeypatch, client):
    """«а теперь открытые» — уточнение без LLM: прошлый список фильтруется по часам."""
    from app.api import ai as ai_api
    import app.ai.hours as hours_mod

    monkeypatch.setattr(hours_mod, "_moscow_now", lambda: _msk(2026, 7, 6, 12, 0))
    items = [
        _obj(1, "Открытый", opening_hours="Ежедневно 10:00-18:00"),
        _obj(2, "Закрытый", opening_hours="Ежедневно 15:00-18:00"),
    ]
    monkeypatch.setattr(ai_api.q, "list_objects", lambda db, **kw: (items, 2))
    _no_llm(monkeypatch)

    sid = client.post("/api/query", json={"query": "музеи"}).json()["session_id"]
    res = client.post("/api/query", json={"query": "а теперь открытые", "session_id": sid})
    assert res.status_code == 200
    body = res.json()
    assert [i["id"] for i in body["items"]] == [1]
    assert "открыто сейчас: 1 из 2" in body["explanation"]


def test_endpoint_llm_walk(monkeypatch, client):
    """Новое действие проходит и по пути LLM: StructuredQuery из парсера исполняется."""
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "available", lambda s: True)
    sq = StructuredQuery.model_validate(
        {"action": "walk", "place": "Эрмитаж", "duration_minutes": 30}
    )
    monkeypatch.setattr(ai_api, "parse_with_fallback", lambda *a, **kw: sq)
    monkeypatch.setattr(ai_api, "resolve_place", lambda db, s, name: {
        "latitude": 59.9398, "longitude": 30.3146, "label": "Эрмитаж",
        "source": "cultural_object", "object_id": 5,
    })
    route = {
        "start": {"latitude": 59.9398, "longitude": 30.3146},
        "radius_meters": 2400.0, "count": 1, "total_meters": 100.0, "walk_minutes": 1,
        "items": [{"id": 7, "name": "Спас на Крови", "category": "monument",
                   "latitude": 59.94, "longitude": 30.32, "leg_meters": 100.0,
                   "cumulative_meters": 100.0, "distance_from_start_meters": None}],
        "polyline": [[59.9398, 30.3146], [59.94, 30.32]],
    }
    monkeypatch.setattr(ai_api, "build_walk_route", lambda db, lat, lon, **kw: route)
    monkeypatch.setattr(ai_api.q, "objects_by_ids",
                        lambda db, ids: {7: _obj(7, "Спас на Крови", "monument")})

    res = client.post("/api/query", json={"query": "построй прогулку на полчаса от Эрмитажа"})
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "walk"
    assert body["route"]["count"] == 1
    assert "шаблонами" not in body["explanation"]  # разбор моделью, не fallback
