"""Тесты валидации API (§42, §44) — без подключения к БД."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.objects import validate_categories, validate_limit, validate_radius
from app.config import Settings
from app.database import get_db
from app.main import app

settings = Settings(_env_file=None)

class _UnusedDB:
    """Сессия, которая «взрывается» только при реальном обращении к БД.

    Валидация в endpoint'ах выполняется до первого запроса к БД,
    поэтому тесты проверяют коды 400/422/503 без PostgreSQL.
    """

    def execute(self, *args, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("БД не нужна для этого теста")

    def __getattr__(self, name):  # noqa: ANN001
        raise RuntimeError(f"БД не нужна для этого теста (обращение к {name})")


app.dependency_overrides[get_db] = _UnusedDB
client = TestClient(app, raise_server_exceptions=False)


# --- unit: валидаторы ---------------------------------------------------------

def test_validate_categories_unknown():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        validate_categories(["museum", "zoo"])
    assert exc.value.status_code == 400
    assert "zoo" in str(exc.value.detail)


def test_validate_categories_ok():
    assert validate_categories(["museum", "gallery"]) == ["museum", "gallery"]
    assert validate_categories(None) is None


def test_validate_radius_limit():
    from fastapi import HTTPException

    assert validate_radius(500, settings) == 500
    with pytest.raises(HTTPException) as exc:
        validate_radius(999999, settings)
    assert exc.value.status_code == 400

    assert validate_limit(50, settings) == 50
    with pytest.raises(HTTPException) as exc:
        validate_limit(10_000, settings)
    assert exc.value.status_code == 400


# --- интеграционные: коды ответов --------------------------------------------

def test_docs_available():
    res = client.get("/docs")
    assert res.status_code == 200


def test_openapi_contains_required_paths():
    res = client.get("/openapi.json")
    assert res.status_code == 200
    paths = res.json()["paths"]
    for p in ("/api/objects", "/api/objects/{object_id}", "/api/objects/nearby",
              "/api/objects/radius", "/api/objects/search", "/api/objects/geojson",
              "/api/categories", "/api/statistics", "/api/query", "/api/recommend",
              "/api/health"):
        assert p in paths, f"нет обязательного endpoint'а {p} (§11–§17, §24)"


def test_objects_rejects_bad_category_without_db():
    res = client.get("/api/objects", params={"category": "zoo"})
    assert res.status_code == 400
    assert "zoo" in res.json()["detail"]


def test_objects_rejects_half_coords():
    res = client.get("/api/objects", params={"latitude": 59.9})
    assert res.status_code == 400


def test_radius_rejects_too_big():
    res = client.get(
        "/api/objects/radius",
        params={"latitude": 59.93, "longitude": 30.33, "radius_meters": 999999},
    )
    assert res.status_code == 400
    assert "радиус" in res.json()["detail"].lower() or "Радиус" in res.json()["detail"]


def test_nearby_rejects_bad_latitude():
    res = client.get("/api/objects/nearby", params={"latitude": 999, "longitude": 30.3})
    assert res.status_code == 400


def test_search_rejects_short_query():
    res = client.get("/api/objects/search", params={"q": "а"})
    assert res.status_code == 422  # min_length=2 в Query


def test_geojson_rejects_huge_limit():
    res = client.get("/api/objects/geojson", params={"limit": 100000})
    assert res.status_code == 400


def test_ai_query_returns_503_when_ai_disabled():
    from app.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, ai_enabled=False)
    try:
        res = client.post("/api/query", json={"query": "музеи рядом со мной"})
        assert res.status_code == 503
    finally:
        app.dependency_overrides.pop(get_settings, None)
        get_settings.cache_clear()


def test_recommend_rejects_unknown_category():
    res = client.post("/api/recommend", json={"categories": ["zoo"], "limit": 5})
    assert res.status_code in (400, 503)
    if res.status_code == 400:
        assert "zoo" in res.json()["detail"]
