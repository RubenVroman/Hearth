"""Overseerr title offers — search once, request by mediaId.

Official contract (Overseerr UI / overtalkerr):

1. ``GET /api/v1/search?query=`` → results with ``id`` + ``mediaType`` movie|tv|person.
   Keep movie/tv (person is filmography, not this path).
2. Offer Did-you-mean / Get only after search returned a row. Persist
   ``{tmdbId: id, mediaType, title, year}`` on chat pending (+ ChatMemory).
3. Yes / Get: ``POST /api/v1/request`` body ``{mediaType, mediaId}``
   (mediaId = that TMDB id). TV: ``seasons: "all"``. NEVER request by title
   string. NEVER re-run ``title_seed_matches`` as a gate that can drop the
   offered row.
4. Already available → honest library copy, not not-found.
5. If search returned movie/tv but pending id was lost: fuzzy-pick the first
   movie/tv whose title matches the offered string (rapidfuzz token_set_ratio /
   WRatio, threshold ~80). Then POST by mediaId. Do NOT return format_not_found
   when search had hits.
6. format_not_found is LAST RESORT only when Overseerr search returned zero
   movie/tv rows.

Short 1-token seeds stay exact (``Land`` ≠ ``La La Land``). Multi-word titles
like ``Late Night with the Devil`` / ``Rescued by Ruby`` / ``The Man from Earth``
keep the first Overseerr movie/tv hit whose title contains the distinctive words.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hearth.memory.redact import redact
from hearth.telegram.parse import normalize_title
from hearth.tools.arr import _normalize_title_tokens, overseerr, title_seed_matches

log = logging.getLogger("hearth.telegram")

FUZZY_THRESHOLD = 80

# Function words — ignored when checking multi-word distinctive containment.
_MULTI_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "de",
        "het",
        "een",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "by",
        "with",
        "met",
        "&",
    }
)

_YEAR_IN_TITLE = re.compile(r"\((\s*(?:19|20)\d{2}\s*)\)\s*$")


def seed_token_count(query: str) -> int:
    """Significant token count after article strip (for short vs multi-word)."""
    return len(_normalize_title_tokens(query or ""))


def is_short_seed(query: str) -> bool:
    """One-token seeds use Land-style exact matching only."""
    return seed_token_count(query) <= 1


def distinctive_tokens(query: str) -> list[str]:
    """Non-stopword tokens used for multi-word containment checks."""
    out: list[str] = []
    for tok in _normalize_title_tokens(query or ""):
        if tok in _MULTI_STOP:
            continue
        if len(tok) < 2:
            continue
        out.append(tok)
    return out


def _fuzzy_score(left: str, right: str) -> float:
    """Best of token_set_ratio / WRatio (0–100). Falls back to difflib."""
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return float(max(fuzz.token_set_ratio(a, b), fuzz.WRatio(a, b)))
    except Exception:  # noqa: BLE001
        import difflib

        return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


def multiword_title_matches(seed: str, title: str) -> bool:
    """True when a multi-word seed is offerable against an Overseerr title.

    Distinctive seed tokens must all appear in the title, or fuzzy score ≥
    ``FUZZY_THRESHOLD``. Does **not** use Land-style exact/prefix-only gates.
    """
    seed = (seed or "").strip()
    title = (title or "").strip()
    if not seed or not title:
        return False
    if title_seed_matches(seed, title):
        return True
    st = distinctive_tokens(seed)
    title_toks = set(_normalize_title_tokens(title))
    if st and all(tok in title_toks for tok in st):
        return True
    return _fuzzy_score(seed, title) >= FUZZY_THRESHOLD


def short_seed_matches(seed: str, title: str) -> bool:
    """Exact / franchise-prefix only — ``Land`` must not match ``La La Land``."""
    return title_seed_matches(seed, title)


def offer_row_matches_seed(seed: str, title: str) -> bool:
    """Seed→title gate used by ``resolve_offer`` (not by Yes on a pending id)."""
    if is_short_seed(seed):
        return short_seed_matches(seed, title)
    return multiword_title_matches(seed, title)


def _row_title(row: dict[str, Any]) -> str:
    return str(row.get("title") or row.get("name") or "").strip()


def _row_year(row: dict[str, Any]) -> int | None:
    raw = row.get("year")
    if raw in (None, ""):
        date = row.get("releaseDate") or row.get("firstAirDate") or ""
        raw = str(date)[:4] or None
    try:
        year = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return year if year is not None and 1900 <= year <= 2100 else None


def _row_media_id(row: dict[str, Any]) -> int | None:
    raw = row.get("tmdbId")
    if raw in (None, ""):
        raw = row.get("mediaId")
    if raw in (None, ""):
        raw = row.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _row_media_type(row: dict[str, Any], hint: str = "") -> str:
    mt = str(row.get("mediaType") or row.get("media_type") or hint or "").strip().lower()
    if mt in {"movie", "tv"}:
        return mt
    return "movie" if hint == "movie" else ("tv" if hint == "tv" else "")


def normalize_offer_row(
    row: dict[str, Any],
    *,
    media_kind_hint: str = "",
) -> dict[str, Any] | None:
    """Normalize an Overseerr/TMDB row to ``{id/tmdbId, mediaType, title, year}``."""
    if not isinstance(row, dict):
        return None
    if row.get("matched") == "fallback":
        return None
    mt = _row_media_type(row, media_kind_hint)
    if mt not in {"movie", "tv"}:
        return None
    tid = _row_media_id(row)
    if tid is None:
        return None
    title = _row_title(row)
    if not title:
        return None
    return {
        "id": tid,
        "tmdbId": tid,
        "mediaId": tid,
        "mediaType": mt,
        "title": title,
        "year": _row_year(row),
        "inLibrary": bool(row.get("inLibrary")),
        "overview": row.get("overview"),
        "posterPath": row.get("posterPath"),
        "popularity": row.get("popularity"),
    }


def movie_tv_hits(results: list[Any] | None) -> list[dict[str, Any]]:
    """Keep normalized movie/tv rows from a raw Overseerr search payload."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for row in results or []:
        if not isinstance(row, dict):
            continue
        item = normalize_offer_row(row)
        if item is None:
            continue
        key = (int(item["tmdbId"]), str(item["mediaType"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def pick_offer_for_title(
    offers: list[dict[str, Any]],
    offered_title: str,
    *,
    year: int | None = None,
    media_kind: str = "",
) -> dict[str, Any] | None:
    """When pending mediaId was lost: fuzzy-pick from search hits.

    Prefer year + media_kind when provided. Threshold ~80 via rapidfuzz.
    If nothing clears the threshold but ``offers`` is non-empty, return the
    first movie/tv row (search had hits — never pretend Overseerr lacks them).
    """
    rows = [r for r in offers if isinstance(r, dict)]
    if media_kind in {"movie", "tv"}:
        typed = [r for r in rows if str(r.get("mediaType") or "") == media_kind]
        if typed:
            rows = typed
    if not rows:
        return None
    seed = (offered_title or "").strip()
    if not seed:
        return rows[0]

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        title = _row_title(row)
        if not title:
            continue
        if offer_row_matches_seed(seed, title):
            score = max(100.0, _fuzzy_score(seed, title))
        else:
            score = _fuzzy_score(seed, title)
        if year is not None and _row_year(row) == year:
            score += 5.0
        scored.append((score, row))
    if not scored:
        return rows[0]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    if best_score >= FUZZY_THRESHOLD or offer_row_matches_seed(seed, _row_title(best)):
        return best
    # Search returned movie/tv — never drop to not-found; take the top hit.
    return best


async def resolve_offer(
    query: str,
    *,
    year: int | None = None,
    media_kind: str = "",
) -> list[dict[str, Any]]:
    """Search Overseerr once; return offerable ``{id, mediaType, title, year}`` rows.

    Short 1-token seeds: exact only (``Land`` ≠ ``La La Land``).
    Multi-word: keep hits whose titles contain the distinctive words (or fuzzy
    ≥80). If multi-word filtering would empty a non-empty movie/tv result set,
    fall back to fuzzy-ranked raw hits so Overseerr UI hits stay offerable.
    """
    seed = (query or "").strip()
    if not seed:
        return []
    # Strip a trailing (YYYY) from the search string but keep year filter.
    year_m = _YEAR_IN_TITLE.search(seed)
    if year_m:
        try:
            parsed_year = int(year_m.group(1).strip())
        except (TypeError, ValueError):
            parsed_year = None
        if year is None and parsed_year is not None:
            year = parsed_year
        seed = seed[: year_m.start()].strip().strip("\"'")

    try:
        found = await overseerr.search(seed)
    except Exception as exc:  # noqa: BLE001
        log.info("resolve_offer search failed: %s", redact(str(exc)))
        return []

    raw = movie_tv_hits(found.get("results") if isinstance(found, dict) else None)
    if media_kind in {"movie", "tv"}:
        typed = [r for r in raw if str(r.get("mediaType") or "") == media_kind]
        if typed:
            raw = typed
    if not raw:
        return []

    grounded = [r for r in raw if offer_row_matches_seed(seed, _row_title(r))]
    if not grounded and not is_short_seed(seed):
        # Multi-word: never claim miss when Overseerr returned movie/tv rows.
        grounded = list(raw)
        grounded.sort(
            key=lambda r: _fuzzy_score(seed, _row_title(r)),
            reverse=True,
        )

    if year is not None:
        by_year = [r for r in grounded if _row_year(r) == year]
        if by_year:
            grounded = by_year
        elif grounded and is_short_seed(seed):
            # Exact short seed, wrong year editions → keep for disambiguation.
            pass

    return grounded


__all__ = [
    "FUZZY_THRESHOLD",
    "distinctive_tokens",
    "is_short_seed",
    "movie_tv_hits",
    "multiword_title_matches",
    "normalize_offer_row",
    "offer_row_matches_seed",
    "pick_offer_for_title",
    "resolve_offer",
    "seed_token_count",
    "short_seed_matches",
]
