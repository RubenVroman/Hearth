"""Thin media tools for the Telegram inbox — catalog lookup + Overseerr queue.

Python never invents titles. After the model names one, these helpers:
- exact / franchise-prefix match only (``catalog_seed_matches_title``)
- TMDB / IMDb / TVDB resolve via Overseerr
- Overseerr request for queueing

Never substring-grab (Land ≠ La La Land, Wild ≠ The Wild Robot).
"""

from __future__ import annotations

import logging
from typing import Any

from hearth.memory.redact import redact
from hearth.telegram.catalog import (
    CatalogHit,
    catalog_search_title,
    catalog_seed_matches_title,
    hit_to_parsed,
    resolve_parsed,
    resolve_title,
)
from hearth.telegram.parse import ParsedRequest, normalize_title
from hearth.tools.arr import overseerr, radarr, sonarr

log = logging.getLogger("hearth.telegram")

MAX_CANDIDATES = 12
MAX_BATCH = 10

# Reply fragments that must never leave the bot as a first/canned answer.
BANNED_REPLY_FRAGMENTS = (
    "any year, actor, or other clue",
    "want another in that vibe",
    "send an imdb/tmdb link",
    "send an imdb/tmdb link?",
    "send the title if you know it",
    "send the title",
    "reply 1–1",
    "reply 1-1",
)


def reply_is_banned(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(bit in lowered for bit in BANNED_REPLY_FRAGMENTS)


def dedupe_choice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse indistinguishable options (same title+year+kind)."""
    best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str, str]] = []
    for row in rows:
        title = normalize_title(str(row.get("title") or ""))
        year = str(row.get("year") or "")
        kind = str(
            row.get("mediaType")
            or row.get("media_kind")
            or ("tv" if row.get("tvdbId") else "movie")
        )
        tmdb = row.get("tmdbId") or row.get("mediaId")
        tvdb = row.get("tvdbId")
        imdb = str(row.get("imdbId") or "").lower()
        if tmdb not in (None, ""):
            id_key = f"tmdb:{tmdb}"
        elif tvdb not in (None, ""):
            id_key = f"tvdb:{tvdb}"
        elif imdb:
            id_key = f"imdb:{imdb}"
        else:
            id_key = ""
        display_key = (title, year, kind, "")
        id_full = (title, year, kind, id_key)
        key = display_key if display_key[0] else id_full
        if key not in best:
            best[key] = row
            order.append(key)
            continue
        prev = best[key]
        prev_id = prev.get("tmdbId") or prev.get("mediaId") or prev.get("tvdbId")
        new_id = tmdb or tvdb
        prev_pop = prev.get("popularity") or prev.get("voteCount") or 0
        new_pop = row.get("popularity") or row.get("voteCount") or 0
        if (not prev_id and new_id) or (new_pop and new_pop > (prev_pop or 0)):
            best[key] = row
    return [best[k] for k in order]


def choices_are_indistinguishable(choices: list[dict[str, Any]]) -> bool:
    if len(choices) <= 1:
        return True
    labels = {
        (
            normalize_title(str(c.get("title") or "")),
            str(c.get("year") or ""),
            str(c.get("mediaType") or ""),
        )
        for c in choices
    }
    return len(labels) == 1


def filter_seed_rows(
    rows: list[dict[str, Any]],
    seed: str,
    *,
    rejected_titles: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep only exact / franchise-prefix matches for ``seed``."""
    seed = (seed or "").strip()
    if not seed:
        return []
    grounded = [
        row
        for row in rows
        if catalog_seed_matches_title(seed, str(row.get("title") or ""))
    ]
    rows = dedupe_choice_rows(grounded)[:MAX_CANDIDATES]
    rejected_norm = {
        normalize_title(t) for t in (rejected_titles or []) if str(t).strip()
    }
    if not rejected_norm:
        return rows
    kept: list[dict[str, Any]] = []
    for row in rows:
        title_n = normalize_title(str(row.get("title") or ""))
        rejected_hit = title_n in rejected_norm or any(
            (title_n in r or r in title_n) for r in rejected_norm if r
        )
        if rejected_hit and not catalog_seed_matches_title(
            seed, str(row.get("title") or "")
        ):
            continue
        kept.append(row)
    return kept


async def tool_lookup_title(
    title: str,
    *,
    year: int | None = None,
    media_kind: str = "",
    strict: bool = True,
) -> list[dict[str, Any]]:
    """Tool: resolve a model-named title via Overseerr (exact/prefix only)."""
    search = catalog_search_title(title) or (title or "").strip()
    if not search:
        return []
    try:
        hits = await resolve_title(
            search,
            year=year,
            media_kind=media_kind if media_kind in {"movie", "tv"} else "",
            strict=strict,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("tool lookup_title failed: %s", redact(str(exc)))
        return []
    rows = dedupe_choice_rows([h.as_dict() for h in hits])
    return filter_seed_rows(rows, search)


async def tool_lookup_parsed(parsed: ParsedRequest) -> tuple[list[CatalogHit], str]:
    """Tool: resolve a parsed catalog id / Title (YYYY) into hits."""
    try:
        return await resolve_parsed(parsed)
    except Exception as exc:  # noqa: BLE001
        log.info("tool lookup_parsed failed: %s", redact(str(exc)))
        return [], parsed.display_label() or parsed.title or "that"


async def tool_request(
    *,
    title: str,
    year: int | None = None,
    media_type: str = "movie",
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
) -> dict[str, Any]:
    """Tool: queue via Overseerr by id when known, else title search + seed filter."""
    media_type = media_type if media_type in {"movie", "tv"} else "movie"
    progress_service = "radarr" if media_type == "movie" else "sonarr"

    # Already downloading via *arr?
    try:
        client = radarr if progress_service == "radarr" else sonarr
        queued = await client.queue(title)
        if queued.get("downloads"):
            return {
                "ok": False,
                "already": True,
                "queued": True,
                "title": title,
                "year": year,
                "service": progress_service,
            }
    except Exception:  # noqa: BLE001
        pass

    from hearth.fixtures import pipeline

    if any(
        normalize_title(str(row.get("title") or row.get("name") or ""))
        == normalize_title(title)
        for row in pipeline.overseerr_queue
    ):
        return {
            "ok": False,
            "already": True,
            "queued": True,
            "title": title,
            "year": year,
            "service": progress_service,
        }

    media_id = tmdb_id
    pick_title = title
    pick_year = year
    pick_type = media_type

    if media_id is None:
        query = catalog_search_title(title) or title
        found = await overseerr.search(query) if query else {"results": []}
        hits = [
            row
            for row in (found.get("results") or [])
            if row.get("matched") != "fallback"
        ]
        if media_type in {"movie", "tv"}:
            typed = [row for row in hits if (row.get("mediaType") or "") == media_type]
            if typed:
                hits = typed
        seeded = filter_seed_rows(hits, query)
        if year is not None:
            year_hits = [
                row
                for row in seeded
                if str(row.get("year") or "") == str(year)
            ]
            if year_hits:
                seeded = year_hits
        if not seeded:
            return {
                "ok": False,
                "not_found": True,
                "title": title,
                "year": year,
                "media_type": media_type,
            }
        if len(seeded) > 1 and not choices_are_indistinguishable(seeded):
            return {
                "ok": False,
                "ambiguous": True,
                "choices": seeded[:MAX_CANDIDATES],
                "title": title,
                "year": year,
                "media_type": media_type,
            }
        pick = seeded[0]
        pick_title = str(pick.get("title") or title)
        pick_year = (
            int(pick["year"])
            if pick.get("year") not in (None, "")
            else year
        )
        pick_type = str(pick.get("mediaType") or media_type)
        if pick_type not in {"movie", "tv"}:
            pick_type = media_type
        media_id = pick.get("mediaId") or pick.get("tmdbId")
        if pick.get("inLibrary"):
            return {
                "ok": False,
                "already": True,
                "library": True,
                "title": pick_title,
                "year": pick_year,
                "service": "radarr" if pick_type == "movie" else "sonarr",
            }

    result = await overseerr.request(
        pick_title,
        media_id=int(media_id) if media_id not in (None, "") else None,
        media_type=pick_type,
    )
    if result.get("ok") is False:
        return {
            "ok": False,
            "not_found": True,
            "title": pick_title,
            "year": pick_year,
            "media_type": pick_type,
        }
    return {
        "ok": True,
        "title": pick_title,
        "year": pick_year,
        "media_type": pick_type,
        "media_id": media_id,
        "tvdb_id": tvdb_id,
        "service": "radarr" if pick_type == "movie" else "sonarr",
    }


def row_to_parsed(
    row: dict[str, Any],
    *,
    query: str = "",
    media_kind_hint: str = "",
) -> ParsedRequest:
    kind = str(
        media_kind_hint
        or row.get("mediaType")
        or row.get("media_kind")
        or ("tv" if row.get("tvdbId") else "movie")
    )
    if kind not in {"movie", "tv"}:
        kind = "movie"
    title = str(row.get("title") or query or "Untitled")
    year = int(row["year"]) if row.get("year") not in (None, "") else None
    tmdb = row.get("tmdbId") or row.get("mediaId")
    tvdb = row.get("tvdbId")
    return ParsedRequest(
        kind="request",
        media_kind=kind,  # type: ignore[arg-type]
        title=title,
        year=year,
        tmdb_id=int(tmdb) if tmdb not in (None, "") else None,
        tvdb_id=int(tvdb) if tvdb not in (None, "") else None,
        reason="tool_row",
    )


__all__ = [
    "BANNED_REPLY_FRAGMENTS",
    "MAX_BATCH",
    "MAX_CANDIDATES",
    "CatalogHit",
    "choices_are_indistinguishable",
    "dedupe_choice_rows",
    "filter_seed_rows",
    "hit_to_parsed",
    "reply_is_banned",
    "row_to_parsed",
    "tool_lookup_parsed",
    "tool_lookup_title",
    "tool_request",
]
