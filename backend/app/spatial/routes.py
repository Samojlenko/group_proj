"""«Прогулка»: пеший маршрут по культурным объектам.

Пользователь ставит точку на карте и просит прогулку — сервис выбирает
объекты в радиусе и выстраивает порядок обхода жадным алгоритмом
(каждый следующий — ближайший к текущей точке). Все расстояния считает
PostGIS по колонке ``geog`` (метры), Python только оркестрирует запросы
и склеивает результат (§45).

Полученный маршрут — это «нитка» по прямой между остановками: без дорожного
графа точные пешие пути не построить, поэтому перегоны — геодезические
расстояния, а время — оценка по средней скорости пешехода.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.spatial.queries import radius_objects

# средняя скорость пешехода в центре города: 4,8 км/ч = 80 м/мин
WALK_SPEED_M_PER_MIN = 80.0

# остановки ближе этого расстояния друг к другу слипаются на карте
# (музеи в одном здании) — такие объекты в маршрут не берём
MIN_LEG_METERS = 25.0


def route_candidates(
    db: Session,
    lat: float,
    lon: float,
    radius_m: float,
    *,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
    limit: int = 60,
) -> list[dict]:
    """Кандидаты для прогулки: объекты в радиусе, ближние — первыми (KNN)."""
    items, _total, _in_radius = radius_objects(
        db,
        lat,
        lon,
        radius_m,
        categories=categories,
        not_keyword=not_keyword,
        limit=limit,
    )
    return items


def nearest_remaining(
    db: Session,
    cur_lat: float,
    cur_lon: float,
    ids: list[int],
) -> tuple[int | None, float | None]:
    """Ближайший к текущей точке объект среди оставшихся — KNN-поиск в PostGIS.

    Возвращает ``(id, метры)``; ``(None, None)``, если список пуст
    или объекты исчезли из БД.
    """
    if not ids:
        return None, None
    row = db.execute(
        text(
            """
            SELECT c.id, ST_Distance(c.geog, CAST(:point AS geography)) AS leg_m
            FROM cultural_objects c
            WHERE c.id = ANY(:ids)
            ORDER BY c.geog <-> CAST(:point AS geography)
            LIMIT 1
            """
        ),
        {"point": f"SRID=4326;POINT({cur_lon} {cur_lat})", "ids": list(ids)},
    ).mappings().first()
    if row is None or row["leg_m"] is None:
        return None, None
    return int(row["id"]), float(row["leg_m"])


def build_walk_route(
    db: Session,
    lat: float,
    lon: float,
    *,
    radius_m: float = 1500.0,
    count: int = 6,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
) -> dict:
    """Собирает прогулку: порядок обхода, длины перегонов, полилиния.

    ``count`` — целевое число остановок; если объектов в радиусе меньше,
    маршрут получится короче. Кандидатов берём с запасом (count × 8),
    чтобы жадный алгоритм не упирался в отброшенный хвост.
    """
    candidates = route_candidates(
        db,
        lat,
        lon,
        radius_m,
        categories=categories,
        not_keyword=not_keyword,
        limit=min(max(count * 8, 24), 80),
    )
    by_id = {c["id"]: c for c in candidates}
    remaining = list(by_id)
    stops: list[dict] = []
    cur_lat, cur_lon = lat, lon
    total_m = 0.0

    while remaining and len(stops) < count:
        obj_id, leg_m = nearest_remaining(db, cur_lat, cur_lon, remaining)
        if obj_id is None or leg_m is None or obj_id not in by_id:
            break
        remaining.remove(obj_id)
        if stops and leg_m < MIN_LEG_METERS:
            # почти та же точка, что и предыдущая остановка: номер на карте
            # не будет виден — объект пропускаем, ищем следующий
            continue
        obj = by_id[obj_id]
        total_m += leg_m
        stops.append(
            {
                "id": obj_id,
                "name": obj["name"],
                "category": obj["category"],
                "latitude": obj["latitude"],
                "longitude": obj["longitude"],
                "leg_meters": round(leg_m, 1),
                "cumulative_meters": round(total_m, 1),
                "distance_from_start_meters": obj.get("distance_meters"),
            }
        )
        cur_lat, cur_lon = obj["latitude"], obj["longitude"]

    polyline = [[lat, lon]] + [[s["latitude"], s["longitude"]] for s in stops]
    return {
        "start": {"latitude": lat, "longitude": lon},
        "radius_meters": radius_m,
        "count": len(stops),
        "total_meters": round(total_m, 1),
        "walk_minutes": int(round(total_m / WALK_SPEED_M_PER_MIN)) if stops else 0,
        "items": stops,
        "polyline": polyline,
    }
