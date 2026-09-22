"""Whole-utterance house phrases that are not media searches.

Matched only when the message *is* the ask, so “Get Out” and “what’s playing”
stay on their existing paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

PhraseKind = Literal["shelf", "scene"]

_LEAD = r"(?:please\s+|can you\s+|could you\s+|hey\s+|hearth\s+)?"
_VERB = r"(?:turn on\s+|start\s+|set\s+|run\s+|activate\s+|do\s+)?"
_ARTICLE = r"(?:a\s+|the\s+|our\s+)?"
_TAIL = r"\s*[.!?]*$"

_SHELF = re.compile(
    rf"^{_LEAD}(?:"
    r"what(?:'s| is|s) on tonight"
    r"|whats on tonight"
    r"|what(?:'s| is) on the shelf"
    r"|what(?:'s| is) already on plex"
    r"|already on plex"
    r"|what can we watch(?: tonight)?"
    r"|what should we watch tonight"
    r"|continue watching"
    r"|on deck"
    r"|wat staat er vanavond"
    r"|wat kunnen we kijken"
    r"|al op plex"
    rf"){_TAIL}",
    re.IGNORECASE,
)

# (preset id, remainder pattern). Order matters: longer scene names first.
_SCENES: tuple[tuple[str, str], ...] = (
    (
        "movie_night",
        r"(?:movie|film|cinema) night|filmavond|lights down(?: for (?:a |the )?(?:movie|film))?",
    ),
    (
        "quiet_hours",
        r"quiet hours|hush the house|stille uren|stilteuur",
    ),
    (
        "good_night",
        r"good night|bedtime|slaap lekker|lights out",
    ),
)


@dataclass(frozen=True, slots=True)
class HousePhrase:
    kind: PhraseKind
    preset: str = ""


def classify_house_phrase(text: str) -> HousePhrase | None:
    """Return a shelf or scene ask, or None when this is some other sentence."""
    raw = " ".join((text or "").strip().split())
    if not raw or len(raw) > 80:
        return None
    if _SHELF.match(raw):
        return HousePhrase(kind="shelf")
    for preset, pattern in _SCENES:
        if re.match(rf"^{_LEAD}{_VERB}{_ARTICLE}(?:{pattern}){_TAIL}", raw, re.IGNORECASE):
            return HousePhrase(kind="scene", preset=preset)
    return None


def house_route(text: str) -> dict[str, object] | None:
    """Local agent route for the same phrases. None leaves the existing router alone."""
    phrase = classify_house_phrase(text)
    if phrase is None:
        return None
    if phrase.kind == "shelf":
        return {"tool": "house_shelf", "args": {}}
    return {"tool": "house_scene", "args": {"preset": phrase.preset}}


__all__ = ["HousePhrase", "classify_house_phrase", "house_route"]
