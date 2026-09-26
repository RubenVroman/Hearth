"""Split compound asks into an ordered multi-item plan.

"grab Inception and Interstellar" is two titles; "Harry Potter and the Chamber
of Secrets" is one. Splitting is therefore conservative: a separator only wins
when both sides survive as plausible titles, and trailing modifiers ("extended
edition", "all movies") fold back into the item they describe.

"and", "&" and a two-item comma are *risky* separators — "Bonnie and Clyde",
"Cowboys & Aliens" and "Crouching Tiger, Hidden Dragon" are single films that
look exactly like two-item plans. Those separators therefore also need an
explicit plan signal: a grab verb ("grab X and Y"), an unambiguous separator
("X + Y", "X and also Y"), or three or more items.
"""

from __future__ import annotations

import re

from hearth.telegram.heuristics import looks_like_concrete_title
from hearth.telegram.media.editions import extract_edition
from hearth.telegram.media.phrases import (
    BARE_SERIES_ALL,
    clean_title_bits,
    extract_exclusion,
    extract_numbered_franchise,
    is_known_franchise,
    normalize_franchise_seed,
    series_seed,
)
from hearth.telegram.media.types import AskPart

MAX_PARTS = 4

_GRAB_PREFIX = re.compile(
    r"^\s*(?:please\s+|pls\s+|even\s+)?"
    r"(?:can\s+you\s+|could\s+you\s+|kun\s+je\s+|kan\s+je\s+)?"
    r"(?:grab|get|download|request|queue|add|find|search|fetch|"
    r"haal|zoek|vraag|voeg)\b"
    r"(?:\s+(?:me|us|mij|ons|for\s+me|voor\s+mij|both|all|alle|beide))?"
    r"\s*[:,-]?\s*",
    re.I,
)
_PLUS = re.compile(r"\s*\+\s*|\s+plus\s+", re.I)
_AMPERSAND = re.compile(r"\s+&\s+")
_AND = re.compile(r"\s+(?:and|en)\s+", re.I)
_COMMA = re.compile(r"\s*[,;]\s*")
_ALSO = re.compile(r"\s*(?:,\s*)?(?:and\s+)?also\s+", re.I)

# A part that begins with a *lower-case* article continues a real title ("the
# Chamber of Secrets", "the Bad and the Ugly") rather than starting a new ask.
# Case matters: "The Matrix" as a second item is a legitimate separate request.
_LOWER_ARTICLE = re.compile(r"^(?:the|a|an|de|het|een)\s+\S")
_CONNECTIVE_ONLY = re.compile(
    r"^\s*(?:and|en|plus|also|too|both|as\s+well|ook|en\s+ook|then|dan)\s*$",
    re.I,
)
# An honorific or initial is part of one name ("Mr. and Mrs. Smith"), never a
# request of its own.
_HONORIFIC_ONLY = re.compile(
    r"^\s*(?:mr|mrs|ms|miss|dr|prof|st|sgt|capt|jr|sr|mme|mlle)\.?\s*$",
    re.I,
)
# A vibe pronoun means the user is describing a mood, not naming a second title
# ("anything good and recent").
_VIBE_LEAD = re.compile(
    r"^\s*(?:something|somethin|anything|everything|nothing|iets|alles|niets|"
    r"whatever|any)\b",
    re.I,
)
_BARE_EDITION = re.compile(
    r"^\s*(?:in\s+|the\s+)?(?:extended|director'?s?|theatrical|unrated|ultimate|special|"
    r"collector'?s?|anniversary|remaster(?:ed)?|criterion|imax|4k|uhd|2160p|1080p|hdr)"
    r"(?:\s+(?:cut|edition|version))?\s*$",
    re.I,
)
_TYPE_HINT_MOVIE = re.compile(r"\b(?:movie|film)\b\s*$", re.I)
_TYPE_HINT_TV = re.compile(r"\b(?:series|show|serie)\b\s*$", re.I)
_TRAILING_POLITE = re.compile(r"\s+(?:please|pls|aub|thanks|thx|graag|alsjeblieft)\s*$", re.I)


def _and_split_is_safe(text: str, pieces: list[str], *, plan_signal: bool) -> bool:
    """Decide whether " and " joins two requests or lives inside one title.

    Three signals veto the split: the whole phrase is a known franchise, the
    left side is itself a franchise seed ("Harry Potter and the …"), or a later
    piece opens with a lower-case article, which only happens mid-title.

    The franchise-prefix veto is dropped once the ask is explicitly a plan:
    "grab Harry Potter and Dune" is two requests, and the lower-case article
    still protects "grab Harry Potter and the Chamber of Secrets".
    """
    if is_known_franchise(text):
        return False
    if any(_LOWER_ARTICLE.match(piece) for piece in pieces[1:]):
        return False
    if plan_signal:
        return True
    prefix = ""
    for piece in pieces[:-1]:
        prefix = f"{prefix} and {piece}".strip(" and ") if prefix else piece
        if is_known_franchise(prefix):
            return False
    return True


def _pieces(pattern: re.Pattern[str], text: str) -> list[str]:
    return [piece.strip() for piece in pattern.split(text) if piece.strip()]


def _has_plan_signal(body: str, *, grab_prefix: bool) -> bool:
    """True when the ask is explicitly a multi-item plan rather than one title.

    Without one of these the risky separators stay inside the title, because
    "Bonnie and Clyde" and "Cowboys & Aliens" are single films.
    """
    return grab_prefix or bool(_PLUS.search(body)) or bool(_ALSO.search(body))


def _split_once(text: str, *, plan_signal: bool) -> list[str]:
    """Split on the strongest separator present, or return a single segment."""
    for pattern in (_PLUS, _ALSO):
        pieces = _pieces(pattern, text)
        if len(pieces) >= 2:
            return pieces
    pieces = _pieces(_COMMA, text)
    if (
        len(pieces) >= 2
        and (plan_signal or len(pieces) >= 3)
        and not any(_LOWER_ARTICLE.match(piece) for piece in pieces[1:])
    ):
        return pieces
    # " and " / " & " are the riskiest separators: plenty of real titles join
    # two bare nouns exactly that way, so they need an explicit plan signal.
    for pattern in (_AND, _AMPERSAND):
        pieces = _pieces(pattern, text)
        if (
            len(pieces) >= 2
            and (plan_signal or len(pieces) >= 3)
            and _and_split_is_safe(text, pieces, plan_signal=plan_signal)
        ):
            return pieces
    return [text.strip()]


def _split_segments(text: str, *, plan_signal: bool) -> list[str]:
    """Split, then split each comma segment again on "and" when it is safe."""
    first = _split_once(text, plan_signal=plan_signal)
    if len(first) < 2:
        return first
    out: list[str] = []
    for segment in first:
        if len(out) >= MAX_PARTS + 2:
            out.append(segment)
            continue
        out.extend(_split_once(segment, plan_signal=plan_signal))
    return out


def _as_part(segment: str) -> AskPart | None:
    raw = _TRAILING_POLITE.sub("", segment.strip(" -–—|,.")).strip()
    if not raw or _CONNECTIVE_ONLY.match(raw):
        return None
    if _HONORIFIC_ONLY.match(raw) or _VIBE_LEAD.match(raw) or len(raw) < 3:
        return None

    remainder, drop_last, drop_first = extract_exclusion(raw)
    media_type = ""
    if _TYPE_HINT_TV.search(remainder):
        media_type = "tv"
        remainder = _TYPE_HINT_TV.sub("", remainder).strip(" -–—|,.")
    elif _TYPE_HINT_MOVIE.search(remainder):
        media_type = "movie"
        remainder = _TYPE_HINT_MOVIE.sub("", remainder).strip(" -–—|,.")

    seed = series_seed(remainder)
    wants_all = seed is not None
    working = seed if seed else remainder

    edition_key = ""
    edition_label = ""
    edition = extract_edition(working)
    if edition is not None and edition.present:
        edition_key = edition.key
        edition_label = edition.label
        working = edition.clean_title

    title, year = clean_title_bits(working)
    title = normalize_franchise_seed(title) if wants_all else title
    if len(title) < 2:
        return None
    return AskPart(
        title=title,
        year=year,
        media_type=media_type,
        edition_key=edition_key,
        edition_label=edition_label,
        series_all=wants_all,
        drop_last=drop_last,
        drop_first=drop_first,
        raw=raw,
    )


def _merge_modifier(parts: list[AskPart], segment: str) -> bool:
    """Fold "extended edition" / "all movies" onto the preceding item."""
    if not parts:
        return False
    stripped = segment.strip(" -–—|,.")
    if BARE_SERIES_ALL.match(stripped):
        previous = parts[-1]
        remainder, drop_last, drop_first = extract_exclusion(stripped)
        del remainder
        parts[-1] = AskPart(
            title=previous.title,
            year=previous.year,
            media_type=previous.media_type,
            edition_key=previous.edition_key,
            edition_label=previous.edition_label,
            series_all=True,
            drop_last=drop_last or previous.drop_last,
            drop_first=drop_first or previous.drop_first,
            raw=previous.raw,
        )
        return True
    if _BARE_EDITION.match(stripped):
        edition = extract_edition(f"placeholder {stripped}")
        if edition is None or not edition.present:
            return False
        previous = parts[-1]
        parts[-1] = AskPart(
            title=previous.title,
            year=previous.year,
            media_type=previous.media_type,
            edition_key=edition.key,
            edition_label=edition.label,
            series_all=previous.series_all,
            drop_last=previous.drop_last,
            drop_first=previous.drop_first,
            raw=previous.raw,
        )
        return True
    return False


def split_compound_ask(text: str, *, max_parts: int = MAX_PARTS) -> tuple[AskPart, ...]:
    """Return ≥2 ordered items for a compound ask, else an empty tuple.

    A single-item result is intentionally dropped: the caller's exact / series /
    edition lanes already handle one title better than a one-item plan would.
    """
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return ()
    body = _GRAB_PREFIX.sub("", raw, count=1).strip()
    if not body:
        return ()
    # "get Harry Potter part 6" is one numbered entry, not "Harry" + "Potter part 6".
    if extract_numbered_franchise(raw) is not None or extract_numbered_franchise(body) is not None:
        return ()

    plan_signal = _has_plan_signal(body, grab_prefix=body != raw)
    segments = _split_segments(body, plan_signal=plan_signal)
    if len(segments) < 2:
        return ()

    parts: list[AskPart] = []
    for segment in segments:
        if _merge_modifier(parts, segment):
            continue
        part = _as_part(segment)
        if part is None:
            # A fragment we cannot read as a title means the split was wrong.
            return ()
        if not (part.series_all or looks_like_concrete_title(part.title)):
            return ()
        parts.append(part)
        if len(parts) >= max(2, int(max_parts)):
            break

    if len(parts) < 2:
        return ()
    deduped: list[AskPart] = []
    seen: set[tuple[str, str, int | None]] = set()
    for part in parts:
        key = (part.title.casefold(), part.edition_key, part.year)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(part)
    return tuple(deduped) if len(deduped) >= 2 else ()


__all__ = ["MAX_PARTS", "split_compound_ask"]
