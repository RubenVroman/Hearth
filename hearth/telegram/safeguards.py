"""Authorization and keyed rate-limit safeguards."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field


def _monotonic() -> float:
    return time.monotonic()


@dataclass
class RateLimiter:
    """Keyed sliding-window limiter.

    A key should normally be ``(chat_id, user_id)``. ``allow()`` without a key
    remains useful for callers that intentionally want a single global bucket.
    """

    max_calls: int = 6
    window_s: float = 60.0
    clock: Callable[[], float] = _monotonic
    _hits: dict[Hashable | None, deque[float]] = field(
        default_factory=lambda: defaultdict(deque),
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset(self, key: Hashable | None = None, *, all_keys: bool = True) -> None:
        with self._lock:
            if all_keys:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _prune_bucket(self, key: Hashable | None, now: float) -> deque[float]:
        bucket = self._hits[key]
        while bucket and now - bucket[0] >= self.window_s:
            bucket.popleft()
        return bucket

    def allow(self, key: Hashable | None = None) -> bool:
        if self.max_calls <= 0:
            return False
        now = self.clock()
        with self._lock:
            bucket = self._prune_bucket(key, now)
            if len(bucket) >= self.max_calls:
                return False
            bucket.append(now)
            return True

    def retry_after(self, key: Hashable | None = None) -> float:
        """Seconds until the keyed bucket may accept another request."""
        now = self.clock()
        with self._lock:
            bucket = self._prune_bucket(key, now)
            if len(bucket) < self.max_calls or not bucket:
                return 0.0
            return max(0.0, self.window_s - (now - bucket[0]))


def chat_allowed(chat_id: int, allowlist: Iterable[int]) -> bool:
    """Chats are fail-closed: at least one explicitly allowed chat is required."""
    allowed = {int(value) for value in allowlist}
    return bool(allowed) and int(chat_id) in allowed


def user_allowed(
    user_id: int | None,
    allowlist: Iterable[int],
    *,
    bot_user_id: int | None,
) -> bool:
    """Reject bots/missing users, then apply the optional household user list."""
    if user_id is None:
        return False
    user = int(user_id)
    if bot_user_id is not None and user == int(bot_user_id):
        return False
    allowed = {int(value) for value in allowlist}
    return not allowed or user in allowed


def authorized(
    *,
    chat_id: int,
    user_id: int | None,
    chat_allowlist: Iterable[int],
    user_allowlist: Iterable[int],
    bot_user_id: int | None,
) -> bool:
    """Return whether this actor may make media requests in this chat."""
    return chat_allowed(chat_id, chat_allowlist) and user_allowed(
        user_id,
        user_allowlist,
        bot_user_id=bot_user_id,
    )
