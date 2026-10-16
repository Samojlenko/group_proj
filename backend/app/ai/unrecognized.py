"""Журнал «не понял» (идея 23): запросы, на которых помощник спасовал.

Три повода записываются отдельными видами:

* ``other``    — модель честно отказалась: вопрос вне темы сервиса
  (погода, переводы) — это не сбой, но по таким запросам видно, чего
  пользователи ждут от помощника;
* ``fallback`` — модель недоступна, запрос спасён разбором шаблонами (§53);
* ``unparsed`` — разбор не удался даже после повторов (пользователю — 422).

Журнал диагностический: последние ``MAX_ENTRIES`` записей хранятся в памяти
процесса (как сессии §40), полный текст каждой записи уходит в лог сервиса,
а ``GET /api/unrecognized`` отдаёт их для разбора формулировок — какие фразы
стоит выучить шаблонами или промптом.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# сколько последних записей держим в памяти процесса
MAX_ENTRIES = 200

# человекочитаемые названия поводов — для сводки в API
KIND_LABELS: dict[str, str] = {
    "other": "модель отказалась: вопрос вне темы сервиса",
    "fallback": "модель недоступна, запрос разобран шаблонами",
    "unparsed": "запрос не удалось разобрать",
}

_entries: deque[dict] = deque(maxlen=MAX_ENTRIES)


def add(kind: str, query: str, detail: str = "") -> None:
    """Записать случай «не понял»: повод, сам запрос и детали для лога."""
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "query": (query or "").strip(),
        "detail": detail,
    }
    _entries.append(entry)
    logger.warning(
        "AI не понял [%s]: %r (%s)", kind, entry["query"], detail or "без деталей"
    )


def recent(limit: int = 50, kind: str | None = None) -> list[dict]:
    """Последние записи (свежие первыми), при необходимости — одного вида."""
    items = [e for e in reversed(_entries) if kind is None or e["kind"] == kind]
    return items[:limit]


def counts() -> dict[str, int]:
    """Сколько записей каждого вида — сводка для ответа API."""
    out: dict[str, int] = {}
    for entry in _entries:
        out[entry["kind"]] = out.get(entry["kind"], 0) + 1
    return out


def total() -> int:
    return len(_entries)


def clear() -> None:
    """Сброс журнала (используется в тестах)."""
    _entries.clear()
