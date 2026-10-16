"""Схема погоды (идея 29, этап H)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WeatherInfo(BaseModel):
    """Текущая погода и прогноз на день (open-meteo, без ключей)."""

    temperature: float = Field(description="Температура воздуха, °C")
    apparent_temperature: float | None = Field(default=None, description="Ощущается как, °C")
    description: str = Field(description="Человеческое описание («небольшой дождь»)")
    precipitation: float = Field(default=0.0, description="Осадки сейчас, мм")
    precipitation_probability: int | None = Field(
        default=None, description="Максимальная вероятность осадков в ближайшие часы, %"
    )
    weather_code: int | None = Field(default=None, description="WMO-код погоды")
    wind_speed: float | None = Field(default=None, description="Ветер, км/ч")
    is_day: bool | None = None
    is_rainy: bool = Field(
        description="Плохая для прогулки погода: дождь, ливень, снег или гроза"
    )
    temp_min: float | None = Field(default=None, description="Минимум на день, °C")
    temp_max: float | None = Field(default=None, description="Максимум на день, °C")
    updated_at: str | None = Field(default=None, description="Когда данные получены (UTC)")
