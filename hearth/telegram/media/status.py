"""Plex / Overseerr availability truth for Telegram cards.

Overseerr already embeds library state on search hits (``mediaInfo.status`` /
``mediaStatus``). This module turns that into honest labels and button copy so
we never offer Get for something that is already on Plex or mid-download.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hearth.telegram.models import MediaHit

# Overseerr / Seerr MediaStatus.
STATUS_UNKNOWN = 1
STATUS_PENDING = 2
STATUS_PROCESSING = 3
STATUS_PARTIAL = 4
STATUS_AVAILABLE = 5
STATUS_BLOCKED = 6
STATUS_REMOVED = 7

AvailabilityKind = Literal[
    "missing",
    "pending",
    "downloading",
    "partial",
    "available",
    "blocked",
]


@dataclass(frozen=True, slots=True)
class Availability:
    """Honest availability derived from Overseerr media status."""

    kind: AvailabilityKind
    status: int | None
    mark: str
    button: str
    requestable: bool
    playable: bool

    @property
    def on_plex(self) -> bool:
        return self.kind == "available"

    @property
    def in_flight(self) -> bool:
        return self.kind in {"pending", "downloading"}


_MARKS: dict[int, str] = {
    STATUS_UNKNOWN: "○ Not requested",
    STATUS_PENDING: "◷ Pending approval",
    STATUS_PROCESSING: "◷ Downloading…",
    STATUS_PARTIAL: "◐ Partly on Plex",
    STATUS_AVAILABLE: "✓ On Plex",
    STATUS_BLOCKED: "◇ Blocklisted or deleted",
    STATUS_REMOVED: "○ Removed",
}

_BUTTON: dict[AvailabilityKind, str] = {
    "missing": "Get",
    "pending": "Pending…",
    "downloading": "Downloading…",
    "partial": "Get rest",
    "available": "On Plex",
    "blocked": "Blocked",
}


def availability_of(hit: MediaHit, *, season: int | None = None) -> Availability:
    """Map one hit's Overseerr status into display + button truth."""
    status = hit.media_status
    explicitly_requesting_tv_season = hit.media_type == "tv" and season is not None

    if status == STATUS_AVAILABLE:
        return Availability(
            kind="available",
            status=status,
            mark=_MARKS[STATUS_AVAILABLE],
            button=_BUTTON["available"],
            requestable=False,
            playable=True,
        )
    if status == STATUS_PENDING and not explicitly_requesting_tv_season:
        return Availability(
            kind="pending",
            status=status,
            mark=_MARKS[STATUS_PENDING],
            button=_BUTTON["pending"],
            requestable=False,
            playable=False,
        )
    if status == STATUS_PROCESSING and not explicitly_requesting_tv_season:
        return Availability(
            kind="downloading",
            status=status,
            mark=_MARKS[STATUS_PROCESSING],
            button=_BUTTON["downloading"],
            requestable=False,
            playable=False,
        )
    if status == STATUS_PARTIAL:
        return Availability(
            kind="partial",
            status=status,
            mark=_MARKS[STATUS_PARTIAL],
            button=_BUTTON["partial"],
            requestable=True,
            playable=False,
        )
    if status in {STATUS_BLOCKED, STATUS_REMOVED}:
        # Status 6 is ambiguous across Overseerr (deleted) and Seerr (blocklisted).
        # Keep the label honest but still offer Get so the backend can decide.
        return Availability(
            kind="blocked",
            status=status,
            mark=_MARKS.get(status or 0, "◇ Unavailable"),
            button=_BUTTON["missing"],
            requestable=True,
            playable=False,
        )
    return Availability(
        kind="missing",
        status=status,
        mark=_MARKS.get(status or STATUS_UNKNOWN, "○ Not requested"),
        button=_BUTTON["missing"],
        requestable=True,
        playable=False,
    )


def status_mark(hit: MediaHit) -> str:
    return availability_of(hit).mark


def request_button_label(
    index: int,
    hit: MediaHit,
    *,
    season: int | None = None,
) -> str:
    """Get / Get rest button copy — never used for On Plex or in-flight."""
    avail = availability_of(hit, season=season)
    year_bit = f" ({hit.year})" if hit.year is not None else ""
    season_bit = f" S{season:02d}" if season is not None else ""
    kind = "movie" if hit.media_type == "movie" else "TV"
    verb = "Get rest" if avail.kind == "partial" else "Get"
    return f"{verb} {index} · {hit.title}{year_bit} {kind}{season_bit}"


def play_button_label(hit: MediaHit) -> str:
    year_bit = f" ({hit.year})" if hit.year is not None else ""
    return f"▶ Play · {hit.title}{year_bit}"


def status_button_label(hit: MediaHit, *, season: int | None = None) -> str:
    """Non-queue button reflecting pending / downloading state."""
    return availability_of(hit, season=season).button


__all__ = [
    "STATUS_AVAILABLE",
    "STATUS_BLOCKED",
    "STATUS_PARTIAL",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "STATUS_REMOVED",
    "STATUS_UNKNOWN",
    "Availability",
    "AvailabilityKind",
    "availability_of",
    "play_button_label",
    "request_button_label",
    "status_button_label",
    "status_mark",
]
