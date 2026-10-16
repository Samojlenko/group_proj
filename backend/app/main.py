"""Точка входа FastAPI."""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import health, objects
from app.api import ai as ai_router
from app.api import amenities as amenities_router
from app.api import analytics as analytics_router
from app.api import route as route_router
from app.api import weather as weather_router
from app.config import get_settings
from app.logging_config import AccessLogMiddleware, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001, ANN201
    logger.info("Старт сервиса. AI=%s model=%s", settings.ai_enabled, settings.ai_model)
    if settings.ai_enabled:
        # прогрев модели в фоне: холодная загрузка qwen2.5:7b может занять
        # больше таймаута запроса, и первый вопрос пользователя зря ушёл бы
        # в rule-based fallback (§53). Прогрев неблокирующий и best-effort.
        threading.Thread(target=_warm_up_ai, name="ai-warmup", daemon=True).start()
    yield
    logger.info("Остановка сервиса")


def _warm_up_ai() -> None:
    """Неблокирующий прогрев AI-модели при старте сервиса."""
    try:
        from app.ai import client as ai_client

        if not ai_client.check_available(settings):
            logger.info("Прогрев AI: inference server недоступен — пропущено")
            return
        ai_client.chat_completion(settings, [{"role": "user", "content": "ping"}])
        logger.info("Прогрев AI: модель загружена")
    except Exception as exc:  # noqa: BLE001 — прогрев не должен ломать старт сервиса
        logger.info("Прогрев AI не удался: %s", exc)


app = FastAPI(
    title="Культурная карта Санкт-Петербурга",
    description=(
        "Интеллектуальный геопространственный веб-сервис культурных объектов "
        "Центрального района Санкт-Петербурга.\n\n"
        "- Пространственные запросы выполняются в PostgreSQL + PostGIS (§11–§16)\n"
        "- Естественный язык → валидируемый структурированный запрос (§22–§25)\n"
        "- Семантический поиск и рекомендации на pgvector (§27–§30)"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(AccessLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo-проект; в проде ограничить списком origins
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(objects.router)
app.include_router(ai_router.router)
app.include_router(amenities_router.router)
app.include_router(analytics_router.router)
app.include_router(route_router.router)
app.include_router(weather_router.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    logger.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера"})


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "cultural-map-api", "docs": "/docs", "health": "/api/health"}
