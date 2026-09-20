"""Short-lived per-chat media context so follow-ups resolve in-thread.

Stored in the existing bounded ``telegram_callback_media`` KV table, which
already expires and prunes itself. Only the coordinates needed to answer "the
sequel" / "all of them" / "more like that" are kept — never message text beyond
the last ask.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from hearth.config import settings
from hearth.telegram.models import MediaHit

_CONTEXT_PREFIX = "mediactx:"
_MAX_REMEMBERED_HITS = 8
_MAX_SHOWN_IDS = 40


class ContextStore(Protocol):
    """The slice of ``TelegramStore`` this module needs."""

    def put_callback_media(
        self,
        callback_key: str,
        metadata: Mapping[str, Any],
        *,
        ttl_s: float = ...,
    ) -> None: ...

    def get_callback_media(self, callback_key: str) -> dict[str, Any] | None: ...

    def clear_callback_media(self, callback_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RememberedHit:
    """One card that was on screen, in display order."""

    media_type: str
    tmdb_id: int
    title: str
    year: int | None = None
    media_status: int | None = None
    season: int | None = None

    @classmethod
    def from_hit(cls, hit: MediaHit, *, season: int | None = None) -> RememberedHit:
        return cls(
            media_type=hit.media_type,
            tmdb_id=hit.tmdb_id,
            title=hit.title,
            year=hit.year,
            media_status=hit.media_status,
            season=season,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "year": self.year,
            "media_status": self.media_status,
            "season": self.season,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RememberedHit | None:
        media_type = str(payload.get("media_type") or "").lower()
        if media_type not in {"movie", "tv"}:
            return None
        try:
            tmdb_id = int(payload.get("tmdb_id"))
        except (TypeError, ValueError):
            return None
        title = str(payload.get("title") or "").strip()
        if tmdb_id <= 0 or not title:
            return None

        def _maybe_int(value: Any) -> int | None:
            if value is None or isinstance(value, bool):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            year=_maybe_int(payload.get("year")),
            media_status=_maybe_int(payload.get("media_status")),
            season=_maybe_int(payload.get("season")),
        )

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass(frozen=True, slots=True)
class ChatContext:
    """What the bot last put in front of this chat."""

    hits: tuple[RememberedHit, ...] = ()
    ask_kind: str = ""
    ask_text: str = ""
    search_title: str = ""
    franchise_seed: str = ""
    person_name: str = ""
    person_role: str = ""
    media_type: str = ""
    # The title a "like X" / franchise lane was anchored on, so "more" can page.
    anchor_id: int | None = None
    anchor_type: str = ""
    shown_ids: tuple[int, ...] = ()
    page: int = 1
    # Next franchise entry after a successful queue (same TTL as the thread).
    watch_next_media_type: str = ""
    watch_next_tmdb_id: int | None = None
    watch_next_title: str = ""
    watch_next_year: int | None = None
    watch_next_from_title: str = ""
    watch_next_from_tmdb_id: int | None = None
    updated_at: float = field(default=0.0)

    @property
    def present(self) -> bool:
        return bool(
            self.hits or self.search_title or self.person_name or self.ask_text
            or self.watch_next_tmdb_id
        )

    @property
    def top(self) -> RememberedHit | None:
        return self.hits[0] if self.hits else None

    def nth(self, index: int) -> RememberedHit | None:
        """1-based pick; ``-1`` means the last card shown."""
        if not self.hits:
            return None
        if index == -1:
            return self.hits[-1]
        if 1 <= index <= len(self.hits):
            return self.hits[index - 1]
        return None

    def subject(self) -> str:
        if self.search_title:
            return self.search_title
        if self.franchise_seed:
            return self.franchise_seed
        top = self.top
        return top.title if top is not None else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "ask_kind": self.ask_kind,
            "ask_text": self.ask_text[:240],
            "search_title": self.search_title,
            "franchise_seed": self.franchise_seed,
            "person_name": self.person_name,
            "person_role": self.person_role,
            "media_type": self.media_type,
            "anchor_id": self.anchor_id,
            "anchor_type": self.anchor_type,
            "shown_ids": list(self.shown_ids),
            "page": self.page,
            "watch_next_media_type": self.watch_next_media_type,
            "watch_next_tmdb_id": self.watch_next_tmdb_id,
            "watch_next_title": self.watch_next_title,
            "watch_next_year": self.watch_next_year,
            "watch_next_from_title": self.watch_next_from_title,
            "watch_next_from_tmdb_id": self.watch_next_from_tmdb_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ChatContext:
        raw_hits = payload.get("hits")
        hits: list[RememberedHit] = []
        if isinstance(raw_hits, list):
            for item in raw_hits:
                if not isinstance(item, Mapping):
                    continue
                parsed = RememberedHit.from_dict(item)
                if parsed is not None:
                    hits.append(parsed)
        shown: list[int] = []
        raw_shown = payload.get("shown_ids")
        if isinstance(raw_shown, list):
            for value in raw_shown:
                try:
                    shown.append(int(value))
                except (TypeError, ValueError):
                    continue
        try:
            page = max(1, int(payload.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            updated_at = float(payload.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        anchor_id: int | None
        try:
            anchor_id = int(payload.get("anchor_id"))
        except (TypeError, ValueError):
            anchor_id = None
        if anchor_id is not None and anchor_id <= 0:
            anchor_id = None

        watch_next_tmdb_id: int | None
        try:
            watch_next_tmdb_id = int(payload.get("watch_next_tmdb_id"))
        except (TypeError, ValueError):
            watch_next_tmdb_id = None
        if watch_next_tmdb_id is not None and watch_next_tmdb_id <= 0:
            watch_next_tmdb_id = None
        watch_next_year: int | None
        try:
            watch_next_year = int(payload.get("watch_next_year")) if payload.get("watch_next_year") is not None else None
        except (TypeError, ValueError):
            watch_next_year = None
        watch_next_from_tmdb_id: int | None
        try:
            watch_next_from_tmdb_id = (
                int(payload.get("watch_next_from_tmdb_id"))
                if payload.get("watch_next_from_tmdb_id") is not None
                else None
            )
        except (TypeError, ValueError):
            watch_next_from_tmdb_id = None
        return cls(
            hits=tuple(hits[:_MAX_REMEMBERED_HITS]),
            ask_kind=str(payload.get("ask_kind") or ""),
            ask_text=str(payload.get("ask_text") or ""),
            search_title=str(payload.get("search_title") or ""),
            franchise_seed=str(payload.get("franchise_seed") or ""),
            person_name=str(payload.get("person_name") or ""),
            person_role=str(payload.get("person_role") or ""),
            media_type=str(payload.get("media_type") or ""),
            anchor_id=anchor_id,
            anchor_type=str(payload.get("anchor_type") or ""),
            shown_ids=tuple(dict.fromkeys(shown))[-_MAX_SHOWN_IDS:],
            page=page,
            watch_next_media_type=str(payload.get("watch_next_media_type") or ""),
            watch_next_tmdb_id=watch_next_tmdb_id,
            watch_next_title=str(payload.get("watch_next_title") or ""),
            watch_next_year=watch_next_year,
            watch_next_from_title=str(payload.get("watch_next_from_title") or ""),
            watch_next_from_tmdb_id=watch_next_from_tmdb_id,
            updated_at=updated_at,
        )


    def watch_next(self) -> dict[str, Any] | None:
        """Return the stored watch-next payload, or None."""
        if self.watch_next_tmdb_id is None or not self.watch_next_title:
            return None
        if self.watch_next_media_type not in {"movie", "tv"}:
            return None
        return {
            "media_type": self.watch_next_media_type,
            "tmdb_id": self.watch_next_tmdb_id,
            "title": self.watch_next_title,
            "year": self.watch_next_year,
            "from_title": self.watch_next_from_title,
            "from_tmdb_id": self.watch_next_from_tmdb_id,
        }


class MediaMemory:
    """Per-chat conversation context with a short, configurable TTL."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    @staticmethod
    def _key(chat_id: int) -> str:
        return f"{_CONTEXT_PREFIX}{int(chat_id)}"

    @property
    def ttl_seconds(self) -> int:
        return max(60, int(getattr(settings, "telegram_context_ttl_seconds", 1800)))

    def load(self, chat_id: int) -> ChatContext | None:
        payload = self.store.get_callback_media(self._key(chat_id))
        if not payload:
            return None
        context = ChatContext.from_dict(payload)
        return context if context.present else None

    def forget(self, chat_id: int) -> None:
        self.store.clear_callback_media(self._key(chat_id))

    def remember(
        self,
        chat_id: int,
        *,
        hits: list[MediaHit] | tuple[MediaHit, ...] = (),
        ask_kind: str = "",
        ask_text: str = "",
        search_title: str = "",
        franchise_seed: str = "",
        person_name: str = "",
        person_role: str = "",
        media_type: str = "",
        anchor_id: int | None = None,
        anchor_type: str = "",
        season: int | None = None,
        page: int = 1,
        accumulate_shown: bool = True,
    ) -> ChatContext:
        """Record what was just shown, keeping earlier "already seen" ids."""
        previous = self.load(chat_id)
        remembered = tuple(
            RememberedHit.from_hit(hit, season=season if hit.media_type == "tv" else None)
            for hit in list(hits)[:_MAX_REMEMBERED_HITS]
        )
        shown: list[int] = []
        if accumulate_shown and previous is not None:
            shown.extend(previous.shown_ids)
        shown.extend(hit.tmdb_id for hit in remembered)
        context = ChatContext(
            hits=remembered,
            ask_kind=ask_kind,
            ask_text=ask_text,
            search_title=search_title,
            franchise_seed=franchise_seed,
            person_name=person_name,
            person_role=person_role,
            media_type=media_type,
            anchor_id=anchor_id,
            anchor_type=anchor_type,
            shown_ids=tuple(dict.fromkeys(shown))[-_MAX_SHOWN_IDS:],
            page=max(1, int(page)),
            updated_at=time.time(),
        )
        self.store.put_callback_media(
            self._key(chat_id),
            context.to_dict(),
            ttl_s=self.ttl_seconds,
        )
        return context

    def carry_forward(self, chat_id: int, previous: ChatContext, *, page: int) -> ChatContext:
        """Keep a lane's context alive while advancing its page counter."""
        return self.remember(
            chat_id,
            hits=(),
            ask_kind=previous.ask_kind,
            ask_text=previous.ask_text,
            search_title=previous.search_title,
            franchise_seed=previous.franchise_seed,
            person_name=previous.person_name,
            person_role=previous.person_role,
            media_type=previous.media_type,
            anchor_id=previous.anchor_id,
            anchor_type=previous.anchor_type,
            page=page,
        )



    def remember_watch_next(
        self,
        chat_id: int,
        *,
        media_type: str,
        tmdb_id: int,
        title: str,
        year: int | None = None,
        from_title: str = "",
        from_tmdb_id: int | None = None,
    ) -> ChatContext:
        """Attach a watch-next entry to the existing chat context (or create one)."""
        import time as _time
        previous = self.load(chat_id)
        base = previous or ChatContext()
        context = ChatContext(
            hits=base.hits,
            ask_kind=base.ask_kind,
            ask_text=base.ask_text,
            search_title=base.search_title,
            franchise_seed=base.franchise_seed,
            person_name=base.person_name,
            person_role=base.person_role,
            media_type=base.media_type or media_type,
            anchor_id=base.anchor_id,
            anchor_type=base.anchor_type,
            shown_ids=base.shown_ids,
            page=base.page,
            watch_next_media_type=media_type,
            watch_next_tmdb_id=int(tmdb_id),
            watch_next_title=title.strip(),
            watch_next_year=year,
            watch_next_from_title=(from_title or "").strip(),
            watch_next_from_tmdb_id=from_tmdb_id,
            updated_at=_time.time(),
        )
        self.store.put_callback_media(
            self._key(chat_id),
            context.to_dict(),
            ttl_s=self.ttl_seconds,
        )
        return context

    def clear_watch_next(self, chat_id: int) -> ChatContext | None:
        """Drop only the watch-next fields, keeping the rest of the thread."""
        import time as _time
        previous = self.load(chat_id)
        if previous is None:
            return None
        context = ChatContext(
            hits=previous.hits,
            ask_kind=previous.ask_kind,
            ask_text=previous.ask_text,
            search_title=previous.search_title,
            franchise_seed=previous.franchise_seed,
            person_name=previous.person_name,
            person_role=previous.person_role,
            media_type=previous.media_type,
            anchor_id=previous.anchor_id,
            anchor_type=previous.anchor_type,
            shown_ids=previous.shown_ids,
            page=previous.page,
            updated_at=_time.time(),
        )
        if not context.present:
            self.forget(chat_id)
            return None
        self.store.put_callback_media(
            self._key(chat_id),
            context.to_dict(),
            ttl_s=self.ttl_seconds,
        )
        return context

__all__ = ["ChatContext", "ContextStore", "MediaMemory", "RememberedHit"]
