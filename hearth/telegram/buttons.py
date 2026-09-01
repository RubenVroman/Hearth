"""Telegram inline keyboards for offer → confirm HITL.

callback_data binds a concrete TMDB id (not a list index), so free-text
"3" / "all" / "those" can never collide with a queue action.

Release-switch offers use ``r:movie:<token>`` where token maps to a pending
release row (guid stays server-side).
"""

from __future__ import annotations

import re
from typing import Any

# Telegram callback_data max is 64 bytes.
_CALLBACK_QUEUE = re.compile(r"^q:(movie|tv):(\d+)$")
_CALLBACK_RELEASE = re.compile(r"^r:(movie|tv):([A-Za-z0-9]{4,16})$")
_CALLBACK_NONE = re.compile(r"^q:none$")

# TMDB genre ids used by discover_by_genre.
GENRE_FANTASY = 14
GENRE_SCI_FI = 878
GENRE_HORROR = 27
GENRE_ACTION = 28
GENRE_ADVENTURE = 12
GENRE_ANIMATION = 16
GENRE_DRAMA = 18


def parse_queue_callback(data: str) -> tuple[str, int] | None:
    """Return (media_type, tmdb_id) for a Get-button callback, else None."""
    raw = (data or "").strip()
    match = _CALLBACK_QUEUE.match(raw)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def parse_release_callback(data: str) -> tuple[str, str] | None:
    """Return (media_type, release_token) for a release Get callback."""
    raw = (data or "").strip()
    match = _CALLBACK_RELEASE.match(raw)
    if not match:
        return None
    return match.group(1), match.group(2)


def is_none_of_these_callback(data: str) -> bool:
    return bool(_CALLBACK_NONE.match((data or "").strip()))


def queue_callback_data(media_type: str, tmdb_id: int) -> str:
    kind = media_type if media_type in {"movie", "tv"} else "movie"
    return f"q:{kind}:{int(tmdb_id)}"


def release_callback_data(media_type: str, token: str) -> str:
    kind = media_type if media_type in {"movie", "tv"} else "movie"
    return f"r:{kind}:{token}"


def offer_inline_keyboard(options: list[dict[str, Any]]) -> dict[str, Any]:
    """Build Get 1..N + None of these for an offer message."""
    rows: list[list[dict[str, str]]] = []
    get_row: list[dict[str, str]] = []
    for idx, option in enumerate(options[:4], start=1):
        # Release-switch offers bind a short token, not a TMDB id.
        token = option.get("releaseToken")
        if token:
            kind = str(option.get("mediaType") or option.get("media_kind") or "movie")
            if kind not in {"movie", "tv"}:
                kind = "movie"
            get_row.append(
                {
                    "text": f"Get {idx}",
                    "callback_data": release_callback_data(kind, str(token)[:16]),
                }
            )
            continue
        tmdb = option.get("tmdbId") or option.get("mediaId")
        if tmdb in (None, ""):
            continue
        try:
            tmdb_i = int(tmdb)
        except (TypeError, ValueError):
            continue
        kind = str(option.get("mediaType") or option.get("media_kind") or "movie")
        if kind not in {"movie", "tv"}:
            kind = "movie"
        get_row.append(
            {
                "text": f"Get {idx}",
                "callback_data": queue_callback_data(kind, tmdb_i),
            }
        )
    if get_row:
        rows.append(get_row)
    rows.append([{"text": "None of these", "callback_data": "q:none"}])
    return {"inline_keyboard": rows}


def single_get_keyboard(row: dict[str, Any]) -> dict[str, Any] | None:
    """One Get button for a Did-you-mean / exact Title (YYYY) / release offer."""
    token = row.get("releaseToken")
    if token:
        kind = str(row.get("mediaType") or row.get("media_kind") or "movie")
        if kind not in {"movie", "tv"}:
            kind = "movie"
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "Get",
                        "callback_data": release_callback_data(kind, str(token)[:16]),
                    }
                ],
                [{"text": "None of these", "callback_data": "q:none"}],
            ]
        }
    tmdb = row.get("tmdbId") or row.get("mediaId")
    if tmdb in (None, ""):
        return None
    try:
        tmdb_i = int(tmdb)
    except (TypeError, ValueError):
        return None
    kind = str(row.get("mediaType") or row.get("media_kind") or "movie")
    if kind not in {"movie", "tv"}:
        kind = "movie"
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Get",
                    "callback_data": queue_callback_data(kind, tmdb_i),
                }
            ],
            [{"text": "None of these", "callback_data": "q:none"}],
        ]
    }


def genre_hint_from_text(text: str) -> tuple[list[int], list[int]]:
    """Map NL/EN genre phrases → (genre_ids, exclude_genre_ids).

    Fantasy asks must use TMDB 14 and exclude Sci-Fi 878 so Matrix/Arrival/
    Interstellar never become the fantasy set.
    """
    raw = (text or "").lower()
    include: list[int] = []
    exclude: list[int] = []
    if re.search(r"\bfantas(?:y|ie)\b", raw):
        include.append(GENRE_FANTASY)
        exclude.append(GENRE_SCI_FI)
    if re.search(r"\b(?:sci-?fi|science fiction|scifi)\b", raw):
        include.append(GENRE_SCI_FI)
    if re.search(r"\bhorror\b", raw):
        include.append(GENRE_HORROR)

    # Deduplicate preserving order.
    def _uniq(ids: list[int]) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for gid in ids:
            if gid not in seen:
                seen.add(gid)
                out.append(gid)
        return out

    return _uniq(include), _uniq(exclude)


__all__ = [
    "GENRE_ACTION",
    "GENRE_ADVENTURE",
    "GENRE_ANIMATION",
    "GENRE_DRAMA",
    "GENRE_FANTASY",
    "GENRE_HORROR",
    "GENRE_SCI_FI",
    "genre_hint_from_text",
    "is_none_of_these_callback",
    "offer_inline_keyboard",
    "parse_queue_callback",
    "parse_release_callback",
    "queue_callback_data",
    "release_callback_data",
    "single_get_keyboard",
]
