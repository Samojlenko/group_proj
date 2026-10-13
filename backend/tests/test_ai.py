"""Тесты AI-слоя (§25): валидация структурированного запроса и разбор."""

import json

import pytest
from pydantic import ValidationError

from app.ai.parser import AIQueryError, build_user_message, parse_query
from app.ai.schemas_ai import StructuredQuery, CATEGORY_SYNONYMS
from app.ai.recommend import _weights
from app.config import Settings

settings = Settings(_env_file=None)


# --- StructuredQuery (§25) ----------------------------------------------------

def test_structured_query_minimal():
    sq = StructuredQuery.model_validate({"action": "list", "categories": ["museum"]})
    assert sq.action == "list"
    assert sq.categories == ["museum"]
    assert sq.limit == 20


def test_structured_query_russian_category_synonyms():
    sq = StructuredQuery.model_validate({"categories": ["Музеи", "памятники"]})
    assert sq.categories == ["museum", "monument"]
    assert CATEGORY_SYNONYMS["галереи"] == "gallery"


def test_structured_query_rejects_unknown_category():
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({"categories": ["zoo"]})


def test_structured_query_bounds():
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({"latitude": 120.0})
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({"radius_meters": 999999})
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({"limit": 0})
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({"text": "а"})


def test_structured_query_action_literal():
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({"action": "drop_table"})


def test_structured_query_action_other_accepted():
    """«other» — внятный отказ на запрос вне схемы (погода, переводы)."""
    sq = StructuredQuery.model_validate({"action": "other"})
    assert sq.action == "other"
    assert sq.text is None and sq.categories == []


def test_structured_query_null_categories_accepted():
    """Модель по промпту шлёт null в ненужные поля — это не ошибка (§25)."""
    sq = StructuredQuery.model_validate(
        {"action": "other", "categories": None, "text": None, "place": None}
    )
    assert sq.categories == []
    assert sq.text is None


def test_system_prompt_instructs_refusal():
    """Промпт обязан содержать правило про отказ, иначе модель притянет
    любой запрос к list/statistics и пользователь получит бессмысленный ответ."""
    from app.ai.parser import SYSTEM_PROMPT

    assert '"other"' in SYSTEM_PROMPT
    assert "погода" in SYSTEM_PROMPT


# --- parse_query с подменённой моделью ---------------------------------------

def _fake_chat_factory(payload):
    def fake(settings_arg, system, user, **kwargs):
        return json.dumps(payload, ensure_ascii=False)

    return fake


def test_parse_query_ok(monkeypatch):
    monkeypatch.setattr("app.ai.parser.chat", _fake_chat_factory({
        "action": "nearby", "categories": ["museum"], "place": "Дворцовая площадь",
        "radius_meters": 800, "limit": 15,
    }))
    sq = parse_query(settings, "какие музеи рядом с Дворцовой площадью")
    assert sq.action == "nearby"
    assert sq.categories == ["museum"]
    assert sq.place == "Дворцовая площадь"
    assert sq.radius_meters == 800


def test_parse_query_invalid_category_reports_422(monkeypatch):
    monkeypatch.setattr("app.ai.parser.chat", _fake_chat_factory({
        "action": "list", "categories": ["nightclub"],
    }))
    with pytest.raises(AIQueryError) as exc:
        parse_query(settings, "покажи ночные клубы")
    assert "категория" in str(exc.value)


def test_parse_query_not_json(monkeypatch):
    monkeypatch.setattr("app.ai.parser.chat", lambda *a, **k: "извините, я не могу")
    with pytest.raises(AIQueryError):
        parse_query(settings, "музеи")


def test_parse_query_retries_once(monkeypatch):
    calls = {"n": 0}

    def flaky(settings_arg, system, user, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "{broken json"
        return json.dumps({"action": "statistics"})

    monkeypatch.setattr("app.ai.parser.chat", flaky)
    sq = parse_query(settings, "статистика") if False else None
    # parse_with_fallback делает ровно одну повторную попытку (§24)
    from app.ai.parser import parse_with_fallback

    sq = parse_with_fallback(settings, "сколько объектов")
    assert sq.action == "statistics"
    assert calls["n"] == 2


def test_user_message_contains_coords():
    msg = build_user_message("музеи", 59.93, 30.33)
    assert "59.93" in msg and "30.33" in msg


# --- action="other": внятный отказ на запрос вне схемы ------------------------

def test_query_other_action_returns_refusal(monkeypatch):
    """POST /api/query с action=other -> 200 и понятный отказ (без SQL)."""
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, ai_enabled=True)
    monkeypatch.setattr(ai_api, "available", lambda s: True)
    monkeypatch.setattr(
        ai_api, "parse_with_fallback",
        lambda s, q, lat=None, lon=None: StructuredQuery.model_validate({"action": "other"}),
    )
    try:
        res = TestClient(app, raise_server_exceptions=False).post(
            "/api/query", json={"query": "Какая завтра погода?"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["action"] == "other"
        assert body["total"] == 0
        assert "умею" in body["explanation"]
        # отказ не должен выглядеть как «ничего не найдено»
        assert "Ничего не найдено" not in body["explanation"]
    finally:
        app.dependency_overrides.pop(get_settings, None)
        get_settings.cache_clear()


# --- веса гибридного скоринга (§28) ------------------------------------------

def test_weights_normalization_with_all_components():
    w = _weights(settings, has_vector=True, has_point=True)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w["sem"] > w["spatial"] > w["category"]  # 0.5 > 0.3 > 0.2


def test_weights_without_vector_and_point():
    w = _weights(settings, has_vector=False, has_point=False)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w["category"] == 1.0  # переходим на категорийное сравнение
