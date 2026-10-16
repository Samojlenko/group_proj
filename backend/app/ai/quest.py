"""Квесты (идея 27): маршрут из N точек, вопросы — из описаний объектов.

Квест — та же «прогулка» (§14), но остановки берутся только с содержательным
описанием, а по каждому описанию генерируется вопрос. Модель не имеет доступа
к БД (§4): она видит лишь текст описания и не знает ни ответа на вопрос, ни
расстояний — за это отвечает сервис. Вопрос, где прозвучало название объекта
или который не опирается на описание, отбрасывается: остаётся шаблонный
вопрос, квест продолжает работать и без модели (§53).
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai.client import AIServiceUnavailable, chat_completion, check_available
from app.config import Settings
from app.spatial.queries import OBJ_COLS
from app.spatial.routes import MIN_LEG_METERS, WALK_SPEED_M_PER_MIN, nearest_remaining

logger = logging.getLogger(__name__)

QUEST_RADIUS_M = 1500.0
QUEST_MAX_POINTS = 8
# описание короче этого не годится: вопрос не из чего составить
DESC_MIN = 40
HINT_LIMIT = 180
QUESTION_MAX = 220
QUESTION_TIMEOUT_SECONDS = 12.0

QUESTION_SYSTEM = (
    "Ты — автор городского квеста по Санкт-Петербургу. По описанию объекта "
    "придумай один короткий вопрос (одно предложение, до 160 символов), "
    "ответ на который — этот объект. Не упоминай название объекта и его "
    "адрес. Опирайся только на факты из описания. Верни только текст "
    "вопроса, без кавычек и пояснений."
)

_WORD_RE = re.compile(r"[а-яёa-z]{5,}", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20[0-2]\d)\b")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
# «Гдеlocated»: буквы двух алфавитов в одном слове — брак генерации
_MIXED_ALPHABET_RE = re.compile(r"[а-яё][a-z]|[a-z][а-яё]", re.IGNORECASE)

# выражение «описание» для SQL: короткое предпочтительнее полного
_DESC_SQL = "coalesce(nullif(c.short_description, ''), nullif(c.description, ''))"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _desc(obj: dict) -> str:
    """Описание объекта; служебный префикс ETL («Из-за ограничений…») срезаем."""
    text = (obj.get("short_description") or obj.get("description") or "").strip()
    return re.sub(r"^Из-за ограничений[^:]*:\s*\"?", "", text).strip()


def _quest_ready(obj: dict) -> bool:
    """Годится ли объект в квест: описание достаточно длинное и по-русски.

    Англоязычные описания (источник OSM) отсекаем: русский вопрос по ним
    не сгенерировать, а подсказка на чужом языке в квесте бесполезна.
    """
    text = _desc(obj)
    return len(text) >= DESC_MIN and bool(_CYRILLIC_RE.search(text))


def quest_candidates(
    db: Session,
    lat: float,
    lon: float,
    radius_m: float,
    *,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
    limit: int = 60,
) -> list[dict]:
    """Кандидаты квеста: объекты в радиусе, у которых есть русское описание.

    Фильтр по описанию — в SQL: если взять просто N ближайших (§14), среди
    них почти не окажется объектов с текстом (описания есть у единиц
    процентов базы), и квест получится короче заказанного.
    """
    where = [
        "ST_DWithin(c.geog, CAST(:point AS geography), :radius)",
        f"length({_DESC_SQL}) >= :desc_min",
        f"{_DESC_SQL} ~ '[а-яёА-ЯЁ]'",
    ]
    params: dict = {
        "point": f"SRID=4326;POINT({lon} {lat})",
        "radius": radius_m,
        "desc_min": DESC_MIN,
        "limit": limit,
    }
    if categories:
        names = [f"cat_{i}" for i in range(len(categories))]
        where.append(f"c.category IN ({', '.join(':' + n for n in names)})")
        params.update(dict(zip(names, categories)))
    if not_keyword:
        params["not_keyword"] = not_keyword
        where.append("NOT (coalesce(c.keywords, '{}') @> ARRAY[:not_keyword]::text[])")
    sql = f"""
    SELECT {OBJ_COLS}, ST_Distance(c.geog, CAST(:point AS geography)) AS distance_meters
    FROM cultural_objects c
    WHERE {' AND '.join(where)}
    ORDER BY distance_meters
    LIMIT :limit
    """
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


def first_sentence(text: str, limit: int = HINT_LIMIT) -> str:
    """Подсказка: первое предложение описания, обрезанное до ``limit``."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    match = re.search(r"^(.+?[.!?])(?:\s|$)", text)
    out = match.group(1) if match else text
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def fallback_question(obj: dict) -> str:
    """Вопрос без LLM: год из описания — иначе общая форма.

    Квест не должен «ломаться» из-за недоступной модели: без неё вопросы
    становятся однотипными, но играть можно.
    """
    years = _YEAR_RE.findall(_desc(obj))
    if years:
        return f"Какой объект связан с {years[0]} годом?"
    return "О каком объекте идёт речь?"


def question_for(settings: Settings, obj: dict) -> str:
    """Вопрос от модели; пустая строка — сбой или брак (тогда шаблон)."""
    name = obj.get("name", "")
    desc = _desc(obj)[:1200]
    messages = [
        {"role": "system", "content": QUESTION_SYSTEM},
        {
            "role": "user",
            "content": f"Название (не упоминать): {name}\nОписание: {desc}",
        },
    ]
    try:
        raw = chat_completion(
            settings, messages, json_format=False, timeout=QUESTION_TIMEOUT_SECONDS
        )
    except AIServiceUnavailable as exc:
        logger.warning("Квест: вопрос не сгенерирован (%s)", exc)
        return ""
    question = re.sub(r"\s+", " ", raw.strip().strip('"').strip("«»").strip())
    if not (12 <= len(question) <= QUESTION_MAX):
        return ""
    if _norm(name) in _norm(question):
        return ""  # название в вопросе — загадка сломана
    if _MIXED_ALPHABET_RE.search(question):
        return ""  # «Гдеlocated»: склейка алфавитов — брак генерации
    # вопрос обязан опираться на описание: общий содержательный токен или год
    tokens = set(_WORD_RE.findall(_norm(desc)))
    asked = set(_WORD_RE.findall(_norm(question)))
    years = set(_YEAR_RE.findall(desc)) & set(_YEAR_RE.findall(question))
    if not (tokens & asked or years):
        return ""
    return question


def build_quest(
    db: Session,
    settings: Settings,
    lat: float,
    lon: float,
    *,
    count: int = 5,
    radius_m: float = QUEST_RADIUS_M,
    categories: list[str] | None = None,
    not_keyword: str | None = None,
    theme: str | None = None,
) -> dict:
    """Собирает квест: порядок обхода, вопросы, подсказки, полилиния.

    Порядок — как в прогулке (каждый следующий — ближайший к текущей точке).
    Если объектов с описанием меньше, чем ``count``, квест получится короче.
    """
    count = min(max(count, 1), QUEST_MAX_POINTS)
    items = quest_candidates(
        db,
        lat,
        lon,
        radius_m,
        categories=categories,
        not_keyword=not_keyword,
        limit=min(max(count * 6, 24), 60),
    )
    by_id = {c["id"]: c for c in items if _quest_ready(c)}
    remaining = list(by_id)
    stops: list[dict] = []
    cur_lat, cur_lon = lat, lon
    total_m = 0.0

    while remaining and len(stops) < count:
        obj_id, leg_m = nearest_remaining(db, cur_lat, cur_lon, remaining)
        if obj_id is None or leg_m is None or obj_id not in by_id:
            break
        remaining.remove(obj_id)
        if stops and leg_m < MIN_LEG_METERS:
            continue  # почти та же точка, что предыдущая: номер на карте не виден
        obj = by_id[obj_id]
        total_m += leg_m
        stops.append(
            {
                "id": obj_id,
                "name": obj["name"],
                "category": obj["category"],
                "latitude": obj["latitude"],
                "longitude": obj["longitude"],
                "leg_meters": round(leg_m, 1),
                "cumulative_meters": round(total_m, 1),
                "distance_from_start_meters": obj.get("distance_meters"),
            }
        )
        cur_lat, cur_lon = obj["latitude"], obj["longitude"]

    # вопросы: LLM — по описаниям; сбой или брак — шаблон, квест живёт
    llm_ready = settings.ai_enabled and check_available(settings)
    for stop in stops:
        source = by_id[stop["id"]]
        question = question_for(settings, source) if llm_ready else ""
        stop["question"] = question or fallback_question(source)
        stop["hint"] = first_sentence(_desc(source))

    polyline = [[lat, lon]] + [[s["latitude"], s["longitude"]] for s in stops]
    return {
        "start": {"latitude": lat, "longitude": lon},
        "theme": theme,
        "radius_meters": radius_m,
        "count": len(stops),
        "total_meters": round(total_m, 1),
        "walk_minutes": int(round(total_m / WALK_SPEED_M_PER_MIN)) if stops else 0,
        "points": stops,
        "polyline": polyline,
    }
