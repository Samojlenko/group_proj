"""Точка входа FastAPI."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import health, objects
from app.api import ai as ai_router
from app.api import analytics as analytics_router
from app.config import get_settings
from app.logging_config import AccessLogMiddleware, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001, ANN201
    logger.info("Старт сервиса. AI=%s model=%s", settings.ai_enabled, settings.ai_model)
    yield
    logger.info("Остановка сервиса")


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
app.include_router(analytics_router.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    logger.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера"})


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "cultural-map-api", "docs": "/docs", "health": "/api/health"}
