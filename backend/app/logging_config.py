"""Настройка логирования (§43): запрос, endpoint, время выполнения, ошибки."""

import logging
import sys
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Логи AI-парсинга и spatial-запросов идут с этими тегами
AI_LOG_TAG = "ai.parse"
SPATIAL_LOG_TAG = "spatial.query"


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Шумные библиотеки
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Логирует каждый HTTP-запрос: метод, путь, статус, длительность."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        path = request.url.path
        if path.startswith("/api"):
            logging.getLogger("access").info(
                "%s %s -> %s (%.1f ms)", request.method, path, response.status_code, duration_ms
            )
        return response
