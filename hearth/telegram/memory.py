"""Per-chat Telegram conversation window for plot/follow-up context.

Rolling ~8 turns, 30-minute idle TTL. Persists under ``data/`` so a container
recreate does not wipe a mid-chat. No new env keys — fixed path next to the
other house data files.
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

MAX_TURNS = 8
IDLE_TTL_S = 30 * 60
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
    updated_at: float = field(default_factory=time.time)

    def alive(self, *, now: float | None = None, ttl_s: float = IDLE_TTL_S) -> bool:
        stamp = now if now is not None else time.time()
        return bool(self.turns) and (stamp - self.updated_at) <= ttl_s


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
            thread.rejected_titles = list(existing.values())[-12:]
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
        if not raw and not search_title and not offered:
            return
        compact = _compact_offered(offered)
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            thread.turns.append(
                ChatTurn(
                    role="bot",
                    text=(raw or search_title)[:400],
                    search_title=(search_title or "")[:200],
                    media_kind=media_kind if media_kind in {"movie", "tv"} else "",
                    offered=compact,
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
            if compact:
                thread.offered = compact
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
    ) -> None:
        title = (title or "").strip()
        if not title:
            return
        compact = _compact_offered(offered)
        with self._lock:
            thread = self._ensure_unlocked(int(chat_id))
            thread.subject_title = title[:200]
            if media_kind in {"movie", "tv"}:
                thread.subject_media_kind = media_kind
            if compact:
                thread.offered = compact
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
        if thread is None or not thread.alive():
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
                ][:12],
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
    "MAX_TURNS",
    "ChatMemory",
    "ChatThread",
    "ChatTurn",
]
