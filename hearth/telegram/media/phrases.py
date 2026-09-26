"""Shared title / series / exclusion phrases for Telegram media asks.

Kept in one module so the classifier, the compound splitter and the franchise
lane agree on what "all of them except the last" means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    r"(?:\s+(?P<amount>one|two|three|four|five|[1-5]|twee|drie|vier|vijf))?"
    r"(?:\s+(?:one|ones|movie|movies|film|films|part|parts|deel|delen))?\s*$",
    re.I,
)
_AMOUNTS = {
    "one": 1,
    "1": 1,
    "two": 2,
    "2": 2,
    "twee": 2,
    "three": 3,
    "3": 3,
    "drie": 3,
    "four": 4,
    "4": 4,
    "vier": 4,
    "five": 5,
    "5": 5,
    "vijf": 5,
}
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


# "Harry Potter part 6" / "star wars episode 5" name a slot in a franchise, not
# a catalog string. Only seeds already in KNOWN_FRANCHISE_SEEDS qualify, so a
# real title that merely ends in "Part Two" ("Dune: Part Two") stays literal.
_MAX_INSTALLMENT = 20
_LEADING_ASK = re.compile(
    r"^(?:please\s+|pls\s+)?"
    r"(?:can\s+you\s+|could\s+you\s+|kun\s+je\s+)?"
    r"(?:please\s+)?"
    r"(?:grab|get|download|request|queue|find|search|fetch|haal|zoek|vraag)\s+"
    r"(?:me\s+|us\s+|mij\s+)?",
    re.I,
)
_TRAILING_POLITE = re.compile(r"\s+(?:please|pls|thanks|thx|graag|alsjeblieft)\s*$", re.I)
_MARKER = r"part|pt\.?|episode|chapter|vol\.?|volume|deel|film|movie|nr\.?|no\.?|number"
_NUMBER_WORD = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"een|twee|drie|vier|vijf|zes|zeven|acht|negen|tien"
)
_NUM_TOKEN = rf"\d{{1,2}}(?:st|nd|rd|th)?|{_NUMBER_WORD}|[ivx]{{1,4}}"
_MARKED_INSTALLMENT = re.compile(
    rf"^(?P<seed>.+?)\s+(?P<marker>{_MARKER})\s+(?P<num>{_NUM_TOKEN})\s*$",
    re.I,
)
# Bare "harry potter 6". Roman numerals need 2+ characters so "batman v" is not
# installment 5; "episode v" still works through the marked pattern above.
_BARE_INSTALLMENT = re.compile(
    r"^(?P<seed>.+?)\s+(?P<num>\d{1,2}(?:st|nd|rd|th)?|[ivx]{2,4})\s*$",
    re.I,
)
_ORDINAL_SUFFIX = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)$", re.I)
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an|de|het)\s+", re.I)
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "een": 1,
    "twee": 2,
    "drie": 3,
    "vier": 4,
    "vijf": 5,
    "zes": 6,
    "zeven": 7,
    "acht": 8,
    "negen": 9,
    "tien": 10,
}
_ROMAN = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
    "xiii": 13,
    "xiv": 14,
    "xv": 15,
}


@dataclass(frozen=True, slots=True)
class NumberedFranchise:
    """A known franchise seed plus the 1-based slot the user asked for."""

    seed: str
    index: int
    numbering: str  # "episode" follows saga numbers; "index" follows release order


def _prepare_installment_text(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    raw = raw.strip("\"'“”‘’")
    raw = re.sub(r"[.!?]+$", "", raw).strip()
    raw = re.sub(r"\s*\(\s*(?:19|20)\d{2}\s*\)\s*$", "", raw).strip()
    raw = _TRAILING_POLITE.sub("", raw).strip()
    return _LEADING_ASK.sub("", raw, count=1).strip()


def _clean_installment_seed(seed: str) -> str:
    text = re.sub(r"\s+", " ", (seed or "").strip())
    return text.strip(" -–—:|,.'\"")


def _known_installment_seed(seed: str) -> str | None:
    cleaned = _clean_installment_seed(seed)
    if cleaned and is_known_franchise(cleaned):
        return cleaned
    stripped = _LEADING_ARTICLE.sub("", cleaned, count=1).strip()
    if stripped and stripped != cleaned and is_known_franchise(stripped):
        return stripped
    return None


def _parse_installment_index(token: str, *, allow_short_roman: bool) -> int | None:
    raw = (token or "").casefold().strip(".")
    ordinal = _ORDINAL_SUFFIX.match(raw)
    if ordinal:
        value = int(ordinal.group(1))
    elif raw.isdigit():
        value = int(raw)
    elif raw in _NUMBER_WORDS:
        value = _NUMBER_WORDS[raw]
    elif raw in _ROMAN and (allow_short_roman or len(raw) >= 2):
        value = _ROMAN[raw]
    else:
        return None
    if 1 <= value <= _MAX_INSTALLMENT:
        return value
    return None


def extract_numbered_franchise(text: str) -> NumberedFranchise | None:
    """Parse "Harry potter part 6" into seed ``Harry potter`` and slot 6.

    Returns None unless the words in front of the number are a known franchise
    seed. That keeps exact titles such as "Dune: Part Two" and "Deathly
    Hallows: Part 1" on the literal search path.
    """
    prepared = _prepare_installment_text(text)
    if not prepared:
        return None
    marked = _MARKED_INSTALLMENT.match(prepared)
    if marked:
        seed = _known_installment_seed(marked.group("seed"))
        index = _parse_installment_index(marked.group("num"), allow_short_roman=True)
        if not seed or index is None:
            return None
        marker = marked.group("marker").casefold().rstrip(".")
        numbering = "episode" if marker == "episode" else "index"
        return NumberedFranchise(seed=seed, index=index, numbering=numbering)
    bare = _BARE_INSTALLMENT.match(prepared)
    if not bare:
        return None
    seed = _known_installment_seed(bare.group("seed"))
    index = _parse_installment_index(bare.group("num"), allow_short_roman=False)
    if not seed or index is None:
        return None
    return NumberedFranchise(seed=seed, index=index, numbering="index")


__all__ = [
    "ALL_PREFIX",
    "BARE_SERIES_ALL",
    "EXCLUDE_TAIL",
    "KNOWN_FRANCHISE_SEEDS",
    "SERIES_ALL",
    "TITLE_ALL_SUFFIX",
    "WHOLE_OF",
    "YEAR_PAREN",
    "NumberedFranchise",
    "clean_title_bits",
    "extract_exclusion",
    "extract_numbered_franchise",
    "is_known_franchise",
    "normalize_franchise_seed",
    "series_seed",
]
