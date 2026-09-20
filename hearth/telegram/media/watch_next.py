"""Watch-next memory: after Part One, offer Part Two / the rest of the pack.

Persisted on the existing per-chat SQLite media context (same TTL). The bot
records the next franchise entry when a title is queued; the next Telegram turn
or a soft “what’s next” / “the sequel” follow-up can pick it up without a fresh
title search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from hearth.telegram.models import MediaHit

# Soft prompts that should surface the stored watch-next card.
_CONTINUE_PACK = re.compile(
    r"^\s*(?:"
    r"(?:what(?:'s|\s+is)\s+next|whats\s+next|what\s+next)|"
    r"(?:continue(?:\s+the)?\s+pack|(?:the\s+)?rest\s+of\s+(?:the\s+)?(?:pack|collection|series))|"
    r"(?:next\s+(?:in\s+(?:the\s+)?(?:series|pack|collection)|up))|"
    r"(?:keep\s+going|carry\s+on)|"
    r"(?:volgende|wat\s+nu|ga\s+verder)"
    r")\s*[.!?]*\s*$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class WatchNext:
    """The next franchise entry to offer after a successful queue."""

    media_type: str
    tmdb_id: int
    title: str
    year: int | None = None
    from_title: str = ""
    from_tmdb_id: int | None = None

    @property
    def present(self) -> bool:
        return self.tmdb_id > 0 and bool(self.title) and self.media_type in {"movie", "tv"}

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "year": self.year,
            "from_title": self.from_title,
            "from_tmdb_id": self.from_tmdb_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> WatchNext | None:
        if not isinstance(payload, Mapping):
            return None
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
        year: int | None
        try:
            year = int(payload["year"]) if payload.get("year") is not None else None
        except (TypeError, ValueError):
            year = None
        from_tmdb_id: int | None
        try:
            from_tmdb_id = (
                int(payload["from_tmdb_id"])
                if payload.get("from_tmdb_id") is not None
                else None
            )
        except (TypeError, ValueError):
            from_tmdb_id = None
        return cls(
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            from_title=str(payload.get("from_title") or "").strip(),
            from_tmdb_id=from_tmdb_id,
        )

    def as_hit(self, *, media_status: int | None = None) -> MediaHit:
        return MediaHit(
            media_type=self.media_type,  # type: ignore[arg-type]
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            media_status=media_status,
        )


def pick_next_in_order(
    entries: list[MediaHit] | tuple[MediaHit, ...],
    *,
    after_tmdb_id: int,
    after_year: int | None = None,
) -> MediaHit | None:
    """Return the next release-order entry after the queued title."""
    ordered = [hit for hit in entries if hit.tmdb_id > 0]
    if not ordered:
        return None
    # Prefer year-ordered when years are present; otherwise keep given order.
    if all(hit.year is not None for hit in ordered):
        ordered = sorted(ordered, key=lambda hit: (hit.year or 0, hit.tmdb_id))
    index = next(
        (i for i, hit in enumerate(ordered) if hit.tmdb_id == after_tmdb_id),
        None,
    )
    if index is not None and index + 1 < len(ordered):
        return ordered[index + 1]
    if after_year is not None:
        later = [
            hit
            for hit in ordered
            if hit.tmdb_id != after_tmdb_id
            and hit.year is not None
            and hit.year > after_year
        ]
        return later[0] if later else None
    return None


def looks_like_continue_pack(text: str) -> bool:
    """True for bare “what’s next” / “continue the pack” style asks."""
    raw = (text or "").strip()
    if not raw or len(raw) > 60:
        return False
    return bool(_CONTINUE_PACK.match(raw))


def soft_prompt(watch: WatchNext) -> str:
    """One-line nudge after queuing Part One."""
    prior = watch.from_title or "that one"
    return (
        f"Queued. Next in the pack after {prior}: {watch.label} — "
        "say “what’s next” or “the sequel” when you want it."
    )


def offer_header(watch: WatchNext) -> str:
    prior = watch.from_title or "the last one"
    return f"Continuing the pack after {prior} — {watch.label}:"


__all__ = [
    "WatchNext",
    "looks_like_continue_pack",
    "offer_header",
    "pick_next_in_order",
    "soft_prompt",
]
