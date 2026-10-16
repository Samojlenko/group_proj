"""Погода (идея 29, этап H): open-meteo, без ключей.

GET /api/weather отдаёт текущую погоду и прогноз на день; недоступность
внешнего сервиса — 503 с человеческим текстом (§53: отказ честнее выдумки).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.ai import weather as weather_mod
from app.config import Settings, get_settings
from app.schemas.weather_schemas import WeatherInfo

router = APIRouter(prefix="/api", tags=["weather"])


@router.get(
    "/weather",
    response_model=WeatherInfo,
    summary="Погода в Петербурге сейчас (идея 29)",
)
def current_weather(settings: Settings = Depends(get_settings)) -> dict:
    data = weather_mod.get_weather(settings)
    if data is None:
        raise HTTPException(503, weather_mod.UNAVAILABLE_HINT)
    return data
