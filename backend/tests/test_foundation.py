"""Этап 1: тесты фундамента — конфигурация, категории, health (§50)."""

from app.constants import CATEGORY_LABELS, category_label
from app.config import Settings


def test_categories_have_russian_labels():
    """§6: категории отображаются на русском."""
    assert CATEGORY_LABELS["museum"] == "Музеи"
    assert CATEGORY_LABELS["monument"] == "Памятники"
    assert CATEGORY_LABELS["memorial"] == "Мемориалы"
    assert CATEGORY_LABELS["gallery"] == "Галереи"


def test_unknown_category_label_fallback():
    """§6: новые категории можно добавить без изменения схемы."""
    assert category_label("theatre") == "theatre"


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.max_radius_meters > 0
    assert s.max_limit > 0
    # §28: веса гибридного ранжирования задаются конфигурацией, а не кодом
    total = s.score_weight_semantic + s.score_weight_spatial + s.score_weight_category
    assert abs(total - 1.0) < 1e-6


def test_settings_weights_from_env(monkeypatch):
    monkeypatch.setenv("SCORE_WEIGHT_SEMANTIC", "0.7")
    monkeypatch.setenv("SCORE_WEIGHT_SPATIAL", "0.2")
    monkeypatch.setenv("SCORE_WEIGHT_CATEGORY", "0.1")
    s = Settings(_env_file=None)
    assert s.score_weight_semantic == 0.7
