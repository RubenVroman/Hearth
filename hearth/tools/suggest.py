"""Resolve recommended movie/TV titles into overlay-ready metadata.

Hearth (or the local intent router) passes title names — often from web-sourced
or conversational recommendations. This module looks them up via Overseerr /
Radarr / Sonarr (keys stay on VAULT) and returns the same ``results`` shape the
glass media overlay already consumes. Never sends API keys to the browser.
"""

from __future__ import annotations

import re
from typing import Any

from hearth.fixtures import MOCK_RADARR_LOOKUP, MOCK_SONARR_LOOKUP, pipeline
from hearth.tools.arr import overseerr, radarr, sonarr
from hearth.tools.media_art import enrich_media_hit

MAX_TITLES = 6
DEFAULT_LIMIT = 4
SPEAK_LEN = 720

_WS = re.compile(r"\s+")
_YEAR = re.compile(r"\((\d{4})\)\s*$")
_SPLIT = re.compile(r"\s*(?:,|;|\n|\||/|•|·|\band\b)\s*", re.I)

# Curated packs for local "suggest … movies" when no explicit title list is given.
# Resolved through the same Overseerr/*arr path so posters/ids stay consistent.
_QUERY_PACKS: list[tuple[tuple[str, ...], list[str]]] = [
    (
        ("sci-fi", "scifi", "science fiction", "mind-bending", "space"),
        ["Dune: Part Two", "The Endless", "Annihilation"],
    ),
    (
        ("horror", "scary", "cult"),
        ["The Endless", "Annihilation"],
    ),
    (
        ("architecture", "drama", "serious"),
        ["The Brutalist", "Dune: Part Two"],
    ),
    (
        ("tv", "show", "series", "streaming"),
        ["Severance", "Slow Horses"],
    ),
]

_DEFAULT_MOVIE_PACK = ["Dune: Part Two", "The Endless", "The Brutalist", "Annihilation"]
_DEFAULT_TV_PACK = ["Severance", "Slow Horses"]


def _clip(text: str, limit: int) -> str:
    text = _WS.sub(" ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _normalize_media_type(raw: Any) -> str:
    kind = str(raw or "any").strip().lower().replace("-", " ")
    if kind in {"tv", "show", "shows", "series", "television"}:
        return "tv"
    if kind in {"movie", "movies", "film", "films"}:
        return "movie"
    return "any"


def parse_title_list(raw: Any) -> list[str]:
    """Accept a list, JSON-ish string, or comma/newline/numbered titles."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("name") or "").strip()
                year = item.get("year")
                if title and year is not None and not _YEAR.search(title):
                    title = f"{title} ({year})"
                if title:
                    out.append(title)
            else:
                title = _WS.sub(" ", str(item or "")).strip(" .\"'")
                if title:
                    out.append(title)
        return out[:MAX_TITLES]
    text = str(raw).strip()
    if not text:
        return []

    def _clean_bit(chunk: str) -> str:
        cleaned = re.sub(r"^\s*\d+[\).\]]\s*", "", chunk)
        cleaned = _WS.sub(" ", cleaned).strip(" .\"'-•·")
        cleaned = re.sub(
            r"^(?:top picks?|recommendations?|suggestions?|here(?:'s| is)|try)\s*:?\s*",
            "",
            cleaned,
            flags=re.I,
        ).strip(" .\"'-•·")
        cleaned = re.sub(r"^\d+[\).\]]\s*", "", cleaned).strip(" .\"'-•·")
        return cleaned

    # Numbered lists (inline or multiline): "1. Foo 2. Bar".
    if re.search(r"(?:^|\s)\d+[\).\]]\s+\S", text):
        chunks = re.split(r"(?:^|\s)\d+[\).\]]\s+", text)
        out = []
        for chunk in chunks:
            cleaned = _clean_bit(chunk)
            if cleaned and cleaned.lower() not in {"and", "or", "also", "top", "picks", "pick"}:
                out.append(cleaned)
            if len(out) >= MAX_TITLES:
                break
        if len(out) >= 2:
            return out[:MAX_TITLES]

    # "Title (YYYY)" extractions for prose lists without numbers.
    year_hits = re.findall(r"\b([A-Z][^(\n]{1,60}?)\s*\((\d{4})\)", text)
    if len(year_hits) >= 2:
        cleaned_years: list[str] = []
        for title, year in year_hits[:MAX_TITLES]:
            bit = _clean_bit(title)
            # Drop prose openings that aren't a title.
            if not bit or ":" in bit or len(bit) > 60:
                continue
            cleaned_years.append(f"{bit} ({year})")
        if len(cleaned_years) >= 2:
            return cleaned_years[:MAX_TITLES]

    if "\n" in text:
        chunks = re.split(r"[\n]+", text)
    else:
        chunks = _SPLIT.split(text)
    out = []
    for chunk in chunks:
        cleaned = _clean_bit(chunk)
        if cleaned and cleaned.lower() not in {"and", "or", "also", "top", "picks", "pick"}:
            out.append(cleaned)
        if len(out) >= MAX_TITLES:
            break
    return out


def split_title_year(raw: str) -> tuple[str, int | None]:
    text = _WS.sub(" ", (raw or "").strip())
    match = _YEAR.search(text)
    if not match:
        return text, None
    title = text[: match.start()].strip(" -–—")
    try:
        return title or text, int(match.group(1))
    except ValueError:
        return title or text, None


def titles_for_query(query: str, media_type: str, *, limit: int) -> list[str]:
    """Pick a short list of speakable titles for a freeform recommendation ask."""
    lowered = (query or "").strip().lower()
    if media_type == "tv":
        pack = list(_DEFAULT_TV_PACK)
    else:
        pack = list(_DEFAULT_MOVIE_PACK)
    if lowered:
        for keys, titles in _QUERY_PACKS:
            if any(key in lowered for key in keys):
                pack = list(titles)
                break
    # Prefer TV pack when query clearly asks for shows but type was "any".
    if media_type == "any" and re.search(r"\b(tv|shows?|series)\b", lowered):
        pack = list(_DEFAULT_TV_PACK)
    return pack[: max(1, min(limit, MAX_TITLES))]


def catalog_links(*, media_type: str, tmdb_id: int | None, imdb_id: str | None = None) -> dict[str, str]:
    """Public catalog URLs only — no API keys or house tokens."""
    links: dict[str, str] = {}
    if tmdb_id is not None:
        kind = "tv" if media_type in {"tv", "show"} else "movie"
        links["tmdb"] = f"https://www.themoviedb.org/{kind}/{int(tmdb_id)}"
    if imdb_id:
        iid = str(imdb_id).strip()
        if not iid.startswith("tt"):
            iid = f"tt{iid}"
        links["imdb"] = f"https://www.imdb.com/title/{iid}/"
    return links


def _score_hit(hit: dict[str, Any], *, needle: str, year: int | None) -> int:
    title = str(hit.get("title") or hit.get("name") or "").strip().lower()
    score = 0
    if title == needle:
        score += 100
    elif needle and needle in title:
        score += 60
    elif title and title in needle:
        score += 40
    hit_year = hit.get("year")
    try:
        hit_year_n = int(hit_year) if hit_year is not None else None
    except (TypeError, ValueError):
        hit_year_n = None
    if year is not None and hit_year_n == year:
        score += 30
    if hit.get("matched") == "fallback":
        score -= 50
    if hit.get("tmdbId") or hit.get("mediaId") or hit.get("tvdbId"):
        score += 5
    return score


def _pick_best(hits: list[dict[str, Any]], *, needle: str, year: int | None) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = -10_000
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        score = _score_hit(hit, needle=needle, year=year)
        if score > best_score:
            best = hit
            best_score = score
    if best is None or best_score < 0:
        return None
    return best


def _skeleton(title: str, *, year: int | None, media_type: str) -> dict[str, Any]:
    kind = "show" if media_type == "tv" else "movie"
    return {
        "title": title,
        "year": year,
        "type": kind,
        "mediaType": "tv" if kind == "show" else "movie",
        "summary": "",
        "overview": "",
        "tmdbId": None,
        "posterPath": None,
        "skeleton": True,
        "source": "suggest",
        "links": {},
    }


def _normalize_hit(hit: dict[str, Any], *, asked: str, media_type: str) -> dict[str, Any]:
    title = str(hit.get("title") or hit.get("name") or asked).strip() or asked
    year = hit.get("year")
    try:
        year_n = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_n = None

    raw_type = str(hit.get("mediaType") or hit.get("type") or media_type or "movie").lower()
    if raw_type in {"tv", "show", "series"}:
        kind = "show"
        tmdb_kind = "tv"
    else:
        kind = "movie"
        tmdb_kind = "movie"

    tmdb = hit.get("tmdbId") if hit.get("tmdbId") is not None else hit.get("mediaId")
    try:
        tmdb_id = int(tmdb) if tmdb is not None else None
    except (TypeError, ValueError):
        tmdb_id = None

    overview = str(hit.get("overview") or hit.get("summary") or "").strip()
    art = enrich_media_hit(
        {
            "tmdbId": tmdb_id,
            "posterPath": hit.get("posterPath") or hit.get("poster_path"),
        }
    )
    imdb = hit.get("imdbId") or hit.get("imdb_id")
    links = catalog_links(media_type=tmdb_kind, tmdb_id=tmdb_id, imdb_id=imdb if imdb else None)
    out = {
        "title": title,
        "year": year_n,
        "type": kind,
        "mediaType": tmdb_kind,
        "summary": overview[:220],
        "overview": overview[:280],
        "tmdbId": art.get("tmdbId") if art.get("tmdbId") is not None else tmdb_id,
        "posterPath": art.get("posterPath"),
        "thumb": bool(art.get("thumb")),
        "inLibrary": bool(hit.get("inLibrary")),
        "skeleton": False,
        "source": "suggest",
        "links": links,
    }
    if hit.get("tvdbId") is not None:
        out["tvdbId"] = hit.get("tvdbId")
    if hit.get("ratingKey") is not None:
        out["ratingKey"] = str(hit.get("ratingKey"))
    return out


async def _search_overseerr(title: str) -> list[dict[str, Any]]:
    try:
        payload = await overseerr.search(title)
    except Exception:  # noqa: BLE001
        return []
    return list(payload.get("results") or [])


async def _search_radarr(title: str) -> list[dict[str, Any]]:
    try:
        payload = await radarr.search(title)
    except Exception:  # noqa: BLE001
        return []
    return list(payload.get("results") or [])


async def _search_sonarr(title: str) -> list[dict[str, Any]]:
    try:
        payload = await sonarr.search(title)
    except Exception:  # noqa: BLE001
        return []
    return list(payload.get("results") or [])


def _mock_fallback(title: str, year: int | None, media_type: str) -> dict[str, Any] | None:
    """When *arr lookup misses, still return a card from fixture catalogs if possible."""
    needle = title.lower()
    catalogs: list[dict[str, Any]] = []
    if media_type in {"movie", "any"}:
        catalogs.extend(MOCK_RADARR_LOOKUP)
    if media_type in {"tv", "any"}:
        catalogs.extend(MOCK_SONARR_LOOKUP)
    # Also try Overseerr fixtures via pipeline (keeps TMDB ids for TV like Severance).
    catalogs.extend(pipeline.search_overseerr(title))
    pick = _pick_best(catalogs, needle=needle, year=year)
    if pick is None:
        return None
    # Prefer the asked title when the fixture was an exact/partial hit.
    return pick


async def resolve_title(raw: str, *, media_type: str = "any") -> dict[str, Any]:
    """Resolve one spoken/web title into overlay metadata."""
    asked, year = split_title_year(raw)
    if not asked:
        return _skeleton(raw or "Untitled", year=year, media_type=media_type if media_type != "any" else "movie")
    needle = asked.lower()

    candidates: list[dict[str, Any]] = []
    # Overseerr is the house front door for both movies and TV (TMDB ids).
    for hit in await _search_overseerr(asked):
        if media_type == "movie" and str(hit.get("mediaType") or hit.get("type") or "").lower() in {
            "tv",
            "show",
            "series",
        }:
            continue
        if media_type == "tv" and str(hit.get("mediaType") or hit.get("type") or "").lower() in {
            "movie",
            "film",
        }:
            continue
        candidates.append(hit)

    pick = _pick_best(candidates, needle=needle, year=year)
    if pick is None and media_type in {"movie", "any"}:
        pick = _pick_best(await _search_radarr(asked), needle=needle, year=year)
    if pick is None and media_type in {"tv", "any"}:
        pick = _pick_best(await _search_sonarr(asked), needle=needle, year=year)

    mode_live = overseerr.live or radarr.live or sonarr.live
    if pick is None and not mode_live:
        pick = _mock_fallback(asked, year, media_type)

    if pick is None:
        return _skeleton(asked, year=year, media_type=media_type if media_type != "any" else "movie")

    # Reject weak Overseerr/Radarr fallbacks that clearly aren't the asked title.
    if pick.get("matched") == "fallback" and asked.lower() not in str(pick.get("title") or "").lower():
        soft = _mock_fallback(asked, year, media_type)
        if soft is not None and soft.get("matched") != "fallback":
            pick = soft
        else:
            return _skeleton(asked, year=year, media_type=media_type if media_type != "any" else "movie")

    return _normalize_hit(pick, asked=asked, media_type=media_type)


def format_speak(results: list[dict[str, Any]], *, query: str = "") -> str:
    if not results:
        q = _clip(query, 60) if query else "that"
        return f"I couldn't resolve suggestion cards for {q}."
    bits: list[str] = []
    for row in results:
        title = str(row.get("title") or "Untitled")
        year = row.get("year")
        label = f"{title} ({year})" if year else title
        if row.get("skeleton"):
            bits.append(f"{label} (looking up)")
        else:
            bits.append(label)
    if len(bits) == 1:
        spoken = f"On screen: {bits[0]}."
    else:
        spoken = "On screen: " + "; ".join(f"{i}. {bit}" for i, bit in enumerate(bits, 1)) + "."
    return _clip(spoken, SPEAK_LEN)


async def suggest_titles(args: dict[str, Any]) -> dict[str, Any]:
    """Resolve a list of titles (or a recommendation query) for the glass overlay."""
    media_type = _normalize_media_type(args.get("type") or args.get("media_type") or args.get("mediaType"))
    raw_limit = args.get("limit") if args.get("limit") is not None else args.get("count")
    try:
        limit = int(raw_limit) if raw_limit is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_TITLES, limit))

    titles = parse_title_list(args.get("titles") if args.get("titles") is not None else args.get("title"))
    query = _WS.sub(" ", str(args.get("query") or args.get("q") or "")).strip()

    if not titles and query:
        # If the query looks like an explicit title list, prefer parsing it.
        maybe = parse_title_list(query)
        if len(maybe) >= 2:
            titles = maybe
        else:
            titles = titles_for_query(query, media_type, limit=limit)

    if not titles:
        speak = "Tell me which titles to show, or ask for a recommendation like 'suggest sci-fi movies'."
        return {"ok": False, "error": speak, "speak": speak, "results": [], "query": query or None}

    titles = titles[:limit]
    results: list[dict[str, Any]] = []
    modes: set[str] = set()
    for title in titles:
        hit = await resolve_title(title, media_type=media_type)
        results.append(hit)
        if hit.get("skeleton"):
            modes.add("partial")
        elif overseerr.live or radarr.live or sonarr.live:
            modes.add("live")
        else:
            modes.add("mock")

    if "live" in modes and "mock" not in modes and "partial" not in modes:
        mode = "live"
    elif "mock" in modes and "live" not in modes:
        mode = "mock"
    elif results and all(r.get("skeleton") for r in results):
        mode = "partial"
    else:
        mode = "mixed"

    speak = format_speak(results, query=query or ", ".join(titles[:3]))
    return {
        "ok": True,
        "mode": mode,
        "query": query or None,
        "asked": titles,
        "media_type": media_type,
        "results": results,
        "speak": speak,
    }
