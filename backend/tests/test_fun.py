"""Тесты этапа G (идеи 24–27): «Удиви меня», профиль вкуса, подборки, квесты.

Разбор фраз проверяется правилами (§53) без модели; выборки — на фейковой
БД (без Postgres), как в test_answers.py; эндпоинты — с моками SQL-слоя,
как в test_intents.py. Вопросы квеста проверяются на «честность»: вопрос
с названием объекта или без опоры на описание отбрасывается.
"""
from __future__ import annotations

import pytest

from app.ai import fun
from app.ai import quest as quest_mod
from app.ai.client import AIServiceUnavailable
from app.ai.rules import parse_rules
from app.ai.schemas_ai import StructuredQuery
from app.config import Settings


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

    def first(self):  # noqa: ANN201
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Фейковая сессия: отдаёт заранее заданные ответы по порядку вызовов."""

    def __init__(self, responses: list):  # noqa: ANN001
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(sql), params or {}))
        return _Result(self._responses.pop(0) if self._responses else [])


# --- разбор фраз правилами -----------------------------------------------------


def test_rules_surprise():
    for phrase in ("Удиви меня", "Что-нибудь необычное", "Покажи случайное место"):
        sq = parse_rules(phrase)
        assert sq is not None and sq.action == "surprise", phrase


def test_rules_taste():
    for phrase in ("Подбери похожее на моё избранное", "Что-то в моём вкусе"):
        sq = parse_rules(phrase)
        assert sq is not None and sq.action == "taste", phrase


def test_rules_plain_favorites_is_not_taste():
    # «покажи избранные музеи» — это список, а не профиль вкуса: избранное
    # фильтруется на клиенте, фраза про вкус должна быть явной
    sq = parse_rules("Покажи избранные музеи")
    assert sq is not None and sq.action in ("list", "search")


def test_rules_collection_with_top_n():
    sq = parse_rules("Собери топ-7 музеев и сохрани")
    assert sq is not None and sq.action == "collection"
    assert sq.categories == ["museum"] and sq.count == 7


def test_rules_collection_defaults_to_five():
    sq = parse_rules("Сохрани подборку галерей")
    assert sq is not None and sq.action == "collection"
    assert sq.categories == ["gallery"] and sq.count == 5


def test_rules_quest_with_theme_and_count():
    sq = parse_rules("Сделай квест по истории на 5 точек")
    assert sq is not None and sq.action == "quest"
    assert sq.count == 5 and sq.theme == "история"
    assert sq.radius_meters == 1500.0  # радиус прогулки по умолчанию


def test_rules_quest_place_and_defaults():
    sq = parse_rules("Сделай квест от Дворцовой площади")
    assert sq is not None and sq.action == "quest"
    assert sq.count == 5 and sq.place == "Дворцовой площади"
    assert sq.theme is None


def test_rules_quest_theme_count_and_place_together():
    # демо-фраза целиком: тема и число точек не должны попасть в место старта
    sq = parse_rules("Сделай квест по истории на 5 точек от Дворцовой площади")
    assert sq is not None and sq.action == "quest"
    assert sq.count == 5 and sq.theme == "история"
    assert sq.place == "Дворцовой площади"


def test_schema_quest_and_collection_defaults():
    quest = StructuredQuery(action="quest")
    assert quest.count == 5 and quest.radius_meters == 1500.0
    collection = StructuredQuery(action="collection")
    assert collection.count == 5
    # count по-прежнему ограничен 12 и для новых действий
    capped = StructuredQuery(action="collection", count=99)
    assert capped.count == 12


# --- «Удиви меня» (идея 24) -----------------------------------------------------


def test_surprise_pick_returns_random_from_pool(monkeypatch):
    pool = [_obj(1, "Первый"), _obj(2, "Второй"), _obj(3, "Третий")]
    db = _FakeDb([pool])
    monkeypatch.setattr(fun.random, "choice", lambda rows: rows[1])
    obj = fun.surprise_pick(db)
    assert obj["name"] == "Второй"
    sql, params = db.calls[0]
    assert "image_url IS NOT NULL" in sql and "length" in sql
    assert params["desc_min"] == fun.DESC_MIN


def test_surprise_pick_falls_back_to_any_object():
    # первый запрос (с фото и описанием) пуст — второй отдаёт любой объект
    db = _FakeDb([[], [_obj(9, "Запасной")]])
    obj = fun.surprise_pick(db, exclude=[1, 2])
    assert obj["name"] == "Запасной"
    assert "NOT (c.id = ANY(:exclude))" in db.calls[1][0]
    assert db.calls[1][1]["exclude"] == [1, 2]


def test_surprise_pick_excludes_seen_and_relaxes(monkeypatch):
    # всё исключено: ступени пустеют одна за другой — последняя разрешает повтор
    db = _FakeDb([[], [], [], [_obj(5, "Повтор")]])
    obj = fun.surprise_pick(db, exclude=[5])
    assert obj["name"] == "Повтор"
    assert "exclude" in db.calls[2][1]  # третья ступень ещё не повторяет
    assert "exclude" not in db.calls[3][1]


# --- профиль вкуса (идея 25) ----------------------------------------------------


def test_taste_recommend_averages_vectors():
    embeddings = [("[1.0, 0.0]",), ("[0.0, 1.0]",)]
    knn = [
        {**_obj(10, "Похожий"), "score": 0.93},
        {**_obj(11, "Ещё похожий"), "score": 0.81},
    ]
    db = _FakeDb([embeddings, knn])
    result = fun.taste_recommend(db, favorite_ids=[1, 2], limit=5)
    assert result["used"] == 2 and result["favorites"] == 2
    assert [i["name"] for i in result["items"]] == ["Похожий", "Ещё похожий"]
    # средний вектор избранного уходит в SQL параметром
    assert db.calls[1][1]["vec"] == "[0.5, 0.5]"
    assert "NOT (c.id = ANY(:ids))" in db.calls[1][0]
    first = result["items"][0]
    assert first["score"] == 0.93
    assert first["factors"] == {"semantic": 0.93, "spatial": 0.0, "category": 0.0}
    assert first["distance_meters"] is None


def test_taste_recommend_without_favorites():
    result = fun.taste_recommend(_FakeDb([]), favorite_ids=[])
    assert result == {"items": [], "used": 0, "favorites": 0}


def test_taste_recommend_without_embeddings():
    result = fun.taste_recommend(_FakeDb([[]]), favorite_ids=[1])
    assert result["used"] == 0 and result["items"] == []


# --- подборки (идея 26) ---------------------------------------------------------


def test_collection_name():
    assert fun.collection_name(["museum"], 7) == "Топ-7 музеев"
    assert fun.collection_name(None, 5) == "Топ-5 объектов"
    assert fun.collection_name(["gallery", "museum"], 3) == "Топ-3 галерей и музеев"


def test_build_collection_sql_order():
    db = _FakeDb([[_obj(1, "А"), _obj(2, "Б")]])
    items = fun.build_collection(db, categories=["museum"], count=2)
    assert len(items) == 2
    sql, params = db.calls[0]
    assert "image_url IS NOT NULL" in sql and "DESC" in sql
    assert params["count"] == 2 and params["cat_0"] == "museum"


# --- квесты (идея 27) -----------------------------------------------------------


def test_first_sentence():
    assert quest_mod.first_sentence("Первое. Второе.") == "Первое."
    long_text = "Слово " * 60 + "конец"
    hint = quest_mod.first_sentence(long_text)
    assert len(hint) <= quest_mod.HINT_LIMIT and hint.endswith("…")
    assert quest_mod.first_sentence("") == ""


def test_fallback_question_uses_year():
    with_year = _obj(1, "X", description="Здание построено в 1819 году для училища.")
    assert quest_mod.fallback_question(with_year) == "Какой объект связан с 1819 годом?"
    without = _obj(2, "Y", description="Просто длинное описание без дат и годов вообще.")
    assert quest_mod.fallback_question(without) == "О каком объекте идёт речь?"


def test_question_for_rejects_name(monkeypatch):
    obj = _obj(
        1, "Дворик Ангела",
        short_description="Маленький дворик с фресками и старинной аркой на Невском.",
    )
    monkeypatch.setattr(
        quest_mod, "chat_completion",
        lambda *a, **k: "Где находится Дворик Ангела с фресками?",
    )
    assert quest_mod.question_for(_settings(), obj) == ""


def test_question_for_rejects_ungrounded(monkeypatch):
    obj = _obj(
        1, "Особняк",
        short_description="Особняк украшен мозаичными панно и лепниной на фасаде.",
    )
    monkeypatch.setattr(
        quest_mod, "chat_completion", lambda *a, **k: "Какой объект самый красивый в мире?",
    )
    assert quest_mod.question_for(_settings(), obj) == ""


def test_question_for_rejects_mixed_alphabet(monkeypatch):
    # «Гдеlocated»: склейка двух алфавитов в одном слове — брак модели
    obj = _obj(
        1, "Храм",
        short_description="Православный храм на месте ранения императора Александра II.",
    )
    monkeypatch.setattr(
        quest_mod, "chat_completion",
        lambda *a, **k: "Гдеlocated православный храм на месте ранения?",
    )
    assert quest_mod.question_for(_settings(), obj) == ""


def test_question_for_accepts_grounded(monkeypatch):
    obj = _obj(
        1, "Особняк",
        short_description="Особняк украшен мозаичными панно и лепниной на фасаде.",
    )
    monkeypatch.setattr(
        quest_mod, "chat_completion",
        lambda *a, **k: "У какого объекта фасад украшают мозаичные панно?",
    )
    question = quest_mod.question_for(_settings(), obj)
    assert question == "У какого объекта фасад украшают мозаичные панно?"


def test_question_for_handles_outage(monkeypatch):
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AIServiceUnavailable("нет модели")

    monkeypatch.setattr(quest_mod, "chat_completion", boom)
    obj = _obj(1, "X", short_description="Описание достаточно длинное для вопроса.")
    assert quest_mod.question_for(_settings(), obj) == ""


def test_quest_candidates_filters_in_sql():
    # фильтр по описанию — в SQL: иначе среди ближайших N почти нет объектов
    # с текстом, и квест короче заказанного
    db = _FakeDb([[_obj(1, "Годный", short_description="Описание подлиннее для квеста.")]])
    rows = quest_mod.quest_candidates(db, 59.9, 30.3, 1500.0, categories=["museum"],
                                      not_keyword="мемориальная доска", limit=12)
    assert len(rows) == 1
    sql, params = db.calls[0]
    assert "ST_DWithin" in sql and "desc_min" in sql and "[а-яёА-ЯЁ]" in sql
    assert params["cat_0"] == "museum" and params["not_keyword"] == "мемориальная доска"
    assert params["limit"] == 12


def test_build_quest_without_llm(monkeypatch):
    desc = "Старинное здание построено в 1819 году и хранит коллекцию гравюр."
    candidates = [
        _obj(1, "Первый", short_description=desc),
        _obj(2, "Второй", short_description=desc),
        _obj(3, "Третий", short_description=desc),
    ]
    monkeypatch.setattr(
        quest_mod, "quest_candidates", lambda *a, **k: candidates
    )
    legs = iter([(1, 100.0), (2, 10.0), (3, 120.0)])
    monkeypatch.setattr(
        quest_mod, "nearest_remaining", lambda *a, **k: next(legs, (None, None))
    )
    monkeypatch.setattr(quest_mod, "check_available", lambda s: False)

    quest = quest_mod.build_quest(_FakeDb([]), _settings(), 59.9, 30.3, count=3)
    # точка 2 ближе 25 м к предыдущей — пропущена, как в прогулке (§14)
    assert [p["id"] for p in quest["points"]] == [1, 3]
    assert quest["points"][1]["cumulative_meters"] == 220.0
    assert all(p["question"] for p in quest["points"])
    assert quest["points"][0]["question"] == "Какой объект связан с 1819 годом?"
    assert quest["points"][0]["hint"].startswith("Старинное здание")
    assert quest["walk_minutes"] > 0
    assert quest["polyline"][0] == [59.9, 30.3]


def test_build_quest_skips_english_descriptions(monkeypatch):
    # англоязычные описания (OSM) в квест не берём: вопрос и подсказка
    # получатся на чужом языке — а их читает человек
    russian = _obj(1, "Русский", short_description="Старинное здание с гравюрами и лепниной.")
    english = _obj(
        2, "English", short_description="Historic building with engravings and stucco decoration."
    )
    monkeypatch.setattr(quest_mod, "quest_candidates", lambda *a, **k: [english, russian])
    # фейк уважает список оставшихся: англоязычный объект в нём отсутствует
    monkeypatch.setattr(
        quest_mod, "nearest_remaining",
        lambda db, lat, lon, ids: (ids[0], 50.0) if ids else (None, None),
    )
    monkeypatch.setattr(quest_mod, "check_available", lambda s: False)
    quest = quest_mod.build_quest(_FakeDb([]), _settings(), 59.9, 30.3, count=2)
    assert [p["id"] for p in quest["points"]] == [1]


def test_build_quest_uses_llm_questions(monkeypatch):
    desc = "Особняк украшен мозаичными панно и лепниной на фасаде, а внутри — старинный зал."
    candidates = [_obj(1, "Особняк", short_description=desc)]
    monkeypatch.setattr(
        quest_mod, "quest_candidates", lambda *a, **k: candidates
    )
    monkeypatch.setattr(
        quest_mod, "nearest_remaining", lambda *a, **k: (1, 50.0)
    )
    monkeypatch.setattr(quest_mod, "check_available", lambda s: True)
    monkeypatch.setattr(
        quest_mod, "chat_completion",
        lambda *a, **k: "У какого объекта фасад украшают мозаичные панно?",
    )
    quest = quest_mod.build_quest(_FakeDb([]), _settings(), 59.9, 30.3, count=1)
    assert quest["points"][0]["question"].startswith("У какого объекта")


# --- эндпоинт /api/query --------------------------------------------------------


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


def test_endpoint_surprise(client, monkeypatch):
    from app.api import ai as ai_api

    obj = _obj(7, "Дворик Ангела", address="Невский проспект, 3",
               short_description="Маленький дворик с фресками.")
    seen: dict = {}

    def fake_pick(db, *, categories=None, not_keyword=None, exclude=None):  # noqa: ANN001
        seen["exclude"] = exclude
        return obj

    monkeypatch.setattr(ai_api, "surprise_pick", fake_pick)
    resp = client.post("/api/query", json={"query": "Удиви меня"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "surprise"
    assert [i["id"] for i in data["items"]] == [7]
    assert "Дворик Ангела" in data["explanation"]

    # второй запрос в той же сессии: прошлый объект исключён («ещё раз» не повторяется)
    resp2 = client.post("/api/query", json={"query": "Удиви меня", "session_id": data["session_id"]})
    assert resp2.status_code == 200
    assert seen["exclude"] == [7]


def test_endpoint_taste_requires_favorites(client):
    resp = client.post("/api/query", json={"query": "Похожее на моё избранное"})
    assert resp.status_code == 422
    assert "избранн" in resp.json()["detail"].lower()


def test_endpoint_taste(client, monkeypatch):
    from app.api import ai as ai_api

    taste = {
        "items": [{**_obj(10, "Похожий"), "score": 0.9,
                   "factors": {"semantic": 0.9, "spatial": 0.0, "category": 0.0}}],
        "used": 2, "favorites": 2,
    }
    monkeypatch.setattr(ai_api, "taste_recommend", lambda *a, **k: taste)
    resp = client.post(
        "/api/query",
        json={"query": "Подбери похожее на моё избранное", "favorite_ids": [1, 2]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "taste"
    assert data["items"][0]["factors"]["semantic"] == 0.9


def test_endpoint_taste_without_embeddings(client, monkeypatch):
    from app.api import ai as ai_api

    monkeypatch.setattr(
        ai_api, "taste_recommend",
        lambda *a, **k: {"items": [], "used": 0, "favorites": 2},
    )
    resp = client.post(
        "/api/query",
        json={"query": "Подбери похожее на моё избранное", "favorite_ids": [1, 2]},
    )
    assert resp.status_code == 422


def test_endpoint_collection(client, monkeypatch):
    from app.api import ai as ai_api

    items = [_obj(i, f"Музей {i}") for i in range(1, 8)]
    monkeypatch.setattr(ai_api, "build_collection", lambda *a, **k: items)
    resp = client.post("/api/query", json={"query": "Собери топ-7 музеев и сохрани"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "collection"
    assert data["collection"] == {"name": "Топ-7 музеев", "count": 7}
    assert len(data["items"]) == 7


def test_endpoint_collection_empty(client, monkeypatch):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "build_collection", lambda *a, **k: [])
    resp = client.post("/api/query", json={"query": "Собери подборку мемориалов"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "collection"
    assert data["collection"] is None and data["items"] == []


def test_endpoint_quest(client, monkeypatch):
    from app.api import ai as ai_api

    points = [
        {**_obj(1, "Первый"), "question": "Какой объект связан с 1819 годом?",
         "hint": "Старинное здание.", "leg_meters": 100.0, "cumulative_meters": 100.0},
        {**_obj(2, "Второй"), "question": "О каком объекте идёт речь?",
         "hint": "Особняк с панно.", "leg_meters": 120.0, "cumulative_meters": 220.0},
    ]
    quest = {
        "start": {"latitude": 59.9, "longitude": 30.3},
        "theme": "история", "radius_meters": 1500.0, "count": 2,
        "total_meters": 220.0, "walk_minutes": 3,
        "points": points, "polyline": [[59.9, 30.3], [59.91, 30.31]],
    }
    monkeypatch.setattr(ai_api, "build_quest", lambda *a, **k: quest)
    resp = client.post(
        "/api/query",
        json={"query": "Сделай квест по истории на 5 точек",
              "latitude": 59.9, "longitude": 30.3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "quest"
    assert data["quest"]["count"] == 2 and data["quest"]["theme"] == "история"
    assert [p["question"] for p in data["quest"]["points"]] == [
        "Какой объект связан с 1819 годом?", "О каком объекте идёт речь?"
    ]
    # точки квеста — остановки маршрута: рисуются тем же слоем, что прогулка
    assert data["route"]["count"] == 2 and data["route"]["polyline"]
    assert data["items"][0]["distance_meters"] == 100.0


def test_endpoint_quest_without_origin(client):
    resp = client.post("/api/query", json={"query": "Сделай квест на 5 точек"})
    assert resp.status_code == 422


def _fake_quest() -> dict:
    points = [
        {**_obj(1, "Первый"), "question": "Какой объект связан с 1819 годом?",
         "hint": "Старинное здание.", "leg_meters": 100.0, "cumulative_meters": 100.0},
        {**_obj(2, "Второй"), "question": "О каком объекте идёт речь?",
         "hint": "Особняк с панно.", "leg_meters": 120.0, "cumulative_meters": 220.0},
    ]
    return {
        "start": {"latitude": 59.9, "longitude": 30.3},
        "theme": "история", "radius_meters": 1500.0, "count": 2,
        "total_meters": 220.0, "walk_minutes": 3,
        "points": points, "polyline": [[59.9, 30.3], [59.91, 30.31]],
    }


def test_fun_actions_skip_agent_and_llm(monkeypatch):
    """Фан-фразы (идеи 24–27) разбираются правилами до агента и модели.

    Агент сводит «квест» к прогулке, а «подборку» — к списку, поэтому
    проверка идёт раньше: при доступных агенте и LLM они не вызываются.
    """
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api
    from app.ai import sessions
    from app.config import Settings as _S, get_settings
    from app.main import app

    sessions.store.clear_all()
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: _S(
        _env_file=None, ai_enabled=True, ai_agent_enabled=True, ai_summary_enabled=False
    )

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("агент и LLM не должны вызываться для фан-фраз")

    monkeypatch.setattr(ai_api, "available", lambda s: True)
    monkeypatch.setattr(ai_api, "run_agent", boom)
    monkeypatch.setattr(ai_api, "parse_with_fallback", boom)
    monkeypatch.setattr(ai_api, "build_collection",
                        lambda *a, **k: [_obj(i, f"Музей {i}") for i in range(1, 8)])
    monkeypatch.setattr(ai_api, "build_quest", lambda *a, **k: _fake_quest())

    test_client = TestClient(app, raise_server_exceptions=True)
    try:
        resp = test_client.post("/api/query", json={"query": "Собери топ-7 музеев и сохрани"})
        assert resp.status_code == 200 and resp.json()["action"] == "collection"

        resp = test_client.post(
            "/api/query",
            json={"query": "Сделай квест по истории на 5 точек",
                  "latitude": 59.9, "longitude": 30.3},
        )
        assert resp.status_code == 200 and resp.json()["action"] == "quest"

        resp = test_client.post("/api/query", json={"query": "Удиви меня"})
        assert resp.status_code == 200 and resp.json()["action"] == "surprise"
    finally:
        app.dependency_overrides.clear()


# --- резюме: квест исключён, фан-режим — нет ------------------------------------


def test_summary_skipped_for_quest(monkeypatch):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "available", lambda s: True)
    calls: list = []
    monkeypatch.setattr(ai_api.ai_summary, "summarize",
                        lambda s, r: calls.append(1) or "живой текст")
    result = {"action": "quest", "explanation": "Квест: 5 точек, ~10 мин пешком"}
    ai_api._apply_summary(_settings(ai_summary_enabled=True), result)
    assert result["explanation"] == "Квест: 5 точек, ~10 мин пешком"
    assert calls == []  # названия точек не должны попасть в резюме


def test_summary_applied_for_surprise(monkeypatch):
    from app.api import ai as ai_api

    monkeypatch.setattr(ai_api, "available", lambda s: True)
    monkeypatch.setattr(ai_api.ai_summary, "summarize",
                        lambda s, r: "Случайное открытие — загляните в «Дворик Ангела»!")
    result = {"action": "surprise", "explanation": "Случайное открытие: «Дворик Ангела»"}
    ai_api._apply_summary(_settings(ai_summary_enabled=True), result)
    assert result["explanation"].startswith("Случайное открытие —")


def test_facts_include_collection_and_description():
    from app.ai.summary import build_facts

    result = {
        "action": "collection", "explanation": "Подборка «Топ-7 музеев»: 7 объектов",
        "structured": {}, "total": 7,
        "items": [_obj(1, "Музей", short_description="Короткое описание музея.")],
        "collection": {"name": "Топ-7 музеев", "count": 7},
    }
    facts = build_facts(result)
    assert facts["подборка"] == {"название": "Топ-7 музеев", "объектов": 7}
    assert facts["объекты"][0]["описание"] == "Короткое описание музея."
