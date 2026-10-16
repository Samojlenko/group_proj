"""Тесты диалоговых сессий (§40): контекст уточнений, «ещё», «покажи N-й».

Сессии — надстройка над AI-слоем: уточнения («а теперь только музеи»)
разбираются правилами без обращения к LLM, поэтому тесты не зависят
от доступности модели.
"""

from __future__ import annotations

import time

import pytest

from app.ai import sessions
from app.ai.rules import detect_refinement, merge_refinement
from app.ai.schemas_ai import StructuredQuery


# --- detect_refinement: что считается уточнением ------------------------------

@pytest.mark.parametrize("text,expected", [
    ("а теперь только музеи", {"kind": "categories", "categories": ["museum"]}),
    ("теперь галереи", {"kind": "categories", "categories": ["gallery"]}),
    ("покажи мемориальные доски", {"kind": "categories", "categories": ["memorial"]}),
    ("доски", {"kind": "categories", "categories": ["memorial"]}),
    ("покажи ещё", {"kind": "more", "count": None}),
    ("дальше", {"kind": "more", "count": None}),
    ("ещё 5", {"kind": "more", "count": 5}),
    ("покажи 2-й", {"kind": "nth", "n": 2}),
    ("второй объект", {"kind": "nth", "n": 2}),
    ("5", {"kind": "nth", "n": 5}),
    ("в радиусе 500 метров", {"kind": "radius", "radius": 500.0}),
    ("радиус 1 км", {"kind": "radius", "radius": 1000.0}),
])
def test_detect_refinement_positive(text, expected):
    assert detect_refinement(text) == expected


@pytest.mark.parametrize("text", [
    # самостоятельные запросы не должны перехватываться как уточнения
    "музеи рядом с Эрмитажем",
    "галереи в радиусе 1 км от Невского проспекта",
    "покажи 5 музеев рядом с Эрмитажем",
    "какая завтра погода",
    "что посмотреть недалеко от Дворцовой площади",
])
def test_detect_refinement_negative(text):
    assert detect_refinement(text) is None


# --- merge_refinement: неуказанное наследуется --------------------------------

def test_merge_categories_keeps_place_and_action():
    prev = {"action": "nearby", "categories": ["gallery"], "place": "Эрмитаж",
            "radius_meters": 1000.0, "limit": 20}
    merged = merge_refinement(prev, {"kind": "categories", "categories": ["museum"]})
    assert merged["categories"] == ["museum"]
    assert merged["place"] == "Эрмитаж"
    assert merged["action"] == "nearby"
    assert merged["radius_meters"] == 1000.0
    # прошлый разбор не мутируется
    assert prev["categories"] == ["gallery"]


def test_merge_radius_promotes_list_to_spatial():
    merged = merge_refinement(
        {"action": "list", "categories": ["museum"], "limit": 20},
        {"kind": "radius", "radius": 500.0},
    )
    assert merged["action"] == "nearby"
    assert merged["radius_meters"] == 500.0


def test_merge_radius_keeps_place_origin():
    merged = merge_refinement(
        {"action": "list", "place": "Эрмитаж"}, {"kind": "radius", "radius": 500.0}
    )
    assert merged["action"] == "radius_search"
    assert merged["place"] == "Эрмитаж"


def test_merge_result_validates_as_structured_query():
    merged = merge_refinement(
        StructuredQuery.model_validate({"action": "list", "categories": ["museum"]}).model_dump(),
        {"kind": "categories", "categories": ["gallery"]},
    )
    sq = StructuredQuery.model_validate(merged)
    assert sq.categories == ["gallery"]
    assert sq.action == "list"


# --- SessionStore: LRU и TTL ---------------------------------------------------

def test_store_lru_eviction():
    store = sessions.SessionStore(max_sessions=2, ttl_seconds=3600)
    a = store.get_or_create(None)
    store.remember(a, sessions.Turn(query="музеи", structured={}, action="list"))
    b = store.get_or_create(None)
    c = store.get_or_create(None)
    assert b.id != c.id
    # самая старая сессия вытеснена: тот же id, но контекст пуст
    assert store.get_or_create(a.id).last is None


def test_store_ttl_expiry():
    store = sessions.SessionStore(max_sessions=10, ttl_seconds=0.0)
    s = store.get_or_create(None)
    store.remember(s, sessions.Turn(query="музеи", structured={}, action="list"))
    time.sleep(0.01)
    assert store.get_or_create(s.id).last is None


def test_store_keeps_only_last_turns():
    store = sessions.SessionStore()
    s = store.get_or_create(None)
    for i in range(12):
        store.remember(s, sessions.Turn(query=f"q{i}", structured={}, action="list"))
    assert len(s.turns) == sessions.SESSIONS_KEPT_TURNS
    assert s.last.query == "q11"


# --- эндпоинт: уточнения идут без LLM, контекст доходит до модели --------------

def _obj(i: int, name: str, category: str = "museum") -> dict:
    return {
        "id": i, "name": name, "category": category,
        "latitude": 59.9, "longitude": 30.3, "source": "test",
    }


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.config import Settings, get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, ai_enabled=True, ai_agent_enabled=False, ai_summary_enabled=False
    )
    # по умолчанию — путь правил (LLM недоступен): уточнения не зависят от модели
    monkeypatch.setattr(ai_api, "available", lambda s: False)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_settings, None)
    get_settings.cache_clear()
    sessions.store.clear_all()


def _no_llm(monkeypatch):
    from app.api import ai as ai_api

    def boom(*args, **kwargs):
        raise AssertionError("уточнение должно разбираться без LLM")

    monkeypatch.setattr(ai_api, "parse_with_fallback", boom)


def test_refinement_categories_fast_path(monkeypatch, client):
    from app.api import ai as ai_api

    calls: list[dict] = []

    def fake_list(db, **kwargs):
        calls.append(kwargs)
        return [_obj(1, "Русский музей")], 7

    monkeypatch.setattr(ai_api.q, "list_objects", fake_list)
    _no_llm(monkeypatch)

    res1 = client.post("/api/query", json={"query": "музеи"})
    assert res1.status_code == 200
    body1 = res1.json()
    sid = body1["session_id"]
    assert sid
    assert calls[-1]["categories"] == ["museum"]

    res2 = client.post("/api/query", json={"query": "только галереи", "session_id": sid})
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["session_id"] == sid  # тот же диалог
    assert calls[-1]["categories"] == ["gallery"]


def test_refinement_more_paginates(monkeypatch, client):
    from app.api import ai as ai_api

    offsets: list[int] = []

    def fake_list(db, **kwargs):
        off = kwargs.get("offset", 0)
        offsets.append(off)
        ids = list(range(off + 1, min(off + 20, 30) + 1))
        return [_obj(i, f"Объект {i}") for i in ids], 30

    monkeypatch.setattr(ai_api.q, "list_objects", fake_list)
    _no_llm(monkeypatch)

    sid = client.post("/api/query", json={"query": "музеи"}).json()["session_id"]

    res2 = client.post("/api/query", json={"query": "ещё", "session_id": sid})
    body2 = res2.json()
    assert offsets == [0, 20]
    assert [i["id"] for i in body2["items"]] == list(range(21, 31))
    assert "продолжение" in body2["explanation"]
    assert body2["offset"] == 20

    res3 = client.post("/api/query", json={"query": "ещё", "session_id": sid})
    body3 = res3.json()
    assert offsets[-1] == 30
    assert body3["items"] == []
    assert body3["offset"] == 30
    assert "больше объектов нет" in body3["explanation"]


def test_refinement_nth_selects_object(monkeypatch, client):
    from app.api import ai as ai_api

    names = {11: "А", 12: "Б", 13: "В"}
    monkeypatch.setattr(
        ai_api.q, "list_objects",
        lambda db, **kw: ([_obj(i, n) for i, n in names.items()], 3),
    )
    got: list[int] = []

    def fake_get(db, object_id):
        got.append(object_id)
        return _obj(object_id, names.get(object_id, "?"))

    monkeypatch.setattr(ai_api.q, "get_object", fake_get)
    _no_llm(monkeypatch)

    sid = client.post("/api/query", json={"query": "музеи"}).json()["session_id"]
    res = client.post("/api/query", json={"query": "покажи 2-й", "session_id": sid})
    assert res.status_code == 200
    body = res.json()
    assert got == [12]
    assert [i["id"] for i in body["items"]] == [12]
    assert "№2" in body["explanation"]


def test_refinement_nth_out_of_range(monkeypatch, client):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api.q, "list_objects",
                        lambda db, **kw: ([_obj(1, "А"), _obj(2, "Б")], 2))
    _no_llm(monkeypatch)

    sid = client.post("/api/query", json={"query": "музеи"}).json()["session_id"]
    res = client.post("/api/query", json={"query": "покажи 5-й", "session_id": sid})
    assert res.status_code == 422
    assert "№5" in res.json()["detail"]


def test_new_query_passes_dialog_context_to_llm(monkeypatch, client):
    from app.api import ai as ai_api

    seen: list[dict] = []

    def fake_parse(settings, q, lat=None, lon=None, **kwargs):
        seen.append({"query": q, **kwargs})
        return StructuredQuery.model_validate({"action": "list", "categories": ["monument"]})

    monkeypatch.setattr(ai_api, "available", lambda s: True)
    monkeypatch.setattr(ai_api, "parse_with_fallback", fake_parse)
    monkeypatch.setattr(ai_api.q, "list_objects",
                        lambda db, **kw: ([_obj(1, "Памятник", "monument")], 1))

    res1 = client.post("/api/query", json={"query": "музеи"})
    sid = res1.json()["session_id"]
    assert sid
    assert "context" not in seen[0]  # первый обмен — без контекста

    client.post("/api/query", json={"query": "а что рядом с Исаакием?", "session_id": sid})
    assert len(seen) == 2
    assert "context" in seen[1]
    assert "музеи" in seen[1]["context"]


def test_user_message_includes_dialog_context():
    from app.ai.parser import build_user_message

    msg = build_user_message("а теперь только музеи", None, None,
                             context='{"query": "музеи"}')
    assert "музеи" in msg
    assert "уточнени" in msg.lower()
