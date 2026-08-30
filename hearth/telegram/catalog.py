"""Resolve catalog ids and titles to TMDB-backed title + year + media kind.

Never search a raw ``tt…`` / bare id string as a movie title. Prefer TMDB
primary release / first_air_date year. Uses Overseerr when configured (it
speaks TMDB), plus Radarr/Sonarr ``imdb:`` / ``tmdb:`` / ``tvdb:`` lookups —
no new env keys.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.parse import ParsedRequest, normalize_title
from hearth.tools.arr import overseerr, radarr, sonarr

log = logging.getLogger("hearth.telegram")

MediaKind = Literal["movie", "tv"]

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


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
    # ISO date → first four digits
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
    # Overseerr / TMDB find rows: id is tmdb; ignore non-media junk.
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
    """TMDB ``/find`` shape. Prefer Overseerr live proxy; mockable in tests.

    Returns ``{"movie_results": [...], "tv_results": [...]}`` (TMDB layout).
    Empty dict when nothing is configured / found — callers fall back to *arr.
    """
    external_id = (external_id or "").strip()
    if not external_id:
        return {"movie_results": [], "tv_results": []}

    # Overseerr does not expose /find publicly; when live, probe movie+tv via
    # its search + detail endpoints is lossy for IMDb. Leave a hook for tests
    # and optional future proxy. Production path uses *arr imdb: below.
    if settings.overseerr_configured and external_source == "imdb_id":
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
        # Normalize TMDB find movie shape → title/year/tmdbId
        norm = {
            "title": row.get("title") or row.get("name"),
            "year": row.get("year")
            or _year_from(row.get("release_date") or row.get("releaseDate")),
            "tmdbId": row.get("tmdbId") or row.get("mediaId") or row.get("id"),
            "mediaType": "movie",
        }
        hit = _hit_from_row(norm, media_kind="movie", imdb_id=imdb_id, source="tmdb_find")
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
        hit = _hit_from_row(norm, media_kind="tv", imdb_id=imdb_id, source="tmdb_find")
        if hit:
            hits.append(hit)
    return hits


async def _arr_imdb_hits(imdb_id: str) -> list[CatalogHit]:
    """Radarr + Sonarr ``imdb:`` lookups (TMDB-backed metadata, existing keys)."""
    hits: list[CatalogHit] = []
    term = imdb_id if imdb_id.lower().startswith("imdb:") else f"imdb:{imdb_id}"
    try:
        movie = await radarr.search(term)
        for row in movie.get("results") or []:
            hit = _hit_from_row(row, media_kind="movie", imdb_id=imdb_id, source="radarr")
            if hit:
                hits.append(hit)
    except Exception as exc:  # noqa: BLE001
        log.info("radarr imdb lookup failed: %s", redact(str(exc)))
    try:
        show = await sonarr.search(term)
        for row in show.get("results") or []:
            hit = _hit_from_row(row, media_kind="tv", imdb_id=imdb_id, source="sonarr")
            if hit:
                hits.append(hit)
    except Exception as exc:  # noqa: BLE001
        log.info("sonarr imdb lookup failed: %s", redact(str(exc)))
    return hits


async def find_by_imdb(imdb_id: str) -> list[CatalogHit]:
    """Resolve an IMDb id to catalog hit(s) — movie AND tv."""
    imdb_id = (imdb_id or "").strip().lower()
    if not imdb_id:
        return []
    if not imdb_id.startswith("tt"):
        imdb_id = f"tt{imdb_id}" if imdb_id.isdigit() else imdb_id

    payload = await tmdb_find(imdb_id, external_source="imdb_id")
    hits = _hits_from_tmdb_find(payload, imdb_id=imdb_id)
    if hits:
        return _dedupe_hits(hits)

    return _dedupe_hits(await _arr_imdb_hits(imdb_id))


async def find_by_tmdb(tmdb_id: int, *, media_kind: str = "") -> list[CatalogHit]:
    hits: list[CatalogHit] = []
    kinds: list[MediaKind]
    if media_kind in {"movie", "tv"}:
        kinds = [media_kind]  # type: ignore[list-item]
    else:
        kinds = ["movie", "tv"]

    if settings.overseerr_configured or True:
        # Overseerr / mock search by numeric id often fails; use *arr tmdb: + overseerr detail.
        pass

    if "movie" in kinds:
        try:
            found = await radarr.search(f"tmdb:{int(tmdb_id)}")
            for row in found.get("results") or []:
                if row.get("tmdbId") == int(tmdb_id) or not row.get("tmdbId"):
                    hit = _hit_from_row(
                        {**row, "tmdbId": row.get("tmdbId") or tmdb_id},
                        media_kind="movie",
                        source="radarr",
                    )
                    if hit:
                        hits.append(hit)
        except Exception as exc:  # noqa: BLE001
            log.info("radarr tmdb lookup failed: %s", redact(str(exc)))

    if "tv" in kinds and settings.overseerr_configured:
        try:
            found = await overseerr.search(str(tmdb_id))
            for row in found.get("results") or []:
                if (row.get("mediaType") or "") != "tv":
                    continue
                if row.get("tmdbId") == int(tmdb_id) or row.get("mediaId") == int(tmdb_id):
                    hit = _hit_from_row(row, media_kind="tv", source="overseerr")
                    if hit:
                        hits.append(hit)
        except Exception as exc:  # noqa: BLE001
            log.info("overseerr tmdb tv lookup failed: %s", redact(str(exc)))

    return _dedupe_hits(hits)


async def find_by_tvdb(tvdb_id: int) -> list[CatalogHit]:
    hits: list[CatalogHit] = []
    try:
        found = await sonarr.search(str(int(tvdb_id)))
        for row in found.get("results") or []:
            if row.get("tvdbId") == int(tvdb_id):
                hit = _hit_from_row(row, media_kind="tv", source="sonarr")
                if hit:
                    hits.append(hit)
        if not hits:
            broad = await sonarr.search("")
            for row in broad.get("results") or []:
                if row.get("tvdbId") == int(tvdb_id):
                    hit = _hit_from_row(row, media_kind="tv", source="sonarr")
                    if hit:
                        hits.append(hit)
    except Exception as exc:  # noqa: BLE001
        log.info("sonarr tvdb lookup failed: %s", redact(str(exc)))
    return _dedupe_hits(hits)


async def resolve_title(
    title: str,
    *,
    year: int | None = None,
    media_kind: str = "",
) -> list[CatalogHit]:
    """Search by title; prefer TMDB/catalog year. Multiple years → all returned."""
    title = (title or "").strip()
    if not title:
        return []

    hits: list[CatalogHit] = []
    want_movie = media_kind != "tv"
    want_tv = media_kind != "movie"

    if settings.overseerr_configured or settings.telegram_prefer_overseerr:
        try:
            found = await overseerr.search(title)
            for row in found.get("results") or []:
                mt = str(row.get("mediaType") or "")
                if mt == "movie" and want_movie:
                    hit = _hit_from_row(row, media_kind="movie", source="overseerr")
                    if hit:
                        hits.append(hit)
                elif mt == "tv" and want_tv:
                    hit = _hit_from_row(row, media_kind="tv", source="overseerr")
                    if hit:
                        hits.append(hit)
        except Exception as exc:  # noqa: BLE001
            log.info("overseerr title search failed: %s", redact(str(exc)))

    if want_movie and not hits:
        try:
            found = await radarr.search(title)
            for row in found.get("results") or []:
                hit = _hit_from_row(row, media_kind="movie", source="radarr")
                if hit:
                    hits.append(hit)
        except Exception as exc:  # noqa: BLE001
            log.info("radarr title search failed: %s", redact(str(exc)))

    if want_tv and (not hits or media_kind == "tv"):
        try:
            found = await sonarr.search(title)
            for row in found.get("results") or []:
                hit = _hit_from_row(row, media_kind="tv", source="sonarr")
                if hit:
                    hits.append(hit)
        except Exception as exc:  # noqa: BLE001
            log.info("sonarr title search failed: %s", redact(str(exc)))

    hits = _dedupe_hits(hits)
    hits = [h for h in hits if True]  # matched=fallback already dropped in _hit_from_row
    # Prefer exact title matches (normalize punctuation so "can't" ≈ "Can't").
    needle = normalize_title(title)
    needle_loose = re.sub(r"[^a-z0-9à-ÿ]+", " ", needle).strip()

    def _loose(value: str) -> str:
        return re.sub(r"[^a-z0-9à-ÿ]+", " ", normalize_title(value)).strip()

    exact = [h for h in hits if normalize_title(h.title) == needle]
    if not exact:
        exact = [h for h in hits if _loose(h.title) == needle_loose]
    if exact:
        hits = exact

    if year is not None:
        year_hits = [h for h in hits if h.year == year]
        if year_hits:
            return year_hits
        # Catalog disagrees with the requested year — return other years for
        # disambiguation rather than grabbing the wrong edition.
        if hits:
            return hits
        return []

    return hits


def prefer_hits(
    hits: list[CatalogHit],
    *,
    implied_kind: str = "",
) -> list[CatalogHit]:
    """If both movie and TV exist, prefer the implied type (IMDb/TMDB URL)."""
    if not hits:
        return []
    if implied_kind in {"movie", "tv"}:
        typed = [h for h in hits if h.media_kind == implied_kind]
        if typed:
            return typed
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
    """Resolve a parsed catalog id / title into CatalogHit(s).

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
            # Still allow direct id queue when lookup is empty but kind is known.
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
        hits = prefer_hits(
            await resolve_title(
                parsed.title,
                year=parsed.year,
                media_kind=implied,
            ),
            implied_kind=implied,
        )
        label = (
            f"{parsed.title} ({parsed.year})"
            if parsed.year
            else (parsed.title or "that title")
        )
        return hits, label

    return [], "that title"


__all__ = [
    "CatalogHit",
    "find_by_imdb",
    "find_by_tmdb",
    "find_by_tvdb",
    "hit_to_parsed",
    "prefer_hits",
    "resolve_parsed",
    "resolve_title",
    "tmdb_find",
]
