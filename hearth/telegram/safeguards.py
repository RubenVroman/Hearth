"""Allowlist, dedup, and rate-limit safeguards for the Telegram inbox."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    max_calls: int = 6
    window_s: float = 60.0
    _hits: deque[float] = field(default_factory=deque)

    def reset(self) -> None:
        self._hits.clear()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > self.window_s:
            self._hits.popleft()
        if len(self._hits) >= self.max_calls:
            return False
        self._hits.append(now)
        return True


@dataclass
class Deduper:
    """Dedup by Telegram message id and by title+year within a short window."""

    window_s: float = 120.0
    _message_keys: dict[str, float] = field(default_factory=dict)
    _title_keys: dict[str, float] = field(default_factory=dict)

    def reset(self) -> None:
        self._message_keys.clear()
        self._title_keys.clear()

    def _prune(self, now: float) -> None:
        for store in (self._message_keys, self._title_keys):
            expired = [key for key, ts in store.items() if now - ts > self.window_s]
            for key in expired:
                del store[key]

    def seen_message(self, chat_id: int, message_id: int) -> bool:
        now = time.monotonic()
        self._prune(now)
        key = f"{chat_id}:{message_id}"
        if key in self._message_keys:
            return True
        self._message_keys[key] = now
        return False

    def seen_title(self, chat_id: int, dedup_key: str) -> bool:
        now = time.monotonic()
        self._prune(now)
        key = f"{chat_id}:{dedup_key}"
        if key in self._title_keys:
            return True
        self._title_keys[key] = now
        return False


def chat_allowed(chat_id: int, allowlist: list[int]) -> bool:
    return bool(allowlist) and int(chat_id) in allowlist


def user_allowed(user_id: int | None, allowlist: list[int], *, bot_user_id: int | None) -> bool:
    if user_id is None:
        return False
    if bot_user_id is not None and int(user_id) == int(bot_user_id):
        return False
    if not allowlist:
        return True
    return int(user_id) in allowlist
