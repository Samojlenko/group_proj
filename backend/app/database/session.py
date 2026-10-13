"""Подключение к PostgreSQL + PostGIS."""

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(get_settings().db_url, pool_pre_ping=True, pool_size=10, future=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: сессия на запрос."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_db() -> dict:
    """Проверка состояния БД для /api/health (§17)."""
    result: dict = {"status": "ok"}
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
        postgis = conn.execute(text("SELECT postgis_version()")).scalar()
        pgvector = conn.execute(
            text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        ).scalar()
        objects = conn.execute(text("SELECT count(*) FROM cultural_objects")).scalar()
        districts = conn.execute(text("SELECT count(*) FROM districts")).scalar()
    result.update(
        {
            "postgis_version": postgis,
            "pgvector": bool(pgvector),
            "objects_count": objects,
            "districts_count": districts,
        }
    )
    return result
