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
            "statistics" | "recommend" | "distance" | "compare" |
            "walk" | "analytics" | "other",
  "categories": ["museum" | "monument" | "memorial" | "gallery"],
  "text": строка для полнотекстового поиска или null,
  "theme": строка (тематика) или null,
  "keyword": строка (ключевое слово) или null,
  "place": "название места/улицы/площади, если пользователь назвал точку отсчёта" или null,
  "place2": "второе место для distance/compare (от A до B)" или null,
  "latitude": число или null,
  "longitude": число или null,
  "radius_meters": число (1..10000) или null,
  "duration_minutes": целое (минуты на прогулку, action=walk) или null,
  "count": целое 1..12 (сколько остановок в прогулке) или null,
  "open_now": true|false|null — только объекты, открытые сейчас,
  "metric": "oldest" | "newest" | null — метрика аналитики,
  "limit": целое 1..100,
  "has_website": true|false|null,
  "has_description": true|false|null,
  "within_district": true|false
}

Правила:
1. «музеи», «памятники», «мемориалы», «галереи» (и их сочетания) -> action="list",
   categories=[...]. Используй коды: museum, monument, memorial, gallery.
2. «найди X», «поищи X», «где X» (без пространства) -> action="search", text="X".
   action="search" допустим ТОЛЬКО при непустом text. Если текста нет, а есть
   место — nearby; если есть категории — list.
3. «рядом с X», «возле X», «вокруг X», «что рядом с X», «ближайший ... к X» ->
   action="nearby" или "radius_search" (если задан радиус) и place="X".
   Название места передавай как в запросе — в падеже, латиницей, с опечаткой:
   сервис сопоставит его со своей базой сам.
4. «в радиусе N метров/метрах от X» -> action="radius_search", place="X",
   radius_meters=N.
5. «статистика», «сколько объектов», «какие категории» -> action="statistics".
6. «порекомендуй», «посоветуй», «что посмотреть», «подбери» -> action="recommend".
7. НИКОГДА не выдумывай координаты. Для точки отсчёта ставь place, для
   пользователя — координаты из сообщения, как есть.
8. Пересечение/угол улиц — это place, сохрани исходные названия:
   «пересечение Пестеля и Моховой» -> place="пересечение Пестеля и Моховой".
   Не разбирай такое место сам: координаты посчитает сервис.
9. «здесь», «тут», «отсюда», «эта точка», «этой точкой/точки/точке», «эту
   точку», «выбранная точка», «у точки/метки», «центр карты» ->
   latitude/longitude из строки «Координаты выбранной на карте точки» (если она
   есть в сообщении), place=null. Если такой строки нет — координаты пользователя.
10. «с детьми», «для детей», «с семьёй», «детский» — это не поиск и не
   категория: nearby/recommend с указанным place, categories оставляй только
   явно названные.
11. Не придумывай поля: отсутствующие значения -> null.
12. limit по умолчанию 20; radius_meters не больше 10000.
13. Запрос НЕ о объектах, местах, расстояниях и рекомендациях сервиса
    (погода, политика, переводы, программирование, математика, новости,
    общие разговоры) -> action="other", остальные поля null. Не пытайся
    притянуть такой запрос к list/search/statistics: лучше честный отказ,
    чем бессмысленный ответ. Примеры -> "other":
    «Какая погода завтра?», «Переведи слово на английский», «Сколько будет
    2+2?», «Расскажи анекдот». Примеры НЕ -> other (это про карту):
    «Сколько музеев в Центральном районе?», «Что посмотреть рядом?».
14. «сколько идти/ехать от A до B», «расстояние между A и B», «далеко ли от A
    до B», «как далеко от A до B» -> action="distance", place="A", place2="B".
    Без двух названных мест distance использовать нельзя.
15. «сравни A и B», «чем отличается A от B», «разница между A и B» ->
    action="compare", place="A", place2="B" (A и B — объекты сервиса).
16. «прогулка», «погулять», «маршрут пешком», «что успеть за N минут/часов» ->
    action="walk": place — точка старта (если названа), duration_minutes — время
    на прогулку, count — число остановок (если названо, иначе null).
17. «самый старый/старейший», «самый новый/молодой» + категория ->
    action="analytics", metric="oldest" (или "newest"), categories=[...].
18. «открыто сейчас», «работает сейчас», «какие ... сейчас открыты» — добавь
    "open_now": true к обычному действию (list/nearby/radius_search/search/
    recommend), ничего другого не меняя.
19. «в N минутах ходьбы/пешком от X» — это радиус: radius_meters = N × 80
    (5 минут ≈ 400 м, 10 минут ≈ 800 м), place="X". Без «ходьбы/пешком»
    («прогулка на 30 минут») это время прогулки, а не радиус.

Примеры (формат ответа):
«Какой музей ближе всего к Дворцовой площади?» ->
{"action":"nearby","categories":["museum"],"place":"Дворцовая площадь","radius_meters":1000,"limit":5}
«Покажи все галереи в радиусе 1 км от Невского проспекта» ->
{"action":"radius_search","categories":["gallery"],"place":"Невский проспект","radius_meters":1000,"limit":20}
«Чем заняться с детьми рядом с Эрмитажем?» ->
{"action":"nearby","categories":[],"place":"Эрмитаж","radius_meters":1500}
«Ближайшая галерея к пересечению улиц Пестеля и Моховой» ->
{"action":"nearby","categories":["gallery"],"place":"пересечение Пестеля и Моховой","radius_meters":1500,"limit":5}
«Сколько музеев в Центральном районе?» ->
{"action":"list","categories":["museum"],"within_district":true,"limit":100}
«Сколько от Эрмитажа до Русского музея?» ->
{"action":"distance","place":"Эрмитаж","place2":"Русский музей"}
«Сравни Эрмитаж и Исаакиевский собор» ->
{"action":"compare","place":"Эрмитаж","place2":"Исаакиевский собор"}
«Погулять час от Дворцовой площади» ->
{"action":"walk","place":"Дворцовая площадь","duration_minutes":60}
«Что успеть за два часа рядом с Невским проспектом?» ->
{"action":"walk","place":"Невский проспект","duration_minutes":120}
«Самый старый памятник» ->
{"action":"analytics","metric":"oldest","categories":["monument"],"limit":5}
«Какие музеи открыты сейчас рядом с Дворцовой?» ->
{"action":"nearby","categories":["museum"],"place":"Дворцовая площадь","open_now":true}
«Покажи музеи в 10 минутах ходьбы от Эрмитажа» ->
{"action":"radius_search","categories":["museum"],"place":"Эрмитаж","radius_meters":800}
«Какая погода завтра?» -> {"action":"other"}

Ответ — только JSON без каких-либо пояснений и без markdown."""


class AIQueryError(RuntimeError):
    """Некорректный результат разбора (пользователю отдаём 422, §25)."""


def build_user_message(query: str, user_lat: float | None, user_lon: float | None,
                       map_lat: float | None = None, map_lon: float | None = None,
                       context: str | None = None) -> str:
    parts = [f"Запрос пользователя: {query}"]
    if context:
        parts.append(f"Предыдущий запрос и его разбор (контекст диалога):\n{context}")
        parts.append(
            "Если текущая фраза — уточнение к предыдущему («а теперь только музеи», "
            "«в радиусе 500 метров», «покажи ещё»), сохрани в разборе все поля, "
            "которые пользователь не назвал заново. Если это новый самостоятельный "
            "запрос — контекст игнорируй."
        )
    if user_lat is not None and user_lon is not None:
        parts.append(f"Координаты пользователя: {user_lat}, {user_lon}")
    if map_lat is not None and map_lon is not None:
        parts.append(f"Координаты выбранной на карте точки: {map_lat}, {map_lon}")
    parts.append("Верни только JSON.")
    return "\n".join(parts)


def parse_query(settings: Settings, query: str,
                user_lat: float | None = None,
                user_lon: float | None = None,
                *,
                map_lat: float | None = None,
                map_lon: float | None = None,
                context: str | None = None) -> StructuredQuery:
    """Текст -> валидный StructuredQuery. Ошибки -> AIQueryError (422).

    ``context`` — предыдущий разбор диалога (§40): модель использует его
    только для уточняющих фраз, самостоятельные запросы разбирает как есть.
    """
    raw = chat(
        settings,
        SYSTEM_PROMPT,
        build_user_message(query, user_lat, user_lon, map_lat, map_lon, context),
    )
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
                        user_lon: float | None = None,
                        *,
                        map_lat: float | None = None,
                        map_lon: float | None = None,
                        context: str | None = None) -> StructuredQuery:
    """Одна повторная попытка разбора с сообщением об ошибке модели (§24).

    Повтор делается ТОЛЬКО когда модель ответила, но неверно (AIQueryError).
    Недоступность inference server (LLMError) сразу пробрасывается наружу —
    иначе клиент получил бы 422 вместо 503 (§44) и ждал бы два таймаута.

    После неудачного повтора наружу уходит дружелюбная формулировка: технические
    детали (что именно не так со схемой) остаются в логе.
    """
    common = {"user_lat": user_lat, "user_lon": user_lon,
              "map_lat": map_lat, "map_lon": map_lon, "context": context}
    try:
        return parse_query(settings, query, **common)
    except LLMError:
        raise
    except AIQueryError as first:
        try:
            return parse_query(
                settings,
                f"{query}\n\nПредыдущая попытка была неудачной ({first}). "
                "Соблюдай схему строго.",
                **common,
            )
        except LLMError:
            raise
        except AIQueryError as second:
            logger.warning("Разбор не удался после повтора: %s", second)
            raise AIQueryError("не удалось разобрать запрос") from second
