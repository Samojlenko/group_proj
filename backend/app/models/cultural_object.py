"""Модель основной сущности: cultural_objects (§5, §7)."""

from datetime import datetime

from geoalchemy2 import Geography, Geometry
from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, Boolean, Computed, DateTime, Double, Integer, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Выражение полнотекстового индекса (§15: поиск по названию/описанию/ключевым словам).
# Множественный вес: название важнее описания.
# 'russian'::regconfig и cultural_objects_kw_text(...) — чтобы выражение было immutable
# (требование PostgreSQL для generated-колонок; см. миграцию 0001).
_SEARCH_VECTOR_SQL = """
    setweight(to_tsvector('russian'::regconfig, coalesce(name, '')), 'A') ||
    setweight(to_tsvector('russian'::regconfig, coalesce(short_description, '')), 'B') ||
    setweight(to_tsvector('russian'::regconfig, coalesce(description, '')), 'C') ||
    setweight(to_tsvector('russian'::regconfig, coalesce(cultural_objects_kw_text(keywords), '')), 'B')
"""


class CulturalObject(Base):
    __tablename__ = "cultural_objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)  # museum|monument|memorial|gallery (+ расширяемо, §6)
    description: Mapped[str | None] = mapped_column(Text)
    short_description: Mapped[str | None] = mapped_column(Text)
    long_description: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)

    latitude: Mapped[float] = mapped_column(Double, nullable=False)
    longitude: Mapped[float] = mapped_column(Double, nullable=False)
    # geometry синхронизируется с latitude/longitude триггером (см. миграцию 0001)
    geometry = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    # geography-копия (заполняется тем же триггером) — для метровых операций и GiST
    geog = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=True)

    website: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    opening_hours: Mapped[str | None] = mapped_column(Text)
    working_hours: Mapped[str | None] = mapped_column(Text)

    # Дополнительные характеристики (§7) — NULL, если данные отсутствуют (не выдумываем!)
    themes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    period: Mapped[str | None] = mapped_column(Text)
    art_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    metro_stations: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    year_established: Mapped[int | None] = mapped_column(Integer)
    is_free: Mapped[bool | None] = mapped_column(Boolean)
    ticket_price: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)

    # Происхождение записи (§8)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(Text)

    # Семантический вектор (§27), заполняется скриптом generate_embeddings.py
    embedding = mapped_column(Vector(768), nullable=True)

    # Полнотекстовый векторный столбец (STORED, генерируется PostgreSQL)
    search_vector = mapped_column(
        TSVECTOR,
        Computed(_SEARCH_VECTOR_SQL, persisted=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CulturalObject {self.id} {self.name!r} ({self.category})>"
