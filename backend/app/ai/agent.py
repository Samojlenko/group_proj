"""Агент с function calling (идея 10): модель сама вызывает инструменты сервиса.

Отличие от одиночного разбора (§22–§25): сложный вопрос может требовать
нескольких шагов — «найди самый старый музей и покажи, что рядом с ним».
Агент вызывает инструменты по одному, получает уже посчитанные SQL-слоем
результаты и продолжает, пока не соберёт всё необходимое.

Границы те же, что и у обычного разбора: LLM не видит SQL, не считает
расстояния и не выдумывает координаты — она лишь выбирает функции и их
аргументы, всё исполняет сервис. Любой сбой агента (сервер недоступен,
пустой ответ, все шаги с ошибкой) приводит к тихому откату на обычный
разбор в app/api/ai.py — пользователь не должен этого замечать (§53).
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai import client
from app.ai.resolver import PlaceNotFound, resolve_place
from app.ai.schemas_ai import StructuredQuery
from app.config import Settings
from app.schemas.ai_schemas import AIQueryRequest

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """Агент не получил пригодный результат — нужен обычный разбор (§53)."""


_CATEGORIES = ["museum", "monument", "memorial", "gallery"]

# JSON Schema аргументов для model-инструкций отдельных инструментов
_CATS = {
    "type": "array",
    "items": {"type": "string", "enum": _CATEGORIES},
    "description": "Категории объектов: museum (музеи), monument (памятники), "
                   "memorial (мемориалы), gallery (галереи)",
}


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    """Инструмент в формате OpenAI-совместимых tools (Ollama /api/chat)."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


TOOLS: list[dict] = [
    _fn(
        "find_objects",
        "Найти объекты сервиса: список по категориям, поиск по названию, рядом "
        "с названным местом, в радиусе от него или от точки пользователя, только "
        "открытые сейчас. Основной инструмент для «покажи / найди / что рядом».",
        {
            "categories": _CATS,
            "text": {"type": "string",
                     "description": "Название или часть названия объекта, если человек ищет его"},
            "place": {"type": "string",
                      "description": "Место отсчёта, если названо: «рядом с Эрмитажем» → «Эрмитаж»"},
            "radius_meters": {"type": "number",
                              "description": "Радиус от места, метры (100..10000). "
                                             "«в N минутах ходьбы/пешком» — это N × 80 м "
                                             "(5 минут ≈ 400 м, 10 минут ≈ 800 м)"},
            "near_user": {"type": "boolean",
                          "description": "true — считать от точки пользователя / точки на карте "
                                         "(когда место не названо, но человек говорит «рядом со мной», "
                                         "«рядом с этой точкой», «здесь»)"},
            "open_now": {"type": "boolean", "description": "true — только открытые сейчас"},
            "limit": {"type": "integer", "description": "Сколько объектов показать (1..50), по умолчанию 20"},
        },
    ),
    _fn(
        "find_place",
        "Уточнить точку на карте по имени: вернуть её координаты (улица, площадь, "
        "мост, объект). Использовать для вопроса «где находится X» и чтобы получить "
        "точные координаты перед другими шагами.",
        {"name": {"type": "string", "description": "Название места, например «Дворцовая площадь»"}},
        ["name"],
    ),
    _fn(
        "distance",
        "Расстояние по прямой между двумя НАЗВАННЫМИ местами и примерное время "
        "пешком. Без двух названий использовать нельзя.",
        {
            "from_place": {"type": "string", "description": "Начало, например «Эрмитаж»"},
            "to_place": {"type": "string", "description": "Конец, например «Русский музей»"},
        },
        ["from_place", "to_place"],
    ),
    _fn(
        "compare",
        "Сравнить два объекта сервиса (музей, памятник, мемориал, галерея) по "
        "карточкам: год основания, билет, адрес, расстояние между ними.",
        {
            "first_place": {"type": "string", "description": "Первый объект"},
            "second_place": {"type": "string", "description": "Второй объект"},
        },
        ["first_place", "second_place"],
    ),
    _fn(
        "walk",
        "Пеший маршрут («прогулка») от точки: подбор остановок, метры, время. "
        "Точку старта можно не указывать — тогда считается от точки пользователя.",
        {
            "place": {"type": "string", "description": "Точка старта, если названа"},
            "duration_minutes": {"type": "integer",
                                 "description": "Сколько времени на прогулку, минуты (например, час → 60)"},
            "radius_meters": {"type": "number", "description": "Радиус прогулки, метры"},
            "count": {"type": "integer", "description": "Желаемое число остановок (3..12)"},
            "categories": _CATS,
        },
    ),
    _fn(
        "analytics",
        "Аналитика по годам основания: самый старый или самый новый объект "
        "(и следующие за ним по возрасту).",
        {
            "metric": {"type": "string", "enum": ["oldest", "newest"],
                       "description": "oldest — самый старый, newest — самый новый"},
            "categories": _CATS,
        },
        ["metric"],
    ),
    _fn(
        "statistics",
        "Общая статистика базы сервиса: сколько объектов по категориям, с сайтом, "
        "с описанием, сколько видимых. Аргументов не требует.",
        {},
    ),
    _fn(
        "recommend",
        "Подобрать места по свободному пожеланию («что посмотреть с детьми», "
        "«атмосферные места») — семантический поиск с учётом пространства.",
        {
            "query": {"type": "string", "description": "Пожелание словами пользователя"},
            "categories": _CATS,
            "place": {"type": "string", "description": "Рядом с каким местом, если названо"},
            "radius_meters": {"type": "number", "description": "Радиус, метры"},
            "limit": {"type": "integer", "description": "Сколько показать (1..50)"},
        },
        ["query"],
    ),
]

AGENT_SYSTEM_PROMPT = """Ты — агент сервиса «Культурная карта Санкт-Петербурга».
Сервис знает музеи, памятники, мемориалы и галереи города: их координаты, годы
основания, адреса, часы работы. Ты отвечаешь на вопросы о местах города,
вызывая инструменты (tools). Всё остальное — поиск, расстояния, маршруты —
делают инструменты; сам ты не считаешь и не выдумываешь.

Правила:
1. На каждый вопрос о местах сначала вызывай подходящий инструмент. Названия,
   координаты и числа бери только из результатов вызовов.
2. Составной запрос выполняй по шагам, по одному вызову инструмента за раз.
   Пример: «самый старый музей и что рядом с ним» -> сначала analytics,
   затем find_objects с place = название найденного музея из результата.
3. Названия мест передавай так, как их называет человек, — сервис сам
   разберёт падежи, латиницу и опечатки («рядом с Эрмитажем» -> place
   «Эрмитажем» или «Эрмитаж», «Russian Museum» -> «Русский музей»).
4. Если в сообщении есть координаты точки (пользователя или выбранной на
   карте), а человек говорит «рядом со мной», «рядом с этой точкой», «здесь»,
   «отсюда» — вызывай инструмент без place, а для find_objects поставь
   near_user = true: сервис посчитает от этой точки.
5. «в N минутах ходьбы / пешком от X» — это find_objects с place и
   radius_meters = N × 80 (5 минут ≈ 400 м), категории — что человек ищет.
6. Если инструмент вернул ошибку — исправь аргументы и вызови ещё раз
   (не больше одного раза); не повторяй вызов с теми же аргументами.
7. Когда данных достаточно — ответь текстом БЕЗ вызова инструментов: одна
   короткая фраза по-русски с итогом, без markdown и без списков.
8. Вопросы не о местах города (погода, политика, переводы, математика) —
   коротко ответь текстом, что сервис отвечает только на вопросы о местах.
9. Контекст диалога в сообщении — предыдущий запрос и его разбор. Если текущая
   фраза уточняет его, учитывай контекст; самостоятельный запрос разбирай как есть."""


def _user_message(payload: AIQueryRequest, context: str | None) -> str:
    """Сообщение агента: запрос + точка отсчёта + контекст диалога (§40)."""
    parts = [f"Запрос пользователя: {payload.query}"]
    if context:
        parts.append(f"Предыдущий запрос и его разбор (контекст диалога):\n{context}")
    if payload.latitude is not None and payload.longitude is not None:
        parts.append(f"Координаты пользователя: {payload.latitude}, {payload.longitude}")
    if payload.map_latitude is not None and payload.map_longitude is not None:
        parts.append(
            f"Координаты выбранной на карте точки: {payload.map_latitude}, {payload.map_longitude}"
        )
    return "\n".join(parts)


def _int_arg(value, default: int) -> int:
    """Аргумент-число от модели: строки и дроби приводим к int, мусор — к дефолту."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _tool_query(name: str, args: dict) -> StructuredQuery | None:
    """Аргументы инструмента -> валидированный StructuredQuery (§25).

    ``None`` — инструмент без запроса к объектам (find_place). Неизвестный
    инструмент или неполные аргументы -> ValueError: агент вернёт её модели,
    и та попробует сформулировать иначе (§24).
    """
    if name == "find_place":
        return None

    if name == "find_objects":
        data: dict = {
            "categories": args.get("categories") or [],
            "open_now": args.get("open_now"),
            "limit": _int_arg(args.get("limit"), 20),
        }
        if args.get("text"):
            # поиск по названию; место — только для сортировки по расстоянию
            data["action"] = "search"
            data["text"] = args["text"]
            if args.get("place"):
                data["place"] = args["place"]
        elif args.get("place"):
            data["place"] = args["place"]
            data["action"] = "radius_search" if args.get("radius_meters") else "nearby"
            if args.get("radius_meters"):
                data["radius_meters"] = args["radius_meters"]
        elif args.get("near_user") or args.get("radius_meters"):
            # «рядом с этой точкой»: точка отсчёта — карта/геолокация из запроса
            data["action"] = "nearby"
            if args.get("radius_meters"):
                data["radius_meters"] = args["radius_meters"]
        else:
            data["action"] = "list"
        return StructuredQuery.model_validate(data)

    if name == "distance":
        return StructuredQuery.model_validate({
            "action": "distance",
            "place": args.get("from_place"),
            "place2": args.get("to_place"),
        })

    if name == "compare":
        return StructuredQuery.model_validate({
            "action": "compare",
            "place": args.get("first_place"),
            "place2": args.get("second_place"),
        })

    if name == "walk":
        data = {"action": "walk"}
        for key in ("place", "duration_minutes", "radius_meters", "count", "categories"):
            if args.get(key) is not None:
                data[key] = args[key]
        return StructuredQuery.model_validate(data)

    if name == "analytics":
        return StructuredQuery.model_validate({
            "action": "analytics",
            "metric": args.get("metric") or "oldest",
            "categories": args.get("categories") or [],
            "limit": min(_int_arg(args.get("limit"), 5), 10),
        })

    if name == "statistics":
        return StructuredQuery.model_validate({"action": "statistics"})

    if name == "recommend":
        data = {"action": "recommend", "text": args.get("query")}
        for key in ("categories", "place", "radius_meters", "limit"):
            if args.get(key) is not None:
                data[key] = args[key]
        if data.get("text") is None:
            raise ValueError("для recommend не задан query")
        return StructuredQuery.model_validate(data)

    raise ValueError(f"неизвестный инструмент «{name}»")


def _validation_text(exc: ValidationError) -> str:
    """Компактный текст ошибки валидации — его увидит модель и исправит аргументы."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )


def _parse_args(raw) -> dict:
    """Аргументы вызова: Ollama отдаёт объект, некоторые сборки — JSON-строку."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip() or "{}"
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("аргументы должны быть объектом JSON")
        return data
    raise ValueError(f"неподдержанный формат аргументов: {type(raw).__name__}")


def _run_find_place(db: Session, settings: Settings, args: dict) -> dict:
    """find_place: имя -> координаты (§26). Ошибка — обычный шаг с error."""
    name = str(args.get("name") or "").strip()
    if not name:
        return {"tool": "find_place", "args": args, "error": "не указано название (name)"}
    try:
        place = resolve_place(db, settings, name)
    except PlaceNotFound:
        return {
            "tool": "find_place", "args": args,
            "error": f"место «{name}» не найдено на карте",
        }
    return {"tool": "find_place", "args": args, "place": place}


def _execute_step(
    db: Session,
    settings: Settings,
    payload: AIQueryRequest,
    execute: Callable[..., dict],
    name: str,
    raw_args,
) -> dict:
    """Один вызов инструмента. Ошибки не выбрасываются наружу: они уходят
    модели как результат, чтобы она могла исправиться (§24)."""
    try:
        args = _parse_args(raw_args)
    except (ValueError, TypeError) as exc:
        return {"tool": name, "args": {}, "error": f"аргументы не разобраны: {exc}"}

    try:
        sq = _tool_query(name, args)
    except ValidationError as exc:
        return {"tool": name, "args": args, "error": _validation_text(exc)}
    except ValueError as exc:
        return {"tool": name, "args": args, "error": str(exc)}

    if sq is None:  # find_place
        return _run_find_place(db, settings, args)

    try:
        result = execute(db, settings, payload, sq, source_query=payload.query)
    except HTTPException as exc:  # бизнес-ошибка (место не найдено и т.п.)
        return {"tool": name, "args": args, "error": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 — один упавший инструмент не должен ронять агента
        logger.warning("Инструмент %s упал: %s", name, exc, exc_info=True)
        return {"tool": name, "args": args, "error": "внутренняя ошибка инструмента"}
    return {"tool": name, "args": args, "step": result}


# сколько объектов из выдачи передаём модели (остальное — только счётчик):
# контекст не должен разрастаться на сотни строк
_ITEMS_TO_MODEL = 8


def _tool_reply(step: dict) -> str:
    """Результат инструмента — компании в виде компактного JSON."""
    if step.get("error"):
        return json.dumps({"error": step["error"]}, ensure_ascii=False)
    if "place" in step:
        place = step["place"]
        out = {
            "place": place["label"],
            "latitude": round(place["latitude"], 6),
            "longitude": round(place["longitude"], 6),
        }
        if place.get("object_id"):
            out["object_id"] = place["object_id"]
        return json.dumps(out, ensure_ascii=False)

    result = step["step"]
    out: dict = {"explanation": result["explanation"], "total": result["total"]}
    if result.get("resolved_place"):
        out["place"] = result["resolved_place"]["label"]
    if result.get("items"):
        out["items"] = [
            {
                "id": it["id"],
                "name": it["name"],
                "category": it["category"],
                **({"distance_meters": round(it["distance_meters"])}
                   if it.get("distance_meters") is not None else {}),
            }
            for it in result["items"][:_ITEMS_TO_MODEL]
        ]
        if result["total"] > _ITEMS_TO_MODEL:
            out["items_note"] = f"показаны первые {_ITEMS_TO_MODEL} из {result['total']}"
    if result.get("statistics"):
        out["statistics"] = result["statistics"]
    if result.get("route"):
        route = result["route"]
        out["route"] = {
            "stops": len(route["items"]),
            "total_meters": route["total_meters"],
            "walk_minutes": route["walk_minutes"],
        }
    return json.dumps(out, ensure_ascii=False, default=str)


def _compose(steps: list[dict]) -> dict:
    """Финальный ответ из шагов агента — в формате _execute (§31).

    Итоговые items — из последнего шага, который что-то нашёл: пользователю
    важно то, о чём был последний вопрос агента. Объяснения шагов склеиваются
    («Шаг 1: … Шаг 2: …»), чтобы решение агента было видно и проверяемо.
    """
    executed = [s for s in steps if "step" in s and not s.get("error")]
    if not executed:
        place_step = next((s for s in steps if "place" in s and not s.get("error")), None)
        if place_step is None:
            raise AgentError("все шаги агента завершились ошибкой")
        return _place_only_result(place_step)

    last = executed[-1]["step"]
    items_step = next((s["step"] for s in reversed(executed) if s["step"].get("items")), None)
    items = items_step["items"] if items_step else []
    total = items_step["total"] if items_step else 0
    route = next((s["step"].get("route") for s in reversed(executed) if s["step"].get("route")), None)
    statistics = next(
        (s["step"].get("statistics") for s in reversed(executed) if s["step"].get("statistics")),
        None,
    )
    resolved = last.get("resolved_place") or next(
        (s["step"].get("resolved_place") for s in reversed(executed) if s["step"].get("resolved_place")),
        None,
    )

    if len(executed) == 1:
        explanation = executed[0]["step"]["explanation"]
    else:
        explanation = " ".join(
            f"Шаг {i}: {s['step']['explanation']}." for i, s in enumerate(executed, 1)
        )

    structured = dict(last["structured"])
    structured["agent_steps"] = [
        {
            "tool": s["tool"], "args": s["args"],
            **({"total": s["step"]["total"]} if "step" in s else {}),
        }
        for s in steps if not s.get("error")
    ]
    return {
        "action": last["action"],
        "explanation": explanation,
        "structured": structured,
        "resolved_place": resolved,
        "total": total,
        "items": items,
        "statistics": statistics,
        "offset": 0,
        "route": route,
    }


def _place_only_result(step: dict) -> dict:
    """Ответ «где находится X»: агент уточнил точку, объектов не запрашивал."""
    place = step["place"]
    explanation = (
        f"«{place['label']}» — точка на карте: "
        f"{place['latitude']:.5f}, {place['longitude']:.5f}. "
        "Спросите «что рядом с этим местом» — покажу объекты вокруг"
    )
    return {
        "action": "other",
        "explanation": explanation,
        "structured": {"agent_steps": [{"tool": step["tool"], "args": step["args"]}]},
        "resolved_place": place,
        "total": 0,
        "items": [],
        "statistics": None,
        "offset": 0,
        "route": None,
    }


def run_agent(
    db: Session,
    settings: Settings,
    payload: AIQueryRequest,
    *,
    execute: Callable[..., dict],
    context: str | None = None,
    chat: Callable[..., dict] | None = None,
) -> dict:
    """Цикл агента: LLM выбирает инструменты, сервис их исполняет.

    ``execute`` — обработчик StructuredQuery (app/api/ai.py::_execute);
    ``chat`` — вызов модели с инструментами (подменяется в тестах).
    Возвращает результат такой же формы, как _execute; любой сбой — AgentError
    либо AIServiceUnavailable, и вызывающий код молча уходит на обычный разбор.
    """
    chat = chat or client.chat_tools
    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": _user_message(payload, context)},
    ]
    max_steps = max(1, settings.ai_agent_max_steps)
    steps: list[dict] = []

    for _ in range(max_steps):
        message = chat(settings, messages, TOOLS)
        calls = message.get("tool_calls") or []
        if not calls:
            if steps:
                break  # модель собрала всё, что нужно
            raise AgentError("модель не вызвала ни одного инструмента")
        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": calls,
        })
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or "")
            step = _execute_step(db, settings, payload, execute, name, function.get("arguments"))
            steps.append(step)
            messages.append({"role": "tool", "content": _tool_reply(step), "tool_name": name})
    else:
        logger.warning("Агент остановлен по лимиту шагов (%d)", max_steps)

    return _compose(steps)
