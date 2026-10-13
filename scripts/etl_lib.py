"""Общие утилиты ETL-пайплайна (только stdlib — запускается без venv).

Воспроизводимость (§9): каждый скрипт идемпотентен, сырьё кэшируется в
data/raw/, на выходе — единый формат data/processed/.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"

USER_AGENT = "cultural-map-project/1.0 (educational project; contact: student@example.com)"

# Категории (§6)
CATEGORY_ROOTS = {
    "Q33506": "museum",       # museum
    "Q4989906": "monument",   # monument
    "Q5003624": "memorial",   # memorial
    "Q1007870": "gallery",    # art gallery
}

# Приоритет категории при совпадении предмета сразу с несколькими классами
CATEGORY_PRIORITY = ["gallery", "museum", "memorial", "monument"]


def ensure_dirs() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)


def http_get(url: str, *, headers: dict | None = None, timeout: int = 60,
             retries: int = 5, backoff: tuple[int, ...] = (3, 8, 20, 45, 90)) -> bytes:
    """GET с повторами: WDQS часто отдаёт 504/429 (§9 — воспроизводимость)."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # 429/5xx — временные, повторяем; 4xx — нет
            if exc.code not in (429, 408, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
        time.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise RuntimeError(f"GET failed after retries: {url} ({last_exc})")


def http_post_form(url: str, data: dict, *, timeout: int = 90, retries: int = 5) -> bytes:
    body = urllib.parse.urlencode(data).encode("utf-8")
    hdrs = {"User-Agent": USER_AGENT}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 408, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
        time.sleep((5, 15, 30, 60, 90)[min(attempt, 4)])
    raise RuntimeError(f"POST failed after retries: {url} ({last_exc})")


def http_post_json(url: str, payload: dict, *, timeout: int = 90, retries: int = 5) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 408, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
        time.sleep((5, 15, 30, 60, 90)[min(attempt, 4)])
    raise RuntimeError(f"POST json failed: {url} ({last_exc})")


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray casting (классический алгоритм, §10 — проверка принадлежности району)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def point_in_polygon(lon: float, lat: float, polygon: list[list[tuple[float, float]]]) -> bool:
    """polygon = [outer_ring, hole1, hole2, ...]"""
    if not polygon or not point_in_ring(lon, lat, polygon[0]):
        return False
    for hole in polygon[1:]:
        if point_in_ring(lon, lat, hole):
            return False
    return True


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние в метрах (используется только в ETL для дедупликации)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalize_name(name: str) -> str:
    """Нормализация названия для дедупликации: нижний регистр, без кавычек/пунктуации."""
    import re

    s = name.lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`.,()\-–—]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_similarity(a: str, b: str) -> float:
    """Jaccard по токенам — простая мера схожести названий."""
    ta, tb = set(normalize_name(a).split()), set(normalize_name(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
