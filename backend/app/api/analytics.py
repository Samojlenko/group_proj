"""Аналитические endpoint'ы (§36–§38)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.spatial import analytics as a

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/district", summary="Граница Центрального района (GeoJSON, §10)")
def district(db: Session = Depends(get_db)) -> dict:
    return a.district_geojson(db)


@router.get("/analytics/heatmap", summary="Плотность объектов: ячейки сетки (§37)")
def heatmap(
    cell_m: float = Query(default=100.0, ge=20, le=2000, description="Размер ячейки, м"),
    category: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    return a.heatmap(db, cell_m=cell_m, categories=category or None)


@router.get("/analytics/density", summary="Самые насыщенные зоны (§36, §37)")
def density(
    cell_m: float = Query(default=250.0, ge=50, le=5000),
    top: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    return a.density_grid(db, cell_m=cell_m, top=top)


@router.get("/analytics/neighbours", summary="Расстояния до ближайших соседей (§36)")
def neighbours(
    category: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    return a.nearest_neighbour_stats(db, categories=category or None)
