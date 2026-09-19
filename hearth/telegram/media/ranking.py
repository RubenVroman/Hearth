"""Deterministic ranking and franchise ordering for Overseerr rows.

Pure functions: no settings, no network, no LLM. The router feeds them raw rows
and gets back the order a human would expect to read.
"""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from hearth.telegram.heuristics import looks_like_concrete_title
from hearth.telegram.models import MediaHit, MediaQuery
from hearth.tools.arr import title_seed_matches

MAX_RESULTS = 5
SERIES_MAX_RESULTS = 8


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
            if title_seed_matches(franchise_seed or "", hit.title)
            or title_seed_matches(franchise_seed or "", hit.original_title)
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


def in_release_order(hits: list[MediaHit]) -> list[MediaHit]:
    """Chronological franchise order; undated entries sink to the end."""
    return sorted(hits, key=lambda hit: (hit.year is None, hit.year or 0, hit.title))


def apply_exclusions(
    hits: list[MediaHit],
    *,
    drop_last: int = 0,
    drop_first: int = 0,
) -> list[MediaHit]:
    """Honour "all of them except the last" against release order."""
    ordered = in_release_order(hits)
    start = max(0, int(drop_first))
    end = len(ordered) - max(0, int(drop_last))
    if end <= start:
        return ordered
    return ordered[start:end]


def without_ids(hits: list[MediaHit], exclude: set[int] | frozenset[int]) -> list[MediaHit]:
    """Drop titles already shown in this chat so "more" stays fresh."""
    if not exclude:
        return list(hits)
    return [hit for hit in hits if hit.tmdb_id not in exclude]


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
    "MAX_RESULTS",
    "SERIES_MAX_RESULTS",
    "apply_exclusions",
    "best_franchise_seed",
    "in_release_order",
    "normalized",
    "rank_hits",
    "to_hits",
    "without_ids",
]
