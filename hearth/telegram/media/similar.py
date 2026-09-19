"""“Something like X” detection → TMDB similar/recommendations coordinates.

The anchor title is stripped out deterministically so the lane can resolve it
once and then ask Overseerr for real neighbours instead of searching the whole
sentence as if it were a catalog name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LIKE_TITLE = re.compile(
    r"\b(?:something|anything|movies?|films?|shows?|series|more|stuff|iets|meer)\b"
    r"[^,.!?]{0,20}?\b(?:like|similar\s+to|in\s+the\s+vein\s+of|in\s+the\s+style\s+of|"
    r"reminds?\s+me\s+of|zoals|net\s+als|vergelijkbaar\s+met)\s+(?P<anchor>.+)$",
    re.I,
)
_BARE_LIKE = re.compile(
    r"^(?:\s*(?:got|have|any)\s+)?(?:something|anything|iets)?\s*"
    r"(?:like|similar\s+to|zoals)\s+(?P<anchor>.+)$",
    re.I,
)
_VEIN_ONLY = re.compile(
    r"\b(?:more\s+(?:like\s+)?(?:that|this|those|it)|"
    r"more\s+in\s+that\s+vein|same\s+vibe|same\s+energy|"
    r"in\s+that\s+vein|meer\s+(?:van\s+)?(?:dat|die|zoiets)|"
    r"iets\s+in\s+die\s+(?:stijl|richting))\b",
    re.I,
)
_ANCHOR_TAIL = re.compile(
    r"\s+(?:but|maar|only|alleen|except|behalve|though|however)\b.*$",
    re.I,
)
_TRAILING_NOUN = re.compile(
    r"\s+(?:please|aub|thanks|thx|dan|graag)\s*$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class SimilarAsk:
    """A "more like X" ask. An empty anchor means "use the recent context"."""

    anchor: str = ""
    uses_context: bool = False

    @property
    def present(self) -> bool:
        return bool(self.anchor) or self.uses_context


def _clean_anchor(raw: str) -> str:
    anchor = _ANCHOR_TAIL.sub("", (raw or "").strip())
    anchor = _TRAILING_NOUN.sub("", anchor)
    anchor = anchor.strip(" -–—|,.?!\"'“”")
    anchor = re.sub(r"\s+", " ", anchor)
    if anchor.casefold() in {
        "that",
        "this",
        "it",
        "those",
        "these",
        "them",
        "dat",
        "die",
        "dit",
    }:
        return ""
    return anchor


def detect_similar_ask(text: str) -> SimilarAsk | None:
    """Return the "like X" anchor, a context-relative ask, or None."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return None

    if _VEIN_ONLY.search(raw):
        return SimilarAsk(anchor="", uses_context=True)

    for pattern in (_LIKE_TITLE, _BARE_LIKE):
        match = pattern.search(raw)
        if match:
            anchor = _clean_anchor(match.group("anchor"))
            if anchor and len(anchor) >= 2:
                return SimilarAsk(anchor=anchor)
            return SimilarAsk(anchor="", uses_context=True)
    return None


__all__ = ["SimilarAsk", "detect_similar_ask"]
