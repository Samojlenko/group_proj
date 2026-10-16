"""Тесты «Прогулки» (POST /api/route).

Логика сборки маршрута проверяется на подменённых кандидатах (без БД),
валидация — через TestClient с «пустой» сессией: все проверки параметров
выполняются до первого обращения к PostgreSQL.
"""

from fastapi.testclient import TestClient

from app.api.objects import ALLOWED_CATEGORIES
from app.database import get_db
from app.main import app
from app.spatial import routes as route_mod


class _UnusedDB:
    """Сессия, которая «взрывается» при реальном обращении к БД."""

    def execute(self, *args, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("БД не нужна для этого теста")

    def __getattr__(self, name):  # noqa: ANN001
        raise RuntimeError(f"БД не нужна для этого теста (обращение к {name})")


app.dependency_overrides[get_db] = _UnusedDB
client = TestClient(app, raise_server_exceptions=False)


# --- unit: сборка маршрута -----------------------------------------------------

_CANDIDATES = [
    {
        "id": 1, "name": "Объект А", "category": "museum",
        "latitude": 59.9410, "longitude": 30.3450, "distance_meters": 90.0,
    },
    {
        "id": 2, "name": "Объект Б", "category": "monument",
        "latitude": 59.9420, "longitude": 30.3460, "distance_meters": 140.0,
    },
    {
        "id": 3, "name": "Объект В", "category": "gallery",
        "latitude": 59.9430, "longitude": 30.3470, "distance_meters": 210.0,
    },
]

# перегоны «по прямой» от предыдущей точки до объекта
_LEGS = {1: 90.0, 2: 150.0, 3: 400.0}


def _patch(monkeypatch, candidates=_CANDIDATES, legs=_LEGS):  # noqa: ANN001
    monkeypatch.setattr(route_mod, "route_candidates", lambda *a, **k: list(candidates))

    def fake_nearest(db, cur_lat, cur_lon, ids):  # noqa: ANN001
        i = min(ids)  # всегда «ближайший» — меньший id, порядок предсказуем
        return i, legs[i]

    monkeypatch.setattr(route_mod, "nearest_remaining", fake_nearest)


def test_route_respects_count_and_sums_legs(monkeypatch):
    _patch(monkeypatch)
    out = route_mod.build_walk_route(None, 59.9400, 30.3440, count=2)
    assert [s["id"] for s in out["items"]] == [1, 2]
    assert out["count"] == 2
    assert out["total_meters"] == 240.0
    # 240 м / 80 м в минуту = 3 минуты
    assert out["walk_minutes"] == 3
    # накопительные метры растут по ходу маршрута
    assert out["items"][1]["cumulative_meters"] == 240.0


def test_route_polyline_starts_at_start_point(monkeypatch):
    _patch(monkeypatch)
    out = route_mod.build_walk_route(None, 59.9400, 30.3440, count=3)
    assert out["polyline"][0] == [59.9400, 30.3440]
    assert len(out["polyline"]) == len(out["items"]) + 1
    assert out["polyline"][1] == [59.9410, 30.3450]


def test_route_shorter_when_few_candidates(monkeypatch):
    _patch(monkeypatch, candidates=_CANDIDATES[:1], legs={1: 90.0})
    out = route_mod.build_walk_route(None, 59.9400, 30.3440, count=6)
    assert out["count"] == 1
    assert out["items"][0]["leg_meters"] == 90.0


def test_route_skips_too_close_stops(monkeypatch):
    # вторая остановка в 10 м от первой — та же точка на карте: пропускаем
    _patch(monkeypatch, legs={1: 100.0, 2: 10.0, 3: 400.0})
    out = route_mod.build_walk_route(None, 59.9400, 30.3440, count=2)
    assert [s["id"] for s in out["items"]] == [1, 3]
    # перегон к пропущенному объекту в сумму не входит
    assert out["total_meters"] == 500.0


def test_route_empty_candidates_is_empty_route(monkeypatch):
    _patch(monkeypatch, candidates=[], legs={})
    out = route_mod.build_walk_route(None, 59.9400, 30.3440, count=6)
    assert out["count"] == 0
    assert out["total_meters"] == 0.0
    assert out["walk_minutes"] == 0
    assert out["polyline"] == [[59.9400, 30.3440]]


def test_route_stop_fields_complete(monkeypatch):
    _patch(monkeypatch)
    out = route_mod.build_walk_route(None, 59.9400, 30.3440, count=1)
    stop = out["items"][0]
    assert {"id", "name", "category", "latitude", "longitude",
            "leg_meters", "cumulative_meters", "distance_from_start_meters"} <= set(stop)
    assert stop["distance_from_start_meters"] == 90.0


# --- API: валидация ------------------------------------------------------------

def test_route_endpoint_rejects_single_stop():
    r = client.post("/api/route", json={"latitude": 59.94, "longitude": 30.34, "count": 1})
    assert r.status_code == 422


def test_route_endpoint_rejects_unknown_category():
    r = client.post(
        "/api/route",
        json={"latitude": 59.94, "longitude": 30.34, "categories": ["zoo"]},
    )
    assert r.status_code == 400


def test_route_endpoint_rejects_huge_radius():
    r = client.post(
        "/api/route",
        json={"latitude": 59.94, "longitude": 30.34, "radius_meters": 20000},
    )
    assert r.status_code == 422


def test_route_endpoint_requires_coordinates():
    r = client.post("/api/route", json={"radius_meters": 1000})
    assert r.status_code == 422


def test_route_in_openapi():
    paths = app.openapi()["paths"]
    assert "/api/route" in paths
    assert "post" in paths["/api/route"]


def test_allowed_categories_fixture_sanity():
    # страховка: категории в тестовых кандидатах — настоящие
    assert {c["category"] for c in _CANDIDATES} <= ALLOWED_CATEGORIES
