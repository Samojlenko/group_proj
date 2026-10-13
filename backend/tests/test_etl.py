"""Тесты утилит ETL (§9, §10): геометрия, расстояния, нормализация имён."""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from etl_lib import (  # noqa: E402
    haversine_m,
    name_similarity,
    normalize_name,
    point_in_polygon,
    point_in_ring,
)

SQUARE = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]]


def test_point_in_ring_inside_outside():
    assert point_in_ring(0.5, 0.5, SQUARE[0])
    assert not point_in_ring(1.5, 0.5, SQUARE[0])
    assert not point_in_ring(-0.1, 0.5, SQUARE[0])


def test_point_in_polygon_with_hole():
    hole = [(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6), (0.4, 0.4)]
    poly = [SQUARE[0], hole]
    assert point_in_polygon(0.2, 0.2, poly)
    assert not point_in_polygon(0.5, 0.5, poly)  # внутри дыры


def test_haversine_known_distance():
    # Невский проспект (30.35, 59.93) -> Дворцовая площадь (30.315, 59.939)
    d = haversine_m(59.93, 30.35, 59.939, 30.315)
    assert 1500 < d < 3000  # ~2 км по прямой
    assert haversine_m(59.93, 30.35, 59.93, 30.35) == 0


def test_normalize_name():
    assert normalize_name("«Эрмитаж»") == normalize_name("  Эрмитаж ")
    assert normalize_name("Музей-заповедник") == normalize_name("музей заповедник")


def test_name_similarity():
    assert name_similarity("Музей Ф. М. Достоевского", "Музей Ф. М. Достоевского") == 1.0
    assert name_similarity("Музей Ф. М. Достоевского", "Музей Достоевского") >= 0.5
    assert name_similarity("Эрмитаж", "Автобусная остановка") < 0.2
