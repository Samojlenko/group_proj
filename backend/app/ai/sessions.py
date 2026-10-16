"""Сессии диалога AI (§40): помним состоявшиеся обмены, чтобы понимать уточнения.

Без этого модуля каждый запрос живёт сам по себе: «а теперь только музеи»
или «покажи ещё» не к чему прицепить. Сессия хранит последние разборы,
список выданных объектов и сколько из них уже показано.

Хранилище — в памяти процесса: диалоги живут час и вытесняются по LRU
(500 последних). Для демо-сервиса этого достаточно; при горизонтальном
масштабировании на Redis интерфейс модуля менять не придётся.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_SESSIONS = 500
SESSION_TTL_SECONDS = 3600.0
SESSIONS_KEPT_TURNS = 8


@dataclass
class Turn:
    """Один состоявшийся обмен: вопрос, разбор и что показали."""

    query: str
    structured: dict
    action: str
    item_ids: list[int] = field(default_factory=list)
    total: int = 0
    # сколько объектов уже показано в этой ветке разговора (для «покажи ещё»)
    shown: int = 0


@dataclass
class Session:
    id: str
    turns: list[Turn] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def last(self) -> Turn | None:
        return self.turns[-1] if self.turns else None


class SessionStore:
    """Потокобезопасный LRU-набор сессий с временем жизни."""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        ttl_seconds: float = SESSION_TTL_SECONDS,
    ) -> None:
        self._max = max_sessions
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: OrderedDict[str, Session] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [sid for sid, s in self._items.items() if now - s.updated_at > self._ttl]
        for sid in expired:
            del self._items[sid]

    def get_or_create(self, session_id: str | None) -> Session:
        """Диалог по идентификатору; неизвестный/пустой — новый.

        Неизвестный id (сервер перезапустился, срок жизни истёк) получает
        чистую сессию с тем же именем: клиент продолжает работать как обычно.
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if session_id and session_id in self._items:
                session = self._items.pop(session_id)
                session.updated_at = now
                self._items[session_id] = session  # освежаем позицию в LRU
                return session
            session = Session(id=session_id or secrets.token_urlsafe(12))
            self._items[session.id] = session
            while len(self._items) > self._max:
                self._items.popitem(last=False)
            return session

    def remember(self, session: Session, turn: Turn,
                 keep: int = SESSIONS_KEPT_TURNS) -> None:
        """Запоминает обмен; старые шаги диалога вытесняются."""
        with self._lock:
            session.turns.append(turn)
            del session.turns[:-keep]
            session.updated_at = time.monotonic()

    def clear_all(self) -> None:
        """Полная очистка — нужна тестам и отладке."""
        with self._lock:
            self._items.clear()


store = SessionStore()
