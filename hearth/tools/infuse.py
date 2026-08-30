"""Infuse (Firecore) on Apple TV — deep-link launch via Home Assistant / pyatv.

Infuse has no playback-state API. Hearth resolves a library title → TMDB id
(from Plex Guids / Radarr / Overseerr), builds an ``infuse://…?play`` URL, and
asks HA's Apple TV ``media_player`` to open it (``play_media`` type ``url``).
Pause / play / stop / skip use the same HA Apple TV entity — not Infuse REST.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from hearth.config import settings
from hearth.tools.arr import overseerr, radarr
from hearth.tools.ha import ha
from hearth.tools.plex import plex

_SETUP_SPEAK = (
    "Apple TV isn't wired up in Home Assistant yet, so I can't open Infuse. "
    "In HA, add the Apple TV integration and pair it, create a long-lived token "
    "if you haven't, set HA_TOKEN and HA_APPLE_TV_ENTITY in VAULT .env "
    "(entity_id from Developer Tools → States), then recreate the hearth "
    "container. Infuse must be installed on the Apple TV with your Plex and/or "
    "SMB library already connected."
)

_TMDB_RE = re.compile(r"(?:tmdb|themoviedb)[:/]+(\d+)", re.I)


class Infuse:
    """Launch Infuse deep links on the house Apple TV via Home Assistant."""

    async def resolve_play(
        self,
        query: str,
        *,
        tmdb_id: int | str | None = None,
        rating_key: str | int | None = None,
        season: int | None = None,
        episode: int | None = None,
        play: bool = True,
    ) -> dict[str, Any]:
        """Resolve title → TMDB → Infuse URL and check the Apple TV entity."""
        title_query = (query or "").strip()
        if not title_query and tmdb_id is None and rating_key is None:
            return {
                "ok": False,
                "error": "query, tmdb_id, or rating_key required",
                "speak": "Tell me which title to play in Infuse.",
            }

        item: dict[str, Any] | None = None
        item_mode: str | None = None
        if title_query or rating_key is not None:
            item_result = await plex._resolve_item(title_query, rating_key=rating_key)
            if not item_result.get("ok"):
                # Ambiguous / hard library miss should surface — don't guess via Radarr.
                if item_result.get("ambiguous") or item_result.get("ambiguous_titles"):
                    return item_result
                if item_result.get("in_library") is False and tmdb_id is None:
                    # Still try Radarr/Overseerr for TMDB when the title isn't in Plex,
                    # but keep the library miss speak if TMDB also fails later.
                    item = {
                        "title": title_query,
                        "type": "movie",
                        "query_only": True,
                        "in_library": False,
                    }
                    item_mode = item_result.get("mode")
                elif tmdb_id is None and not title_query:
                    return item_result
                elif tmdb_id is not None:
                    item = {
                        "title": title_query or f"TMDB {tmdb_id}",
                        "type": "movie",
                        "tmdbId": int(tmdb_id),
                    }
                    item_mode = item_result.get("mode")
                else:
                    return item_result
            else:
                item = item_result["item"]
                item_mode = item_result.get("mode")
                # Enrich Guids / season+episode from full metadata when live.
                enriched = await plex.metadata(str(item.get("ratingKey") or ""))
                if enriched.get("ok") and enriched.get("item"):
                    item = {**item, **enriched["item"]}
                    item_mode = enriched.get("mode") or item_mode
        else:
            item = {
                "title": f"TMDB {tmdb_id}",
                "type": "movie",
                "tmdbId": int(tmdb_id) if tmdb_id is not None else None,
            }

        assert item is not None
        kind = _infuse_kind(item, season=season, episode=episode)
        resolved_tmdb = await self._resolve_tmdb(
            item,
            query=title_query,
            tmdb_id=tmdb_id,
            kind=kind,
        )
        if not resolved_tmdb.get("ok"):
            return {
                **resolved_tmdb,
                "mode": item_mode or resolved_tmdb.get("mode"),
                "item": item,
            }

        tmdb = int(resolved_tmdb["tmdbId"])
        season_n = season if season is not None else item.get("season")
        episode_n = episode if episode is not None else item.get("episode")
        deep_link = build_infuse_url(
            tmdb,
            kind=kind,
            season=int(season_n) if season_n is not None else None,
            episode=int(episode_n) if episode_n is not None else None,
            play=play,
        )

        atv = await ha.resolve_device_state("apple_tv")
        if not atv.get("ok"):
            return {
                "ok": False,
                "needs_setup": True,
                "error": atv.get("error") or "Apple TV entity not found",
                "entity_id": atv.get("entity_id") or settings.ha_apple_tv_entity,
                "speak": _SETUP_SPEAK,
                "item": item,
                "tmdbId": tmdb,
                "deep_link": deep_link,
                "mode": atv.get("mode") or item_mode,
            }

        entity_id = str(atv.get("entity_id") or settings.ha_apple_tv_entity)
        title = item.get("title") or title_query or f"TMDB {tmdb}"
        year = item.get("year")
        label = f"{title} ({year})" if year else str(title)
        speak = (
            f"I'll open {label} in Infuse on the Apple TV"
            f"{' and start playback' if play else ''}. Confirm to launch."
        )
        return {
            "ok": True,
            "mode": atv.get("mode") or item_mode or resolved_tmdb.get("mode"),
            "player": "infuse",
            "item": item,
            "tmdbId": tmdb,
            "tmdb_source": resolved_tmdb.get("source"),
            "kind": kind,
            "season": int(season_n) if season_n is not None else None,
            "episode": int(episode_n) if episode_n is not None else None,
            "deep_link": deep_link,
            "entity_id": entity_id,
            "apple_tv": atv.get("state"),
            "speak": speak,
        }

    async def play(
        self,
        query: str,
        *,
        tmdb_id: int | str | None = None,
        rating_key: str | int | None = None,
        season: int | None = None,
        episode: int | None = None,
        play: bool = True,
    ) -> dict[str, Any]:
        plan = await self.resolve_play(
            query,
            tmdb_id=tmdb_id,
            rating_key=rating_key,
            season=season,
            episode=episode,
            play=play,
        )
        if not plan.get("ok"):
            return plan

        deep_link = str(plan["deep_link"])
        entity_id = str(plan["entity_id"])
        item = plan.get("item") or {}
        title = item.get("title") or query or f"TMDB {plan.get('tmdbId')}"

        result = await ha.call_service(
            "media_player",
            "play_media",
            entity_id,
            {
                "media_content_id": deep_link,
                "media_content_type": "url",
            },
        )
        if result.get("ok") is False and not settings.mock_if_unconfigured:
            return {
                "ok": False,
                "mode": result.get("mode"),
                "error": result.get("error") or "HA play_media failed",
                "item": item,
                "deep_link": deep_link,
                "entity_id": entity_id,
                "speak": (
                    f"Couldn't open Infuse on the Apple TV: {result.get('error')}. "
                    "Check HA_APPLE_TV_ENTITY and that the Apple TV is awake."
                ),
            }

        speak = f"Opening {title} in Infuse on the Apple TV."
        if result.get("mode") == "mock":
            speak = f"Opening {title} in Infuse on the Apple TV (mock)."
        return {
            "ok": True,
            "mode": result.get("mode") or plan.get("mode"),
            "played": True,
            "player": "infuse",
            "item": item,
            "tmdbId": plan.get("tmdbId"),
            "tmdb_source": plan.get("tmdb_source"),
            "deep_link": deep_link,
            "entity_id": entity_id,
            "result": result,
            "speak": speak,
        }

    async def transport(self, action: str) -> dict[str, Any]:
        """Pause / play / stop / skip via HA Apple TV media_player (not Infuse API)."""
        action = (action or "").strip().lower()
        mapping = {
            "play": "media_play",
            "media_play": "media_play",
            "pause": "media_pause",
            "media_pause": "media_pause",
            "stop": "media_stop",
            "media_stop": "media_stop",
            "next": "media_next_track",
            "skip": "media_next_track",
            "media_next_track": "media_next_track",
            "previous": "media_previous_track",
            "back": "media_previous_track",
            "media_previous_track": "media_previous_track",
        }
        service = mapping.get(action)
        if not service:
            return {
                "ok": False,
                "error": f"unknown transport action {action!r}",
                "speak": "I can pause, play, stop, skip, or go back on the Apple TV.",
            }

        atv = await ha.resolve_device_state("apple_tv")
        if not atv.get("ok"):
            return {
                "ok": False,
                "needs_setup": True,
                "error": atv.get("error") or "Apple TV entity not found",
                "speak": _SETUP_SPEAK,
                "entity_id": atv.get("entity_id") or settings.ha_apple_tv_entity,
            }

        entity_id = str(atv.get("entity_id") or settings.ha_apple_tv_entity)
        result = await ha.call_service("media_player", service, entity_id)
        label = {
            "media_play": "Playing",
            "media_pause": "Paused",
            "media_stop": "Stopped",
            "media_next_track": "Skipped ahead",
            "media_previous_track": "Skipped back",
        }.get(service, service)
        ok = result.get("ok", True) is not False
        speak = (
            f"{label} on the Apple TV"
            + (" (mock)." if result.get("mode") == "mock" else ".")
        )
        if not ok:
            speak = (
                f"Couldn't {action} on the Apple TV: {result.get('error')}. "
                "Transport goes through Home Assistant's Apple TV remote — "
                "Infuse has no now-playing API."
            )
        return {
            "ok": ok,
            "mode": result.get("mode"),
            "action": action,
            "service": f"media_player.{service}",
            "entity_id": entity_id,
            "result": result,
            "speak": speak,
            "note": "Infuse has no playback-state API; this is HA Apple TV transport only.",
        }

    async def _resolve_tmdb(
        self,
        item: dict[str, Any],
        *,
        query: str,
        tmdb_id: int | str | None,
        kind: str,
    ) -> dict[str, Any]:
        if tmdb_id is not None:
            try:
                return {"ok": True, "tmdbId": int(tmdb_id), "source": "arg"}
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": f"invalid tmdb_id {tmdb_id!r}",
                    "speak": "That TMDB id doesn't look right.",
                }

        from_item = _tmdb_from_item(item)
        if from_item is not None:
            return {"ok": True, "tmdbId": from_item, "source": "plex"}

        title = str(item.get("title") or query or "").strip()
        if not title:
            return {
                "ok": False,
                "error": "no TMDB id and no title to look up",
                "speak": "I need a title or TMDB id to open Infuse.",
            }

        if kind == "movie":
            searched = await radarr.search(title)
            hits = searched.get("results") or []
            pick = _best_title_match(hits, title, year=item.get("year"))
            if pick and pick.get("tmdbId"):
                return {
                    "ok": True,
                    "tmdbId": int(pick["tmdbId"]),
                    "source": "radarr",
                    "mode": searched.get("mode"),
                }

        # Overseerr uses TMDB ids for both movies and TV.
        ov = await overseerr.search(title)
        hits = ov.get("results") or []
        want_type = "movie" if kind == "movie" else "tv"
        typed = [h for h in hits if str(h.get("mediaType") or "").lower() == want_type] or hits
        pick = _best_title_match(typed, title, year=item.get("year"))
        if pick and pick.get("mediaId"):
            return {
                "ok": True,
                "tmdbId": int(pick["mediaId"]),
                "source": "overseerr",
                "mode": ov.get("mode"),
            }

        return {
            "ok": False,
            "error": f"could not resolve TMDB id for {title!r}",
            "speak": (
                f"I found {title} but couldn't get a TMDB id for Infuse. "
                "Check Plex Guids, or say the TMDB id explicitly."
            ),
            "item": item,
        }


def build_infuse_url(
    tmdb_id: int,
    *,
    kind: str = "movie",
    season: int | None = None,
    episode: int | None = None,
    play: bool = True,
) -> str:
    """Build an Infuse deep link (Firecore URL API v8.4.7+)."""
    tid = int(tmdb_id)
    if kind in {"series", "show", "season", "episode"}:
        if season is not None and episode is not None:
            path = f"series/{tid}-{int(season)}-{int(episode)}"
        elif season is not None:
            path = f"series/{tid}-{int(season)}"
        else:
            path = f"series/{tid}"
    else:
        path = f"movie/{tid}"
    url = f"infuse://{path}"
    if play:
        url = f"{url}?play"
    return url


def build_infuse_xcallback(
    file_url: str,
    *,
    filename: str | None = None,
    position: int | None = None,
) -> str:
    """Fallback: play a direct file URL in Infuse (does not sync Plex watch state)."""
    parts = [f"url={quote(file_url, safe='')}"]
    if filename:
        parts.append(f"filename={quote(filename, safe='')}")
    if position is not None:
        parts.append(f"position={int(position)}")
    return "infuse://x-callback-url/play?" + "&".join(parts)


def prefer_infuse_for_apple_tv(player_hint: str | None = None) -> bool:
    """True when Apple TV playback should go through Infuse, not the Plex app."""
    pref = (settings.apple_tv_player or "infuse").strip().lower()
    if pref in {"plex", "plex_app", "plex-client"}:
        return False
    hint = (player_hint or "").strip().lower()
    if not hint:
        # Vague “play X” / “the TV” — Infuse is the house default for living-room ATV.
        return pref in {"infuse", "firecore", ""}
    if "infuse" in hint or "firecore" in hint:
        return True
    if "apple" in hint or hint in {"tv", "television", "living room", "livingroom"}:
        return True
    # Explicit LG / Shield / named Plex client → keep Plex path.
    return False


def _infuse_kind(
    item: dict[str, Any],
    *,
    season: int | None,
    episode: int | None,
) -> str:
    kind = str(item.get("type") or "movie").lower()
    if season is not None or episode is not None:
        return "series"
    if kind in {"episode", "season", "show", "series"}:
        return "series"
    return "movie"


def _tmdb_from_item(item: dict[str, Any]) -> int | None:
    if item.get("tmdbId") is not None:
        try:
            return int(item["tmdbId"])
        except (TypeError, ValueError):
            pass
    for key in ("guid", "Guid", "guids"):
        raw = item.get(key)
        found = _tmdb_from_guid_blob(raw)
        if found is not None:
            return found
    return None


def _tmdb_from_guid_blob(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return _tmdb_from_guid_blob(raw.get("id") or raw.get("guid"))
    if isinstance(raw, list):
        for entry in raw:
            found = _tmdb_from_guid_blob(entry)
            if found is not None:
                return found
        return None
    text = str(raw)
    match = _TMDB_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def _best_title_match(
    hits: list[dict[str, Any]],
    title: str,
    *,
    year: Any = None,
) -> dict[str, Any] | None:
    if not hits:
        return None
    needle = title.lower().strip()
    exact = [h for h in hits if str(h.get("title") or "").lower() == needle]
    pool = exact or hits
    if year is not None:
        year_s = str(year)
        by_year = [h for h in pool if str(h.get("year") or "") == year_s]
        if by_year:
            return by_year[0]
    return pool[0]


infuse = Infuse()
