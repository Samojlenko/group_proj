"""Схема БД: расширения, cultural_objects, districts, locations, индексы, триггер.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography, Geometry
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Immutable-обёртка над array_to_string: generated-колонка требует immutable-выражения,
# а array_to_string(anyarray, text) объявлен в PostgreSQL как stable.
KEYWORDS_TEXT_FN = """
CREATE OR REPLACE FUNCTION cultural_objects_kw_text(p text[])
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT array_to_string(p, ' ')
$$;
"""

SEARCH_VECTOR_SQL = """
    setweight(to_tsvector('russian'::regconfig, coalesce(name, '')), 'A') ||
    setweight(to_tsvector('russian'::regconfig, coalesce(short_description, '')), 'B') ||
    setweight(to_tsvector('russian'::regconfig, coalesce(description, '')), 'C') ||
    setweight(to_tsvector('russian'::regconfig, coalesce(cultural_objects_kw_text(keywords), '')), 'B')
"""

# geometry (4326) — единственный источник истины; geog (geography) нужен для
# точных метровых запросов (ST_DWithin / <-> в метрах) и обслуживается GiST-индексом.
SYNC_GEOMETRY_FN = """
CREATE OR REPLACE FUNCTION cultural_objects_sync_geometry()
RETURNS trigger AS $$
BEGIN
    NEW.geometry := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    NEW.geog := NEW.geometry::geography;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # --- Расширения (§5 geometry/индекс, §27 pgvector) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- Основная сущность (§5, §7) ---
    op.execute(KEYWORDS_TEXT_FN)
    op.create_table(
        "cultural_objects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("long_description", sa.Text(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Double(), nullable=False),
        sa.Column("longitude", sa.Double(), nullable=False),
        sa.Column("geometry", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("geog", Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("opening_hours", sa.Text(), nullable=True),
        sa.Column("working_hours", sa.Text(), nullable=True),
        sa.Column("themes", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("period", sa.Text(), nullable=True),
        sa.Column("art_types", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("keywords", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("metro_stations", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("year_established", sa.Integer(), nullable=True),
        sa.Column("is_free", sa.Boolean(), nullable=True),
        sa.Column("ticket_price", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_latitude_range"),
        sa.CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_longitude_range"),
    )

    # --- Районы (§10) ---
    op.create_table(
        "districts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=True),
        sa.Column("geometry", Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- Именованные места для Location Resolver (§26) ---
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("synonyms", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Double(), nullable=False),
        sa.Column("longitude", sa.Double(), nullable=False),
        sa.Column("geometry", Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- Пространственные и поисковые индексы (§5, §45) ---
    op.execute(
        "CREATE INDEX ix_cultural_objects_geometry "
        "ON cultural_objects USING GIST (geometry)"
    )
    op.execute("CREATE INDEX ix_cultural_objects_category ON cultural_objects (category)")
    op.execute(
        "CREATE INDEX ix_cultural_objects_geog "
        "ON cultural_objects USING GIST (geog)"
    )
    op.execute(
        "CREATE INDEX ix_cultural_objects_search_vector "
        "ON cultural_objects USING GIN (search_vector)"
    )
    # HNSW-индекс pgvector для семантического поиска (§27)
    op.execute(
        "CREATE INDEX ix_cultural_objects_embedding "
        "ON cultural_objects USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX ix_districts_geometry ON districts USING GIST (geometry)")
    op.execute("CREATE INDEX ix_locations_geometry ON locations USING GIST (geometry)")

    # --- Триггер: geometry всегда синхронизирована с latitude/longitude,
    #     updated_at обновляется автоматически ---
    op.execute(SYNC_GEOMETRY_FN)
    op.execute(
        "CREATE TRIGGER trg_cultural_objects_sync "
        "BEFORE INSERT OR UPDATE ON cultural_objects "
        "FOR EACH ROW EXECUTE FUNCTION cultural_objects_sync_geometry();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_cultural_objects_sync ON cultural_objects")
    op.execute("DROP FUNCTION IF EXISTS cultural_objects_sync_geometry()")
    op.execute("DROP FUNCTION IF EXISTS cultural_objects_kw_text(text[])")
    op.drop_table("locations")
    op.drop_table("districts")
    op.drop_table("cultural_objects")
