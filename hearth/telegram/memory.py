"""Per-chat Telegram conversation window for plot/follow-up context.

Rolling ~24 turns, 6-hour idle TTL. Persists under ``data/`` so a container
recreate does not wipe a mid-chat. Tracks offered TMDB ids across that window
so "others" / None-of-these can paginate without repeats. No new env keys —
fixed path next to the other house data files.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from hearth.memory.redact import redact

log = logging.getLogger("hearth.telegram")

MAX_TURNS = 24
IDLE_TTL_S = 6 * 60 * 60
MAX_SHOWN_IDS = 64
DEFAULT_PATH = Path("./data/telegram-chat-memory.json")

Role = Literal["user", "bot"]


@dataclass
class ChatTurn:
    role: Role
    text: str
    ts: float = field(default_factory=time.time)
    search_title: str = ""
    media_kind: str = ""
    offered: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ChatThread:
    chat_id: int
    turns: list[ChatTurn] = field(default_factory=list)
    subject_title: str = ""
    subject_media_kind: str = ""
    offered: list[dict[str, Any]] = field(default_factory=list)
    rejected_titles: list[str] = field(default_factory=list)
    shown_tmdb_ids: list[int] = field(default_factory=list)
    last_genre_ids: list[int] = field(default_factory=list)
    last_exclude_genre_ids: list[int] = field(default_factory=list)
    last_discover_page: int = 1
    last_discover_media_type: str = "movie"
    updated_at: float = field(default_factory=time.time)

    def alive(self, *, now: float | None = None, ttl_s: float = IDLE_TTL_S) -> bool:
        stamp = now if now is not None else time.time()
        if (stamp - self.updated_at) > ttl_s:
            return False
        # Turns OR discover/session metadata keep the window warm.
        return bool(
            self.turns
            or self.shown_tmdb_ids
            or self.last_genre_ids
            or self.offered
            or self.rejected_titles
        )


def _compact_offered(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in (rows or [])[:12]:
        out.append(
            {
                "title": str(row.get("title") or "")[:120],
                "year": row.get("year"),
                "tmdbId": row.get("tmdbId") or row.get("mediaId"),
                "tvdbId": row.get("tvdbId"),
            }
        )
    return out


def _tmdb_ids_from_rows(rows: list[dict[str, Any]] | None) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for row in rows or []:
        raw = row.get("tmdbId") if isinstance(row, dict) else row
        if isinstance(row, dict) and raw in (None, ""):
            raw = row.get("mediaId")
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def _int_list(raw: Any) -> list[int]:
    out: list[int] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


class ChatMemory:
    """In-memory + JSON-backed chat windows keyed by Telegram chat_id."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._lock = threading.Lock()
        self._threads: dict[int, ChatThread] = {}
        self._load()

    def reset(self) -> None:
        with self._lock:
            self._threads.clear()
            self._persist_unlocked()

    def get(self, chat_id: int) -> ChatThread | None:
        with self._lock:
            self._prune_unlocked()
            thread = self._threads.get(int(chat_id))
            if thread is None or not thread.alive():
                return None
            return thread

    def has_history(self, chat_id: int) -> bool:
        thread = self.get(chat_id)
        return thread is not None and bool(thread.turns)

    def history_blob(self, chat_id: int) -> list[dict[str, Any]]:
        thread = self.get(chat_id)
        if thread is None:
            return []
        rows: list[dict[str, Any]] = []
        for turn in thread.turns[-MAX_TURNS:]:
            row: dict[str, Any] = {
                "role": turn.role,
                "text": redact(turn.text)[:240],
            }
            if turn.search_title:
                row["search_title"] = redact(turn.search_title)[:120]
            if turn.media_kind:
                row["media_kind"] = turn.media_kind
            if turn.offered:
                row["offered"] = turn.offered[:8]
            rows.append(row)
        return rows

    def subject(self, chat_id: int) -> tuple[str, str]:
        thread = self.get(chat_id)
        if thread is None:
            return "", ""
        return thread.subject_title, thread.subject_media_kind

    def offered(self, chat_id: int) -> list[dict[str, Any]]:
        thread = self.get(chat_id)
        if thread is None:
            return []
        return list(thread.offered)

    def rejected(self, chat_id: int) -> list[str]:
        thread = self.get(chat_id)
        if thread is None:
            return []
        return list(thread.rejected_titles)

    def shown_tmdb_ids(self, chat_id: int) -> list[int]:
        thread = self.get(chat_id)
        if thread is None:
            return []
        return list(thread.shown_tmdb_ids)

    def remember_shown(
        self,
        chat_id: int,
        rows: list[dict[str, Any]] | None,
    ) -> None:
        ids = _tmdb_ids_from_rows(rows)
        if not ids:
            return
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            existing = list(thread.shown_tmdb_ids)
            seen = set(existing)
            for tid in ids:
                if tid not in seen:
                    seen.add(tid)
                    existing.append(tid)
            thread.shown_tmdb_ids = existing[-MAX_SHOWN_IDS:]
            thread.updated_at = time.time()
            self._persist_unlocked()

    def set_discover_cursor(
        self,
        chat_id: int,
        *,
        genre_ids: list[int] | None = None,
        exclude_genre_ids: list[int] | None = None,
        page: int | None = None,
        media_type: str | None = None,
    ) -> None:
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            if genre_ids is not None:
                thread.last_genre_ids = [int(g) for g in genre_ids]
            if exclude_genre_ids is not None:
                thread.last_exclude_genre_ids = [int(g) for g in exclude_genre_ids]
            if page is not None:
                try:
                    thread.last_discover_page = max(1, int(page))
                except (TypeError, ValueError):
                    thread.last_discover_page = 1
            if media_type in {"movie", "tv"}:
                thread.last_discover_media_type = media_type
            thread.updated_at = time.time()
            self._persist_unlocked()

    def discover_cursor(self, chat_id: int) -> dict[str, Any]:
        thread = self.get(chat_id)
        if thread is None:
            return {
                "genre_ids": [],
                "exclude_genre_ids": [],
                "page": 1,
                "media_type": "movie",
            }
        return {
            "genre_ids": list(thread.last_genre_ids),
            "exclude_genre_ids": list(thread.last_exclude_genre_ids),
            "page": int(thread.last_discover_page or 1),
            "media_type": thread.last_discover_media_type
            if thread.last_discover_media_type in {"movie", "tv"}
            else "movie",
        }

    def remember_rejected(
        self,
        chat_id: int,
        titles: list[str] | None,
        *,
        clear_offered: bool = False,
        clear_subject: bool = False,
    ) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for title in titles or []:
            text = re.sub(r"\s+", " ", str(title or "")).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text[:200])
        if not cleaned and not clear_offered and not clear_subject:
            return
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            existing = {
                re.sub(r"\s+", " ", t).strip().lower(): t
                for t in thread.rejected_titles
            }
            for title in cleaned:
                existing[title.lower()] = title
            thread.rejected_titles = list(existing.values())[-24:]
            if clear_offered:
                thread.offered = []
            if clear_subject:
                thread.subject_title = ""
                thread.subject_media_kind = ""
            thread.updated_at = time.time()
            self._persist_unlocked()

    def clear_rejected(self, chat_id: int) -> None:
        with self._lock:
            thread = self._threads.get(int(chat_id))
            if thread is None:
                return
            thread.rejected_titles = []
            thread.updated_at = time.time()
            self._persist_unlocked()

    def clear_offered(self, chat_id: int) -> None:
        """Drop leftover disambiguation rows (e.g. after a unique grab)."""
        with self._lock:
            thread = self._threads.get(int(chat_id))
            if thread is None:
                return
            thread.offered = []
            thread.updated_at = time.time()
            self._persist_unlocked()

    def record_user(self, chat_id: int, text: str) -> None:
        raw = (text or "").strip()
        if not raw:
            return
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            thread.turns.append(ChatTurn(role="user", text=raw[:240]))
            self._trim_unlocked(thread)
            thread.updated_at = time.time()
            self._persist_unlocked()

    def record_bot(
        self,
        chat_id: int,
        reply: str,
        *,
        search_title: str = "",
        media_kind: str = "",
        offered: list[dict[str, Any]] | None = None,
    ) -> None:
        raw = (reply or "").strip()
        if not raw and not search_title and offered is None:
            return
        # offered=None → leave sticky list alone; offered=[] clears it.
        compact = _compact_offered(offered) if offered is not None else None
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            thread.turns.append(
                ChatTurn(
                    role="bot",
                    text=(raw or search_title)[:400],
                    search_title=(search_title or "")[:200],
                    media_kind=media_kind if media_kind in {"movie", "tv"} else "",
                    offered=compact or [],
                )
            )
            if search_title:
                thread.subject_title = search_title[:200]
                if media_kind in {"movie", "tv"}:
                    thread.subject_media_kind = media_kind
                # Successful new subject replaces prior rejects for that title.
                thread.rejected_titles = [
                    t
                    for t in thread.rejected_titles
                    if t.strip().lower() != search_title.strip().lower()
                ]
            if offered is not None:
                thread.offered = compact or []
                for tid in _tmdb_ids_from_rows(compact):
                    if tid not in thread.shown_tmdb_ids:
                        thread.shown_tmdb_ids.append(tid)
                thread.shown_tmdb_ids = thread.shown_tmdb_ids[-MAX_SHOWN_IDS:]
            self._trim_unlocked(thread)
            thread.updated_at = time.time()
            self._persist_unlocked()

    def set_subject(
        self,
        chat_id: int,
        title: str,
        *,
        media_kind: str = "",
        offered: list[dict[str, Any]] | None = None,
        clear_rejected: bool = False,
        clear_offered: bool = False,
    ) -> None:
        title = (title or "").strip()
        if not title:
            return
        compact = _compact_offered(offered) if offered is not None else None
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            thread.subject_title = title[:200]
            if media_kind in {"movie", "tv"}:
                thread.subject_media_kind = media_kind
            if clear_offered:
                thread.offered = []
            elif compact:
                thread.offered = compact
                for tid in _tmdb_ids_from_rows(compact):
                    if tid not in thread.shown_tmdb_ids:
                        thread.shown_tmdb_ids.append(tid)
                thread.shown_tmdb_ids = thread.shown_tmdb_ids[-MAX_SHOWN_IDS:]
            if clear_rejected:
                thread.rejected_titles = [
                    t
                    for t in thread.rejected_titles
                    if t.strip().lower() != title.lower()
                ]
            thread.updated_at = time.time()
            self._persist_unlocked()

    def _ensure_unlocked(self, chat_id: int) -> ChatThread:
        thread = self._threads.get(chat_id)
        now = time.time()
        if thread is None:
            thread = ChatThread(chat_id=chat_id)
            self._threads[chat_id] = thread
            return thread
        # Idle TTL expired → fresh thread. Do NOT wipe a brand-new thread that
        # only has discover cursor / shown ids before the first turn is recorded
        # (alive() requires turns, which land in _finish after tools run).
        if (now - thread.updated_at) > IDLE_TTL_S:
            thread = ChatThread(chat_id=chat_id)
            self._threads[chat_id] = thread
        return thread

    def _trim_unlocked(self, thread: ChatThread) -> None:
        if len(thread.turns) > MAX_TURNS:
            thread.turns = thread.turns[-MAX_TURNS:]

    def _prune_unlocked(self) -> None:
        now = time.time()
        dead = [cid for cid, th in self._threads.items() if not th.alive(now=now)]
        for cid in dead:
            del self._threads[cid]

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.info("telegram chat memory unreadable; starting empty")
            return
        chats = raw.get("chats") if isinstance(raw, dict) else None
        if not isinstance(chats, dict):
            return
        now = time.time()
        for key, blob in chats.items():
            try:
                chat_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(blob, dict):
                continue
            turns_raw = blob.get("turns") or []
            turns: list[ChatTurn] = []
            if isinstance(turns_raw, list):
                for item in turns_raw[-MAX_TURNS:]:
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role") or "")
                    if role not in {"user", "bot"}:
                        continue
                    turns.append(
                        ChatTurn(
                            role=role,  # type: ignore[arg-type]
                            text=str(item.get("text") or "")[:400],
                            ts=float(item.get("ts") or now),
                            search_title=str(item.get("search_title") or "")[:200],
                            media_kind=str(item.get("media_kind") or ""),
                            offered=_compact_offered(item.get("offered") or []),
                        )
                    )
            thread = ChatThread(
                chat_id=chat_id,
                turns=turns,
                subject_title=str(blob.get("subject_title") or "")[:200],
                subject_media_kind=str(blob.get("subject_media_kind") or ""),
                offered=_compact_offered(blob.get("offered") or []),
                rejected_titles=[
                    str(t)[:200]
                    for t in (blob.get("rejected_titles") or [])
                    if str(t).strip()
                ][:24],
                shown_tmdb_ids=_int_list(blob.get("shown_tmdb_ids"))[-MAX_SHOWN_IDS:],
                last_genre_ids=_int_list(blob.get("last_genre_ids")),
                last_exclude_genre_ids=_int_list(blob.get("last_exclude_genre_ids")),
                last_discover_page=max(1, int(blob.get("last_discover_page") or 1)),
                last_discover_media_type=(
                    "tv"
                    if str(blob.get("last_discover_media_type") or "") == "tv"
                    else "movie"
                ),
                updated_at=float(blob.get("updated_at") or now),
            )
            if thread.alive(now=now):
                self._threads[chat_id] = thread

    def _persist_unlocked(self) -> None:
        self._prune_unlocked()
        payload = {
            "version": 1,
            "chats": {
                str(cid): {
                    "chat_id": cid,
                    "subject_title": th.subject_title,
                    "subject_media_kind": th.subject_media_kind,
                    "offered": th.offered,
                    "rejected_titles": th.rejected_titles,
                    "shown_tmdb_ids": th.shown_tmdb_ids[-MAX_SHOWN_IDS:],
                    "last_genre_ids": th.last_genre_ids,
                    "last_exclude_genre_ids": th.last_exclude_genre_ids,
                    "last_discover_page": th.last_discover_page,
                    "last_discover_media_type": th.last_discover_media_type,
                    "updated_at": th.updated_at,
                    "turns": [asdict(t) for t in th.turns[-MAX_TURNS:]],
                }
                for cid, th in self._threads.items()
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            log.info("telegram chat memory persist failed: %s", redact(str(exc)))


__all__ = [
    "DEFAULT_PATH",
    "IDLE_TTL_S",
    "MAX_SHOWN_IDS",
    "MAX_TURNS",
    "ChatMemory",
    "ChatThread",
    "ChatTurn",
]
