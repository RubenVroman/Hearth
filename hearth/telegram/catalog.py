"""Resolve catalog ids and titles via Overseerr (TMDB-backed movie + TV).

Telegram Movies inbox searches and requests through Overseerr only — it returns
``mediaType`` movie|tv and can request either. Radarr/Sonarr are not used for
title lookup or queueing here (they remain for download progress / queue tools).

Never search a raw ``tt…`` / bare id string as a movie title. Prefer TMDB
primary release / first_air_date year. No new env keys.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from hearth.memory.redact import redact
from hearth.telegram.parse import ParsedRequest, normalize_title, strip_title_year_media
from hearth.tools.arr import overseerr

log = logging.getLogger("hearth.telegram")

MediaKind = Literal["movie", "tv"]

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# Trailing cast clues for gpt-4o — never part of the Overseerr search string.
# Skip mid-title "with the …" (Gone with the Wind).
_CAST_STOP = frozenset({"the", "a", "an", "my", "his", "her", "our", "their", "no", "me"})
_FEATURING = re.compile(
    r"\s+(?:featuring|starring|feat\.?|ft\.?)\s+.+$",
    re.I,
)
_WITH_PERSON = re.compile(
    r"\s+(?:with|met)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\-]+)"
    r"(?:\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\-]+)+\s*$",
    re.I,
)


def catalog_search_title(title: str) -> str:
    """Strip trailing actor/cast clauses, film/movie words, and year disambiguators."""
    raw = (title or "").strip()
    if not raw:
        return ""
    cleaned = _FEATURING.sub("", raw).strip(" -–—|,.")
    match = _WITH_PERSON.search(cleaned)
    if match and match.group(1).lower() not in _CAST_STOP:
        cleaned = cleaned[: match.start()].strip(" -–—|,.")
    cleaned, _year = strip_title_year_media(cleaned)
    return cleaned or raw


@dataclass(frozen=True)
class CatalogHit:
    title: str
    year: int | None
    media_kind: MediaKind
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    source: str = ""

    def label(self) -> str:
        if self.title and self.year:
            return f"{self.title} ({self.year})"
        return self.title or "that title"

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "year": self.year,
            "mediaType": self.media_kind,
            "tmdbId": self.tmdb_id,
            "tvdbId": self.tvdb_id,
            "imdbId": self.imdb_id,
            "mediaId": self.tmdb_id,
        }


def _year_from(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value if 1900 <= value <= 2100 else None
    text = str(value).strip()
    if _YEAR_RE.match(text):
        return int(text)
    if len(text) >= 4 and _YEAR_RE.match(text[:4]):
        return int(text[:4])
    try:
        year = int(text)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _hit_from_row(
    row: dict[str, Any],
    *,
    media_kind: MediaKind,
    imdb_id: str | None = None,
    source: str = "",
) -> CatalogHit | None:
    title = str(row.get("title") or row.get("name") or "").strip()
    if not title:
        return None
    year = _year_from(row.get("year"))
    if year is None:
        year = _year_from(
            row.get("releaseDate")
            or row.get("firstAirDate")
            or row.get("release_date")
            or row.get("first_air_date")
        )
    tmdb = row.get("tmdbId") or row.get("mediaId") or row.get("id")
    try:
        tmdb_i = int(tmdb) if tmdb not in (None, "") else None
    except (TypeError, ValueError):
        tmdb_i = None
    tvdb = row.get("tvdbId")
    try:
        tvdb_i = int(tvdb) if tvdb not in (None, "") else None
    except (TypeError, ValueError):
        tvdb_i = None
    if row.get("mediaType") in {"movie", "tv"}:
        media_kind = row["mediaType"]  # type: ignore[assignment]
    if row.get("matched") == "fallback":
        return None
    return CatalogHit(
        title=title,
        year=year,
        media_kind=media_kind,
        tmdb_id=tmdb_i,
        tvdb_id=tvdb_i,
        imdb_id=(imdb_id or str(row.get("imdbId") or "") or None),
        source=source,
    )


async def tmdb_find(
    external_id: str,
    *,
    external_source: str = "imdb_id",
) -> dict[str, Any]:
    """TMDB ``/find`` shape via Overseerr search (movie + tv).

    Returns ``{"movie_results": [...], "tv_results": [...]}``.
    Empty dict when nothing is found — never Radarr-searches a ``tt…`` string.
    """
    external_id = (external_id or "").strip()
    if not external_id:
        return {"movie_results": [], "tv_results": []}

    if external_source == "imdb_id":
        try:
            found = await overseerr.search(external_id)
            rows = list(found.get("results") or [])
            movies = [r for r in rows if (r.get("mediaType") or "") == "movie"]
            shows = [r for r in rows if (r.get("mediaType") or "") == "tv"]
            if movies or shows:
                return {"movie_results": movies, "tv_results": shows, "source": "overseerr"}
        except Exception as exc:  # noqa: BLE001
            log.info("overseerr imdb search failed: %s", redact(str(exc)))

    return {"movie_results": [], "tv_results": [], "source": "empty"}


def _hits_from_tmdb_find(
    payload: dict[str, Any],
    *,
    imdb_id: str | None = None,
) -> list[CatalogHit]:
    hits: list[CatalogHit] = []
    for row in payload.get("movie_results") or []:
        if not isinstance(row, dict):
            continue
        norm = {
            "title": row.get("title") or row.get("name"),
            "year": row.get("year")
            or _year_from(row.get("release_date") or row.get("releaseDate")),
            "tmdbId": row.get("tmdbId") or row.get("mediaId") or row.get("id"),
            "mediaType": "movie",
        }
        hit = _hit_from_row(norm, media_kind="movie", imdb_id=imdb_id, source="overseerr")
        if hit:
            hits.append(hit)
    for row in payload.get("tv_results") or []:
        if not isinstance(row, dict):
            continue
        norm = {
            "title": row.get("name") or row.get("title"),
            "year": row.get("year")
            or _year_from(row.get("first_air_date") or row.get("firstAirDate")),
            "tmdbId": row.get("tmdbId") or row.get("mediaId") or row.get("id"),
            "mediaType": "tv",
        }
        hit = _hit_from_row(norm, media_kind="tv", imdb_id=imdb_id, source="overseerr")
        if hit:
            hits.append(hit)
    return hits


async def find_by_imdb(imdb_id: str) -> list[CatalogHit]:
    """Resolve an IMDb id via Overseerr — movie AND tv."""
    imdb_id = (imdb_id or "").strip().lower()
    if not imdb_id:
        return []
    if not imdb_id.startswith("tt"):
        imdb_id = f"tt{imdb_id}" if imdb_id.isdigit() else imdb_id

    payload = await tmdb_find(imdb_id, external_source="imdb_id")
    return _dedupe_hits(_hits_from_tmdb_find(payload, imdb_id=imdb_id))


async def find_by_tmdb(tmdb_id: int, *, media_kind: str = "") -> list[CatalogHit]:
    hits: list[CatalogHit] = []
    try:
        found = await overseerr.search(str(int(tmdb_id)))
        for row in found.get("results") or []:
            mt = str(row.get("mediaType") or "")
            if mt not in {"movie", "tv"}:
                continue
            if media_kind in {"movie", "tv"} and mt != media_kind:
                continue
            rid = row.get("tmdbId") or row.get("mediaId") or row.get("id")
            try:
                if int(rid) != int(tmdb_id):
                    continue
            except (TypeError, ValueError):
                continue
            hit = _hit_from_row(row, media_kind=mt, source="overseerr")  # type: ignore[arg-type]
            if hit:
                hits.append(hit)
    except Exception as exc:  # noqa: BLE001
        log.info("overseerr tmdb lookup failed: %s", redact(str(exc)))

    if not hits and media_kind in {"movie", "tv"}:
        # Still allow direct id queue when Overseerr search is empty but kind is known.
        hits = [
            CatalogHit(
                title=f"TMDB {tmdb_id}",
                year=None,
                media_kind=media_kind,  # type: ignore[arg-type]
                tmdb_id=int(tmdb_id),
                source="parsed",
            )
        ]
    return _dedupe_hits(hits)


async def find_by_tvdb(tvdb_id: int) -> list[CatalogHit]:
    """TVDB id → Overseerr search (numeric / tvdb: term)."""
    hits: list[CatalogHit] = []
    for term in (f"tvdb:{int(tvdb_id)}", str(int(tvdb_id))):
        try:
            found = await overseerr.search(term)
            for row in found.get("results") or []:
                if (row.get("mediaType") or "") != "tv":
                    continue
                if row.get("tvdbId") is not None and int(row["tvdbId"]) != int(tvdb_id):
                    continue
                hit = _hit_from_row(row, media_kind="tv", source="overseerr")
                if hit:
                    hits.append(hit)
        except Exception as exc:  # noqa: BLE001
            log.info("overseerr tvdb lookup failed: %s", redact(str(exc)))
        if hits:
            break
    return _dedupe_hits(hits)


def _filter_title_year(
    hits: list[CatalogHit],
    *,
    title: str,
    year: int | None,
    strict: bool = False,
) -> list[CatalogHit]:
    """Prefer exact / franchise-prefix matches. Never keep bare substring hits.

    ``Land`` must not become ``La La Land``; ``Wild`` must not become
    ``The Wild Robot``. ``strict`` is retained for callers that already know
    the model named a specific title (+ people/year).
    """
    del strict  # exact-or-prefix is always required now
    needle = normalize_title(title)
    needle_loose = re.sub(r"[^a-z0-9à-ÿ]+", " ", needle).strip()

    def _loose(value: str) -> str:
        return re.sub(r"[^a-z0-9à-ÿ]+", " ", normalize_title(value)).strip()

    exact = [h for h in hits if normalize_title(h.title) == needle]
    if not exact:
        exact = [h for h in hits if _loose(h.title) == needle_loose]
    if exact:
        hits = exact
    else:
        prefix = [h for h in hits if catalog_seed_matches_title(title, h.title)]
        if not prefix:
            return []
        hits = prefix

    if year is not None:
        year_hits = [h for h in hits if h.year == year]
        if year_hits:
            return year_hits
        # Exact title, wrong year editions → keep for disambiguation.
        if hits:
            return hits
        return []

    return hits


def catalog_seed_matches_title(seed: str, title: str) -> bool:
    """True when a catalog row is a real match for the search seed.

    Exact title, or multi-word franchise prefix (Harry Potter → Chamber…).
    Single-token seeds must be exact — ``Land`` must not match ``La La Land``,
    ``Wild`` must not match ``The Wild Robot``.
    """
    def _tokens(value: str) -> list[str]:
        text = re.sub(r"[^a-z0-9à-ÿ]+", " ", normalize_title(value)).strip()
        return [t for t in text.split() if t]

    seed_tokens = _tokens(seed)
    title_tokens = _tokens(title)
    if not seed_tokens or not title_tokens:
        return False
    if seed_tokens == title_tokens:
        return True

    # Drop leading articles for equality / prefix checks.
    def _strip_articles(tokens: list[str]) -> list[str]:
        while tokens and tokens[0] in {"the", "a", "an", "de", "het", "een"}:
            tokens = tokens[1:]
        return tokens

    seed_tokens = _strip_articles(seed_tokens)
    title_tokens = _strip_articles(title_tokens)
    if not seed_tokens or not title_tokens:
        return False
    if seed_tokens == title_tokens:
        return True
    # Multi-word seed as leading phrase (franchise / longer official title).
    if len(seed_tokens) >= 2 and title_tokens[: len(seed_tokens)] == seed_tokens:
        return True
    return False


async def resolve_title(
    title: str,
    *,
    year: int | None = None,
    media_kind: str = "",
    strict: bool = False,
) -> list[CatalogHit]:
    """Search Overseerr by title (movie + TV). Prefer catalog year."""
    title = catalog_search_title(title)
    if not title:
        return []

    hits: list[CatalogHit] = []
    try:
        found = await overseerr.search(title)
        for row in found.get("results") or []:
            mt = str(row.get("mediaType") or "")
            if mt not in {"movie", "tv"}:
                continue
            hit = _hit_from_row(row, media_kind=mt, source="overseerr")  # type: ignore[arg-type]
            if hit:
                hits.append(hit)
    except Exception as exc:  # noqa: BLE001
        log.info("overseerr title search failed: %s", redact(str(exc)))

    hits = _dedupe_hits(hits)
    hits = _filter_title_year(hits, title=title, year=year, strict=strict)
    return prefer_hits(hits, implied_kind=media_kind if media_kind in {"movie", "tv"} else "")


def prefer_hits(
    hits: list[CatalogHit],
    *,
    implied_kind: str = "",
) -> list[CatalogHit]:
    """If both movie and TV exist, prefer the implied type; else disambiguate."""
    if not hits:
        return []
    if implied_kind in {"movie", "tv"}:
        typed = [h for h in hits if h.media_kind == implied_kind]
        if typed:
            return typed
        # Implied kind missed (e.g. movie ask for a TV-only title) — keep the rest.
    kinds = {h.media_kind for h in hits}
    if len(kinds) > 1:
        # Ambiguous type — keep one of each kind for disambiguation (title+year+type).
        by_kind: dict[str, CatalogHit] = {}
        for hit in hits:
            by_kind.setdefault(hit.media_kind, hit)
        return list(by_kind.values())
    # Same title, different years — keep distinct years (cap 3).
    by_year: dict[int | None, CatalogHit] = {}
    for hit in hits:
        by_year.setdefault(hit.year, hit)
    return list(by_year.values())[:3]


def _dedupe_hits(hits: list[CatalogHit]) -> list[CatalogHit]:
    seen: set[tuple[str, int | None, str, int | None]] = set()
    out: list[CatalogHit] = []
    for hit in hits:
        key = (normalize_title(hit.title), hit.year, hit.media_kind, hit.tmdb_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def hit_to_parsed(
    hit: CatalogHit,
    *,
    base: ParsedRequest | None = None,
    reason: str = "catalog_resolve",
) -> ParsedRequest:
    return ParsedRequest(
        kind="request",
        media_kind=hit.media_kind,
        title=hit.title,
        year=hit.year,
        imdb_id=hit.imdb_id or (base.imdb_id if base else None),
        tmdb_id=hit.tmdb_id,
        tvdb_id=hit.tvdb_id or (base.tvdb_id if base else None),
        season=base.season if base else None,
        episode=base.episode if base else None,
        quality=base.quality if base else None,
        catalog_host=base.catalog_host if base else None,
        reason=reason,
        raw_text=base.raw_text if base else "",
    )


async def resolve_parsed(parsed: ParsedRequest) -> tuple[list[CatalogHit], str]:
    """Resolve a parsed catalog id / title into CatalogHit(s) via Overseerr.

    Returns ``(hits, error_label)``. ``error_label`` is a human title for
    not-found messages — never a raw ``tt…`` id.
    """
    implied = parsed.media_kind if parsed.media_kind in {"movie", "tv"} else ""

    if parsed.imdb_id:
        hits = prefer_hits(await find_by_imdb(parsed.imdb_id), implied_kind=implied)
        label = hits[0].label() if len(hits) == 1 else (hits[0].title if hits else "that title")
        return hits, label

    if parsed.tmdb_id:
        hits = prefer_hits(
            await find_by_tmdb(parsed.tmdb_id, media_kind=implied),
            implied_kind=implied,
        )
        if not hits and implied:
            hits = [
                CatalogHit(
                    title=parsed.title or f"TMDB {parsed.tmdb_id}",
                    year=parsed.year,
                    media_kind=implied,  # type: ignore[arg-type]
                    tmdb_id=parsed.tmdb_id,
                    source="parsed",
                )
            ]
        label = hits[0].label() if len(hits) == 1 else (parsed.title or "that title")
        return hits, label

    if parsed.tvdb_id:
        hits = prefer_hits(await find_by_tvdb(parsed.tvdb_id), implied_kind="tv")
        label = hits[0].label() if len(hits) == 1 else (parsed.title or "that title")
        return hits, label

    if parsed.title:
        search = catalog_search_title(parsed.title)
        hits = await resolve_title(
            search,
            year=parsed.year,
            media_kind=implied,
        )
        label = (
            f"{search} ({parsed.year})"
            if parsed.year and search
            else (search or parsed.title or "that title")
        )
        return hits, label

    return [], "that title"


__all__ = [
    "CatalogHit",
    "catalog_search_title",
    "catalog_seed_matches_title",
    "find_by_imdb",
    "find_by_tmdb",
    "find_by_tvdb",
    "hit_to_parsed",
    "prefer_hits",
    "resolve_parsed",
    "resolve_title",
    "tmdb_find",
]
