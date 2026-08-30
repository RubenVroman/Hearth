"""SQLite house memory: conversations, preferences, house events, FTS5.

Sits next to auth on the compose ``./data`` volume. WAL + indexes, no Postgres.
Schema bumps live in ``SCHEMA_VERSION`` / ``_MIGRATIONS``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact

SCHEMA_VERSION = 1

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None

_KEY_SLUG = re.compile(r"[^a-z0-9]+")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def slug_key(text: str) -> str:
    raw = _KEY_SLUG.sub("-", (text or "").lower()).strip("-")
    return (raw[:48] or "note")


def memory_enabled() -> bool:
    return bool(settings.memory_enabled)


def db_path() -> Path:
    return Path(settings.memory_db_path)


def reset_memory() -> None:
    """Close the connection so tests can point at a fresh tmp db."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn = None
        _conn_path = None


def connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = str(db_path())
    with _lock:
        if _conn is not None and _conn_path == path:
            return _conn
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _conn = conn
        _conn_path = path
        return conn


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    ended_at TEXT,
    channel TEXT NOT NULL DEFAULT 'chat',
    title TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    summary_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    tool_name TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_turns_session_ts ON turns(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    text TEXT NOT NULL,
    covers_until_ts TEXT,
    source TEXT NOT NULL DEFAULT 'heuristic',
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, ts);

CREATE TABLE IF NOT EXISTS preferences (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'explicit',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_preferences_active ON preferences(active, key);

CREATE TABLE IF NOT EXISTS house_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    tool_name TEXT,
    ok INTEGER,
    notable INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_house_events_ts ON house_events(ts);
CREATE INDEX IF NOT EXISTS idx_house_events_kind ON house_events(kind, ts);

CREATE TABLE IF NOT EXISTS embeddings (
    id TEXT PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    ts TEXT NOT NULL,
    UNIQUE(owner_kind, owner_id)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_owner ON embeddings(owner_kind, owner_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    owner_kind UNINDEXED,
    owner_id UNINDEXED,
    body,
    tokenize = 'porter unicode61'
);
"""

# Future schema bumps: version -> list of SQL statements.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (),
}


def init_memory_db() -> None:
    conn = connect()
    with _lock:
        conn.executescript(_SCHEMA_V1)
        row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
        current = int(row["version"]) if row else 0
        if current == 0:
            conn.execute(
                "INSERT INTO schema_meta (id, version, applied_at) VALUES (1, ?, ?)",
                (SCHEMA_VERSION, _utcnow()),
            )
            current = SCHEMA_VERSION
        for version in sorted(_MIGRATIONS):
            if version <= current:
                continue
            for stmt in _MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute(
                "UPDATE schema_meta SET version = ?, applied_at = ? WHERE id = 1",
                (version, _utcnow()),
            )
            current = version
        if current < SCHEMA_VERSION:
            conn.execute(
                "UPDATE schema_meta SET version = ?, applied_at = ? WHERE id = 1",
                (SCHEMA_VERSION, _utcnow()),
            )
        conn.commit()


def schema_version() -> int:
    conn = connect()
    row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
    return int(row["version"]) if row else 0


def kv_get(key: str) -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def kv_set(key: str, value: str) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def _fts_delete(conn: sqlite3.Connection, owner_kind: str, owner_id: str) -> None:
    conn.execute(
        "DELETE FROM memory_fts WHERE owner_kind = ? AND owner_id = ?",
        (owner_kind, owner_id),
    )


def fts_upsert(owner_kind: str, owner_id: str, body: str) -> None:
    text = redact(body).strip()
    if not text:
        return
    conn = connect()
    with _lock:
        _fts_delete(conn, owner_kind, owner_id)
        conn.execute(
            "INSERT INTO memory_fts(owner_kind, owner_id, body) VALUES (?, ?, ?)",
            (owner_kind, owner_id, text),
        )
        conn.commit()


def fts_delete(owner_kind: str, owner_id: str) -> None:
    conn = connect()
    with _lock:
        _fts_delete(conn, owner_kind, owner_id)
        conn.commit()


def delete_embedding(owner_kind: str, owner_id: str) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "DELETE FROM embeddings WHERE owner_kind = ? AND owner_id = ?",
            (owner_kind, owner_id),
        )
        conn.commit()


def put_embedding(owner_kind: str, owner_id: str, model: str, vector: bytes, dim: int) -> None:
    conn = connect()
    now = _utcnow()
    with _lock:
        conn.execute(
            """
            INSERT INTO embeddings(id, owner_kind, owner_id, model, dim, vector, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_kind, owner_id) DO UPDATE SET
                model = excluded.model,
                dim = excluded.dim,
                vector = excluded.vector,
                ts = excluded.ts
            """,
            (new_id(), owner_kind, owner_id, model, dim, vector, now),
        )
        conn.commit()


def embeddings_for(kinds: list[str], *, limit: int = 400) -> list[dict[str, Any]]:
    conn = connect()
    if not kinds:
        return []
    placeholders = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"""
        SELECT owner_kind, owner_id, model, dim, vector, ts
        FROM embeddings
        WHERE owner_kind IN ({placeholders})
        ORDER BY ts DESC
        LIMIT ?
        """,
        (*kinds, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def fts_search(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    tokens = re.findall(r"[A-Za-z0-9_]+", query or "")
    if not tokens:
        return []
    match = " OR ".join(f'"{tok}"' for tok in tokens[:12])
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT owner_kind, owner_id, body, bm25(memory_fts) AS rank
            FROM memory_fts
            WHERE memory_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def ensure_session(channel: str = "chat") -> str:
    """Resume the live session if it is still within the idle window."""
    if not memory_enabled():
        return ""
    conn = connect()
    now = datetime.now(timezone.utc)
    idle = timedelta(minutes=max(1, int(settings.memory_session_idle_minutes)))
    current_id = kv_get("current_session_id")
    if current_id:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (current_id,)).fetchone()
        if row is not None:
            last = _parse_ts(str(row["last_ts"]))
            if now - last <= idle:
                return str(row["id"])
            with _lock:
                conn.execute(
                    "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    (_utcnow(), current_id),
                )
                conn.commit()
    session_id = new_id()
    ts = _utcnow()
    with _lock:
        conn.execute(
            """
            INSERT INTO sessions(id, started_at, last_ts, channel, turn_count)
            VALUES (?, ?, ?, ?, 0)
            """,
            (session_id, ts, ts, channel),
        )
        conn.execute(
            "INSERT INTO kv(key, value) VALUES ('current_session_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (session_id,),
        )
        conn.commit()
    return session_id


def persist_turn(
    role: str,
    text: str,
    *,
    session_id: str | None = None,
    channel: str = "chat",
    tool_name: str | None = None,
) -> dict[str, Any] | None:
    if not memory_enabled() or not settings.memory_store_conversations:
        return None
    cleaned = redact(text).strip()
    if not cleaned:
        return None
    if len(cleaned) > 8000:
        cleaned = cleaned[:8000]
    sid = session_id or ensure_session(channel)
    if not sid:
        return None
    turn_id = new_id()
    ts = _utcnow()
    conn = connect()
    with _lock:
        conn.execute(
            """
            INSERT INTO turns(id, session_id, ts, role, text, tool_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (turn_id, sid, ts, role, cleaned, tool_name),
        )
        conn.execute(
            """
            UPDATE sessions
            SET last_ts = ?, turn_count = turn_count + 1, channel = ?
            WHERE id = ?
            """,
            (ts, channel, sid),
        )
        conn.commit()
    if role in {"user", "assistant"}:
        fts_upsert("turn", turn_id, cleaned)
    return {"id": turn_id, "session_id": sid, "ts": ts, "role": role, "text": cleaned}


def session_row(session_id: str) -> dict[str, Any] | None:
    conn = connect()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def recent_turns(session_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT id, session_id, ts, role, text, tool_name
        FROM turns
        WHERE session_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def turns_since(session_id: str, after_ts: str | None, *, limit: int = 80) -> list[dict[str, Any]]:
    conn = connect()
    if after_ts:
        rows = conn.execute(
            """
            SELECT id, session_id, ts, role, text, tool_name
            FROM turns
            WHERE session_id = ? AND ts > ?
            ORDER BY ts ASC
            LIMIT ?
            """,
            (session_id, after_ts, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, session_id, ts, role, text, tool_name
            FROM turns
            WHERE session_id = ?
            ORDER BY ts ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_summary(session_id: str) -> dict[str, Any] | None:
    conn = connect()
    row = conn.execute(
        """
        SELECT id, session_id, ts, text, covers_until_ts, source
        FROM summaries
        WHERE session_id = ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def add_summary(session_id: str, text: str, *, covers_until_ts: str, source: str) -> dict[str, Any]:
    cleaned = redact(text).strip()
    summary_id = new_id()
    ts = _utcnow()
    conn = connect()
    with _lock:
        conn.execute(
            """
            INSERT INTO summaries(id, session_id, ts, text, covers_until_ts, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (summary_id, session_id, ts, cleaned, covers_until_ts, source),
        )
        conn.execute(
            "UPDATE sessions SET summary_id = ? WHERE id = ?",
            (summary_id, session_id),
        )
        conn.commit()
    fts_upsert("summary", summary_id, cleaned)
    return {
        "id": summary_id,
        "session_id": session_id,
        "text": cleaned,
        "source": source,
        "ts": ts,
    }


def remember_preference(
    key: str,
    value: str,
    *,
    category: str = "general",
    source: str = "explicit",
) -> dict[str, Any]:
    slug = slug_key(key or value)
    cleaned = redact(value).strip()
    if not cleaned:
        return {"ok": False, "error": "empty value after redaction"}
    if len(cleaned) > 2000:
        cleaned = cleaned[:2000]
    now = _utcnow()
    conn = connect()
    with _lock:
        existing = conn.execute("SELECT id FROM preferences WHERE key = ?", (slug,)).fetchone()
        if existing:
            pref_id = str(existing["id"])
            conn.execute(
                """
                UPDATE preferences
                SET value = ?, category = ?, source = ?, updated_at = ?, active = 1
                WHERE id = ?
                """,
                (cleaned, category or "general", source, now, pref_id),
            )
        else:
            pref_id = new_id()
            conn.execute(
                """
                INSERT INTO preferences(id, key, value, category, source, created_at, updated_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (pref_id, slug, cleaned, category or "general", source, now, now),
            )
        conn.commit()
    fts_upsert("preference", pref_id, f"{slug}: {cleaned}")
    return {
        "ok": True,
        "id": pref_id,
        "key": slug,
        "value": cleaned,
        "category": category or "general",
        "source": source,
        "updated_at": now,
    }


def list_preferences(*, limit: int = 50, include_inactive: bool = False) -> list[dict[str, Any]]:
    conn = connect()
    sql = """
        SELECT id, key, value, category, source, created_at, updated_at, active
        FROM preferences
    """
    params: list[Any] = []
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_preference(key: str) -> dict[str, Any] | None:
    conn = connect()
    row = conn.execute(
        "SELECT id, key, value, category, source, created_at, updated_at, active FROM preferences WHERE key = ?",
        (slug_key(key),),
    ).fetchone()
    return dict(row) if row else None


def forget(*, pref_id: str = "", key: str = "") -> dict[str, Any]:
    conn = connect()
    row = None
    if pref_id:
        row = conn.execute("SELECT * FROM preferences WHERE id = ?", (pref_id,)).fetchone()
    if row is None and key:
        row = conn.execute("SELECT * FROM preferences WHERE key = ?", (slug_key(key),)).fetchone()
    if row is None:
        return {"ok": False, "error": "not found"}
    owner_id = str(row["id"])
    with _lock:
        conn.execute("DELETE FROM preferences WHERE id = ?", (owner_id,))
        _fts_delete(conn, "preference", owner_id)
        conn.execute(
            "DELETE FROM embeddings WHERE owner_kind = ? AND owner_id = ?",
            ("preference", owner_id),
        )
        conn.commit()
    return {"ok": True, "forgotten": {"id": owner_id, "key": row["key"]}}


def log_house_event(
    title: str,
    detail: str,
    *,
    kind: str = "tool",
    tool_name: str | None = None,
    ok: bool | None = None,
    notable: bool = True,
) -> dict[str, Any] | None:
    if not memory_enabled() or not settings.memory_store_house_events:
        return None
    cleaned_title = redact(title).strip()[:240] or "event"
    cleaned_detail = redact(detail).strip()[:2000]
    event_id = new_id()
    ts = _utcnow()
    conn = connect()
    with _lock:
        conn.execute(
            """
            INSERT INTO house_events(id, ts, kind, title, detail, tool_name, ok, notable)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                ts,
                kind,
                cleaned_title,
                cleaned_detail,
                tool_name,
                None if ok is None else int(bool(ok)),
                int(bool(notable)),
            ),
        )
        conn.commit()
    fts_upsert("house_event", event_id, f"{cleaned_title} {cleaned_detail}")
    return {
        "id": event_id,
        "ts": ts,
        "kind": kind,
        "title": cleaned_title,
        "detail": cleaned_detail,
        "tool_name": tool_name,
    }


def recent_house_events(*, limit: int = 20) -> list[dict[str, Any]]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT id, ts, kind, title, detail, tool_name, ok, notable
        FROM house_events
        ORDER BY ts DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def lookup_owner(owner_kind: str, owner_id: str) -> dict[str, Any] | None:
    conn = connect()
    if owner_kind == "preference":
        row = conn.execute(
            "SELECT id, key, value, category, updated_at AS ts FROM preferences WHERE id = ?",
            (owner_id,),
        ).fetchone()
        if row:
            data = dict(row)
            data["kind"] = "preference"
            data["text"] = f"{data['key']}: {data['value']}"
            return data
    elif owner_kind == "summary":
        row = conn.execute(
            "SELECT id, session_id, ts, text, source FROM summaries WHERE id = ?",
            (owner_id,),
        ).fetchone()
        if row:
            data = dict(row)
            data["kind"] = "summary"
            return data
    elif owner_kind == "turn":
        row = conn.execute(
            "SELECT id, session_id, ts, role, text FROM turns WHERE id = ?",
            (owner_id,),
        ).fetchone()
        if row:
            data = dict(row)
            data["kind"] = "turn"
            return data
    elif owner_kind == "house_event":
        row = conn.execute(
            "SELECT id, ts, kind, title, detail, tool_name FROM house_events WHERE id = ?",
            (owner_id,),
        ).fetchone()
        if row:
            data = dict(row)
            data["kind"] = "house_event"
            data["text"] = f"{data['title']}: {data['detail']}"
            return data
    return None


def counts() -> dict[str, int]:
    conn = connect()
    out: dict[str, int] = {}
    for table in ("sessions", "turns", "summaries", "preferences", "house_events", "embeddings"):
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        out[table] = int(row["n"]) if row else 0
    active = conn.execute("SELECT COUNT(*) AS n FROM preferences WHERE active = 1").fetchone()
    out["preferences_active"] = int(active["n"]) if active else 0
    return out


def export_snapshot(*, limit_turns: int = 500, limit_events: int = 200) -> dict[str, Any]:
    """Redacted JSON-able dump. Caller must have confirmed."""
    prefs = list_preferences(limit=200)
    conn = connect()
    turns = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, session_id, ts, role, text, tool_name
            FROM turns ORDER BY ts DESC LIMIT ?
            """,
            (limit_turns,),
        ).fetchall()
    ]
    events = recent_house_events(limit=limit_events)
    summaries = [
        dict(row)
        for row in conn.execute(
            "SELECT id, session_id, ts, text, source FROM summaries ORDER BY ts DESC LIMIT 50"
        ).fetchall()
    ]
    return {
        "exported_at": _utcnow(),
        "schema_version": schema_version(),
        "counts": counts(),
        "preferences": prefs,
        "summaries": summaries,
        "turns": list(reversed(turns)),
        "house_events": events,
        "note": "Embeddings are omitted. Secrets were redacted on write.",
    }


def purge(
    *,
    conversations: bool = False,
    house_events: bool = False,
    preferences: bool = False,
) -> dict[str, Any]:
    conn = connect()
    deleted = {"turns": 0, "sessions": 0, "summaries": 0, "house_events": 0, "preferences": 0}
    with _lock:
        if conversations:
            deleted["turns"] = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
            deleted["summaries"] = conn.execute("SELECT COUNT(*) AS n FROM summaries").fetchone()["n"]
            deleted["sessions"] = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            conn.execute("DELETE FROM turns")
            conn.execute("DELETE FROM summaries")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM embeddings WHERE owner_kind IN ('turn', 'summary')")
            conn.execute("DELETE FROM memory_fts WHERE owner_kind IN ('turn', 'summary')")
            conn.execute("DELETE FROM kv WHERE key = 'current_session_id'")
        if house_events:
            deleted["house_events"] = conn.execute("SELECT COUNT(*) AS n FROM house_events").fetchone()["n"]
            conn.execute("DELETE FROM house_events")
            conn.execute("DELETE FROM embeddings WHERE owner_kind = 'house_event'")
            conn.execute("DELETE FROM memory_fts WHERE owner_kind = 'house_event'")
        if preferences:
            deleted["preferences"] = conn.execute("SELECT COUNT(*) AS n FROM preferences").fetchone()["n"]
            conn.execute("DELETE FROM preferences")
            conn.execute("DELETE FROM embeddings WHERE owner_kind = 'preference'")
            conn.execute("DELETE FROM memory_fts WHERE owner_kind = 'preference'")
        conn.commit()
    return {"ok": True, "purged": deleted}


def prune(*, now: datetime | None = None) -> dict[str, Any]:
    """Time-based plus cap-based cleanup. Preferences are kept unless retention > 0."""
    moment = now or datetime.now(timezone.utc)
    conv_days = max(0, int(settings.memory_retention_days))
    event_days = max(0, int(settings.memory_house_event_retention_days))
    pref_days = max(0, int(settings.memory_preference_retention_days))
    max_turns = max(0, int(settings.memory_max_turns))
    max_events = max(0, int(settings.memory_max_house_events))
    conn = connect()
    removed = {"turns": 0, "sessions": 0, "summaries": 0, "house_events": 0, "preferences": 0, "embeddings": 0}
    cutoff_iso = (moment - timedelta(days=conv_days)).isoformat() if conv_days else None
    event_cutoff = (moment - timedelta(days=event_days)).isoformat() if event_days else None
    pref_cutoff = (moment - timedelta(days=pref_days)).isoformat() if pref_days else None

    with _lock:
        if cutoff_iso:
            old_turns = conn.execute("SELECT id FROM turns WHERE ts < ?", (cutoff_iso,)).fetchall()
            ids = [row["id"] for row in old_turns]
            if ids:
                removed["turns"] = len(ids)
                conn.executemany("DELETE FROM memory_fts WHERE owner_kind = 'turn' AND owner_id = ?", [(i,) for i in ids])
                conn.executemany(
                    "DELETE FROM embeddings WHERE owner_kind = 'turn' AND owner_id = ?",
                    [(i,) for i in ids],
                )
                conn.execute("DELETE FROM turns WHERE ts < ?", (cutoff_iso,))
            old_sum = conn.execute("SELECT id FROM summaries WHERE ts < ?", (cutoff_iso,)).fetchall()
            for row in old_sum:
                conn.execute("DELETE FROM memory_fts WHERE owner_kind = 'summary' AND owner_id = ?", (row["id"],))
                conn.execute("DELETE FROM embeddings WHERE owner_kind = 'summary' AND owner_id = ?", (row["id"],))
            removed["summaries"] = len(old_sum)
            if old_sum:
                conn.execute("DELETE FROM summaries WHERE ts < ?", (cutoff_iso,))
            dangling = conn.execute(
                """
                SELECT id FROM sessions
                WHERE last_ts < ?
                  AND id NOT IN (SELECT DISTINCT session_id FROM turns)
                """,
                (cutoff_iso,),
            ).fetchall()
            removed["sessions"] = len(dangling)
            for row in dangling:
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))

        if event_cutoff:
            old_ev = conn.execute("SELECT id FROM house_events WHERE ts < ?", (event_cutoff,)).fetchall()
            removed["house_events"] = len(old_ev)
            for row in old_ev:
                conn.execute("DELETE FROM memory_fts WHERE owner_kind = 'house_event' AND owner_id = ?", (row["id"],))
                conn.execute("DELETE FROM embeddings WHERE owner_kind = 'house_event' AND owner_id = ?", (row["id"],))
            if old_ev:
                conn.execute("DELETE FROM house_events WHERE ts < ?", (event_cutoff,))

        if pref_cutoff:
            old_p = conn.execute("SELECT id FROM preferences WHERE updated_at < ?", (pref_cutoff,)).fetchall()
            removed["preferences"] = len(old_p)
            for row in old_p:
                conn.execute("DELETE FROM memory_fts WHERE owner_kind = 'preference' AND owner_id = ?", (row["id"],))
                conn.execute("DELETE FROM embeddings WHERE owner_kind = 'preference' AND owner_id = ?", (row["id"],))
            if old_p:
                conn.execute("DELETE FROM preferences WHERE updated_at < ?", (pref_cutoff,))

        if max_turns:
            n = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
            extra = int(n) - max_turns
            if extra > 0:
                oldest = conn.execute(
                    "SELECT id FROM turns ORDER BY ts ASC LIMIT ?",
                    (extra,),
                ).fetchall()
                for row in oldest:
                    conn.execute("DELETE FROM memory_fts WHERE owner_kind = 'turn' AND owner_id = ?", (row["id"],))
                    conn.execute("DELETE FROM embeddings WHERE owner_kind = 'turn' AND owner_id = ?", (row["id"],))
                    conn.execute("DELETE FROM turns WHERE id = ?", (row["id"],))
                removed["turns"] += extra

        if max_events:
            n = conn.execute("SELECT COUNT(*) AS n FROM house_events").fetchone()["n"]
            extra = int(n) - max_events
            if extra > 0:
                oldest = conn.execute(
                    "SELECT id FROM house_events ORDER BY ts ASC LIMIT ?",
                    (extra,),
                ).fetchall()
                for row in oldest:
                    conn.execute("DELETE FROM memory_fts WHERE owner_kind = 'house_event' AND owner_id = ?", (row["id"],))
                    conn.execute("DELETE FROM embeddings WHERE owner_kind = 'house_event' AND owner_id = ?", (row["id"],))
                    conn.execute("DELETE FROM house_events WHERE id = ?", (row["id"],))
                removed["house_events"] += extra

        conn.commit()

    kv_set("last_prune_at", _utcnow())
    return {"ok": True, "pruned": removed, "at": kv_get("last_prune_at")}
