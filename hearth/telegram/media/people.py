"""Person asks → TMDB person credits (never a literal title search).

"anything with Florence Pugh" has no catalog title in it, so searching the
sentence verbatim is a guaranteed miss. Detection here is deterministic; the
lane then uses the already-wired ``search_person`` + ``person_combined_credits``
Overseerr routes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_NAME_WORD = r"[A-Z][\w'’.-]+|[a-z]{2,}"
_STOP_TAIL = re.compile(
    r"\s+(?:movies?|films?|shows?|series|filmography|stuff|things|catalog(?:ue)?|"
    r"anything|everything|titles?)\s*$",
    re.I,
)
_LEAD_FILLER = re.compile(
    r"^(?:(?:show|give|get|grab|find|download|request|queue|haal|zoek|vraag)\s+"
    r"(?:me\s+|us\s+|mij\s+|ons\s+)?)?"
    r"(?:some|any|all|alle|the|de|het|a\s+few)?\s*",
    re.I,
)

# "movies with X", "anything starring X", "films by X", "X's filmography"
_WITH_PERSON = re.compile(
    r"\b(?:movies?|films?|shows?|series|something|anything|everything|iets|alles|titles?)\b"
    r"[^,.!?]{0,24}?\b(?:with|starring|featuring|met|van)\s+(?P<who>.+)$",
    re.I,
)
_BY_PERSON = re.compile(
    r"\b(?:directed\s+by|dir\.?\s+by|from\s+director|by\s+director|regie\s+van|"
    r"geregisseerd\s+door)\s+(?P<who>.+)$",
    re.I,
)
_PERSON_POSSESSIVE = re.compile(
    # The apostrophe must be explicit: a bare "s?" would eat the s of "Hanks".
    r"^(?P<who>.+?)(?:'s|’s|'|’)\s+(?:filmography|movies?|films?|shows?|catalog(?:ue)?|"
    r"back\s+catalog(?:ue)?|oeuvre|work)\s*$",
    re.I,
)
_PERSON_SUFFIX = re.compile(
    r"^(?P<who>.+?)\s+(?:filmography|movies|films|shows|movie\s+list)\s*$",
    re.I,
)
_FILMOGRAPHY_OF = re.compile(
    r"\b(?:filmography|movies?|films?|shows?|credits)\s+(?:of|van)\s+(?P<who>.+)$",
    re.I,
)
_DIRECTOR_HINT = re.compile(
    r"\b(?:directed\s+by|dir\.?\s+by|director|regie|geregisseerd)\b",
    re.I,
)
# Words that prove the tail is a description, not a human being.
_NOT_A_NAME = re.compile(
    r"\b(?:the|a|an|de|het|een|that|this|those|scar|glasses|wizard|robot|"
    r"spaceship|about|where|who|which|when|plot|vibe|ending|twist|"
    r"subtitles?|dubbed|4k|1080p)\b",
    re.I,
)
_TITLE_TAIL = re.compile(r"\b(?:in\s+it|please|thanks|aub)\s*$", re.I)


@dataclass(frozen=True, slots=True)
class PersonAsk:
    """A detected person ask and the role it implies."""

    name: str
    role: str = "cast"  # cast | directing

    @property
    def present(self) -> bool:
        return bool(self.name)


def _clean_name(raw: str) -> str:
    name = _TITLE_TAIL.sub("", (raw or "").strip())
    name = _STOP_TAIL.sub("", name)
    name = _LEAD_FILLER.sub("", name, count=1)
    name = name.strip(" -–—|,.?!\"'“”")
    name = re.sub(r"\s+", " ", name)
    return name


def _plausible_name(name: str) -> bool:
    if not name or len(name) < 3 or len(name) > 60:
        return False
    words = name.split()
    if not 1 <= len(words) <= 4:
        return False
    if _NOT_A_NAME.search(name):
        return False
    if not re.fullmatch(rf"(?:{_NAME_WORD})(?:\s+(?:{_NAME_WORD}))*", name):
        return False
    # A single lower-case word is almost always a genre or a title fragment.
    if len(words) == 1 and name[:1].islower():
        return False
    return True


def detect_person_ask(text: str) -> PersonAsk | None:
    """Return the person and role behind a filmography ask, else None."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return None

    role = "directing" if _DIRECTOR_HINT.search(raw) else "cast"
    for pattern in (_BY_PERSON, _FILMOGRAPHY_OF, _WITH_PERSON):
        match = pattern.search(raw)
        if match:
            name = _clean_name(match.group("who"))
            if _plausible_name(name):
                return PersonAsk(name=name, role=role)

    for pattern in (_PERSON_POSSESSIVE, _PERSON_SUFFIX):
        match = pattern.match(raw)
        if match:
            name = _clean_name(match.group("who"))
            if _plausible_name(name):
                return PersonAsk(name=name, role=role)
    return None


def _credit_year(row: dict[str, Any]) -> int:
    date = str(row.get("releaseDate") or row.get("firstAirDate") or "")
    return int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else 0


def rank_credits(
    payload: dict[str, Any],
    *,
    role: str = "cast",
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Pick the credits a human would name first: popular, real, newest-ish.

    Crew rows are filtered to the directing department for "directed by" asks so
    a composer credit cannot masquerade as a filmography.
    """
    buckets: list[dict[str, Any]] = []
    if role == "directing":
        for row in payload.get("crew") or []:
            if not isinstance(row, dict):
                continue
            department = str(row.get("department") or "").strip().lower()
            job = str(row.get("job") or "").strip().lower()
            if department == "directing" or job == "director":
                buckets.append(row)
        if not buckets:
            buckets = [row for row in (payload.get("cast") or []) if isinstance(row, dict)]
    else:
        buckets = [row for row in (payload.get("cast") or []) if isinstance(row, dict)]

    seen: set[tuple[str, int]] = set()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in buckets:
        media_type = str(row.get("mediaType") or row.get("media_type") or "").strip().lower()
        if media_type not in {"movie", "tv"}:
            continue
        try:
            tmdb_id = int(row.get("id") or row.get("tmdbId") or 0)
        except (TypeError, ValueError):
            continue
        if tmdb_id <= 0:
            continue
        key = (media_type, tmdb_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            votes = float(row.get("voteCount") or row.get("vote_count") or 0)
        except (TypeError, ValueError):
            votes = 0.0
        try:
            popularity = float(row.get("popularity") or 0)
        except (TypeError, ValueError):
            popularity = 0.0
        year = _credit_year(row)
        if votes < 50 and popularity < 6:
            # Bit parts and unreleased projects are noise in a filmography card.
            continue
        score = popularity * 2 + votes / 40.0 + max(0, year - 1980) / 8.0
        scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[: max(1, int(limit))]]


def pick_person(rows: list[Any], asked: str) -> dict[str, Any] | None:
    """Choose the person row that best matches the asked name."""
    needle = " ".join((asked or "").casefold().split())
    best: tuple[float, dict[str, Any]] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = " ".join(str(row.get("name") or row.get("title") or "").casefold().split())
        if not name:
            continue
        try:
            popularity = float(row.get("popularity") or 0)
        except (TypeError, ValueError):
            popularity = 0.0
        score = popularity
        if name == needle:
            score += 10_000
        elif needle and (needle in name or name in needle):
            score += 1_000
        if best is None or score > best[0]:
            best = (score, row)
    return best[1] if best is not None else None


__all__ = ["PersonAsk", "detect_person_ask", "pick_person", "rank_credits"]
