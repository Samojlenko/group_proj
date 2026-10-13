"""Тесты сборки SQL-фильтров (§15) — без подключения к БД."""

from app.spatial.queries import _filters_sql

BASE = dict(
    categories=None,
    q=None,
    theme=None,
    keyword=None,
    has_website=None,
    has_description=None,
    within_district=False,
)


def test_empty_filters_no_where():
    where, params = _filters_sql(**BASE)
    assert where == ""
    assert params == {}


def test_categories_split_into_named_params():
    where, params = _filters_sql(**{**BASE, "categories": ["museum", "gallery"]})
    assert "c.category IN (:cat_0, :cat_1)" in where
    assert params == {"cat_0": "museum", "cat_1": "gallery"}


def test_not_keyword_excludes_objects():
    """Фильтр «скрыть мемориальные доски» (§8) попадает в WHERE."""
    where, params = _filters_sql(**{**BASE, "not_keyword": "мемориальная доска"})
    assert "not_keyword" in where
    assert params["not_keyword"] == "мемориальная доска"
    # NULL-безопасно: keywords IS NULL -> coalesce в '{}' -> объект остаётся
    assert "coalesce(c.keywords, '{}')" in where


def test_keyword_included_and_not_keyword_excluded_together():
    where, params = _filters_sql(
        **{**BASE, "keyword": "музей", "not_keyword": "мемориальная доска"}
    )
    assert ":keyword" in where
    assert ":not_keyword" in where
    assert params["keyword"] == "музей"


def test_within_district_uses_polygon():
    where, _ = _filters_sql(**{**BASE, "within_district": True})
    assert "ST_Within(c.geometry" in where


def test_has_website_false_is_null_or_empty():
    where, _ = _filters_sql(**{**BASE, "has_website": False})
    assert "c.website IS NULL OR c.website = ''" in where
