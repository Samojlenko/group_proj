"""LLM-резюме ответа (идея 16) и его стриминг (идея 18).

Архитектурное ограничение (§4, §23, §53): модель не имеет доступа к БД —
резюме строится строго по полям уже готового результата. Каждое число из
сгенерированного текста обязано встречаться в фактах, иначе резюме
отбрасывается и пользователь видит прежний шаблон. Недоступность модели —
не ошибка: шаблон остаётся как есть.
"""

from __future__ import annotations

import json
import logging
import re

from app.ai import client
from app.config import Settings
from app.constants import CATEGORY_LABELS

logger = logging.getLogger(__name__)

# сколько объектов показываем модели: этого хватает, чтобы «посоветовать,
# с чего начать», а контекст не разрастается (§45)
FACTS_ITEM_LIMIT = 5

SYSTEM_PROMPT = (
    "Ты — помощник сервиса «Культурная карта Санкт-Петербурга». Ниже — "
    "готовый ответ сервиса и факты о нём в JSON. Перепиши ответ живым "
    "человеческим языком: одно-два коротких предложения, дружелюбно, без "
    "канцелярита. Строго опирайся на факты: названия, расстояния, числа, "
    "категории — ничего не выдумывай и не меняй числа. Если объектов "
    "несколько, подскажи, с какого начать (обычно с ближайшего) и что "
    "логично посмотреть после него. Сохрани смысл важных оговорок из "
    "ответа сервиса (сколько объектов открыто сейчас и т. п.). Пиши "
    "по-русски, без маркдауна, списков и обёртывающих кавычек."
)


def _item_fact(item: dict) -> dict:
    """Только поля карточки — ничего, чего нет в ответе."""
    out: dict = {"название": item.get("name")}
    category = item.get("category")
    if category:
        out["категория"] = CATEGORY_LABELS.get(category, category)
    distance = item.get("distance_meters")
    if distance is not None:
        out["расстояние_м"] = round(float(distance))
    year = item.get("year_established")
    if year:
        out["год"] = year
    if item.get("is_free"):
        out["вход"] = "бесплатный"
    elif item.get("ticket_price"):
        out["билет"] = item["ticket_price"]
    if item.get("address"):
        out["адрес"] = item["address"]
    # описание — часть карточки: даёт «эффектную подачу» (идея 24) и контекст
    # для подсказок; числа из него становятся фактами, а не выдумкой
    text = (item.get("short_description") or item.get("description") or "").strip()
    if text:
        out["описание"] = text[:240]
    return out


def build_facts(result: dict) -> dict:
    """Факты строго из полей результата: модель не видит ничего сверх ответа."""
    structured = result.get("structured") or {}
    facts: dict = {
        "действие": result.get("action"),
        "ответ_сервиса": result.get("explanation"),
    }
    place = result.get("resolved_place") or {}
    if place.get("label"):
        facts["место"] = place["label"]
    categories = structured.get("categories") or []
    if categories:
        facts["категории"] = [CATEGORY_LABELS.get(c, c) for c in categories]
    if structured.get("radius_meters"):
        facts["радиус_м"] = structured["radius_meters"]
    if structured.get("open_now"):
        facts["только_открытые_сейчас"] = True
    facts["всего_найдено"] = result.get("total", 0)
    items = result.get("items") or []
    if items:
        facts["объекты"] = [_item_fact(i) for i in items[:FACTS_ITEM_LIMIT]]
        if len(items) > FACTS_ITEM_LIMIT:
            facts["объектов_показано"] = FACTS_ITEM_LIMIT
    if result.get("statistics"):
        facts["статистика"] = result["statistics"]
    collection = result.get("collection") or {}
    if collection:
        facts["подборка"] = {
            "название": collection.get("name"),
            "объектов": collection.get("count"),
        }
    route = result.get("route") or {}
    if route:
        facts["маршрут"] = {
            "остановок": len(route.get("items") or []),
            "метров": route.get("total_meters"),
            "минут_пешком": route.get("walk_minutes"),
        }
    return facts


def facts_json(result: dict) -> str:
    return json.dumps(build_facts(result), ensure_ascii=False)


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# округление модели — не галлюцинация: «примерно в 80 метрах» при фактических
# 78 м допустимо (max(2, 5%)), выдуманное «999 музеев» при 33 — отсекается.
# Четырёхзначные сверяются только точно: это годы (1830 вместо 1829 — уже
# выдумка) и крупные метры.
_TOLERANCE_MIN = 2.0
_TOLERANCE_SHARE = 0.05
_TOLERANCE_MAX_VALUE = 1000.0


def clean_summary(text: str) -> str:
    """Обёртывающие кавычки/пробелы модели — не часть ответа."""
    return text.strip().strip('"').strip("«»").strip()


def _facts_numbers(facts: str) -> list[float]:
    out: list[float] = []
    for m in _NUMBER_RE.findall(facts):
        try:
            out.append(float(m.replace(",", ".")))
        except ValueError:  # pragma: no cover - регулярка даёт только числа
            continue
    return out


def _close_to_known(value: float, known: list[float]) -> bool:
    for fact in known:
        if fact >= _TOLERANCE_MAX_VALUE:
            continue
        tolerance = max(_TOLERANCE_MIN, fact * _TOLERANCE_SHARE)
        if abs(fact - value) <= tolerance:
            return True
    return False


def failing_numbers(text: str, facts: str) -> list[str]:
    """Числа текста, которых нет в фактах даже с поправкой на округление."""
    known = _facts_numbers(facts)
    bad: list[str] = []
    for number in _NUMBER_RE.findall(text):
        if number in facts or number.replace(",", ".") in facts:
            continue
        try:
            value = float(number.replace(",", "."))
        except ValueError:  # pragma: no cover - регулярка даёт только числа
            continue
        if _close_to_known(value, known):
            continue
        bad.append(number)
    return bad


def validate_summary(text: str, facts: str) -> bool:
    """Резюме честное, если все его числа есть в фактах (антигаллюцинация)."""
    if not 12 <= len(text) <= 400:
        return False
    if "{" in text or "}" in text:
        return False
    return not failing_numbers(text, facts)


def _messages(facts: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Факты (JSON):\n{facts}"},
    ]


def summarize(settings: Settings, result: dict) -> str | None:
    """Резюме без стриминга: None — любой сбой или провал проверки фактов."""
    if not settings.ai_enabled or not settings.ai_summary_enabled:
        return None
    facts = facts_json(result)
    try:
        text = client.chat_completion(
            settings, _messages(facts),
            json_format=False,
            timeout=settings.ai_summary_timeout_seconds,
        )
    except client.AIServiceUnavailable as exc:
        logger.info("LLM-резюме недоступно: %s", exc)
        return None
    text = clean_summary(text)
    if not validate_summary(text, facts):
        logger.info("LLM-резюме отброшено (числа вне фактов: %s): %r",
                    failing_numbers(text, facts), text)
        return None
    return text


def stream_summarize(settings: Settings, result: dict):
    """Чанки резюме для SSE (идея 18).

    Повторный запрос того же резюме обслуживается из кэша (§46) — целиком
    одним куском. Проверка фактов здесь не делается: решение о ``reset``
    принимает эндпоинт, когда текст собран целиком.
    """
    if not settings.ai_enabled or not settings.ai_summary_enabled:
        return
    facts = facts_json(result)
    messages = _messages(facts)
    cached = client.get_chat_cache(settings, messages)
    if cached:
        yield cached
        return
    chunks: list[str] = []
    try:
        for chunk in client.chat_completion_stream(
            settings, messages, timeout=settings.ai_summary_timeout_seconds
        ):
            chunks.append(chunk)
            yield chunk
    except client.AIServiceUnavailable as exc:
        logger.info("Стриминг LLM-резюме прерван: %s", exc)
    text = clean_summary("".join(chunks))
    if text and validate_summary(text, facts):
        client.put_chat_cache(settings, messages, text)
