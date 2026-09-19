"""In-thread follow-ups: "the sequel", "all of them", "nah the other one".

Detection only — resolving a follow-up needs the recent chat context, which the
router supplies from :mod:`hearth.telegram.media.memory`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hearth.telegram.media.types import FollowUpKind

# "the second one" / "the first one" are ordinals against the cards on screen,
# so they are deliberately absent here and handled by ``_ORDINAL`` below.
_SEQUEL = re.compile(
    r"\b(?:the\s+)?(?:sequel|next\s+one|next\s+part|part\s+(?:two|2)|"
    r"follow[-\s]?up|het\s+vervolg|vervolg|deel\s+(?:twee|2))\b",
    re.I,
)
_PREQUEL = re.compile(
    r"\b(?:the\s+)?(?:prequel|the\s+original|earlier\s+one|"
    r"het\s+origineel|eerste\s+deel)\b",
    re.I,
)
_ALL_OF_THEM = re.compile(
    r"\b(?:all\s+of\s+(?:them|those|these|it)|all\s+(?:three|four|five|of\s+em)|"
    r"the\s+whole\s+(?:lot|set|thing)|give\s+me\s+(?:them\s+)?all|"
    r"alle(?:maal)?|allemaal|de\s+hele\s+(?:set|reeks))\b",
    re.I,
)
_MORE_LIKE_THAT = re.compile(
    r"\b(?:more\s+like\s+(?:that|this|those|it)|more\s+in\s+that\s+vein|"
    r"same\s+(?:vibe|energy|kind)|something\s+similar|"
    r"meer\s+(?:zoals\s+)?(?:dat|die|zoiets))\b",
    re.I,
)
_MORE = re.compile(
    r"^\s*(?:"
    r"more|more\s+(?:please|options|choices)|others?|any\s+others?|what\s+else|"
    r"something\s+else|anything\s+else|next|keep\s+going|show\s+more|"
    r"meer|anders|nog\s+(?:meer|wat|iets)|volgende"
    r")\s*[.!?]*\s*$",
    re.I,
)
_OTHER_ONE = re.compile(
    r"\b(?:(?:nah|no|nope|nee),?\s*)?the\s+other\s+(?:one|movie|film|version)|"
    r"\b(?:nah|nee),?\s*(?:the\s+)?other\b|"
    r"\bnot\s+that\s+one\b|"
    r"\bde\s+andere\b",
    re.I,
)
_THAT_ONE = re.compile(
    r"^\s*(?:"
    r"(?:yeah|yea|yes|yep|yup|ja|jup|sure|ok|okay)[,!]?\s*"
    r"(?:that\s+(?:one|is\s+the\s+one|one\s+please)|die|dat\s+is\s+(?:hem|die)|deze)"
    r"|that\s+one|that\s+one\s+please|die\s+bedoel\s+ik"
    r")\s*[.!?]*\s*$",
    re.I,
)
_ORDINAL_WORDS = {
    "first": 1,
    "1st": 1,
    "one": 1,
    "eerste": 1,
    "second": 2,
    "2nd": 2,
    "two": 2,
    "tweede": 2,
    "third": 3,
    "3rd": 3,
    "three": 3,
    "derde": 3,
    "fourth": 4,
    "4th": 4,
    "four": 4,
    "vierde": 4,
    "fifth": 5,
    "5th": 5,
    "five": 5,
    "vijfde": 5,
    "last": -1,
    "laatste": -1,
}
_ORDINAL = re.compile(
    r"^\s*(?:(?:yeah|yes|yep|ja|ok|okay|nah|no|nee)[,!]?\s*)?"
    r"(?:the\s+|de\s+|nummer\s+|number\s+|#)?"
    r"(?P<word>first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last|"
    r"eerste|tweede|derde|vierde|vijfde|laatste|[1-5])"
    r"(?:\s+(?:one|ones|movie|film|show|please|graag))?\s*[.!?]*\s*$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class FollowUpAsk:
    """A short message that only makes sense against the recent context."""

    kind: FollowUpKind
    ordinal: int | None = None

    @property
    def present(self) -> bool:
        return bool(self.kind)


def detect_follow_up(text: str) -> FollowUpAsk | None:
    """Classify a context-relative follow-up, or return None."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw or len(raw) > 60:
        return None

    if _MORE_LIKE_THAT.search(raw):
        return FollowUpAsk(kind="more_like_that")
    if _ALL_OF_THEM.search(raw):
        return FollowUpAsk(kind="all_of_them")
    if _SEQUEL.search(raw):
        return FollowUpAsk(kind="sequel")
    if _PREQUEL.search(raw):
        return FollowUpAsk(kind="prequel")
    if _OTHER_ONE.search(raw):
        return FollowUpAsk(kind="other_one")
    if _THAT_ONE.match(raw):
        return FollowUpAsk(kind="that_one")
    if _MORE.match(raw):
        return FollowUpAsk(kind="more")

    ordinal = _ORDINAL.match(raw)
    if ordinal:
        word = ordinal.group("word").casefold()
        value = _ORDINAL_WORDS.get(word)
        if value is None and word.isdigit():
            value = int(word)
        if value is not None:
            return FollowUpAsk(kind="ordinal", ordinal=value)
    return None


__all__ = ["FollowUpAsk", "detect_follow_up"]
