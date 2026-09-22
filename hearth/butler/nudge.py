"""After a successful queue, mention it when Plex already has the title in progress."""

from __future__ import annotations

import re
from typing import Any

from hearth.tools.plex import plex


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def matching_item(title: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Exact title match only — “Dune” must not grab “Dune: Part Two”."""
    needle = _norm(title)
    if len(needle) < 2:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("title", "show", "grandparentTitle"):
            candidate = item.get(key)
            if candidate and _norm(str(candidate)) == needle:
                return item
    return None


def aside_for(
    title: str,
    *,
    on_deck: list[dict[str, Any]],
    now: list[dict[str, Any]],
) -> str | None:
    live = matching_item(title, now)
    if live is not None:
        shown = str(live.get("title") or title)
        state = str(live.get("state") or "playing")
        player = str(live.get("player") or "the TV")
        return f"{shown} is {state} on {player} right now."
    deck = matching_item(title, on_deck)
    if deck is None:
        return None
    percent = deck.get("progress_pct")
    if not isinstance(percent, int) or percent <= 0:
        return None
    label = str(deck.get("label") or deck.get("title") or title)
    play_as = str(deck.get("title") or title)
    return (
        f"{label} is already on the shelf, about {percent}% in. "
        f"Say “play {play_as}” when you want the rest."
    )


async def queue_shelf_aside(title: str) -> str | None:
    """One extra line after Get. Silent when the title isn’t mid-watch."""
    try:
        deck = await plex.on_deck(limit=12)
        playing = await plex.now_playing()
    except Exception:  # noqa: BLE001
        return (
            "I couldn’t check whether that’s already on Plex. "
            "Say “what’s on tonight” once the server answers."
        )
    if deck.get("error") or playing.get("error"):
        return (
            "Plex didn’t answer, so I can’t tell if that’s already on the shelf. "
            "Say “what’s on tonight” to try again."
        )
    items = deck.get("items") if isinstance(deck.get("items"), list) else []
    sessions = playing.get("sessions") if isinstance(playing.get("sessions"), list) else []
    return aside_for(title, on_deck=items, now=sessions)


__all__ = ["aside_for", "matching_item", "queue_shelf_aside"]
