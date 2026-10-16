"""Погода из open-meteo (идея 29, этап H).

open-meteo не требует ключей и отдаёт текущую погоду и прогноз. Модуль
кэширует ответ на несколько минут: и бейдж в интерфейсе, и вопрос «что
посмотреть в дождь» не должны дёргать внешний сервис на каждый запрос.
Любой сбой — ``None``: помощник честно скажет, что погоду узнать не удалось,
а не выдумает градусы (§53).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
UNAVAILABLE_HINT = (
    "Не удалось узнать погоду — сервис прогноза не отвечает. Попробуйте позже"
)

# WMO-коды -> короткие русские описания
WMO_DESCRIPTIONS: dict[int, str] = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь",
    51: "слабая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежные зёрна",
    80: "небольшие ливни", 81: "ливни", 82: "сильные ливни",
    85: "снегопады", 86: "сильные снегопады",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}
RAIN_CODES = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99})
SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})

# кэш в памяти процесса: (время по monotonic, значение)
_cache: dict = {"value": None, "ts": 0.0}


def describe_code(code: int | None) -> str:
    if code is None:
        return "погода без описания"
    return WMO_DESCRIPTIONS.get(code, "необычная погода")


def is_rainy(weather: dict) -> bool:
    """Плохая для прогулки погода: дождь, ливень, снег или гроза.

    Смотрим и факт осадков, и вероятность: сейчас дождя может не быть, а
    через час — стена воды; для совета «под крышей или нет» важен риск.
    """
    if (weather.get("precipitation") or 0) > 0.05:
        return True
    code = weather.get("weather_code")
    if code in RAIN_CODES or code in SNOW_CODES:
        return True
    prob = weather.get("precipitation_probability")
    return prob is not None and prob >= 50


def reset_cache() -> None:
    """Сброс кэша (тесты и ручная проверка)."""
    _cache.update(value=None, ts=0.0)


def get_weather(settings: Settings, *, force: bool = False) -> dict | None:
    """Текущая погода + прогноз на день; ``None`` — сервис недоступен.

    При сбое отдаётся устаревший кэш, если он есть: лучше вчерашний градус,
    чем ничего, — но только когда данные вообще когда-то приходили.
    """
    if not settings.weather_enabled:
        return None
    now = time.monotonic()
    if (
        not force
        and _cache["value"] is not None
        and now - _cache["ts"] < settings.weather_cache_seconds
    ):
        return _cache["value"]
    data = _fetch(settings)
    if data is None:
        return _cache["value"]
    _cache.update(value=data, ts=now)
    return data


def _fetch(settings: Settings) -> dict | None:
    params = {
        "latitude": settings.weather_lat,
        "longitude": settings.weather_lon,
        "current": (
            "temperature_2m,apparent_temperature,precipitation,"
            "weather_code,wind_speed_10m,is_day"
        ),
        "hourly": "precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Europe/Moscow",
        "forecast_days": 1,
    }
    try:
        with httpx.Client(timeout=settings.weather_timeout_seconds) as client:
            resp = client.get(WEATHER_URL, params=params)
            resp.raise_for_status()
            raw = resp.json()
    except Exception as exc:  # noqa: BLE001 — любая сетевая проблема = нет погоды
        logger.warning("open-meteo недоступен: %s", exc)
        return None
    return _normalize(raw)


def _num(value) -> float | None:  # noqa: ANN001
    return float(value) if value is not None else None


def _first(values) -> float | None:  # noqa: ANN001
    if isinstance(values, list) and values:
        return _num(values[0])
    return None


def _next_hours_probability(
    hourly: dict, now_iso: str | None, hours: int = 6
) -> int | None:
    """Максимальная вероятность осадков на ближайшие часы из почасового ряда."""
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    if not times or not probs:
        return None
    start = 0
    if now_iso:
        key = str(now_iso)[:13]  # «2026-10-17T12» — час текущего момента
        for i, t in enumerate(times):
            if str(t)[:13] >= key:
                start = i
                break
        else:
            start = max(0, len(times) - hours)
    window = [p for p in probs[start:start + hours] if p is not None]
    if not window:
        return None
    return int(round(max(window)))


def _normalize(raw: dict) -> dict | None:
    current = raw.get("current") or {}
    temp = current.get("temperature_2m")
    if temp is None:
        return None
    code = current.get("weather_code")
    prob = _next_hours_probability(raw.get("hourly") or {}, current.get("time"))
    daily = raw.get("daily") or {}
    weather: dict = {
        "temperature": float(temp),
        "apparent_temperature": _num(current.get("apparent_temperature")),
        "description": describe_code(code),
        "precipitation": float(current.get("precipitation") or 0.0),
        "precipitation_probability": prob,
        "weather_code": code,
        "wind_speed": _num(current.get("wind_speed_10m")),
        "is_day": bool(current["is_day"]) if current.get("is_day") is not None else None,
        "is_rainy": False,
        "temp_min": _first(daily.get("temperature_2m_min")),
        "temp_max": _first(daily.get("temperature_2m_max")),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    weather["is_rainy"] = is_rainy(weather)
    return weather
