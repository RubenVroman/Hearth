"""Map rich tool results into visual info overlays (weather, movie/TV, downloads).

Action / “update guard” panels were removed — they flickered on status polls
and were not useful. Only content that benefits from visualization is published.
"""

from __future__ import annotations

from typing import Any

from hearth.runtime import Widget, runtime

# Tools that deserve a centered glass-panel visualization.
_MEDIA_TOOLS = {
    "plex_search",
    "plex_now_playing",
    "plex_play",
    "plex_browse_genre",
    "infuse_play",
    "radarr_search",
    "sonarr_search",
    "overseerr_search",
    "suggest_titles",
}

_DOWNLOAD_TOOLS = {
    "radarr_queue",
    "sonarr_queue",
    "radarr_retry",
    "sonarr_retry",
    "radarr_list_releases",
    "radarr_grab_release",
}

_VISUAL_KINDS = frozenset({"weather", "media", "downloads"})


def new_id(prefix: str = "w") -> str:
    from uuid import uuid4

    return f"{prefix}-{uuid4().hex[:10]}"


def start_turn(message: str) -> Widget | None:
    """New user turn — keep overlays in memory; relevance soft-hides in the UI.

    Hard-deleting here would prevent reappear when talk returns to on-screen
    content. Context is re-evaluated on the next widgets/status payload.
    """
    _ = message
    return None


def finish_turn(*, ok: bool = True, detail: str = "") -> Widget | None:
    """No-op: thinking / update guards are gone."""
    _ = ok, detail
    return None


def publish_tool(result: dict[str, Any]) -> Widget | None:
    """Upsert a visual overlay from a tool result dict (ToolResult.as_dict())."""
    name = str(result.get("name") or "")
    if not name:
        return None
    if name == "get_weather":
        return _weather_widget(result)
    if name in _DOWNLOAD_TOOLS:
        return _downloads_widget(result)
    if name in _MEDIA_TOOLS:
        return _media_widget(result)
    return None


def is_visual(kind: str | None) -> bool:
    return (kind or "") in _VISUAL_KINDS


def _weather_widget(result: dict[str, Any]) -> Widget:
    data = result.get("data") or {}
    place = data.get("place") or data.get("location") or "Outside"
    ok = bool(result.get("ok")) and data.get("ok") is not False
    if not ok:
        return runtime.upsert_widget(
            Widget(
                id="weather",
                kind="weather",
                title="Weather",
                status="error",
                body=str(data.get("error") or "Could not fetch weather."),
                detail="",
                data=data,
            )
        )
    temp = data.get("temperature")
    unit = data.get("temperature_unit") or "°C"
    condition = data.get("condition") or "Unknown"
    wind = data.get("wind_speed")
    wind_unit = data.get("wind_unit") or "km/h"
    body = f"{temp}{unit} · {condition}" if temp is not None else str(condition)
    detail_parts = []
    if wind is not None:
        detail_parts.append(f"Wind {wind} {wind_unit}")
    if data.get("mode") == "mock":
        detail_parts.append("mock")
    humidity = data.get("humidity")
    if humidity is not None:
        detail_parts.append(f"Humidity {humidity}%")
    return runtime.upsert_widget(
        Widget(
            id="weather",
            kind="weather",
            title=str(place),
            status="done",
            body=body,
            detail=" · ".join(detail_parts),
            data=data,
            sticky=True,
        )
    )


def _art_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Poster identifiers safe to send to the browser (no API keys / tokens)."""
    from hearth.tools.media_art import enrich_media_hit, poster_path_for_tmdb, sanitize_poster_path

    tmdb = row.get("tmdbId") or row.get("mediaId")
    try:
        tmdb_id = int(tmdb) if tmdb is not None else None
    except (TypeError, ValueError):
        tmdb_id = None
    path = sanitize_poster_path(row.get("posterPath") or row.get("poster_path"))
    if path is None and tmdb_id is not None:
        path = poster_path_for_tmdb(tmdb_id)
    rating_key = row.get("ratingKey")
    enriched = enrich_media_hit(
        {
            "ratingKey": str(rating_key) if rating_key is not None else None,
            "tmdbId": tmdb_id,
            "posterPath": path,
        }
    )
    return {
        "ratingKey": enriched.get("ratingKey"),
        "tmdbId": enriched.get("tmdbId"),
        "posterPath": enriched.get("posterPath"),
        "thumb": bool(enriched.get("thumb")),
    }


def _format_bytes(value: Any) -> str | None:
    try:
        if value is None:
            return None
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    units = ("B", "KB", "MB", "GB", "TB")
    size = n
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def _downloads_rows(downloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in downloads[:8]:
        if not isinstance(item, dict):
            continue
        percent = item.get("percent")
        try:
            percent_n = float(percent) if percent is not None else None
        except (TypeError, ValueError):
            percent_n = None
        rows.append(
            {
                "title": item.get("title") or "Untitled",
                "status": item.get("status") or "unknown",
                "percent": percent_n,
                "timeleft": item.get("timeleft"),
                "sizeleft": item.get("sizeleft"),
                "sizeleft_label": _format_bytes(item.get("sizeleft")),
                "size_label": _format_bytes(item.get("size")),
                "quality": item.get("quality"),
                "indexer": item.get("indexer"),
                "service": item.get("service"),
            }
        )
    return rows


def _downloads_widget(result: dict[str, Any]) -> Widget:
    """Glass panel for Radarr/Sonarr queue progress — including calm empty states."""
    name = str(result.get("name") or "radarr_queue")
    data = result.get("data") or {}
    ok = bool(result.get("ok")) and data.get("ok") is not False
    service = str(data.get("service") or ("sonarr" if "sonarr" in name else "radarr"))
    label = "Sonarr" if service == "sonarr" else "Radarr"
    query = str(data.get("query") or data.get("title") or "").strip()
    downloads = _downloads_rows(list(data.get("downloads") or []))
    found = data.get("found")
    is_retry = any(
        key in name
        for key in ("retry", "list_releases", "grab_release")
    )

    if not ok and not is_retry:
        return runtime.upsert_widget(
            Widget(
                id="downloads",
                kind="downloads",
                title=label,
                status="error",
                body=str(data.get("error") or "Could not read the download queue."),
                detail="",
                data={
                    "tool": name,
                    "service": service,
                    "query": query or None,
                    "downloads": [],
                    "empty": True,
                    "mode": data.get("mode"),
                },
                sticky=True,
            )
        )

    if is_retry:
        title = str(data.get("title") or query or label)
        speak = str(data.get("speak") or "").strip()
        reason = str(data.get("reason") or "")
        if data.get("ok") and reason == "retried":
            body = "Retrying another source"
        elif data.get("ok") and reason == "switched":
            body = "Grabbing alternate release"
        elif data.get("ok") and reason == "kept_both":
            body = "Extra download — keeping library file"
        elif reason in {"needs_pick", "needs_pick_large", "needs_pick_keep"}:
            if reason == "needs_pick_keep":
                body = "Already has file — pick extra release"
            elif reason == "needs_pick":
                body = "No file / retrying smaller release"
            else:
                body = "Too large — pick smaller release"
        elif reason == "exhausted":
            body = "No more sources"
        elif reason == "no_alternate":
            body = "No other release"
        elif reason == "not_found":
            body = "Not in queue"
        elif reason == "not_in_library":
            body = "Not in library"
        elif not data.get("ok"):
            body = "Retry failed"
        else:
            body = "Retry"
        detail_parts = [label]
        if data.get("indexer"):
            detail_parts.append(str(data.get("indexer")))
        if data.get("mode") == "mock":
            detail_parts.append("mock")
        return runtime.upsert_widget(
            Widget(
                id="downloads",
                kind="downloads",
                title=title,
                status="done" if data.get("ok") else "error",
                body=body,
                detail=" · ".join(detail_parts),
                data={
                    "tool": name,
                    "service": service,
                    "query": query or None,
                    "downloads": downloads,
                    "found": found,
                    "empty": None if downloads else "missing",
                    "mode": data.get("mode"),
                    "speak": speak or data.get("speak"),
                    "reason": reason or None,
                    "attempt": data.get("attempt"),
                    "max_attempts": data.get("max_attempts"),
                },
                sticky=True,
            )
        )

    if query and not downloads:
        title = query
        body = "Not downloading"
        detail = f"Not in the {label} queue right now."
        empty_kind = "missing"
    elif not downloads:
        title = label
        body = "Nothing downloading"
        detail = "Queue is quiet."
        empty_kind = "idle"
    else:
        title = query or label
        if len(downloads) == 1:
            row = downloads[0]
            pct = row.get("percent")
            pct_bit = f"{pct:g}%" if isinstance(pct, (int, float)) else ""
            body = " · ".join(
                bit for bit in (str(row.get("status") or "unknown"), pct_bit) if bit
            )
            title = str(row.get("title") or title)
        else:
            body = f"{len(downloads)} active"
        detail_parts = [label]
        if data.get("mode") == "mock":
            detail_parts.append("mock")
        detail = " · ".join(detail_parts)
        empty_kind = None

    return runtime.upsert_widget(
        Widget(
            id="downloads",
            kind="downloads",
            title=title,
            status="done",
            body=body,
            detail=detail,
            data={
                "tool": name,
                "service": service,
                "query": query or None,
                "downloads": downloads,
                "found": found,
                "empty": empty_kind,
                "mode": data.get("mode"),
                "speak": data.get("speak"),
            },
            sticky=True,
        )
    )



_MEDIA_STACK_CAP = 12
_SEARCH_HIT_CAP = 4
# Genre browse is meant to be flicked through on the glass stack — more than a
# search disambiguation set, but still capped like speakable browse results.
_BROWSE_HIT_CAP = 12


def _media_item_id(row: dict[str, Any]) -> str:
    rating_key = row.get("ratingKey")
    if rating_key is not None and str(rating_key).strip():
        return f"plex:{rating_key}"
    tmdb = row.get("tmdbId")
    if tmdb is not None and str(tmdb).strip():
        media_type = str(row.get("type") or row.get("mediaType") or "movie")
        return f"tmdb:{media_type}:{tmdb}"
    title = str(row.get("title") or row.get("name") or "untitled").strip().lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in title).strip("-") or "untitled"
    year = row.get("year")
    if year is not None:
        return f"title:{slug}:{year}"
    return f"title:{slug}"


def _normalize_media_item(row: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    title = row.get("title") or row.get("name")
    if not title:
        return None
    # Overseerr uses mediaId as the TMDB id.
    if row.get("tmdbId") is None and row.get("mediaId") is not None:
        row = {**row, "tmdbId": row.get("mediaId")}
    art = _art_fields(row)
    genres = row.get("genres")
    if not isinstance(genres, list):
        genres = []
    genres = [str(g).strip() for g in genres if str(g or "").strip()]
    raw_type = str(row.get("type") or row.get("mediaType") or "movie").lower()
    if raw_type in {"tv", "show", "series"}:
        media_type = "show"
    elif raw_type in {"movie", "film"}:
        media_type = "movie"
    else:
        media_type = raw_type or "movie"
    item = {
        "id": _media_item_id({**row, **art, "title": title, "type": media_type}),
        "title": title,
        "type": media_type,
        "year": row.get("year"),
        "show": row.get("show") or row.get("grandparentTitle"),
        "summary": row.get("summary") or row.get("overview") or "",
        "contentRating": row.get("contentRating"),
        "rating": row.get("rating") or row.get("audienceRating"),
        "genres": genres,
        **art,
        "source": source,
        "skeleton": bool(row.get("skeleton")),
    }
    if row.get("player") is not None:
        item["player"] = row.get("player")
    if row.get("state") is not None:
        item["state"] = row.get("state")
    if row.get("pending"):
        item["pending"] = True
    links = row.get("links") if isinstance(row.get("links"), dict) else None
    if links:
        item["links"] = {
            k: str(v) for k, v in links.items() if k in {"tmdb", "imdb"} and v
        }
    return item


def _candidate_media_items(data: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    """Turn ambiguous play candidates into selectable overlay cards."""
    candidates = data.get("candidates") or data.get("results") or []
    if not isinstance(candidates, list) or not candidates:
        return []
    out: list[dict[str, Any]] = []
    for hit in candidates[:_SEARCH_HIT_CAP]:
        if not isinstance(hit, dict):
            continue
        item = _normalize_media_item({**hit, "pending": True}, source=source)
        if item:
            out.append(item)
    return out


def _media_items_from_tool(name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one or many hits from a media tool into stack cards."""
    if name == "plex_now_playing":
        sessions = data.get("sessions") or []
        out: list[dict[str, Any]] = []
        for row in sessions[:_SEARCH_HIT_CAP]:
            if not isinstance(row, dict):
                continue
            item = _normalize_media_item(
                {
                    **row,
                    "show": row.get("show") or row.get("grandparentTitle"),
                    "player": row.get("player"),
                    "state": row.get("state"),
                },
                source="plex",
            )
            if item:
                out.append(item)
        return out
    if name == "plex_play":
        item_raw = data.get("item") or {}
        if isinstance(item_raw, dict) and item_raw.get("title"):
            client = data.get("client") or {}
            item = _normalize_media_item(
                {
                    **item_raw,
                    "player": client.get("name") if isinstance(client, dict) else None,
                    "pending": bool(data.get("needs_confirm") or data.get("would_call_with")),
                },
                source="plex",
            )
            return [item] if item else []
        # Ambiguous title / client miss: surface candidates as pickable play cards
        # instead of publishing nothing (which left the glass empty or stale).
        return _candidate_media_items(data, source="plex")
    if name == "infuse_play":
        # Ambiguous library matches must become pickable cards — never a lone
        # query-only shell that looks like an empty/broken play popup.
        if data.get("ambiguous") or data.get("ambiguous_titles"):
            picked = _candidate_media_items(data, source="infuse")
            if picked:
                return picked
        item_raw = data.get("item") or {}
        if not isinstance(item_raw, dict):
            item_raw = {}
        # Prefer a real title/query — never use `speak` (e.g. "Which title?") as the card.
        title = item_raw.get("title") or data.get("query")
        if not title and data.get("tmdbId") is not None:
            title = f"TMDB {data.get('tmdbId')}"
        if not title:
            return _candidate_media_items(data, source="infuse")
        row = {
            **item_raw,
            "title": title,
            "tmdbId": item_raw.get("tmdbId") or data.get("tmdbId"),
            "player": "Infuse",
            "state": "opening" if data.get("played") else ("ready" if data.get("ok") is not False else None),
            "pending": bool(data.get("needs_confirm") or data.get("would_call_with")),
        }
        item = _normalize_media_item(row, source="infuse")
        return [item] if item else []
    if name in {
        "plex_search",
        "plex_browse_genre",
        "radarr_search",
        "sonarr_search",
        "overseerr_search",
        "suggest_titles",
    }:
        results = data.get("results") or []
        if not results:
            return []
        source = {
            "plex_search": "plex",
            "plex_browse_genre": "plex",
            "radarr_search": "radarr",
            "sonarr_search": "sonarr",
            "overseerr_search": "overseerr",
            "suggest_titles": "suggest",
        }.get(name, "media")
        default_type = "movie" if "radarr" in name or name == "plex_browse_genre" else "show"
        if name == "plex_browse_genre" and str(data.get("media_type") or "").lower() == "show":
            default_type = "show"
        if name == "suggest_titles":
            asked_type = str(data.get("media_type") or "").lower()
            if asked_type in {"tv", "show"}:
                default_type = "show"
            else:
                default_type = "movie"
        cap = _BROWSE_HIT_CAP if name == "plex_browse_genre" else _SEARCH_HIT_CAP
        out = []
        for hit in results[:cap]:
            if not isinstance(hit, dict):
                continue
            row = {
                **hit,
                "type": hit.get("type") or hit.get("mediaType") or default_type,
            }
            item = _normalize_media_item(row, source=source)
            if item:
                # Browse stack: keep copy tight so title + year stay readable.
                if name == "plex_browse_genre" and item.get("summary"):
                    summary = str(item["summary"]).strip()
                    if len(summary) > 140:
                        item["summary"] = summary[:137].rstrip() + "…"
                out.append(item)
        return out
    return []


def _merge_media_stack(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    active_id: str | None,
) -> tuple[list[dict[str, Any]], str]:
    """Merge new cards into the stack; newest active title leads."""
    by_id: dict[str, dict[str, Any]] = {}

    def _absorb(row: dict[str, Any]) -> str | None:
        item_id = str(row.get("id") or "")
        if not item_id:
            return None
        prev = by_id.get(item_id)
        if prev is None:
            by_id[item_id] = dict(row)
            return item_id
        merged = {**prev, **{k: v for k, v in row.items() if v is not None and v != ""}}
        if prev.get("summary") and not row.get("summary"):
            merged["summary"] = prev["summary"]
        if not row.get("skeleton"):
            merged["skeleton"] = False
        elif not prev.get("skeleton"):
            merged["skeleton"] = False
        by_id[item_id] = merged
        return item_id

    for row in existing:
        if isinstance(row, dict):
            _absorb(row)
    incoming_ids: list[str] = []
    for row in incoming:
        item_id = _absorb(row)
        if item_id:
            incoming_ids.append(item_id)

    if not by_id:
        return [], ""

    chosen = active_id if active_id and active_id in by_id else ""
    if not chosen and incoming_ids:
        chosen = incoming_ids[0]
    if not chosen:
        chosen = next(iter(by_id))

    ordered_ids: list[str] = []
    seen: set[str] = set()

    def _push(item_id: str | None) -> None:
        if not item_id or item_id not in by_id or item_id in seen:
            return
        ordered_ids.append(item_id)
        seen.add(item_id)

    _push(chosen)
    for item_id in incoming_ids:
        _push(item_id)
    for row in existing:
        if isinstance(row, dict):
            _push(str(row.get("id") or ""))

    ordered_ids = ordered_ids[:_MEDIA_STACK_CAP]
    return [by_id[item_id] for item_id in ordered_ids], chosen


def _media_panel_copy(
    active: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    mode: Any = None,
    genre: str | None = None,
    total: int | None = None,
) -> tuple[str, str, str]:
    title = str(active.get("title") or "Untitled")
    year = active.get("year")
    media_type = str(active.get("type") or "movie")
    bits = [media_type]
    if year:
        bits.append(str(year))
    if active.get("show"):
        bits.append(str(active["show"]))
    body = " · ".join(bits)
    detail_parts: list[str] = []
    if genre:
        shown = len(items)
        try:
            total_n = int(total) if total is not None else shown
        except (TypeError, ValueError):
            total_n = shown
        if total_n > shown:
            detail_parts.append(f"{genre} · {shown} of {total_n}")
        elif shown > 1:
            detail_parts.append(f"{genre} · {shown} titles")
        else:
            detail_parts.append(str(genre))
    summary = (active.get("summary") or "").strip()
    if summary:
        limit = 140 if genre else 220
        detail_parts.append(summary[:limit] + ("…" if len(summary) > limit else ""))
    if active.get("player") and active.get("state"):
        detail_parts.append(f"{active['player']} · {active['state']}")
    elif active.get("player"):
        detail_parts.append(str(active["player"]))
    if active.get("skeleton"):
        detail_parts.append("looking up")
    if mode == "mock":
        detail_parts.append("mock")
    if not genre and len(items) > 1:
        detail_parts.append(f"{len(items)} on screen")
    return title, body, " · ".join(detail_parts)


def _genre_catalog(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact genre directory for the glass category chips (no secrets)."""
    raw = data.get("genres") or []
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        entry: dict[str, Any] = {"title": title}
        if row.get("key") is not None:
            entry["key"] = str(row["key"])
        if row.get("size") is not None:
            try:
                entry["size"] = int(row["size"])
            except (TypeError, ValueError):
                pass
        out.append(entry)
    return out


def _genres_widget(result: dict[str, Any]) -> Widget | None:
    """Glass panel of library genre buckets — tap one to browse that category."""
    data = result.get("data") or {}
    genres = _genre_catalog(data)
    if not genres:
        return None
    media_type = str(data.get("media_type") or "movie")
    kind_label = "shows" if media_type == "show" else "movies"
    names = [g["title"] for g in genres[:8]]
    body = ", ".join(names)
    if len(genres) > 8:
        body = f"{body}, +{len(genres) - 8} more"
    detail = f"{len(genres)} {kind_label} categories from Plex"
    if data.get("mode") == "mock":
        detail = f"{detail} · mock"
    return runtime.upsert_widget(
        Widget(
            id="media",
            kind="media",
            title=f"{kind_label.capitalize()} by genre",
            status="done",
            body=body,
            detail=detail,
            data={
                "tool": "plex_browse_genre",
                "presentation": "genres",
                "listed_genres": True,
                "media_type": media_type,
                "genres": genres,
                "items": [],
                "item": {"title": f"{kind_label.capitalize()} by genre", "type": media_type},
                "active_id": "",
            },
            sticky=False,
        )
    )


def _media_widget(result: dict[str, Any]) -> Widget | None:
    name = str(result.get("name") or "media")
    data = result.get("data") or {}
    ok = bool(result.get("ok")) and data.get("ok") is not False

    # List-genres asks → category picker (not an empty media stack).
    if name == "plex_browse_genre" and data.get("listed_genres"):
        return _genres_widget(result)

    incoming = _media_items_from_tool(name, data)
    if not incoming:
        # Empty search / nothing playing — no overlay (voice/transcript still answers).
        return None
    if result.get("needs_confirm"):
        incoming[0]["pending"] = True

    # Genre browse replaces the prior stack so ask-once shows only that genre.
    existing_items: list[dict[str, Any]] = []
    if name != "plex_browse_genre":
        existing_widget = runtime.get_widget("media")
        if existing_widget is not None and existing_widget.kind == "media":
            raw_items = (existing_widget.data or {}).get("items")
            if isinstance(raw_items, list) and raw_items:
                existing_items = [row for row in raw_items if isinstance(row, dict)]
            else:
                prev = (existing_widget.data or {}).get("item")
                if isinstance(prev, dict):
                    existing_items = [prev]

    # Query / first hit becomes the spoken focus for this tool round-trip.
    preferred = str(incoming[0].get("id") or "")
    items, active_id = _merge_media_stack(existing_items, incoming, active_id=preferred)
    active = next((row for row in items if str(row.get("id") or "") == active_id), items[0])
    genre = str(data.get("genre") or "").strip() or None if name == "plex_browse_genre" else None
    title, body, detail = _media_panel_copy(
        active,
        items,
        mode=data.get("mode"),
        genre=genre,
        total=data.get("total"),
    )

    payload = {
        "tool": name,
        "item": active,
        "items": items,
        "active_id": active_id,
        "presentation": "carousel",
    }
    if genre:
        payload["genre"] = genre
    if data.get("media_type"):
        payload["media_type"] = data.get("media_type")
    if data.get("total") is not None:
        payload["total"] = data.get("total")
    if data.get("count") is not None:
        payload["count"] = data.get("count")
    if data.get("ambiguous") or data.get("ambiguous_titles"):
        payload["pick"] = True
    # Keep the full genre directory so the UI can switch categories without re-asking.
    genre_catalog = _genre_catalog(data)
    if genre_catalog and name == "plex_browse_genre":
        payload["genres"] = genre_catalog

    if data.get("ambiguous") or data.get("ambiguous_titles"):
        return runtime.upsert_widget(
            Widget(
                id="media",
                kind="media",
                title=title,
                status="info",
                body=str(data.get("speak") or data.get("error") or "Which title should I play?"),
                detail="Tap a card, then Open in Infuse.",
                data=payload,
                sticky=False,
            )
        )
    if not ok:
        return runtime.upsert_widget(
            Widget(
                id="media",
                kind="media",
                title=title,
                status="error",
                body=str(data.get("error") or data.get("speak") or "Could not load media."),
                detail=str(data.get("speak") or ""),
                data=payload,
            )
        )
    return runtime.upsert_widget(
        Widget(
            id="media",
            kind="media",
            title=title,
            status="done",
            body=body,
            detail=detail,
            data=payload,
            # Soft-hide keeps this in memory for reappear; hard X still deletes.
            sticky=False,
        )
    )
