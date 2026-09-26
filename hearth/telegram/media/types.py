"""Typed media-ask intents for the Telegram path.

Jev routes; these dataclasses carry what the deterministic extractors found so a
lane can run without re-parsing the raw text. Nothing here performs I/O or
decides to queue — Get / yes confirm remains the only queue boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hearth.jev.schema import JevVerdict

MediaAskKind = Literal[
    "exact_title",
    "known_franchise",
    "series_all",
    "edition",
    "person",
    "mood",
    "similar",
    "batch",
    "follow_up",
    "house_pick",
    "describe",
    "chat_about",
    "other",
]

# What a short follow-up refers to in the recent Telegram media context.
FollowUpKind = Literal[
    "",
    "sequel",
    "prequel",
    "all_of_them",
    "more_like_that",
    "more",
    "other_one",
    "ordinal",
    "that_one",
    "continue_pack",
]


@dataclass(frozen=True, slots=True)
class MoodSpec:
    """A vibe request turned into real TMDB discover coordinates."""

    key: str
    label: str
    media_type: str = "movie"
    genre_ids: tuple[int, ...] = ()
    exclude_genre_ids: tuple[int, ...] = ()
    runtime_lte: int | None = None
    runtime_gte: int | None = None
    vote_average_gte: float | None = None
    vote_count_gte: int = 200
    release_date_gte: str = ""
    release_date_lte: str = ""
    sort_by: str = "popularity.desc"
    # Set when the same words also read as a catalog title ("Date Night"), so
    # the card can offer a one-tap correction instead of guessing silently.
    ambiguous_title: str = ""

    @property
    def present(self) -> bool:
        return bool(
            self.genre_ids
            or self.runtime_lte
            or self.release_date_gte
            or self.release_date_lte
        )


@dataclass(frozen=True, slots=True)
class AskPart:
    """One title inside a compound ask ("LOTR extended + Hobbit theatrical")."""

    title: str
    year: int | None = None
    media_type: str = ""
    edition_key: str = ""
    edition_label: str = ""
    series_all: bool = False
    drop_last: int = 0
    drop_first: int = 0
    raw: str = ""

    def label(self) -> str:
        base = f"{self.title} ({self.year})" if self.year else self.title
        if self.edition_label:
            return f"{base} · {self.edition_label}"
        if self.series_all:
            return f"{base} · whole series"
        return base


@dataclass(frozen=True, slots=True)
class MediaIntent:
    """One classified Telegram media ask (never a queue decision)."""

    kind: MediaAskKind
    search_title: str = ""
    year: int | None = None
    media_type: str = ""  # movie | tv | ""
    edition_label: str = ""
    edition_key: str = ""
    confidence: float = 1.0
    source: str = "local"  # jev | local | local_failopen
    needs_llm: bool = False
    raw_text: str = ""
    note: str = ""
    jev: JevVerdict | None = None
    # Lane payloads. Each is filled by a deterministic extractor, never by an LLM.
    person_name: str = ""
    person_role: str = ""  # cast | directing | ""
    mood: MoodSpec | None = None
    parts: tuple[AskPart, ...] = ()
    follow_up: FollowUpKind = ""
    ordinal: int | None = None
    drop_last: int = 0
    drop_first: int = 0
    # Set when the ask is "franchise + part/episode N" (Harry Potter part 6).
    # ``installment_kind`` is "episode" (saga order) or "index" (release order).
    installment: int | None = None
    installment_kind: str = ""

    @property
    def wants_download_path(self) -> bool:
        return self.kind in {
            "exact_title",
            "known_franchise",
            "describe",
            "series_all",
            "edition",
            "person",
            "mood",
            "similar",
            "batch",
            "follow_up",
            "house_pick",
        }

    @property
    def is_multi(self) -> bool:
        """True when the ask covers more than one title on purpose."""
        return self.kind in {"series_all", "batch", "mood", "person", "house_pick"} or (
            self.kind == "follow_up" and self.follow_up in {"all_of_them", "more_like_that", "more"}
        )


__all__ = [
    "AskPart",
    "FollowUpKind",
    "MediaAskKind",
    "MediaIntent",
    "MoodSpec",
]
