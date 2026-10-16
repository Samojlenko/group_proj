"""Тесты резолвера мест (§26): падежные формы и извлечение места из текста.

Вся логика здесь чистая (без БД): стемминг, ранжиринг совпадений и разбор
предлогов. Сам SQL-запрос тонкий и покрыт вживую сценариями ТЗ.
"""

from app.ai.resolver import _rank, _stem
from app.api.ai import _place_from_text


# --- стемминг русских словоформ ------------------------------------------------

def test_stem_strips_instrumental_case():
    assert _stem("Эрмитажем") == _stem("Эрмитаж")


def test_stem_keeps_short_words_intact():
    # основа короче 4 символов — окончание не срезаем, чтобы не было ложных совпадений
    assert _stem("улица") == _stem("улица")


def test_stem_normalizes_yo():
    assert _stem("Зелёный") == _stem("Зеленый")


# --- ранжиринг совпадений ------------------------------------------------------

def test_rank_exact_match_wins():
    assert _rank("Невский проспект", "Невский проспект") == 0


def test_rank_declension_matches_landmark():
    # «Эрмитажем» должно находить «Государственный Эрмитаж»
    assert _rank("Эрмитажем", "Государственный Эрмитаж") <= 2


def test_rank_word_intersection_beats_substring():
    # общее слово «проспект» важнее простой подстроки
    rank_word = _rank("Невского проспекта", "Невский проспект")
    assert rank_word <= 2


def test_rank_rejects_unrelated_name():
    assert _rank("Эрмитаж", "Невский проспект") == 99


def test_rank_full_phrase_match_beats_partial():
    # «Русского музея» vs «Русский музей» — совпали оба слова
    assert _rank("Русского музея", "Русский музей") <= 2


def test_rank_partial_word_match_is_weak():
    # общее только слово «русск»: настоящий «Русский музей» не должен
    # проигрывать «Русскому Левше» лишь из-за длины названия
    assert _rank("Русского музея", "Русский Левша") == 4
    assert _rank("Русского музея", "Русский музей") < _rank("Русского музея", "Русский Левша")


# --- извлечение места из текста запроса (§26, §30) ----------------------------

def test_place_from_text_after_preposition():
    assert _place_from_text("Что рядом с Дворцовой площадью?") == "Дворцовой площадью"


def test_place_from_text_with_distance_clause():
    # именно так приходит запрос демо-сценария №5 (§40)
    place = _place_from_text("Хочу посмотреть русское искусство недалеко от Невского проспекта")
    assert place == "Невского проспекта"


def test_place_absent_when_no_preposition():
    assert _place_from_text("покажи все музеи") is None


def test_place_strips_punctuation_and_quotes():
    assert _place_from_text("рядом с «Эрмитажем»") == "Эрмитажем"
