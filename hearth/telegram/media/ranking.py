"""Deterministic ranking and franchise ordering for Overseerr rows.

Pure functions: no settings, no network, no LLM. The router feeds them raw rows
and gets back the order a human would expect to read.
"""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

from hearth.telegram.heuristics import looks_like_concrete_title
from hearth.telegram.models import MediaHit, MediaQuery
from hearth.tools.arr import normalize_title_tokens, title_seed_matches

MAX_RESULTS = 5
SERIES_MAX_RESULTS = 8
# Long enough for a saga (Star Wars, Fast & Furious) without paging the catalog.
INSTALLMENT_LIMIT = 24


def normalized(value: str) -> str:
    return " ".join((value or "").casefold().split())


def to_hits(
    rows: list[dict[str, Any]],
    *,
    media_type: str | None = None,
) -> list[MediaHit]:
    """Normalize Overseerr rows into de-duplicated movie/TV hits."""
    hits: list[MediaHit] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            hit = MediaHit.from_overseerr(row)
        except ValueError:
            continue
        key = (hit.media_type, hit.tmdb_id)
        if key in seen:
            continue
        if media_type and hit.media_type != media_type:
            continue
        seen.add(key)
        hits.append(hit)
    return hits


def rank_hits(
    rows: list[dict[str, Any]],
    query: MediaQuery,
    *,
    franchise_seed: str | None = None,
    limit: int = MAX_RESULTS,
) -> list[MediaHit]:
    """Order search rows by how well they answer the ask."""
    hits = to_hits(rows, media_type=query.media_type)

    asked = normalized(query.title)
    seed = normalized(franchise_seed or "")
    if seed:
        seeded = [
            hit
            for hit in hits
            if franchise_seed_matches(franchise_seed or "", hit.title)
            or franchise_seed_matches(franchise_seed or "", hit.original_title)
        ]
        if seeded:
            hits = seeded
    # Short exact titles must not become substring menus (Land → La La Land).
    elif asked and looks_like_concrete_title(query.title):
        seeded = [
            hit
            for hit in hits
            if title_seed_matches(query.title, hit.title)
            or title_seed_matches(query.title, hit.original_title)
        ]
        if seeded:
            hits = seeded

    def score(hit: MediaHit) -> float:
        title = normalized(hit.title)
        original = normalized(hit.original_title)
        candidates = [candidate for candidate in (title, original) if candidate]
        relevance = (
            max(float(fuzz.WRatio(asked or seed, candidate)) for candidate in candidates)
            if (asked or seed) and candidates
            else 100.0
        )
        if asked and asked in candidates:
            relevance += 1000
        elif asked and any(candidate.startswith(asked) for candidate in candidates):
            relevance += 300
        if query.year is not None and hit.year == query.year:
            relevance += 500
        # Prefer earlier release years for franchise lists (stable order).
        if seed and hit.year is not None:
            relevance += max(0, 2100 - hit.year) / 100.0
        return relevance

    hits.sort(key=score, reverse=True)
    return hits[: max(1, int(limit))]


_FRANCHISE_SEPARATOR = re.compile(r"^\s*[:\-–—]|\s+[-–—]\s+")
_SEQUEL_WORD = re.compile(
    r"^(?:part|chapter|episode|vol\.?|volume|deel|[ivx]+|\d+)\b",
    re.I,
)
_CONNECTIVE = frozenset(
    {"of", "in", "on", "at", "to", "for", "from", "with", "and", "or", "van", "met", "der", "des"}
)


def franchise_seed_matches(seed: str, title: str) -> bool:
    """Franchise membership, which is looser than an exact-title match.

    ``title_seed_matches`` intentionally refuses single-word prefixes so that
    "Land" cannot pull in "La La Land". That guard is right for the exact lane
    but it also hides "Dune: Part Two" from the *Dune* franchise, so a one-word
    seed is accepted here only when the rest of the title reads like a franchise
    entry: a separator ("Dune: Part Two"), a part word ("Rocky II"), or a single
    trailing word ("Matrix Reloaded") — never a prepositional phrase
    ("Land of the Dead").
    """
    if title_seed_matches(seed, title):
        return True
    seed_tokens = normalize_title_tokens(seed)
    title_tokens = normalize_title_tokens(title)
    if len(seed_tokens) != 1 or len(title_tokens) < 2:
        return False
    if title_tokens[0] != seed_tokens[0] or len(seed_tokens[0]) < 3:
        return False
    remainder = " ".join(title_tokens[1:])
    head = normalize_title_tokens(title)[0]
    raw_tail = (title or "").strip()
    index = raw_tail.casefold().find(head)
    if index >= 0:
        raw_tail = raw_tail[index + len(head) :]
    if _FRANCHISE_SEPARATOR.match(raw_tail):
        return True
    if _SEQUEL_WORD.match(remainder):
        return True
    return len(title_tokens) == 2 and title_tokens[1] not in _CONNECTIVE


def plausible_match(asked: str, hit: MediaHit, *, floor: int = 80) -> bool:
    """True when a hit could honestly be the asked title.

    Overseerr search happily returns loosely related rows. In a multi-item plan
    that would silently swap one requested film for another, so each item is
    checked before it earns a Get button.
    """
    needle = normalized(asked)
    if not needle:
        return True
    if title_seed_matches(asked, hit.title) or title_seed_matches(asked, hit.original_title):
        return True
    candidates = [normalized(hit.title), normalized(hit.original_title)]
    return any(
        float(fuzz.WRatio(needle, candidate)) >= float(floor)
        for candidate in candidates
        if candidate
    )


def in_release_order(hits: list[MediaHit]) -> list[MediaHit]:
    """Chronological franchise order; undated entries sink to the end."""
    return sorted(hits, key=lambda hit: (hit.year is None, hit.year or 0, hit.title))


def apply_exclusions(
    hits: list[MediaHit],
    *,
    drop_last: int = 0,
    drop_first: int = 0,
) -> list[MediaHit]:
    """Honour "all of them except the last" against release order.

    An exclusion that swallows the whole list returns nothing. Handing back the
    full list instead would show every title the user just asked to leave out,
    with no sign the exclusion was dropped.
    """
    ordered = in_release_order(hits)
    start = max(0, int(drop_first))
    end = len(ordered) - max(0, int(drop_last))
    if end <= start:
        return []
    return ordered[start:end]


def without_ids(hits: list[MediaHit], exclude: set[int] | frozenset[int]) -> list[MediaHit]:
    """Drop titles already shown in this chat so "more" stays fresh."""
    if not exclude:
        return list(hits)
    return [hit for hit in hits if hit.tmdb_id not in exclude]


_EPISODE_IN_TITLE = re.compile(
    r"\bepisodes?\s+(?P<num>[ivxlcdm]{1,8}|\d{1,2})\b",
    re.I,
)
_TITLE_ROMAN = {
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


def _title_episode_index(title: str) -> int | None:
    """Return the saga episode written in a title, if it has one."""
    match = _EPISODE_IN_TITLE.search(title or "")
    if not match:
        return None
    token = match.group("num").casefold()
    if token.isdigit():
        value = int(token)
    else:
        value = _TITLE_ROMAN.get(token)
    if value is None or value < 1:
        return None
    return value


def _hit_episode_index(hit: MediaHit) -> int | None:
    return _title_episode_index(hit.title) or _title_episode_index(hit.original_title)


def _mostly_episode_numbered(hits: list[MediaHit]) -> bool:
    """True when the pack is a numbered saga (Star Wars), not a dated series."""
    if len(hits) < 2:
        return False
    tagged = sum(1 for hit in hits if _hit_episode_index(hit) is not None)
    return tagged >= 2 and tagged * 2 >= len(hits)


def select_franchise_installment(
    hits: list[MediaHit],
    index: int,
    *,
    numbering: str = "index",
) -> MediaHit | None:
    """Pick part ``index`` (1-based) out of a franchise pack.

    "Episode 5" follows the episode number written in the title, so Star Wars
    episode 5 is Empire and not the fifth film by release date. "Part 6" and a
    bare "6" follow release order, which is how Harry Potter part 6 is the
    Half-Blood Prince. A saga whose titles already say "Episode N" uses that
    number for a bare ask too ("star wars 5").
    """
    if index < 1 or not hits:
        return None
    saga = numbering == "episode" or _mostly_episode_numbered(hits)
    if saga:
        matched = [hit for hit in hits if _hit_episode_index(hit) == index]
        if matched:
            return in_release_order(matched)[0]
        # No "Episode N" labels in this pack ("Harry Potter episode 6") — the
        # number is a release slot. A real saga with no such episode stays a miss
        # so Star Wars episode 15 cannot become some other film.
        if any(_hit_episode_index(hit) is not None for hit in hits):
            return None
    ordered = in_release_order(hits)
    if index > len(ordered):
        return None
    return ordered[index - 1]


def best_franchise_seed(title: str) -> str:
    """Trim a full entry title down to its franchise seed for collection asks."""
    raw = (title or "").strip()
    for separator in (":", " – ", " - ", " — "):
        if separator in raw:
            head = raw.split(separator, 1)[0].strip()
            if len(head.split()) >= 2:
                return head
    words = raw.split()
    if len(words) > 4:
        return " ".join(words[:3])
    return raw


__all__ = [
    "INSTALLMENT_LIMIT",
    "MAX_RESULTS",
    "SERIES_MAX_RESULTS",
    "apply_exclusions",
    "best_franchise_seed",
    "franchise_seed_matches",
    "in_release_order",
    "normalized",
    "plausible_match",
    "rank_hits",
    "select_franchise_installment",
    "to_hits",
    "without_ids",
]
