"""Таблица amenities: кафе, туалеты, парковки из OSM (идея 28 этапа H).

Отдельная сущность со своим триггером синхронизации геометрии (по образцу
`cultural_objects`, миграция 0001): `latitude/longitude` — источник истины,
`geometry`/`geog` заполняются на INSERT/UPDATE, GiST-индекс обслуживает
метровые запросы (ST_DWithin, KNN).

Revision ID: 0003_amenities
Revises: 0002_pg_trgm
Create Date: 2026-10-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography, Geometry

revision: str = "0003_amenities"
down_revision: Union[str, None] = "0002_pg_trgm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYNC_GEOMETRY_FN = """
CREATE OR REPLACE FUNCTION amenities_sync_geometry()
RETURNS trigger AS $$
BEGIN
    NEW.geometry := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    NEW.geog := NEW.geometry::geography;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(SYNC_GEOMETRY_FN)
    op.create_table(
        "amenities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("osm_type", sa.Text(), nullable=False),
        # id в OSM давно переросли int4 (узлы — до 10^10) — только bigint
        sa.Column("osm_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("opening_hours", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Double(), nullable=False),
        sa.Column("longitude", sa.Double(), nullable=False),
        sa.Column("geometry", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("geog", Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("osm_type", "osm_id", name="uq_amenities_osm"),
    )
    op.execute("CREATE INDEX ix_amenities_geog ON amenities USING GIST (geog)")
    op.execute("CREATE INDEX ix_amenities_category ON amenities (category)")
    op.execute(
        "CREATE TRIGGER trg_amenities_sync "
        "BEFORE INSERT OR UPDATE ON amenities "
        "FOR EACH ROW EXECUTE FUNCTION amenities_sync_geometry();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_amenities_sync ON amenities")
    op.drop_table("amenities")
    op.execute("DROP FUNCTION IF EXISTS amenities_sync_geometry()")
