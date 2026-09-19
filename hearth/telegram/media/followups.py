"""In-thread follow-ups: "the sequel", "all of them", "nah the other one".

Detection only — resolving a follow-up needs the recent chat context, which the
router supplies from :mod:`hearth.telegram.media.memory`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hearth.telegram.media.types import FollowUpKind

# A follow-up is a *bare* fragment. Anchoring every pattern is what keeps real
# titles safe: "Dune: Part Two" contains "part two" but is not a follow-up, and
# "alle Harry Potter films" contains "alle" but names its own franchise.
_PREFIX = (
    r"^\s*(?:(?:and|ok|okay|alright|right|hmm+|well|so|en|nou|ja|yeah)[,!]?\s+)?"
    r"(?:(?:can|could)\s+(?:you|i|we)\s+|please\s+|pls\s+|i'?d\s+like\s+|"
    r"give\s+me\s+|get\s+me\s+|grab\s+|doe\s+)?"
)
_SUFFIX = r"(?:\s+(?:please|pls|graag|aub|then|dan|instead|too|also|ook))?\s*[.!?]*\s*$"


def _bare(core: str) -> re.Pattern[str]:
    return re.compile(_PREFIX + r"(?:" + core + r")" + _SUFFIX, re.I)


# "the second one" / "the first one" are ordinals against the cards on screen,
# so they are deliberately absent here and handled by ``_ORDINAL`` below.
_SEQUEL = _bare(
    r"(?:the\s+)?(?:sequel|next\s+one|next\s+part|part\s+(?:two|2)|"
    r"follow[-\s]?up|het\s+vervolg|vervolg|deel\s+(?:twee|2))"
)
_PREQUEL = _bare(
    r"(?:the\s+)?(?:prequel|original|earlier\s+one|het\s+origineel|eerste\s+deel)"
)
_ALL_OF_THEM = _bare(
    r"all\s+of\s+(?:them|those|these|it|em)|all\s+(?:three|four|five)|"
    r"all\s+of\s+the(?:m|se)?|them\s+all|the\s+whole\s+(?:lot|set|thing)|"
    r"all\s+(?:the\s+)?(?:movies|films|parts|ones)|"
    r"alle(?:maal)?|allemaal|de\s+hele\s+(?:set|reeks)"
)
_MORE_LIKE_THAT = _bare(
    r"more\s+like\s+(?:that|this|those|it)|more\s+in\s+that\s+vein|"
    r"(?:the\s+)?same\s+(?:vibe|energy|kind)|something\s+similar|"
    r"meer\s+(?:zoals\s+)?(?:dat|die|zoiets)"
)
_MORE = _bare(
    r"more|more\s+(?:options|choices)|others?|any\s+others?|what\s+else|"
    r"something\s+else|anything\s+else|next|keep\s+going|show\s+more|"
    r"meer|anders|nog\s+(?:meer|wat|iets)|volgende"
)
_OTHER_ONE = _bare(
    r"(?:(?:nah|no|nope|nee)[,!]?\s*)?"
    r"(?:the\s+other(?:\s+(?:one|movie|film|version))?|not\s+that\s+one|"
    r"de\s+andere(?:\s+(?:film|serie))?)"
)
_THAT_ONE = _bare(
    r"(?:(?:yeah|yea|yes|yep|yup|ja|jup|sure|ok|okay)[,!]?\s*)?"
    r"(?:that\s+(?:one|is\s+the\s+one)|that\s+one\s+yes|die|dat\s+is\s+(?:hem|die)|"
    r"deze|die\s+bedoel\s+ik)"
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
# Numerals and "last" alone are too title-like to claim ("1917", "The Last of
# Us"); an ordinal follow-up needs the article or the counting noun.
_ORDINAL_NEEDS_FRAME = re.compile(r"^\s*(?:1|2|3|4|5|last|laatste)\s*$", re.I)


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

    for pattern, kind in (
        (_MORE_LIKE_THAT, "more_like_that"),
        (_ALL_OF_THEM, "all_of_them"),
        (_SEQUEL, "sequel"),
        (_PREQUEL, "prequel"),
        (_OTHER_ONE, "other_one"),
        (_THAT_ONE, "that_one"),
        (_MORE, "more"),
    ):
        if pattern.match(raw):
            return FollowUpAsk(kind=kind)  # type: ignore[arg-type]

    ordinal = _ORDINAL.match(raw)
    if ordinal and not _ORDINAL_NEEDS_FRAME.match(raw):
        word = ordinal.group("word").casefold()
        value = _ORDINAL_WORDS.get(word)
        if value is None and word.isdigit():
            value = int(word)
        if value is not None:
            return FollowUpAsk(kind="ordinal", ordinal=value)
    return None


__all__ = ["FollowUpAsk", "detect_follow_up"]
