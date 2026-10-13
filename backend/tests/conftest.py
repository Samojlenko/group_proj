"""Общие фикстуры тестов."""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://culturalfind:culturalfind@localhost:5432/culturalfind")
os.environ.setdefault("AI_ENABLED", "false")


@pytest.fixture(scope="session")
def settings():  # noqa: ANN201
    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings()
