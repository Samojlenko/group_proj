"""Часы работы → «открыто сейчас» (интенты: фильтр open_now).

В БД часы хранятся текстом: OSM-нотация («Mo-Su 10:00-18:00») или русские
фразы («Ежедневно 10:30-18:00», «Вт-Сб 11:00-20:00, кроме понедельника»).
Полный синтаксис opening_hours здесь не нужен — разбираем типовые формы, а
что не удалось разобрать, честно помечаем как «неизвестно» (None): такие
объекты в выдачу «открыто сейчас» не попадают, но в пояснении посчитаны.

Время московское: в России нет перехода на летнее время, поэтому фиксированный
UTC+3 — без зависимости от tzdata в контейнере.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

# «круглосуточно», «24/7», «работает всегда»
_ALWAYS_RE = re.compile(
    r"круглосуточн|24\s*[/хx×]\s*7|24\s*часа|работает\s+всегда", re.IGNORECASE
)

# «10:00-18:00», «10.00–18.00», «с 11:00 до 20:00»
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2})[:.](\d{2})\s*(?:[-–—]|до)\s*(\d{1,2})[:.](\d{2})"
)

# «кроме понедельника», «кроме пн»
_EXCEPT_RE = re.compile(r"кроме\s+([^;,]+)", re.IGNORECASE)

_RU_DAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
_EN_DAYS = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6}

# полные названия дней — для «кроме понедельника» (сокр. «пн» там не встречается)
_RU_FULL = (
    ("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3),
    ("пятниц", 4), ("суббот", 5), ("воскрес", 6),
)

# токен дня недели: отдельное двухбуквенное слово («пн», «вс», «mo»),
# а не кусок слова («все» → «вс» не считается)
_DAY_TOKEN_RE = re.compile(r"(?<![а-яёa-z])([а-яё]{2}|[a-z]{2})(?![а-яёa-z])")

# диапазон дней: «пн-вс», «пн — пт», «с пн по пт», «mo-su»
_DAY_RANGE_RE = re.compile(
    r"(?<![а-яёa-z])([а-яё]{2}|[a-z]{2})\s*(?:[-–—]+\s*|\s+по\s+)"
    r"([а-яё]{2}|[a-z]{2})(?![а-яёa-z])"
)

# сегменты расписания: «;» и «,» перед днём недели или «кроме»
_SEGMENT_SPLIT_RE = re.compile(
    r";|,(?=\s*(?:[а-яёa-z]{2}(?![а-яёa-z])|кроме|понедельник|вторник|"
    r"сред|четверг|пятниц|суббот|воскрес))",
    re.IGNORECASE,
)


def _moscow_now() -> datetime:
    """Текущее московское время (отдельная функция — тесты подменяют её)."""
    return datetime.now(timezone.utc).astimezone(MSK)


def _day(two: str) -> int | None:
    if two in _RU_DAYS:
        return _RU_DAYS[two]
    return _EN_DAYS.get(two)


def _days_in(text: str) -> set[int]:
    """Дни, упомянутые в тексте: сокращения («пн»), английские («mo»), полные названия."""
    low = text.lower().replace("ё", "е")
    out = {d for two in _DAY_TOKEN_RE.findall(low) if (d := _day(two)) is not None}
    for stem, d in _RU_FULL:
        if stem in low:
            out.add(d)
    return out


def _parse_days(text: str) -> set[int] | None:
    """Дни недели сегмента; None — спецификатора нет (действует ежедневно)."""
    low = text.lower().replace("ё", "е").replace("–", "-").replace("—", "-")
    if not re.search(r"[а-яёa-z]", low):
        return None
    if "ежедневн" in low or "без выходных" in low or "все дни" in low:
        return set(range(7))
    if "выходн" in low:
        return {5, 6}
    if "будн" in low:
        return {0, 1, 2, 3, 4}
    days: set[int] = set()
    for a, b in _DAY_RANGE_RE.findall(low):
        da, db = _day(a), _day(b)
        if da is None or db is None:
            continue
        if da <= db:
            days.update(range(da, db + 1))
        else:  # диапазон через воскресенье: «сб-вт»
            days.update(list(range(da, 7)) + list(range(0, db + 1)))
    days |= _days_in(low)
    return days or None


def _within(now_min: int, start: int, end: int) -> bool:
    """Текущее время внутри интервала; интервалы через полночь тоже поддержаны."""
    if start <= end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def is_open_now(text: str | None, now: datetime | None = None) -> bool | None:
    """Открыто ли сейчас по тексту часов.

    True/False — уверенный ответ; None — данных нет или формат не распознан
    (такие объекты в выдачу «открыто сейчас» не попадают).
    """
    if not text or not text.strip():
        return None
    low = text.lower().replace("ё", "е").replace("–", "-").replace("—", "-")
    if _ALWAYS_RE.search(low):
        return True

    if not _TIME_RANGE_RE.search(low):
        return None

    moment = now or _moscow_now()
    today = moment.weekday()
    now_min = moment.hour * 60 + moment.minute

    # закрытые дни: «кроме понедельника» (рус.) и «Su,Mo off» (OSM-нотация
    # выходных): такие сегменты времени не содержат, но день закрывают
    closed_days = _days_in(" ".join(_EXCEPT_RE.findall(low)))
    segments = _SEGMENT_SPLIT_RE.split(low)
    for segment in segments:
        if "off" in segment and not _TIME_RANGE_RE.search(segment):
            closed_days |= _days_in(segment)
    closed_today = today in closed_days

    for segment in segments:
        segment_ranges = _TIME_RANGE_RE.findall(segment)
        if not segment_ranges:
            continue
        days = _parse_days(_TIME_RANGE_RE.sub(" ", segment))
        if days is None:
            days = set(range(7))
        if closed_today:
            days = days - {today}
        if today not in days:
            continue
        for h1, m1, h2, m2 in segment_ranges:
            if int(h1) > 24 or int(h2) > 24 or int(m1) > 59 or int(m2) > 59:
                continue
            if _within(now_min, int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)):
                return True
    return False
