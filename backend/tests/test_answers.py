"""Тесты этапа E: LLM-резюме (идея 16), «почему эти» (идея 17), SSE (идея 18).

Резюме проверяется на «честность»: все числа текста обязаны быть в фактах
результата, иначе остаётся шаблон. Стриминг — на последовательность
SSE-кадров result → token… → done и на reset при придуманных числах.
"""
from __future__ import annotations

import json

import pytest

from app.ai import client as ai_client
from app.ai.client import AIServiceUnavailable
from app.ai.recommend import recommend as run_recommend
from app.ai.summary import (
    build_facts,
    facts_json,
    stream_summarize,
    summarize,
    validate_summary,
)
from app.config import Settings


def _settings(**kw) -> Settings:
    kw.setdefault("ai_enabled", True)
    kw.setdefault("ai_summary_enabled", True)
    return Settings(_env_file=None, **kw)


def _obj(i: int, name: str, category: str = "museum", **extra) -> dict:
    return {
        "id": i, "name": name, "category": category,
        "latitude": 59.9, "longitude": 30.3, "source": "test", **extra,
    }


def _result(**extra) -> dict:
    base = {
        "action": "list",
        "explanation": "Список объектов: Музеи",
        "structured": {"action": "list", "categories": ["museum"]},
        "resolved_place": None,
        "total": 2,
        "items": [],
        "statistics": None,
    }
    return {**base, **extra}


# --- факты: только поля результата --------------------------------------------


def test_facts_from_template_place_and_items():
    result = _result(
        explanation="Объекты в радиусе 800 м от «Эрмитаж» (Музеи)",
        structured={"action": "radius_search", "categories": ["museum"],
                    "radius_meters": 800.0},
        resolved_place={"label": "Эрмитаж", "latitude": 59.94, "longitude": 30.31},
        total=7,
        items=[_obj(i, f"Музей {i}", distance_meters=100.0 * i) for i in range(1, 7)],
    )
    facts = build_facts(result)
    assert facts["ответ_сервиса"] == "Объекты в радиусе 800 м от «Эрмитаж» (Музеи)"
    assert facts["место"] == "Эрмитаж"
    assert facts["категории"] == ["Музеи"]
    assert facts["радиус_м"] == 800.0
    assert facts["всего_найдено"] == 7
    assert len(facts["объекты"]) == 5  # контекст модели не разрастается
    assert facts["объектов_показано"] == 5
    first = facts["объекты"][0]
    assert first["название"] == "Музей 1" and first["расстояние_м"] == 100
    assert first["категория"] == "Музеи"


def test_facts_route_and_statistics():
    walk = _result(
        action="walk",
        route={"items": [{"id": 1}, {"id": 2}], "total_meters": 1500.0,
               "walk_minutes": 19},
    )
    facts = build_facts(walk)
    assert facts["маршрут"] == {"остановок": 2, "метров": 1500.0, "минут_пешком": 19}

    stats = _result(action="statistics", statistics={"total": 474})
    assert build_facts(stats)["статистика"] == {"total": 474}


# --- проверка честности резюме -------------------------------------------------


def test_validate_accepts_numbers_from_facts():
    facts = facts_json(_result(total=33, items=[_obj(1, "Музей А", distance_meters=350.0)]))
    assert validate_summary("Из 33 музеев начните с «Музей А» — он в 350 м.", facts)


def test_validate_rejects_invented_number():
    facts = facts_json(_result(total=33))
    assert not validate_summary("Здесь больше 500 музеев.", facts)


def test_validate_tolerates_natural_rounding():
    # «примерно в 80 метрах» при фактических 78 — человеческое округление
    facts = facts_json(_result(items=[_obj(1, "Музей А", distance_meters=78.29)]))
    assert validate_summary("Начните с «Музей А» — он примерно в 80 метрах.", facts)


def test_validate_rejects_wrong_year():
    # четырёхзначные сверяются точно: 1830 вместо 1829 — уже выдумка
    facts = facts_json(_result(items=[_obj(1, "Музей А", year_established=1829)]))
    assert not validate_summary("«Музей А» основан в 1830 году.", facts)


def test_validate_rejects_short_or_json():
    facts = facts_json(_result())
    assert not validate_summary("Ок.", facts)
    assert not validate_summary('{"text": "музеи рядом"}', facts)


# --- summarize: живой текст или шаблон ----------------------------------------


def test_summarize_ok(monkeypatch):
    monkeypatch.setattr(ai_client, "chat_completion",
                        lambda s, m, **k: "Из 2 музеев начните с «Музей А».")
    text = summarize(_settings(), _result(items=[_obj(1, "Музей А")]))
    assert text == "Из 2 музеев начните с «Музей А»."


def test_summarize_strips_wrapping_quotes(monkeypatch):
    monkeypatch.setattr(ai_client, "chat_completion",
                        lambda s, m, **k: " «Всего 2 объекта, начните с «Музей А».» ")
    text = summarize(_settings(), _result(items=[_obj(1, "Музей А")]))
    assert text == "Всего 2 объекта, начните с «Музей А»."


def test_summarize_rejects_invented(monkeypatch):
    monkeypatch.setattr(ai_client, "chat_completion",
                        lambda s, m, **k: "Здесь целых 999 музеев!")
    assert summarize(_settings(), _result()) is None


def test_summarize_unavailable_returns_none(monkeypatch):
    def boom(*args, **kwargs):
        raise AIServiceUnavailable("сервер недоступен")

    monkeypatch.setattr(ai_client, "chat_completion", boom)
    assert summarize(_settings(), _result()) is None


def test_summarize_disabled_makes_no_call(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("резюме выключено — модель звать нельзя")

    monkeypatch.setattr(ai_client, "chat_completion", boom)
    assert summarize(_settings(ai_summary_enabled=False), _result()) is None


# --- стриминг резюме -----------------------------------------------------------


def test_stream_summarize_yields_chunks_and_caches(monkeypatch):
    ai_client.clear_caches()
    chunks = ["Из 2 музеев ", "начните с «Музей А»."]
    monkeypatch.setattr(ai_client, "chat_completion_stream",
                        lambda s, m, **k: iter(chunks))
    out = list(stream_summarize(_settings(), _result(items=[_obj(1, "Музей А")])))
    assert out == chunks
    # повтор отдаётся из кэша (§46) — одним куском, без второго вызова модели
    again = list(stream_summarize(_settings(), _result(items=[_obj(1, "Музей А")])))
    assert again == ["".join(chunks)]


def test_stream_summarize_unavailable_is_silent(monkeypatch):
    ai_client.clear_caches()

    def boom(*args, **kwargs):
        raise AIServiceUnavailable("сервер недоступен")

    monkeypatch.setattr(ai_client, "chat_completion_stream", boom)
    assert list(stream_summarize(_settings(), _result())) == []


# --- «почему эти»: вклад факторов (§28, идея 17) --------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, stmt, params=None):
        return _Rows(self._rows)


def test_recommend_items_have_factor_contributions():
    row = {
        "id": 1, "name": "Музей А", "category": "museum", "latitude": 59.9,
        "longitude": 30.3, "source": "test", "distance_m": 500.0,
        "sem_score": 0.0, "spatial_score": 0.5,
        "score": 0.7,
    }
    out = run_recommend(
        _FakeDB([row]), Settings(_env_file=None, ai_enabled=True),
        categories=["museum"], latitude=59.9, longitude=30.3, limit=5,
    )
    item = out["items"][0]
    # веса нормализованы: без семантики остаётся spatial 0.6 / category 0.4
    assert item["factors"]["semantic"] == 0.0
    assert item["factors"]["spatial"] == 0.3   # 0.6 × 0.5
    assert item["factors"]["category"] == 0.4  # 0.4 × 1.0
    assert abs(sum(item["factors"].values()) - item["score"]) < 0.001


def test_ai_query_schema_keeps_factors():
    from app.schemas.ai_schemas import AIQueryResponse

    body = AIQueryResponse.model_validate({
        "action": "recommend",
        "explanation": "Подборка",
        "structured": {},
        "total": 1,
        "items": [{**_obj(1, "Музей А"), "score": 0.7,
                   "factors": {"spatial": 0.3, "category": 0.4}}],
    })
    assert body.items[0].factors == {"spatial": 0.3, "category": 0.4}
    assert body.items[0].score == 0.7


# --- эндпоинты /api/query: резюме и поток ---------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.ai import sessions
    from app.config import get_settings
    from app.main import app

    sessions.store.clear_all()
    ai_client.clear_caches()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, ai_enabled=True, ai_agent_enabled=False, ai_summary_enabled=True
    )
    monkeypatch.setattr(ai_api, "available", lambda s: True)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_settings, None)
    get_settings.cache_clear()
    sessions.store.clear_all()
    ai_client.clear_caches()


def _patch_list(monkeypatch, items, total=None):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api.q, "list_objects",
                        lambda db, **kw: (items, total if total is not None else len(items)))


def _patch_parse_list(monkeypatch):
    from app.api import ai as ai_api
    from app.ai.schemas_ai import StructuredQuery

    monkeypatch.setattr(
        ai_api, "parse_with_fallback",
        lambda *a, **kw: StructuredQuery.model_validate(
            {"action": "list", "categories": ["museum"]}
        ),
    )


def test_endpoint_summary_replaces_template(monkeypatch, client):
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А"), _obj(2, "Музей Б")])
    monkeypatch.setattr(ai_client, "chat_completion",
                        lambda s, m, **k: "Из 2 музеев начните с «Музей А».")

    res = client.post("/api/query", json={"query": "музеи"})
    assert res.status_code == 200
    body = res.json()
    assert body["explanation"] == "Из 2 музеев начните с «Музей А»."
    assert body["action"] == "list"


def test_endpoint_summary_invalid_keeps_template(monkeypatch, client):
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А")])
    monkeypatch.setattr(ai_client, "chat_completion",
                        lambda s, m, **k: "Здесь 999 уникальных музеев!")

    body = client.post("/api/query", json={"query": "музеи"}).json()
    assert body["explanation"].startswith("Список объектов:")


def test_endpoint_summary_disabled_makes_no_call(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.ai import sessions
    from app.config import get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, ai_enabled=True, ai_agent_enabled=False, ai_summary_enabled=False
    )
    monkeypatch.setattr(ai_api, "available", lambda s: True)
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А")])

    def boom(*args, **kwargs):
        raise AssertionError("резюме выключено — модель звать нельзя")

    monkeypatch.setattr(ai_client, "chat_completion", boom)
    try:
        body = TestClient(app, raise_server_exceptions=False).post(
            "/api/query", json={"query": "музеи"}
        ).json()
        assert body["explanation"].startswith("Список объектов:")
    finally:
        app.dependency_overrides.pop(get_settings, None)
        get_settings.cache_clear()
        sessions.store.clear_all()


def test_endpoint_refinement_skips_summary(monkeypatch, client):
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А"), _obj(2, "Музей Б")])
    calls = {"n": 0}

    def fake_chat(settings, messages, **kw):
        calls["n"] += 1
        return "Из 2 музеев начните с «Музей А»."

    monkeypatch.setattr(ai_client, "chat_completion", fake_chat)

    body1 = client.post("/api/query", json={"query": "музеи"}).json()
    assert body1["explanation"] == "Из 2 музеев начните с «Музей А»."
    assert calls["n"] == 1

    # «а теперь только галереи» — быстрый путь (§40): модель не зовётся вовсе
    body2 = client.post("/api/query",
                        json={"query": "а теперь только галереи",
                              "session_id": body1["session_id"]}).json()
    assert body2["explanation"].startswith("Список объектов:")
    assert calls["n"] == 1


def test_endpoint_summary_skipped_without_llm(monkeypatch, client):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "available", lambda s: False)
    _patch_list(monkeypatch, [_obj(1, "Музей А")])

    def boom(*args, **kwargs):
        raise AssertionError("модель недоступна — резюме быть не должно")

    monkeypatch.setattr(ai_client, "chat_completion", boom)
    body = client.post("/api/query", json={"query": "музеи"}).json()
    assert body["explanation"].startswith("Список объектов:")
    assert "шаблонами" in body["explanation"]  # путь правил (§53)


# --- эндпоинт /api/query/stream (идея 18) ---------------------------------------


def _events(text: str) -> list[tuple[str, str]]:
    out = []
    for block in text.split("\n\n"):
        event, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            out.append((event, data))
    return out


def test_stream_emits_result_tokens_done(monkeypatch, client):
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А"), _obj(2, "Музей Б")])
    monkeypatch.setattr(ai_client, "chat_completion_stream",
                        lambda s, m, **k: iter(["Из 2 музеев ", "начните с «Музей А»."]))

    res = client.post("/api/query/stream", json={"query": "музеи"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _events(res.text)
    assert events[0][0] == "result"
    result = json.loads(events[0][1])
    assert result["action"] == "list" and result["session_id"]
    assert result["explanation"].startswith("Список объектов:")  # шаблон в кадре
    tokens = "".join(json.loads(d) for e, d in events if e == "token")
    assert tokens == "Из 2 музеев начните с «Музей А»."
    assert events[-1][0] == "done"
    assert all(e != "reset" for e, _ in events)


def test_stream_resets_on_invented_numbers(monkeypatch, client):
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А")])
    monkeypatch.setattr(ai_client, "chat_completion_stream",
                        lambda s, m, **k: iter(["Здесь целых ", "999 музеев!"]))

    res = client.post("/api/query/stream", json={"query": "музеи"})
    kinds = [e for e, _ in _events(res.text)]
    assert kinds[0] == "result"
    assert "token" in kinds
    assert "reset" in kinds  # проверка фактов не прошла — клиент вернёт шаблон
    assert kinds[-1] == "done"


def test_stream_resets_on_cjk_summary(monkeypatch, client):
    """Китайская вставка модели — тоже reset: числа при этом сходятся."""
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А")])
    monkeypatch.setattr(
        ai_client, "chat_completion_stream",
        lambda s, m, **k: iter(["Из 1 музея начните с «Музей А». ",
                                "預期的 JSON 格式导致信息丢失"]),
    )

    res = client.post("/api/query/stream", json={"query": "музеи"})
    kinds = [e for e, _ in _events(res.text)]
    assert kinds[0] == "result"
    assert "reset" in kinds  # служебный мусор модели: клиент вернёт шаблон
    assert kinds[-1] == "done"


def test_stream_fast_path_has_no_tokens(monkeypatch, client):
    _patch_parse_list(monkeypatch)
    _patch_list(monkeypatch, [_obj(1, "Музей А"), _obj(2, "Музей Б")])
    monkeypatch.setattr(ai_client, "chat_completion_stream",
                        lambda s, m, **k: iter(["Из 2 музеев начните с «Музей А»."]))

    sid = client.post("/api/query/stream", json={"query": "музеи"}).text
    result = json.loads(_events(sid)[0][1])

    res = client.post("/api/query/stream",
                      json={"query": "а теперь только галереи",
                            "session_id": result["session_id"]})
    kinds = [e for e, _ in _events(res.text)]
    assert kinds[0] == "result" and kinds[-1] == "done"
    assert "token" not in kinds  # уточнение — мгновенный путь без модели


# --- /api/recommend: живое объяснение и факторы ----------------------------------


def test_recommend_endpoint_summary_and_factors(monkeypatch, client):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "run_recommend", lambda db, settings, **kw: {
        "items": [{**_obj(1, "Музей А"), "distance_meters": 120.0, "score": 0.7,
                   "factors": {"semantic": 0.4, "spatial": 0.2, "category": 0.1}}],
        "weights": {"sem": 0.5, "spatial": 0.3, "category": 0.2},
        "components": {"semantic": True, "spatial": True, "category": True},
    })
    monkeypatch.setattr(ai_client, "chat_completion",
                        lambda s, m, **k: "Начните с «Музей А» — он всего в 120 м.")

    body = client.post("/api/recommend", json={"query": "музей", "limit": 5}).json()
    assert body["explanation"] == "Начните с «Музей А» — он всего в 120 м."
    assert body["items"][0]["factors"]["semantic"] == 0.4
    assert body["items"][0]["score"] == 0.7
