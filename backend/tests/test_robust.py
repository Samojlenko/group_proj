"""Тесты надёжного разбора (этап F, идеи 20–23).

Structured output (разбор по JSON-схеме), настраиваемые повторы разбора,
нормализация текста запроса ради кэша §46 и журнал «не понял».
"""

from __future__ import annotations

import json

import pytest

from app.ai import client as ai_client
from app.ai import parser, sessions, unrecognized
from app.ai.parser import AIQueryError, normalize_query, parse_query, parse_with_fallback
from app.ai.schemas_ai import StructuredQuery


@pytest.fixture(autouse=True)
def _clean_journal():
    """Журнал «не понял» — общий для процесса: изолируем каждый тест."""
    unrecognized.clear()
    yield
    unrecognized.clear()


# --- идея 20: structured output -----------------------------------------------

def test_output_schema_is_json_schema():
    schema = parser._output_schema()
    assert schema["type"] == "object"
    assert "action" in schema["properties"]
    assert "place" in schema["properties"]
    # схема статична — кэшируется, а не строится каждый раз
    assert parser._output_schema() is schema


def test_chat_passes_schema(monkeypatch):
    from app.config import Settings

    seen: dict = {}

    def fake_completion(settings_arg, messages, **kwargs):
        seen.update(kwargs)
        return json.dumps({"action": "other"})

    monkeypatch.setattr("app.ai.client.chat_completion", fake_completion)
    parser.chat(Settings(_env_file=None, ai_structured_output=True), "sys", "user")
    assert isinstance(seen["json_format"], dict)
    assert seen["json_format"]["type"] == "object"


def test_chat_simple_json_when_structured_disabled(monkeypatch):
    from app.config import Settings

    seen: dict = {}

    def fake_completion(settings_arg, messages, **kwargs):
        seen.update(kwargs)
        return json.dumps({"action": "other"})

    monkeypatch.setattr("app.ai.client.chat_completion", fake_completion)
    parser.chat(Settings(_env_file=None, ai_structured_output=False), "sys", "user")
    assert seen["json_format"] is True


def test_client_falls_back_to_simple_json_on_400(monkeypatch):
    """Сервер старой версии не принимает схему — тихая деградация к JSON."""
    from app.config import Settings

    ai_client.clear_caches()
    posts: list[dict] = []

    class _Resp:
        def __init__(self, status: int):
            self.status_code = status

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"message": {"content": '{"action":"other"}'}}

    def fake_post(url, json=None, timeout=None):  # noqa: A002 - имя параметра httpx
        posts.append(dict(json))
        return _Resp(400 if len(posts) == 1 else 200)

    monkeypatch.setattr("app.ai.client.httpx.post", fake_post)
    out = ai_client.chat_completion(
        Settings(_env_file=None),
        [{"role": "user", "content": "уникальный-запрос-теста-400"}],
        json_format={"type": "object"},
    )
    try:
        assert out == '{"action":"other"}'
        assert posts[0]["format"] == {"type": "object"}
        assert posts[1]["format"] == "json"
    finally:
        ai_client.clear_caches()


# --- идея 22: нормализация текста — кэш не платит дважды ----------------------

def test_normalize_query_collapses_whitespace():
    assert normalize_query("  музеи\n\n рядом\tс   Эрмитажем \n") == "музеи рядом с Эрмитажем"
    assert normalize_query("музеи   рядом") == "музеи рядом"


def test_parse_normalizes_query_before_llm(monkeypatch):
    from app.config import Settings

    seen: dict = {}

    def fake_chat(settings_arg, system, user, **kwargs):
        seen["user"] = user
        return json.dumps({"action": "list", "categories": ["museum"]})

    monkeypatch.setattr("app.ai.parser.chat", fake_chat)
    sq = parse_query(Settings(_env_file=None), "музеи\n\n   рядом")
    assert sq.action == "list"
    # в сообщение модели текст уходит нормализованным — одинаковые формулировки
    # с разным оформлением дают один ключ кэша (§46)
    assert "Запрос пользователя: музеи рядом" in seen["user"]


# --- идея 21: настраиваемые повторы -------------------------------------------

def _always_broken(calls: dict):
    def bad(*args, **kwargs):
        calls["n"] += 1
        return "{broken"

    return bad


def test_parse_retries_zero_means_single_attempt(monkeypatch):
    from app.config import Settings

    calls = {"n": 0}
    monkeypatch.setattr("app.ai.parser.chat", _always_broken(calls))
    with pytest.raises(AIQueryError):
        parse_with_fallback(Settings(_env_file=None, ai_parse_retries=0), "музеи")
    assert calls["n"] == 1


def test_parse_retries_setting_controls_attempts(monkeypatch):
    from app.config import Settings

    calls = {"n": 0}
    monkeypatch.setattr("app.ai.parser.chat", _always_broken(calls))
    with pytest.raises(AIQueryError):
        parse_with_fallback(Settings(_env_file=None, ai_parse_retries=2), "музеи")
    assert calls["n"] == 3


def test_parse_retries_are_capped(monkeypatch):
    from app.config import Settings

    calls = {"n": 0}
    monkeypatch.setattr("app.ai.parser.chat", _always_broken(calls))
    with pytest.raises(AIQueryError):
        parse_with_fallback(Settings(_env_file=None, ai_parse_retries=99), "музеи")
    assert calls["n"] == 1 + parser._MAX_PARSE_RETRIES


def test_retry_message_includes_previous_error(monkeypatch):
    from app.config import Settings

    users: list[str] = []

    def flaky(settings_arg, system, user, **kwargs):
        users.append(user)
        if len(users) == 1:
            return json.dumps({"action": "list", "categories": ["nightclub"]})
        return json.dumps({"action": "statistics"})

    monkeypatch.setattr("app.ai.parser.chat", flaky)
    sq = parse_with_fallback(Settings(_env_file=None), "статистика")
    assert sq.action == "statistics"
    assert "Предыдущая попытка была неудачной" in users[1]
    assert "категория" in users[1]


# --- идея 23: журнал «не понял» ------------------------------------------------

def test_journal_keeps_recent_first_and_filters_by_kind():
    unrecognized.add("other", "какая погода")
    unrecognized.add("fallback", "музеи")
    unrecognized.add("unparsed", "абракадабра", "не JSON")
    assert unrecognized.total() == 3
    assert unrecognized.counts() == {"other": 1, "fallback": 1, "unparsed": 1}
    assert [e["query"] for e in unrecognized.recent(2)] == ["абракадабра", "музеи"]
    only_other = unrecognized.recent(kind="other")
    assert [e["kind"] for e in only_other] == ["other"]
    assert only_other[0]["query"] == "какая погода"
    assert unrecognized.recent(kind="other")[0]["time"]


def test_journal_ring_keeps_last_entries():
    for i in range(unrecognized.MAX_ENTRIES + 20):
        unrecognized.add("other", f"q{i}")
    assert unrecognized.total() == unrecognized.MAX_ENTRIES
    assert unrecognized.recent(1)[0]["query"] == f"q{unrecognized.MAX_ENTRIES + 19}"


# --- эндпоинты: журнал через API и запись поводов ------------------------------

@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import Settings, get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, ai_enabled=True, ai_agent_enabled=False, ai_summary_enabled=False
    )
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_settings, None)
    get_settings.cache_clear()
    sessions.store.clear_all()


def test_unrecognized_endpoint_returns_journal(client):
    unrecognized.add("other", "какая погода")
    unrecognized.add("unparsed", "ыыы", "не JSON")

    res = client.get("/api/unrecognized")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert body["counts"] == {"other": 1, "unparsed": 1}
    assert body["items"][0]["query"] == "ыыы"  # свежие первыми
    assert body["items"][0]["kind_label"]
    assert body["items"][1]["query"] == "какая погода"

    filtered = client.get("/api/unrecognized", params={"kind": "other"}).json()
    assert [i["kind"] for i in filtered["items"]] == ["other"]
    assert filtered["total"] == 2  # total — про весь журнал

    assert client.get("/api/unrecognized", params={"kind": "nope"}).status_code == 400


def test_query_other_is_recorded(monkeypatch, client):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "available", lambda s: True)
    monkeypatch.setattr(
        ai_api, "parse_with_fallback",
        lambda *a, **k: StructuredQuery.model_validate({"action": "other"}),
    )
    res = client.post("/api/query", json={"query": "какая завтра погода"})
    assert res.status_code == 200
    assert res.json()["action"] == "other"
    entries = unrecognized.recent()
    assert entries[0]["kind"] == "other"
    assert entries[0]["query"] == "какая завтра погода"


def test_query_unparsed_is_recorded(monkeypatch, client):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "available", lambda s: True)

    def boom(*args, **kwargs):
        raise AIQueryError("схема не соблюдена")

    monkeypatch.setattr(ai_api, "parse_with_fallback", boom)
    res = client.post("/api/query", json={"query": "фиолетовый кубизм вчера"})
    assert res.status_code == 422
    entry = unrecognized.recent()[0]
    assert entry["kind"] == "unparsed"
    assert "схема" in entry["detail"]


def test_query_rule_fallback_is_recorded(monkeypatch, client):
    from app.api import ai as ai_api

    # модель недоступна: запрос спасён шаблонами (§53) — тоже повод для журнала
    monkeypatch.setattr(ai_api, "available", lambda s: False)
    monkeypatch.setattr(
        ai_api, "_parse_rules",
        lambda payload: StructuredQuery.model_validate({"action": "list", "categories": ["museum"]}),
    )
    monkeypatch.setattr(ai_api.q, "list_objects", lambda db, **kw: ([], 0))

    res = client.post("/api/query", json={"query": "музеи"})
    assert res.status_code == 200
    entry = unrecognized.recent()[0]
    assert entry["kind"] == "fallback"
    assert entry["query"] == "музеи"
