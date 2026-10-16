"""Клиент AI inference server (Ollama-совместимый API: /api/chat, /api/embeddings).

Архитектурное ограничение (§4, §53): модель используется ТОЛЬКО для
преобразования естественного языка в структурированный JSON. Она не имеет
доступа к БД и не вычисляет расстояния. При недоступности AI система
продолжает работать в обычном GIS-режиме.
"""

import logging
import time

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# кэш доступности inference server (§46): /api/query не должен дёргать
# HTTP-проверку на каждый запрос
_AVAIL_TTL_SECONDS = 10.0
_avail_cache: dict[str, tuple[bool, float]] = {}

# кэш эмбеддингов и ответов LLM (§46) — одинаковый текст не должен
# стоить повторного вызова модели
_EMBED_CACHE_SIZE = 512
_embed_cache: dict[str, list[float]] = {}
_chat_cache: dict[str, str] = {}
_CHAT_CACHE_SIZE = 256


def _MESSAGES_KEY(messages: list[dict]) -> str:
    """Ключ кэша LLM: модель + содержимое сообщений."""
    try:
        from json import dumps

        return dumps({"m": messages}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # noqa: PERF203 - нестандартные сообщения
        return ""


class AIServiceUnavailable(Exception):
    """AI inference server недоступен (HTTP 503, §44)."""


def check_available(settings: Settings, timeout: float = 3.0) -> bool:
    """Проверка доступности inference server (используется в /api/health)."""
    if not settings.ai_enabled:
        return False
    key = f"{settings.ai_base_url}|{settings.ai_model}"
    hit = _avail_cache.get(key)
    if hit and time.monotonic() - hit[1] < _AVAIL_TTL_SECONDS:
        return hit[0]
    try:
        resp = httpx.get(f"{settings.ai_base_url.rstrip('/')}/api/tags", timeout=timeout)
        ok = resp.status_code == 200
    except Exception:  # noqa: BLE001 - любая сетевая ошибка означает «недоступен»
        ok = False
    _avail_cache[key] = (ok, time.monotonic())
    return ok


def invalidate_availability_cache() -> None:
    """Сброс кэша доступности (после перезапуска inference server)."""
    _avail_cache.clear()


def chat_completion(settings: Settings, messages: list[dict], *, json_format: bool = True) -> str:
    """Одно сообщение модели через Ollama /api/chat. Возвращает текст ответа.

    Идентичные запросы обслуживаются из кэша (§46) — повторный ввод того же
    текста не приводит к повторному вызову модели.
    """
    cache_key = _MESSAGES_KEY(messages)
    if cache_key:
        cache_key = f"{settings.ai_model}|{cache_key}"
        if cache_key in _chat_cache:
            return _chat_cache[cache_key]
    payload: dict = {
        "model": settings.ai_model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    if json_format:
        payload["format"] = "json"
    try:
        resp = httpx.post(
            f"{settings.ai_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=settings.ai_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise AIServiceUnavailable(f"AI server недоступен: {exc}") from exc
    if resp.status_code >= 500:
        raise AIServiceUnavailable(f"AI server вернул {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    content = data.get("message", {}).get("content", "")
    if cache_key:
        if len(_chat_cache) >= _CHAT_CACHE_SIZE:
            _chat_cache.pop(next(iter(_chat_cache)))
        _chat_cache[cache_key] = content
    return content


def chat_tools(settings: Settings, messages: list[dict], tools: list[dict]) -> dict:
    """Вызов модели с инструментами — function calling (идея 10).

    Возвращает message-объект ответа ``{"content": str, "tool_calls": [...]}``:
    либо модель просит исполнить инструменты, либо отвечает текстом.
    Кэш здесь не используется: результат зависит от всей истории диалога
    с инструментами, а она у каждого запроса своя.
    """
    payload: dict = {
        "model": settings.ai_model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    try:
        resp = httpx.post(
            f"{settings.ai_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=settings.ai_timeout_seconds,
        )
        # 500+ и 4xx (модель без поддержки tools) — сервис, а не пользователь:
        # наверх уходит AIServiceUnavailable, вызывающий код откатывается (§53)
        if resp.status_code >= 500:
            raise AIServiceUnavailable(f"AI server вернул {resp.status_code}")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AIServiceUnavailable(f"AI server недоступен: {exc}") from exc
    message = resp.json().get("message") or {}
    return {
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
    }


def embed(settings: Settings, text: str) -> list[float]:
    """Эмбеддинг текста через inference server (§27, кэш §46)."""
    if not settings.ai_enabled:
        raise AIServiceUnavailable("AI отключён (AI_ENABLED=false)")
    key = f"{settings.embedding_model}|{text}"
    hit = _embed_cache.get(key)
    if hit is not None:
        return hit
    try:
        resp = httpx.post(
            f"{settings.ai_base_url.rstrip('/')}/api/embeddings",
            json={"model": settings.embedding_model, "prompt": text},
            timeout=settings.ai_timeout_seconds,
        )
        resp.raise_for_status()
        vector = resp.json()["embedding"]
    except httpx.HTTPError as exc:
        raise AIServiceUnavailable(f"не удалось получить эмбеддинг: {exc}") from exc
    if len(_embed_cache) >= _EMBED_CACHE_SIZE:  # простой вытесняющий кэш
        _embed_cache.pop(next(iter(_embed_cache)))
    _embed_cache[key] = vector
    return vector


def clear_caches() -> None:
    """Сброс всех кэшей клиента (используется в тестах)."""
    _avail_cache.clear()
    _embed_cache.clear()
    _chat_cache.clear()
