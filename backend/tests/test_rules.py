"""Тесты rule-based fallback-парсера (§31, §53) и кэшей клиента (§46)."""

from app.ai.parser import AIQueryError, parse_with_fallback
from app.ai.rules import parse_rules
from app.ai import client
from app.config import Settings

settings = Settings(_env_file=None)


# --- разбор шаблонами ---------------------------------------------------------

def test_rules_nearby_category_with_place():
    sq = parse_rules("Какие памятники находятся рядом с Эрмитажем?")
    assert sq is not None
    assert sq.action == "nearby"
    assert sq.categories == ["monument"]
    assert sq.place and "Эрмитаж" in sq.place


def test_rules_radius_search():
    sq = parse_rules("Покажи галереи в радиусе 2 км от Невского проспекта")
    assert sq is not None
    assert sq.action == "radius_search"
    assert sq.categories == ["gallery"]
    assert sq.radius_meters == 2000.0
    assert sq.place and "Невск" in sq.place


def test_rules_radius_meters_unit():
    sq = parse_rules("музеи в пределах 500 м от Дворцовой площади")
    assert sq is not None
    assert sq.action == "radius_search"
    assert sq.radius_meters == 500.0
    assert sq.categories == ["museum"]


def test_rules_coordinates():
    sq = parse_rules("Какой музей находится ближе всего к координатам 59.9343, 30.3351?")
    assert sq is not None
    assert sq.action == "nearby"
    assert abs(sq.latitude - 59.9343) < 1e-6
    assert abs(sq.longitude - 30.3351) < 1e-6
    assert sq.categories == ["museum"]


def test_rules_plain_category_list():
    sq = parse_rules("Покажи музеи")
    assert sq is not None
    assert sq.action == "list"
    assert sq.categories == ["museum"]


def test_rules_statistics():
    sq = parse_rules("Сколько объектов в базе?")
    assert sq is not None
    assert sq.action == "statistics"


def test_rules_recommend():
    sq = parse_rules("Порекомендуй музеи")
    assert sq is not None
    assert sq.action == "recommend"
    assert sq.categories == ["museum"]


def test_rules_search_fallback():
    sq = parse_rules("Найди информацию про Пушкина")
    assert sq is not None
    assert sq.action == "search"
    assert "Пушкина" in sq.text


def test_rules_never_returns_unvalidated():
    """Всё, что вернул парсер, обязано проходить Pydantic (§25)."""
    from pydantic import ValidationError

    for q in ("музеи", "рядом с Эрмитажем", "59.93, 30.33", "статистика"):
        sq = parse_rules(q)
        if sq is not None:
            assert sq.model_validate(sq.model_dump()) == sq


# --- поведение при недоступной модели ----------------------------------------

def test_parse_with_fallback_raises_llm_error_unchanged(monkeypatch):
    """Недоступность LLM → исключение типа LLMError (503), а не 422 (§44)."""
    from app.ai.client import AIServiceUnavailable

    def boom(*args, **kwargs):
        raise AIServiceUnavailable("connection refused")

    monkeypatch.setattr("app.ai.parser.chat", boom)
    try:
        parse_with_fallback(settings, "музеи рядом")
        raise AssertionError("ожидалось AIServiceUnavailable")
    except AIQueryError:
        raise AssertionError("LLMError не должен маскироваться под AIQueryError (422)")
    except AIServiceUnavailable:
        pass  # ожидаемый путь: api/ai.py отдаст 503 или rules-fallback


def test_parse_with_fallback_retries_bad_json(monkeypatch):
    """Плохой JSON модели → ровно одна повторная попытка (§24)."""
    import json

    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        return "{broken" if calls["n"] == 1 else json.dumps({"action": "statistics"})

    monkeypatch.setattr("app.ai.parser.chat", flaky)
    sq = parse_with_fallback(settings, "сколько объектов")
    assert sq.action == "statistics"
    assert calls["n"] == 2


# --- кэши (§46) ---------------------------------------------------------------

def test_chat_completion_is_cached(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"content": "ok"}}

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(client.httpx, "post", fake_post)
    client.clear_caches()
    msgs = [{"role": "user", "content": "статистика"}]
    assert client.chat_completion(settings, msgs) == "ok"
    assert client.chat_completion(settings, msgs) == "ok"
    assert calls["n"] == 1, "повторный идентичный запрос должен идти в кэш (§46)"


def test_availability_cache(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        status_code = 200

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(client.httpx, "get", fake_get)
    client.clear_caches()
    on = Settings(_env_file=None, ai_enabled=True)
    assert client.check_available(on) is True
    assert client.check_available(on) is True
    assert calls["n"] == 1, "проверка доступности должна кэшироваться (§46)"


def test_embed_is_cached(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"embedding": [0.1, 0.2]}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(client.httpx, "post", fake_post)
    client.clear_caches()
    on = Settings(_env_file=None, ai_enabled=True)
    assert client.embed(on, "русское искусство") == [0.1, 0.2]
    assert client.embed(on, "русское искусство") == [0.1, 0.2]
    assert calls["n"] == 1, "эмбеддинги должны кэшироваться (§46)"
