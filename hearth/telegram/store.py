"""Durable, bounded Telegram state for restart-safe media requests.

SQLite lives on Hearth's existing ``./data`` volume. The store contains only
the minimum operational metadata needed for polling and idempotency; full
Telegram messages are never persisted.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from hearth.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    processed_at REAL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_state_time
    ON telegram_updates(state, claimed_at);

CREATE TABLE IF NOT EXISTS telegram_callback_actions (
    action_id TEXT PRIMARY KEY,
    callback_query_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER,
    media_key TEXT,
    state TEXT NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    retain_until REAL
);
CREATE INDEX IF NOT EXISTS idx_telegram_callbacks_state_time
    ON telegram_callback_actions(state, updated_at);

CREATE TABLE IF NOT EXISTS telegram_callback_media (
    callback_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_callback_media_expiry
    ON telegram_callback_media(expires_at);

CREATE TABLE IF NOT EXISTS telegram_requests (
    request_key TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    season INTEGER,
    title TEXT NOT NULL,
    external_request_id TEXT,
    state TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_requests_state_time
    ON telegram_requests(state, updated_at);
"""

_ACTIVE_REQUEST_STATES = frozenset(
    {"queued", "pending", "processing", "downloading", "retrying"}
)


def _clean_error(value: str | None) -> str | None:
    if value is None:
        return None
    # Store a bounded operational summary, never an exception-sized payload.
    return str(value).replace("\x00", "")[:500]


def _json_dump(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class TelegramStore:
    """Thread-safe SQLite store plus a non-blocking single-poller lease."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_updates: int = 20_000,
        max_callbacks: int = 10_000,
        max_terminal_requests: int = 5_000,
    ) -> None:
        self.path = Path(path if path is not None else settings.telegram_db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_updates = max(1, int(max_updates))
        self.max_callbacks = max(1, int(max_callbacks))
        self.max_terminal_requests = max(1, int(max_terminal_requests))
        self._lock = threading.RLock()
        self._writes = 0
        self._poller_fd: int | None = None
        self._conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Callback claims guard external POSTs. FULL keeps that claim durable
        # across host power loss before an Overseerr result can be reconciled.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """Add durable-state columns without replacing an existing Vault DB."""
        # Schema setup happens before the poller lease exists. Serialise an
        # old-database upgrade so two concurrently starting containers cannot
        # both attempt the same ALTER TABLE.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            update_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(telegram_updates)"
                ).fetchall()
            }
            if "attempts" not in update_columns:
                self._conn.execute(
                    """
                    ALTER TABLE telegram_updates
                    ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0
                    """
                )

            callback_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(telegram_callback_actions)"
                ).fetchall()
            }
            if "retain_until" not in callback_columns:
                self._conn.execute(
                    """
                    ALTER TABLE telegram_callback_actions
                    ADD COLUMN retain_until REAL
                    """
                )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_telegram_callbacks_retention
                ON telegram_callback_actions(state, retain_until)
                """
            )

            # Existing rows predate the retention column. Preserve any button
            # that could still be valid, including minute-rounding margin.
            retention = max(60, int(settings.telegram_callback_ttl_seconds)) + 60
            self._conn.execute(
                """
                UPDATE telegram_callback_actions
                SET retain_until = created_at + ?
                WHERE retain_until IS NULL
                """,
                (retention,),
            )
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    @property
    def poller_lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.poller.lock")

    @property
    def owns_poller_lock(self) -> bool:
        return self._poller_fd is not None

    def acquire_poller_lock(self) -> bool:
        """Try to become the sole getUpdates poller without waiting."""
        with self._lock:
            if self._poller_fd is not None:
                return True
            fd = os.open(self.poller_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                os.close(fd)
                return False
            self._poller_fd = fd
            # Updates are safe to replay. A callback may have died immediately
            # after Overseerr accepted its POST, so its outcome is ambiguous
            # and must never be blindly retried after a restart.
            with self._conn:
                self._conn.execute(
                    "UPDATE telegram_updates SET claimed_at = 0 WHERE state = 'processing'"
                )
                self._conn.execute(
                    """
                    UPDATE telegram_callback_actions
                    SET state = 'uncertain',
                        error = 'process stopped before request outcome was confirmed',
                        updated_at = ?
                    WHERE state = 'processing'
                    """,
                    (time.time(),),
                )
            return True

    def release_poller_lock(self) -> None:
        with self._lock:
            if self._poller_fd is None:
                return
            try:
                fcntl.flock(self._poller_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._poller_fd)
                self._poller_fd = None

    def close(self) -> None:
        with self._lock:
            self.release_poller_lock()
            self._conn.close()

    def __enter__(self) -> TelegramStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_offset(self) -> int | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM telegram_meta WHERE key = 'update_offset'"
            ).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None

    def bind_bot(self, bot_user_id: int) -> bool:
        """Bind update state to the verified Telegram bot identity.

        A token replacement can point at a bot whose update ids start far below
        the previous bot's offset. Reset only transport/callback state when the
        verified identity changes; accepted media tracking remains useful.
        """
        identity = str(int(bot_user_id))
        changed = False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM telegram_meta WHERE key = 'bot_user_id'"
            ).fetchone()
            previous = str(row["value"]) if row is not None else ""
            if previous and previous != identity:
                changed = True
                self._conn.execute("DELETE FROM telegram_updates")
                self._conn.execute(
                    "DELETE FROM telegram_meta WHERE key = 'update_offset'"
                )
                self._conn.execute("DELETE FROM telegram_callback_actions")
                self._conn.execute("DELETE FROM telegram_callback_media")
            self._conn.execute(
                """
                INSERT INTO telegram_meta(key, value) VALUES ('bot_user_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (identity,),
            )
            self._after_write_locked()
        return changed

    def _set_offset_locked(self, offset: int) -> None:
        row = self._conn.execute(
            "SELECT value FROM telegram_meta WHERE key = 'update_offset'"
        ).fetchone()
        current = int(row["value"]) if row is not None else None
        value = max(int(offset), current) if current is not None else int(offset)
        self._conn.execute(
            """
            INSERT INTO telegram_meta(key, value) VALUES ('update_offset', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(value),),
        )

    def set_offset(self, offset: int) -> None:
        """Persist a monotonically increasing getUpdates offset."""
        with self._lock, self._conn:
            self._set_offset_locked(int(offset))
            self._after_write_locked()

    def claim_update(self, update_id: int, *, lease_s: float = 120.0) -> bool:
        """Atomically claim a new update or a stale in-progress update."""
        uid = int(update_id)
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO telegram_updates(
                    update_id, state, claimed_at, processed_at, error, attempts
                ) VALUES (?, 'processing', ?, NULL, NULL, 1)
                """,
                (uid, now),
            )
            claimed = cursor.rowcount == 1
            if not claimed:
                cursor = self._conn.execute(
                    """
                    UPDATE telegram_updates
                    SET state = 'processing', claimed_at = ?, processed_at = NULL,
                        error = NULL, attempts = attempts + 1
                    WHERE update_id = ? AND (
                        state = 'failed'
                        OR (state = 'processing' AND claimed_at <= ?)
                    )
                    """,
                    (now, uid, now - max(1.0, float(lease_s))),
                )
                claimed = cursor.rowcount == 1
            if claimed:
                self._after_write_locked()
            return claimed

    def finish_update(
        self,
        update_id: int,
        *,
        state: str = "done",
        error: str | None = None,
    ) -> None:
        """Mark an update terminal.

        Offset advancement is intentionally separate: a concurrent poll batch
        must not acknowledge an earlier update merely because a later task
        finished first. Call ``set_offset(last_update_id + 1)`` only after the
        complete fetched batch has reached a terminal state.
        """
        if state not in {"done", "failed", "ignored", "dead_letter"}:
            raise ValueError("invalid update terminal state")
        uid = int(update_id)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO telegram_updates(
                    update_id, state, claimed_at, processed_at, error, attempts
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(update_id) DO UPDATE SET
                    state = excluded.state,
                    processed_at = excluded.processed_at,
                    error = excluded.error
                """,
                (uid, state, now, now, _clean_error(error)),
            )
            self._after_write_locked()

    def mark_update_processed(self, update_id: int) -> None:
        self.finish_update(update_id, state="done")

    def is_update_processed(self, update_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM telegram_updates WHERE update_id = ?",
                (int(update_id),),
            ).fetchone()
        # Failed handlers are retryable on Telegram redelivery. Callback-side
        # mutations remain exactly-once through telegram_callback_actions.
        return row is not None and row["state"] in {"done", "ignored", "dead_letter"}

    def update_record(self, update_id: int) -> dict[str, Any] | None:
        """Return bounded operational state for retry/dead-letter decisions."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM telegram_updates WHERE update_id = ?",
                (int(update_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_attempt_count(self, update_id: int) -> int:
        row = self.update_record(update_id)
        if row is None:
            return 0
        try:
            return max(0, int(row.get("attempts") or 0))
        except (TypeError, ValueError):
            return 0

    def claim_callback(
        self,
        action_id: str,
        *,
        callback_query_id: str,
        chat_id: int,
        user_id: int | None,
        media_key: str | None = None,
        lease_s: float = 300.0,
        reclaim_uncertain: bool = False,
    ) -> bool:
        """Claim a signed action exactly once.

        ``lease_s`` is retained for call-site compatibility but deliberately
        ignored: a stale write has an ambiguous outcome and is marked uncertain
        when a new poller acquires the database lock.
        """
        del lease_s
        now = time.time()
        retain_until = (
            now + max(60, int(settings.telegram_callback_ttl_seconds)) + 60
        )
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO telegram_callback_actions(
                    action_id, callback_query_id, chat_id, user_id, media_key,
                    state, error, created_at, updated_at, retain_until
                ) VALUES (?, ?, ?, ?, ?, 'processing', NULL, ?, ?, ?)
                """,
                (
                    str(action_id),
                    str(callback_query_id),
                    int(chat_id),
                    int(user_id) if user_id is not None else None,
                    str(media_key) if media_key is not None else None,
                    now,
                    now,
                    retain_until,
                ),
            )
            claimed = cursor.rowcount == 1
            if not claimed and reclaim_uncertain:
                cursor = self._conn.execute(
                    """
                    UPDATE telegram_callback_actions
                    SET callback_query_id = ?, user_id = ?, state = 'processing',
                        error = NULL, updated_at = ?, retain_until = ?
                    WHERE action_id = ? AND chat_id = ? AND state = 'uncertain'
                      AND COALESCE(media_key, '') = COALESCE(?, '')
                    """,
                    (
                        str(callback_query_id),
                        int(user_id) if user_id is not None else None,
                        now,
                        retain_until,
                        str(action_id),
                        int(chat_id),
                        str(media_key) if media_key is not None else None,
                    ),
                )
                claimed = cursor.rowcount == 1
            if claimed:
                self._after_write_locked()
            return claimed

    def record_request_and_finish_callback(
        self,
        action_id: str,
        request_key: str,
        *,
        media_type: str,
        tmdb_id: int,
        title: str,
        season: int | None = None,
        external_request_id: str | int | None = None,
        state: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Atomically journal an accepted provider result and close its action."""
        now = time.time()
        external_id = (
            str(external_request_id) if external_request_id is not None else None
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO telegram_requests(
                    request_key, media_type, tmdb_id, season, title,
                    external_request_id, state, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    title = excluded.title,
                    external_request_id = COALESCE(
                        excluded.external_request_id,
                        telegram_requests.external_request_id
                    ),
                    state = excluded.state,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    str(request_key),
                    str(media_type),
                    int(tmdb_id),
                    int(season) if season is not None else None,
                    str(title)[:300],
                    external_id,
                    str(state),
                    _json_dump(metadata),
                    now,
                    now,
                ),
            )
            cursor = self._conn.execute(
                """
                UPDATE telegram_callback_actions
                SET state = 'done', error = NULL, updated_at = ?
                WHERE action_id = ? AND state = 'processing'
                """,
                (now, str(action_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("callback action is not processing")
            self._after_write_locked()

    def finish_callback(
        self,
        action_id: str,
        *,
        state: str = "done",
        error: str | None = None,
    ) -> bool:
        if state not in {"done", "failed", "ignored", "uncertain"}:
            raise ValueError("invalid callback terminal state")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE telegram_callback_actions
                SET state = ?, error = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (state, _clean_error(error), time.time(), str(action_id)),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._after_write_locked()
            return changed

    def callback_state(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM telegram_callback_actions WHERE action_id = ?",
                (str(action_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def put_callback_media(
        self,
        callback_key: str,
        metadata: Mapping[str, Any],
        *,
        ttl_s: float = 6 * 60 * 60,
    ) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO telegram_callback_media(
                    callback_key, payload, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(callback_key) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (str(callback_key), _json_dump(metadata), now, now + max(1.0, float(ttl_s))),
            )
            self._after_write_locked()

    def get_callback_media(self, callback_key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT payload, expires_at FROM telegram_callback_media
                WHERE callback_key = ?
                """,
                (str(callback_key),),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= now:
                self._conn.execute(
                    "DELETE FROM telegram_callback_media WHERE callback_key = ?",
                    (str(callback_key),),
                )
                return None
            return _json_load(row["payload"])

    def clear_callback_media(self, callback_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM telegram_callback_media WHERE callback_key = ?",
                (str(callback_key),),
            )
            self._after_write_locked()

    def upsert_request(
        self,
        request_key: str,
        *,
        media_type: str,
        tmdb_id: int,
        title: str,
        season: int | None = None,
        external_request_id: str | int | None = None,
        state: str = "queued",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = time.time()
        external_id = str(external_request_id) if external_request_id is not None else None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO telegram_requests(
                    request_key, media_type, tmdb_id, season, title,
                    external_request_id, state, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    title = excluded.title,
                    external_request_id = COALESCE(
                        excluded.external_request_id,
                        telegram_requests.external_request_id
                    ),
                    state = excluded.state,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    str(request_key),
                    str(media_type),
                    int(tmdb_id),
                    int(season) if season is not None else None,
                    str(title)[:300],
                    external_id,
                    str(state),
                    _json_dump(metadata),
                    now,
                    now,
                ),
            )
            self._after_write_locked()

    def update_request(
        self,
        request_key: str,
        *,
        state: str | None = None,
        external_request_id: str | int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        assignments = ["updated_at = ?"]
        values: list[Any] = [time.time()]
        if state is not None:
            assignments.append("state = ?")
            values.append(str(state))
        if external_request_id is not None:
            assignments.append("external_request_id = ?")
            values.append(str(external_request_id))
        if metadata is not None:
            assignments.append("metadata = ?")
            values.append(_json_dump(metadata))
        values.append(str(request_key))
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"UPDATE telegram_requests SET {', '.join(assignments)} WHERE request_key = ?",
                values,
            )
            changed = cursor.rowcount == 1
            if changed:
                self._after_write_locked()
            return changed

    @staticmethod
    def _request_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _json_load(result.get("metadata"))
        return result

    def get_request(self, request_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM telegram_requests WHERE request_key = ?",
                (str(request_key),),
            ).fetchone()
        return self._request_dict(row) if row is not None else None

    def list_active_requests(
        self,
        *,
        states: Iterable[str] = _ACTIVE_REQUEST_STATES,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        selected = tuple(dict.fromkeys(str(state) for state in states))
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM telegram_requests
                WHERE state IN ({placeholders})
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (*selected, max(1, min(int(limit), 1_000))),
            ).fetchall()
        return [self._request_dict(row) for row in rows]

    def _after_write_locked(self) -> None:
        self._writes += 1
        if self._writes % 100 == 0:
            self._prune_locked(time.time())

    def _prune_locked(self, now: float) -> dict[str, int]:
        counts: dict[str, int] = {}
        cursor = self._conn.execute(
            "DELETE FROM telegram_callback_media WHERE expires_at <= ?",
            (now,),
        )
        counts["callback_media"] = max(0, cursor.rowcount)

        cursor = self._conn.execute(
            """
            DELETE FROM telegram_updates
            WHERE update_id NOT IN (
                SELECT update_id FROM telegram_updates ORDER BY update_id DESC LIMIT ?
            )
            """,
            (self.max_updates,),
        )
        counts["updates"] = max(0, cursor.rowcount)

        cursor = self._conn.execute(
            """
            DELETE FROM telegram_callback_actions
            WHERE state != 'processing'
              AND COALESCE(retain_until, updated_at) <= ?
              AND action_id NOT IN (
                SELECT action_id FROM telegram_callback_actions
                WHERE state != 'processing'
                ORDER BY updated_at DESC LIMIT ?
            )
            """,
            (now, self.max_callbacks),
        )
        counts["callbacks"] = max(0, cursor.rowcount)

        placeholders = ",".join("?" for _ in _ACTIVE_REQUEST_STATES)
        cursor = self._conn.execute(
            f"""
            DELETE FROM telegram_requests
            WHERE state NOT IN ({placeholders}) AND request_key NOT IN (
                SELECT request_key FROM telegram_requests
                WHERE state NOT IN ({placeholders})
                ORDER BY updated_at DESC LIMIT ?
            )
            """,
            (
                *_ACTIVE_REQUEST_STATES,
                *_ACTIVE_REQUEST_STATES,
                self.max_terminal_requests,
            ),
        )
        counts["requests"] = max(0, cursor.rowcount)
        return counts

    def prune(self) -> dict[str, int]:
        """Expire transient data and cap completed history without touching active work."""
        with self._lock, self._conn:
            return self._prune_locked(time.time())
