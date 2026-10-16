"""Тесты этапа D: опечатки (pg_trgm), латиница, «минуты → метры», синонимы.

Чистая логика (правила, транслитерация) проверяется напрямую; SQL-часть
резолвера — на скриптованном моке сессии: последовательность запросов
(LIKE-префикс → LIKE-подстрока → триграммы → выборка строк) и ранжиринг.
"""
from __future__ import annotations

from app.ai import resolver
from app.ai.resolver import (
    _latin_to_cyrillic,
    _lookup,
    _name_variants,
)
from app.ai.rules import detect_refinement, parse_rules
from app.config import Settings


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _ScriptedDB:
    """Мок сессии: отдаёт заранее заданные строки на последовательные execute()."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params or {}))
        rows = self._responses.pop(0) if self._responses else []
        return _Rows(rows)


_LOC_ROW = {
    "id": 7, "name": "Дворцовая площадь", "latitude": 59.9387, "longitude": 30.3162,
    "kind": "square", "source": "seed", "synonyms": ["Дворцовая"], "match_rank": 0,
}


# --- «минуты → метры» (§14) ------------------------------------------------------

def test_walk_minutes_become_radius():
    sq = parse_rules("музеи в 10 минутах ходьбы от Эрмитажа")
    assert sq is not None and sq.action == "radius_search"
    assert sq.radius_meters == 800.0  # 10 минут × 80 м/мин
    assert sq.place == "Эрмитажа"
    assert sq.categories == ["museum"]


def test_walk_minutes_peшком():
    sq = parse_rules("что посмотреть в 5 минутах пешком от Невского проспекта")
    assert sq is not None and sq.action == "recommend"
    assert sq.radius_meters == 400.0
    assert sq.place == "Невского проспекта"


def test_walk_duration_is_not_radius():
    # «погулка на час» — время прогулки; радиус считает схема (60×80 → кламп 3000)
    sq = parse_rules("погулять час от Эрмитажа")
    assert sq is not None and sq.action == "walk"
    assert sq.duration_minutes == 60
    assert sq.radius_meters == 3000.0


def test_minutes_without_walking_words_are_not_radius():
    sq = parse_rules("погулка на 30 минут от Дворцовой площади")
    assert sq is not None and sq.action == "walk"
    assert sq.duration_minutes == 30


def test_too_long_walk_minutes_rejected():
    # 500 минут ≈ 40 км — за пределами допустимого радиуса, это не радиусный запрос
    sq = parse_rules("музеи в 500 минутах ходьбы от Эрмитажа")
    assert sq is None or sq.action != "radius_search"


def test_minutes_refinement():
    ref = detect_refinement("а в 5 минутах ходьбы")
    assert ref == {"kind": "radius", "radius": 400.0}


# --- латиница и транслитерация (§13) --------------------------------------------

def test_latin_translit_words():
    assert _latin_to_cyrillic("Nevskiy Prospekt") == "невский проспект"
    assert _latin_to_cyrillic("Pestelya") == "пестеля"
    assert _latin_to_cyrillic("Ligovskiy") == "лиговский"


def test_latin_dictionary_and_phrases():
    assert _name_variants("Hermitage") == ["эрмитаж"]
    assert _name_variants("Winter Palace")[0] == "зимний дворец"
    assert _name_variants("Dvortsovaya Ploshchad")[0] == "дворцовая площадь"


def test_latin_variants_only_for_latin():
    assert _name_variants("Дворцовая площадь") == []


def _patch_resolve(monkeypatch, hits: dict[str, dict]):
    """Подменяет _lookup: hits — что возвращать по кириллическому needle."""
    calls: list[str] = []

    def fake_lookup(db, sql, needle, **kwargs):
        calls.append(needle)
        if needle in hits and "cultural_objects" not in sql:
            return dict(hits[needle])
        return None

    monkeypatch.setattr(resolver, "_lookup", fake_lookup)
    return calls


def test_latin_resolves_cyrillic_variant_first(monkeypatch):
    calls = _patch_resolve(monkeypatch, {"невский проспект": _LOC_ROW})
    out = resolver.resolve_place(_ScriptedDB([]), Settings(), "Nevsky Prospekt")
    assert out["label"] == "Дворцовая площадь"
    assert calls[0] == "невский проспект"  # перевод впереди исходной латиницы


def test_mixed_latin_keeps_original_first(monkeypatch):
    row = {**_LOC_ROW, "name": "Государственный Эрмитаж"}
    calls = _patch_resolve(monkeypatch, {"музей эрмитаж": row})
    out = resolver.resolve_place(_ScriptedDB([]), Settings(), "музей Hermitage")
    assert out["label"] == "Государственный Эрмитаж"
    assert calls[0] == "музей Hermitage"  # сначала пробуем как написал человек
    assert "музей эрмитаж" in calls


# --- синонимы из колонки locations.synonyms (§15) --------------------------------

def test_lookup_matches_synonym_and_keeps_label():
    db = _ScriptedDB([[
        {"id": 9, "name": "Михайловский дворец", "latitude": 59.94, "longitude": 30.33,
         "kind": "landmark", "source": "seed",
         "synonyms": ["Михайловский", "Русский музей", "Государственный Русский музей"]},
    ]])
    row = _lookup(db, resolver._LOCATIONS_SELECT, "Русского музея",
                  table="locations", synonyms=True)
    assert row is not None
    assert row["name"] == "Михайловский дворец"
    assert row["matched_label"] == "Русский музей"


def test_exact_synonym_of_location_beats_partial_object(monkeypatch):
    # «Зимний дворец» — объект «Зимний дворец Петра I» совпал частично,
    # но у локации «Государственный Эрмитаж» есть точный синоним
    objects_row = {"id": 5, "name": "Зимний дворец Петра I", "latitude": 59.945,
                   "longitude": 30.31, "source": "cultural_object", "match_rank": 1}
    location_row = {"id": 9, "name": "Государственный Эрмитаж", "latitude": 59.9398,
                    "longitude": 30.3146, "kind": "landmark", "source": "seed",
                    "synonyms": ["Зимний дворец"], "match_rank": 0,
                    "matched_label": "Зимний дворец"}

    def fake_lookup(db, sql, needle, **kwargs):
        if "cultural_objects" in sql:
            return dict(objects_row)
        return dict(location_row)

    monkeypatch.setattr(resolver, "_lookup", fake_lookup)
    out = resolver.resolve_place(_ScriptedDB([]), Settings(), "Зимний дворец")
    assert out["label"] == "Зимний дворец"
    assert out["latitude"] == 59.9398  # координаты Эрмитажа, а не «дворца Петра I»


# --- опечатки: триграммный проход (§12) ------------------------------------------

def test_trigram_fallback_fixes_typo():
    db = _ScriptedDB([
        [],                               # LIKE по префиксу основы
        [],                               # LIKE по подстроке
        [{"id": 7, "sim": 0.75}],         # триграммы
        [dict(_LOC_ROW)],                 # выборка строки по id
    ])
    row = _lookup(db, resolver._LOCATIONS_SELECT, "двоцовая площадь",
                  table="locations", synonyms=True)
    assert row is not None
    assert row["name"] == "Дворцовая площадь"
    assert row["match_rank"] == 4  # это догадка «на исправление опечатки»


def test_trigram_below_threshold_returns_none():
    db = _ScriptedDB([[], [], []])  # триграммы выше порога не нашлись
    assert _lookup(db, resolver._LOCATIONS_SELECT, "атлантида",
                   table="locations", synonyms=True) is None


def test_fuzzy_disabled_skips_trigram_pass():
    db = _ScriptedDB([[], []])
    assert _lookup(db, resolver._OBJECT_SELECT, "двоцовая площадь",
                   table="cultural_objects", fuzzy=False) is None
    assert len(db.calls) == 2  # до триграммного запроса дело не дошло
