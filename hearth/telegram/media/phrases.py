"""Shared title / series / exclusion phrases for Telegram media asks.

Kept in one module so the classifier, the compound splitter and the franchise
lane agree on what "all of them except the last" means.
"""

from __future__ import annotations

import re

YEAR_PAREN = re.compile(r"\(\s*((?:19|20)\d{2})\s*\)")

SERIES_ALL = re.compile(
    r"\b("
    r"all\s+(?:the\s+)?(?:movies|films|parts|ones|of\s+them)|"
    r"all\s+(?:the\s+)?(?:harry\s+potters?|lotr|lord\s+of\s+the\s+rings)|"
    r"(?:the\s+)?whole\s+(?:series|franchise|saga|trilogy|collection)|"
    r"(?:every|alle)\s+(?:movie|film|part|one)|"
    r"complete\s+(?:series|collection|saga|trilogy)|"
    r"alle\s+(?:films|delen|movies)|"
    r"hele\s+(?:reeks|serie|franchise|trilogie)|"
    r"full\s+(?:series|franchise|saga|trilogy)"
    r")\b",
    re.I,
)
ALL_PREFIX = re.compile(
    r"^\s*all\s+(?:of\s+|the\s+)?(?P<title>.+?)(?:\s+movies|\s+films)?\s*$",
    re.I,
)
TITLE_ALL_SUFFIX = re.compile(
    r"^(?P<title>.+?)(?:,\s*|\s+)"
    r"(?:all(?:\s+(?:the\s+)?(?:movies|films|parts|ones))?|"
    r"the\s+whole\s+(?:series|franchise|saga|trilogy)|"
    r"complete\s+(?:series|collection))\s*$",
    re.I,
)
# A fragment that only asks for "every entry" without naming anything.
BARE_SERIES_ALL = re.compile(
    r"^\s*(?:"
    r"all(?:\s+(?:the\s+)?(?:movies|films|parts|ones|of\s+them|of\s+it))?|"
    r"(?:the\s+)?whole\s+(?:series|franchise|saga|trilogy|collection|thing)|"
    r"(?:the\s+)?(?:complete|full)\s+(?:series|collection|saga|trilogy|set)|"
    r"every(?:\s+(?:movie|film|part|one))?|"
    r"alle(?:\s+(?:films|delen|movies))?|"
    r"(?:de\s+)?hele\s+(?:reeks|serie|franchise|trilogie)"
    r")\s*$",
    re.I,
)

# "the whole LOTR trilogy", "alle Harry Potter films", "complete Alien saga".
WHOLE_OF = re.compile(
    r"^\s*(?:the\s+|de\s+|het\s+)?"
    r"(?:whole|complete|full|entire|every|all(?:\s+the)?|alle|hele|gehele)\s+"
    r"(?P<title>.+?)\s+"
    r"(?:series|franchise|saga|trilogy|collection|movies|films|parts|ones|"
    r"reeks|serie|trilogie|delen)\s*$",
    re.I,
)

# "except the last", "minus the last two", "skip the first one", "all but the last"
EXCLUDE_TAIL = re.compile(
    r"[,\s]*\b(?:except|excluding|but\s+not|minus|without|skip(?:ping)?|"
    r"behalve|zonder|niet)\b\s+"
    r"(?:the\s+|de\s+|het\s+)?"
    r"(?P<count>last|first|final|latest|newest|oldest|laatste|eerste)"
    r"(?:\s+(?P<amount>one|two|three|1|2|3|twee|drie))?"
    r"(?:\s+(?:one|ones|movie|movies|film|films|part|parts|deel|delen))?\s*$",
    re.I,
)
_AMOUNTS = {"one": 1, "1": 1, "two": 2, "2": 2, "twee": 2, "three": 3, "3": 3, "drie": 3}
_FROM_END = {"last", "final", "latest", "newest", "laatste"}

KNOWN_FRANCHISE_SEEDS = frozenset(
    {
        "harry potter",
        "lord of the rings",
        "lotr",
        "hobbit",
        "the hobbit",
        "star wars",
        "marvel",
        "avengers",
        "fast and furious",
        "the fast and the furious",
        "mission impossible",
        "john wick",
        "matrix",
        "the matrix",
        "jurassic park",
        "jurassic world",
        "pirates of the caribbean",
        "indiana jones",
        "spider-man",
        "spiderman",
        "batman",
        "transformers",
        "beauty and the beast",
        "the good the bad and the ugly",
        "dungeons and dragons",
        "crouching tiger hidden dragon",
        "how to train your dragon",
        "the lion the witch and the wardrobe",
        "bill and ted",
        "dumb and dumber",
        "romeo and juliet",
        "sense and sensibility",
        "tom and jerry",
    }
)

_FRANCHISE_NOISE = re.compile(
    r"\b(?:movies|films|parts|ones|series|franchise|saga|trilogy)\b",
    re.I,
)


def clean_title_bits(text: str) -> tuple[str, int | None]:
    """Strip a parenthesised year out of a title, returning both."""
    raw = re.sub(r"\s+", " ", (text or "").strip(" -–—|,."))
    year: int | None = None
    match = YEAR_PAREN.search(raw)
    if match:
        try:
            year = int(match.group(1))
        except (TypeError, ValueError):
            year = None
        raw = (raw[: match.start()] + " " + raw[match.end() :]).strip(" -–—|,.")
        raw = re.sub(r"\s+", " ", raw)
    return raw, year


def normalize_franchise_seed(seed: str) -> str:
    """Turn "Harry Potters" / "LOTR movies" into a searchable franchise seed."""
    cleaned = _FRANCHISE_NOISE.sub(" ", seed or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    tokens = cleaned.split()
    if tokens and len(tokens[-1]) > 3 and tokens[-1].casefold().endswith("s"):
        last = tokens[-1]
        if not last.casefold().endswith(("ss", "us", "is", "ones")):
            tokens[-1] = last[:-1]
            cleaned = " ".join(tokens)
    return cleaned


def extract_exclusion(text: str) -> tuple[str, int, int]:
    """Split "<ask> except the last two" into ``(ask, drop_last, drop_first)``."""
    raw = (text or "").strip()
    match = EXCLUDE_TAIL.search(raw)
    if not match:
        return raw, 0, 0
    amount = _AMOUNTS.get((match.group("amount") or "one").casefold(), 1)
    from_end = match.group("count").casefold() in _FROM_END
    remainder = raw[: match.start()].strip(" -–—|,.")
    if not remainder:
        return raw, 0, 0
    return remainder, (amount if from_end else 0), (0 if from_end else amount)


def series_seed(text: str) -> str | None:
    """Return the franchise seed behind an "all of them" ask, else None."""
    raw = (text or "").strip()
    if not raw:
        return None
    # "the whole LOTR trilogy" / "alle Harry Potter films" wrap the seed between
    # the quantifier and the collective noun.
    wrapped = WHOLE_OF.match(raw)
    if wrapped:
        seed = normalize_franchise_seed(clean_title_bits(wrapped.group("title"))[0])
        if len(seed) >= 2:
            return seed
    if not (SERIES_ALL.search(raw) or ALL_PREFIX.match(raw) or TITLE_ALL_SUFFIX.match(raw)):
        return None
    for pattern in (TITLE_ALL_SUFFIX, ALL_PREFIX):
        match = pattern.match(raw)
        if match:
            seed, _ = clean_title_bits(match.group("title"))
            seed = normalize_franchise_seed(seed)
            if len(seed) >= 2:
                return seed
    cleaned = normalize_franchise_seed(SERIES_ALL.sub(" ", raw))
    seed, _ = clean_title_bits(cleaned)
    if len(seed) >= 2:
        return seed
    return None


def is_known_franchise(title: str) -> bool:
    normalized = " ".join((title or "").casefold().split())
    if normalized in KNOWN_FRANCHISE_SEEDS:
        return True
    stripped = re.sub(r"[^a-z0-9 ]+", "", normalized)
    return " ".join(stripped.split()) in KNOWN_FRANCHISE_SEEDS


__all__ = [
    "ALL_PREFIX",
    "BARE_SERIES_ALL",
    "EXCLUDE_TAIL",
    "KNOWN_FRANCHISE_SEEDS",
    "SERIES_ALL",
    "TITLE_ALL_SUFFIX",
    "WHOLE_OF",
    "YEAR_PAREN",
    "clean_title_bits",
    "extract_exclusion",
    "is_known_franchise",
    "normalize_franchise_seed",
    "series_seed",
]
