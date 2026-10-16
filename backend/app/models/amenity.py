"""Модель OSM-заведений (идея 28): кафе, туалеты, парковки.

Отдельная таблица, а не `cultural_objects`: это не объекты культурной карты —
у них другой набор полей (нет эмбеддингов, описаний, категорий культуры) и
другой жизненный цикл (пересобираются из OSM-тайлов скриптом
`fetch_amenities.py`). Пространственные запросы — те же: PostGIS, `geog`
в метрах и GiST-индекс.
"""

from datetime import datetime

from geoalchemy2 import Geography, Geometry
from sqlalchemy import BigInteger, DateTime, Double, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Amenity(Base):
    __tablename__ = "amenities"
    __table_args__ = (UniqueConstraint("osm_type", "osm_id", name="uq_amenities_osm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # происхождение: node/way + id в OSM (id бывают больше 2^31 — bigint)
    osm_type: Mapped[str] = mapped_column(Text, nullable=False)
    osm_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    category: Mapped[str] = mapped_column(Text, nullable=False)  # cafe|toilet|parking
    name: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    opening_hours: Mapped[str | None] = mapped_column(Text)

    latitude: Mapped[float] = mapped_column(Double, nullable=False)
    longitude: Mapped[float] = mapped_column(Double, nullable=False)
    # синхронизируются с latitude/longitude триггером (см. миграцию 0003)
    geometry = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    geog = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=True)

    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Amenity {self.id} {self.name!r} ({self.category})>"
