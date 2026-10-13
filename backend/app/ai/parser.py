"""Разбор естественного языка в структурированный запрос (§22–§25).

Пайплайн: текст -> LLM (JSON-режим) -> Pydantic-валидация (§25) -> SQL.
LLM не видит схему БД и не имеет доступа к SQL (§23): на выходе — только
объект, описанный в app.ai.schemas_ai.StructuredQuery.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from app.ai import client
from app.ai.schemas_ai import StructuredQuery
from app.config import Settings

logger = logging.getLogger(__name__)

# единый клиент AI (app/ai/client.py); LLMError — прежнее имя исключения
LLMError = client.AIServiceUnavailable


def chat(settings: Settings, system: str, user: str, *, json_mode: bool = True,
         temperature: float = 0.0) -> str:
    """Один запрос к модели: system + user -> текст ответа (§22)."""
    del temperature  # temperature=0 задана в client.chat_completion
    return client.chat_completion(
        settings,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        json_format=json_mode,
    )

SYSTEM_PROMPT = """Ты — парсер запросов сервиса «Культурная карта Санкт-Петербурга».
Твоя задача: преобразовать запрос пользователя в СТРОГО JSON по схеме ниже.

Схема JSON:
{
  "action": "list" | "search" | "nearby" | "radius_search" |
            "statistics" | "recommend" | "other",
  "categories": ["museum" | "monument" | "memorial" | "gallery"],
  "text": строка для полнотекстового поиска или null,
  "theme": строка (тематика) или null,
  "keyword": строка (ключевое слово) или null,
  "place": "название места/улицы/площади, если пользователь назвал точку отсчёта" или null,
  "latitude": число или null,
  "longitude": число или null,
  "radius_meters": число (1..10000) или null,
  "limit": целое 1..100,
  "has_website": true|false|null,
  "has_description": true|false|null,
  "within_district": true|false
}

Правила:
1. «музеи», «памятники», «мемориалы», «галереи» (и их сочетания) -> action="list",
   categories=[...]. Используй коды: museum, monument, memorial, gallery.
2. «найди X», «поищи X», «где X» (без пространства) -> action="search", text="X".
3. «рядом с X», «возле X», «вокруг X», «что рядом с X» -> action="nearby" или
   "radius_search" (если задан радиус) и place="X".
4. «в радиусе N метров/метрах от X» -> action="radius_search", place="X",
   radius_meters=N.
5. «статистика», «сколько объектов», «какие категории» -> action="statistics".
6. «порекомендуй», «посоветуй», «что посмотреть», «подбери» -> action="recommend".
7. Если координаты пользователя переданы в запросе, используй их как есть.
   НИКОГДА не выдумывай координаты: для точек отсчёта ставь place.
8. Не придумывай поля: отсутствующие значения -> null.
9. limit по умолчанию 20; radius_meters не больше 10000.
10. Запрос НЕ о объектах, местах, расстояниях и рекомендациях сервиса
    (погода, политика, переводы, программирование, математика, новости,
    общие разговоры) -> action="other", остальные поля null. Не пытайся
    притянуть такой запрос к list/search/statistics: лучше честный отказ,
    чем бессмысленный ответ. Примеры -> "other":
    «Какая погода завтра?», «Переведи слово на английский», «Сколько будет
    2+2?», «Расскажи анекдот». Примеры НЕ -> other (это про карту):
    «Сколько музеев в Центральном районе?», «Что посмотреть рядом?».

Ответ — только JSON без каких-либо пояснений и без markdown."""


class AIQueryError(RuntimeError):
    """Некорректный результат разбора (пользователю отдаём 422, §25)."""


def build_user_message(query: str, user_lat: float | None, user_lon: float | None) -> str:
    parts = [f"Запрос пользователя: {query}"]
    if user_lat is not None and user_lon is not None:
        parts.append(f"Координаты пользователя: {user_lat}, {user_lon}")
    parts.append("Верни только JSON.")
    return "\n".join(parts)


def parse_query(settings: Settings, query: str,
                user_lat: float | None = None,
                user_lon: float | None = None) -> StructuredQuery:
    """Текст -> валидный StructuredQuery. Ошибки -> AIQueryError (422)."""
    raw = chat(settings, SYSTEM_PROMPT, build_user_message(query, user_lat, user_lon))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Модель вернула не JSON: %s", raw[:500])
        raise AIQueryError("модель вернула некорректный JSON") from exc
    if not isinstance(data, dict):
        raise AIQueryError("ожидался JSON-объект")

    # незнакомые ключи игнорируем, неизвестные значения отсекает валидатор
    try:
        return StructuredQuery.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise AIQueryError(f"валидация запроса не пройдена: {details}") from exc


def parse_with_fallback(settings: Settings, query: str,
                        user_lat: float | None = None,
                        user_lon: float | None = None) -> StructuredQuery:
    """Одна повторная попытка разбора с сообщением об ошибке модели (§24).

    Повтор делается ТОЛЬКО когда модель ответила, но неверно (AIQueryError).
    Недоступность inference server (LLMError) сразу пробрасывается наружу —
    иначе клиент получил бы 422 вместо 503 (§44) и ждал бы два таймаута.
    """
    try:
        return parse_query(settings, query, user_lat, user_lon)
    except LLMError:
        raise
    except AIQueryError as first:
        try:
            return parse_query(
                settings,
                f"{query}\n\nПредыдущая попытка была неудачной ({first}). "
                "Соблюдай схему строго.",
                user_lat,
                user_lon,
            )
        except LLMError:
            raise
        except AIQueryError as second:
            raise AIQueryError(str(second)) from second
