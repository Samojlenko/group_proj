"""Именованные географические места (§26: Location Resolver).

Слой собственных «точек отсчёта»: улицы, площади, объекты-ориентиры.
Сначала резолвится по своей БД, внешний геокодер — только fallback.
"""

from geoalchemy2 import Geometry
from sqlalchemy import ARRAY, Double, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    synonyms: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    kind: Mapped[str | None] = mapped_column(Text)  # street | square | landmark | metro | object
    latitude: Mapped[float] = mapped_column(Double, nullable=False)
    longitude: Mapped[float] = mapped_column(Double, nullable=False)
    geometry = mapped_column(Geometry("GEOMETRY", srid=4326, spatial_index=True), nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Location {self.id} {self.name!r}>"
