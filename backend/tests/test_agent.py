"""Тесты агента с function calling (этап C, идея 10).

Агент — надстройка над одиночным разбором: модель выбирает инструменты,
сервис исполняет их сам (§23) и склеивает результат. Здесь проверяются
перевод аргументов в StructuredQuery, цикл вызова инструментов, сшивка
шагов, самовосстановление после ошибки инструмента и откаты (§53).
"""

from __future__ import annotations

import json

import pytest

from app.ai.agent import AgentError, _tool_query, run_agent
from app.ai.client import AIServiceUnavailable
from app.ai.schemas_ai import StructuredQuery
from app.config import Settings
from app.schemas.ai_schemas import AIQueryRequest

# --- helpers -----------------------------------------------------------------


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, ai_enabled=True, ai_agent_enabled=True, **kw)


def _payload(query: str = "музеи рядом с Эрмитажем") -> AIQueryRequest:
    return AIQueryRequest(query=query)


def _obj(i: int, name: str, category: str = "museum", **extra) -> dict:
    return {
        "id": i, "name": name, "category": category,
        "latitude": 59.9, "longitude": 30.3, "source": "test", **extra,
    }


def _tool_call(tool: str, **args) -> dict:
    """Сообщение модели «вызови инструмент» (формат Ollama)."""
    return {"content": "", "tool_calls": [{"function": {"name": tool, "arguments": args}}]}


_DONE = {"content": "Готово.", "tool_calls": []}


def _chat_script(*messages):
    """Поддельный chat_tools: отдаёт заготовленные сообщения по порядку."""
    seen: list[list[dict]] = []
    queue = list(messages)

    def chat(settings, msgs, tools):
        seen.append(msgs)
        assert queue, "модель вызвана больше раз, чем задано сценарием"
        return queue.pop(0)

    chat.seen = seen  # type: ignore[attr-defined]
    return chat


def _result(action: str, items: list[dict] | None = None, *, total: int | None = None,
            explanation: str = "Результат", structured: dict | None = None, **extra) -> dict:
    items = items or []
    return {
        "action": action,
        "explanation": explanation,
        "structured": structured or {"action": action, "categories": []},
        "resolved_place": extra.pop("resolved_place", None),
        "total": total if total is not None else len(items),
        "items": items,
        "statistics": extra.pop("statistics", None),
        **extra,
    }


def _tool_messages(messages: list[dict]) -> list[str]:
    """Содержимое tool-сообщений из истории, отправленной модели."""
    return [m["content"] for m in messages if m.get("role") == "tool"]


# --- перевод аргументов инструментов в StructuredQuery -----------------------


def test_find_objects_place_and_radius():
    sq = _tool_query("find_objects", {"place": "Эрмитаж", "radius_meters": 800, "limit": 5})
    assert sq.action == "radius_search"
    assert sq.place == "Эрмитаж" and sq.radius_meters == 800 and sq.limit == 5


def test_find_objects_text_becomes_search():
    sq = _tool_query("find_objects", {"text": "Левша"})
    assert sq.action == "search" and sq.text == "Левша"


def test_find_objects_near_user():
    sq = _tool_query("find_objects", {"near_user": True})
    assert sq.action == "nearby"
    assert sq.radius_meters == 1000.0  # дефолт схемы


def test_find_objects_plain_list_and_open_now():
    sq = _tool_query("find_objects", {"categories": ["museum"], "open_now": True})
    assert sq.action == "list" and sq.open_now is True


def test_walk_defaults_from_duration():
    sq = _tool_query("walk", {"duration_minutes": 60})
    assert sq.action == "walk"
    assert sq.radius_meters == 3000.0  # 60 мин * 80 м/мин, кламп сверху
    assert sq.count == 4  # ~15 мин на остановку


def test_distance_and_compare_mapping():
    sq = _tool_query("distance", {"from_place": "A", "to_place": "B"})
    assert (sq.action, sq.place, sq.place2) == ("distance", "A", "B")
    sq = _tool_query("compare", {"first_place": "A", "second_place": "B"})
    assert (sq.action, sq.place, sq.place2) == ("compare", "A", "B")


def test_incomplete_tool_args_raise():
    with pytest.raises(Exception):
        _tool_query("distance", {"from_place": "A"})
    with pytest.raises(ValueError):
        _tool_query("teleport", {})


# --- цикл агента --------------------------------------------------------------


def test_agent_single_step():
    chat = _chat_script(_tool_call("find_objects", place="Эрмитаж", radius_meters=800), _DONE)

    def execute(db, settings, payload, sq, **kw):
        assert sq.action == "radius_search"
        return _result("radius_search", [_obj(1, "Музей А")],
                       explanation="Объекты в радиусе 800 м от «Эрмитаж»")

    out = run_agent(None, _settings(), _payload(), execute=execute, chat=chat)
    assert out["action"] == "radius_search"
    assert [i["name"] for i in out["items"]] == ["Музей А"]
    # одиночный шаг выглядит как обычный ответ, без «Шаг 1»
    assert out["explanation"] == "Объекты в радиусе 800 м от «Эрмитаж»"
    assert out["structured"]["agent_steps"][0]["tool"] == "find_objects"
    assert out["route"] is None and out["statistics"] is None


def test_agent_two_steps_compose():
    oldest = _obj(10, "Старый музей", year_established=1825)
    nearby = [_obj(11, "Объект А"), _obj(12, "Объект Б")]
    chat = _chat_script(
        _tool_call("analytics", metric="oldest", categories=["museum"]),
        _tool_call("find_objects", place="Старый музей", radius_meters=500),
        _DONE,
    )

    def execute(db, settings, payload, sq, **kw):
        if sq.action == "analytics":
            return _result("analytics", [oldest],
                           explanation="Самый старый музей — «Старый музей» (1825 г.)")
        assert sq.place == "Старый музей"
        return _result("radius_search", nearby,
                       explanation="Объекты в радиусе 500 м от «Старый музей»")

    out = run_agent(None, _settings(), _payload("самый старый музей и что рядом"), execute=execute, chat=chat)
    # items — из последнего шага, объяснение — цепочка «Шаг 1 / Шаг 2»
    assert [i["id"] for i in out["items"]] == [11, 12]
    assert out["explanation"].startswith("Шаг 1: Самый старый музей")
    assert "Шаг 2: Объекты в радиусе 500 м" in out["explanation"]
    assert [s["tool"] for s in out["structured"]["agent_steps"]] == ["analytics", "find_objects"]
    # результат первого шага ушёл модели, чтобы она могла продолжить
    tool_msgs = _tool_messages(chat.seen[1])
    assert "Старый музей" in tool_msgs[0]


def test_agent_recovers_from_tool_error():
    from fastapi import HTTPException

    chat = _chat_script(
        _tool_call("find_objects", place="Улица Пупкина"),
        _tool_call("find_objects", place="Эрмитаж"),
        _DONE,
    )

    def execute(db, settings, payload, sq, **kw):
        if sq.place == "Улица Пупкина":
            raise HTTPException(422, "такого места нет")
        return _result("nearby", [_obj(1, "Музей А")], explanation="Рядом с «Эрмитаж»")

    out = run_agent(None, _settings(), _payload(), execute=execute, chat=chat)
    assert out["items"][0]["id"] == 1
    # ошибка ушла модели как результат инструмента (§24)
    assert "такого места нет" in _tool_messages(chat.seen[1])[0]
    # черновики не попадают в объяснение — только успешный шаг
    assert out["explanation"] == "Рядом с «Эрмитаж»"
    assert len(out["structured"]["agent_steps"]) == 1


def test_agent_all_steps_failed_raises():
    from fastapi import HTTPException

    chat = _chat_script(
        _tool_call("find_objects", place="Нет такого"),
        _tool_call("find_objects", place="Нет такого"),
        _DONE,
    )

    def execute(db, settings, payload, sq, **kw):
        raise HTTPException(422, "такого места нет")

    with pytest.raises(AgentError):
        run_agent(None, _settings(), _payload(), execute=execute, chat=chat)


def test_agent_no_tool_calls_raises():
    chat = _chat_script({"content": "Не могу помочь.", "tool_calls": []})
    with pytest.raises(AgentError):
        run_agent(None, _settings(), _payload(), execute=lambda *a, **k: {}, chat=chat)


def test_agent_llm_error_propagates():
    def boom(*args, **kwargs):
        raise AIServiceUnavailable("сервер недоступен")

    with pytest.raises(AIServiceUnavailable):
        run_agent(None, _settings(), _payload(), execute=lambda *a, **k: {}, chat=boom)


def test_agent_max_steps():
    chat = _chat_script(
        _tool_call("statistics"), _tool_call("statistics"), _tool_call("statistics"),
    )

    def execute(db, settings, payload, sq, **kw):
        return _result("statistics", explanation="Статистика", total=1,
                       statistics={"total": 474})

    out = run_agent(None, _settings(ai_agent_max_steps=2), _payload("статистика"),
                    execute=execute, chat=chat)
    assert len(out["structured"]["agent_steps"]) == 2
    assert len(chat.seen) == 2  # третьего вызова модели не было


def test_agent_invalid_args_reported_to_model():
    chat = _chat_script(
        _tool_call("find_objects", categories=["инопланетяне"]),
        _tool_call("find_objects", categories=["museum"]),
        _DONE,
    )

    def execute(db, settings, payload, sq, **kw):
        assert sq.categories == ["museum"]
        return _result("list", [_obj(1, "Музей А")])

    out = run_agent(None, _settings(), _payload(), execute=execute, chat=chat)
    assert out["action"] == "list"
    assert "неизвестная категория" in _tool_messages(chat.seen[1])[0].lower()


def test_agent_unknown_tool_reported():
    chat = _chat_script(_tool_call("teleport", to="Марс"), _DONE)

    with pytest.raises(AgentError):
        run_agent(None, _settings(), _payload(), execute=lambda *a, **k: {}, chat=chat)
    # сообщение об ошибке инструмента ушло модели (второй её вызов)
    assert "неизвестный инструмент" in _tool_messages(chat.seen[1])[0]


def test_agent_tool_reply_caps_items():
    items = [_obj(i, f"Объект {i}") for i in range(1, 21)]
    chat = _chat_script(_tool_call("find_objects"), _DONE)

    def execute(db, settings, payload, sq, **kw):
        return _result("list", items, total=150)

    out = run_agent(None, _settings(), _payload(), execute=execute, chat=chat)
    payload_json = json.loads(_tool_messages(chat.seen[1])[0])
    assert len(payload_json["items"]) == 8  # контекст модели не разрастается
    assert "150" in payload_json["items_note"]
    assert len(out["items"]) == 20  # а пользователю — всё из результата шага


def test_agent_route_propagated():
    route = {
        "items": [{"id": 1, "cumulative_meters": 100.0}],
        "total_meters": 2500.0, "walk_minutes": 31, "radius_meters": 1500.0,
        "polyline": [],
    }
    chat = _chat_script(_tool_call("walk", duration_minutes=60), _DONE)

    def execute(db, settings, payload, sq, **kw):
        return _result("walk", [_obj(1, "Остановка")], route=route,
                       explanation="Прогулка: 1 остановка")

    out = run_agent(None, _settings(), _payload("прогулка"), execute=execute, chat=chat)
    assert out["route"] is route
    assert "walk_minutes" in _tool_messages(chat.seen[1])[0]


def test_agent_place_only(monkeypatch):
    monkeypatch.setattr(
        "app.ai.agent.resolve_place",
        lambda db, settings, name: {
            "label": "Дворцовая площадь", "latitude": 59.93863, "longitude": 30.31475,
            "source": "osm",
        },
    )
    chat = _chat_script(_tool_call("find_place", name="Дворцовая площадь"), _DONE)

    def execute(*args, **kwargs):
        raise AssertionError("find_place не должен идти через _execute")

    out = run_agent(None, _settings(), _payload("где Дворцовая площадь"), execute=execute, chat=chat)
    assert out["action"] == "other"
    assert "Дворцовая площадь" in out["explanation"]
    assert out["resolved_place"]["latitude"] == 59.93863


def test_agent_place_not_found_is_error(monkeypatch):
    from app.ai.resolver import PlaceNotFound

    def boom(db, settings, name):
        raise PlaceNotFound("нет такого")

    monkeypatch.setattr("app.ai.agent.resolve_place", boom)
    chat = _chat_script(_tool_call("find_place", name="Атлантида"), _DONE)
    with pytest.raises(AgentError):
        run_agent(None, _settings(), _payload(), execute=lambda *a, **k: {}, chat=chat)


# --- эндпоинт /api/query с агентом -------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.ai import sessions
    from app.config import get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, ai_enabled=True, ai_agent_enabled=True
    )
    monkeypatch.setattr(ai_api, "available", lambda s: True)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_settings, None)
    get_settings.cache_clear()
    sessions.store.clear_all()


def test_endpoint_agent_distance(monkeypatch, client):
    from app.api import ai as ai_api
    from app.ai import client as ai_client

    calls = {"n": 0}

    def fake_chat(settings, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool_call("distance", from_place="Эрмитаж", to_place="Русский музей")
        return _DONE

    monkeypatch.setattr(ai_client, "chat_tools", fake_chat)
    places = {
        "Эрмитаж": {"latitude": 59.9398, "longitude": 30.3146, "label": "Эрмитаж",
                    "source": "seed"},
        "Русский музей": {"latitude": 59.9386, "longitude": 30.3324, "label": "Русский музей",
                          "source": "seed"},
    }
    monkeypatch.setattr(ai_api, "resolve_place", lambda db, s, name: places[name])
    monkeypatch.setattr(ai_api.q, "distance_between_points",
                        lambda db, la1, lo1, la2, lo2: 1200.0)

    res = client.post("/api/query", json={"query": "сколько от Эрмитажа до Русского музея"})
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "distance"
    assert "1,2 км" in body["explanation"]
    assert body["session_id"]
    assert body["structured"]["agent_steps"][0]["tool"] == "distance"
    assert calls["n"] == 2


def test_endpoint_agent_then_refinement(monkeypatch, client):
    from app.api import ai as ai_api
    from app.ai import client as ai_client

    calls = {"n": 0}

    def fake_chat(settings, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool_call("find_objects", categories=["museum"])
        return _DONE

    monkeypatch.setattr(ai_client, "chat_tools", fake_chat)
    monkeypatch.setattr(
        ai_api.q, "list_objects",
        lambda db, **kw: ([_obj(1, "Музей А")], 1),
    )

    r1 = client.post("/api/query", json={"query": "музеи"})
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["action"] == "list" and b1["session_id"]

    # уточнение к ответу агента идёт быстрым путём, без вызовов модели
    r2 = client.post("/api/query",
                     json={"query": "а теперь только галереи", "session_id": b1["session_id"]})
    b2 = r2.json()
    assert b2["action"] == "list"
    assert b2["structured"]["categories"] == ["gallery"]
    assert calls["n"] == 2  # агент вызван только для первого запроса


def test_endpoint_agent_falls_back_to_classic(monkeypatch, client):
    from app.api import ai as ai_api
    from app.ai import client as ai_client

    # агент не вызвал инструменты -> AgentError -> обычный разбор
    monkeypatch.setattr(
        ai_client, "chat_tools",
        lambda settings, messages, tools: {"content": "Не знаю.", "tool_calls": []},
    )
    sq = StructuredQuery.model_validate({"action": "statistics"})
    monkeypatch.setattr(ai_api, "parse_with_fallback", lambda *a, **kw: sq)
    monkeypatch.setattr(ai_api.q, "statistics", lambda db, not_keyword=None: {"total": 474})

    res = client.post("/api/query", json={"query": "сколько объектов"})
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "statistics"
    assert body["statistics"]["total"] == 474


def test_endpoint_agent_disabled(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.ai import client as ai_client
    from app.ai import sessions
    from app.config import get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, ai_enabled=True, ai_agent_enabled=False
    )
    monkeypatch.setattr(ai_api, "available", lambda s: True)

    def boom(*args, **kwargs):
        raise AssertionError("агент выключен — chat_tools вызываться не должен")

    monkeypatch.setattr(ai_client, "chat_tools", boom)
    monkeypatch.setattr(
        ai_api, "parse_with_fallback",
        lambda *a, **kw: StructuredQuery.model_validate({"action": "list"}),
    )
    monkeypatch.setattr(ai_api.q, "list_objects", lambda db, **kw: ([], 0))

    try:
        res = TestClient(app, raise_server_exceptions=False).post(
            "/api/query", json={"query": "музеи"}
        )
        assert res.status_code == 200
        assert res.json()["action"] == "list"
    finally:
        app.dependency_overrides.pop(get_settings, None)
        get_settings.cache_clear()
        sessions.store.clear_all()
