"""Typed media-ask intents for the Telegram path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hearth.jev.schema import JevVerdict

MediaAskKind = Literal[
    "exact_title",
    "known_franchise",
    "series_all",
    "edition",
    "describe",
    "chat_about",
    "other",
]


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

    @property
    def wants_download_path(self) -> bool:
        return self.kind in {
            "exact_title",
            "known_franchise",
            "describe",
            "series_all",
            "edition",
        }
