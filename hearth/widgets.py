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
    "radarr_search",
    "sonarr_search",
    "overseerr_search",
}

_DOWNLOAD_TOOLS = {
    "radarr_queue",
    "sonarr_queue",
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
    query = str(data.get("query") or "").strip()
    downloads = _downloads_rows(list(data.get("downloads") or []))
    found = data.get("found")

    if not ok:
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



_MEDIA_STACK_CAP = 5
_SEARCH_HIT_CAP = 4


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
    item = {
        "id": _media_item_id({**row, **art, "title": title}),
        "title": title,
        "type": row.get("type") or row.get("mediaType") or "movie",
        "year": row.get("year"),
        "show": row.get("show") or row.get("grandparentTitle"),
        "summary": row.get("summary") or row.get("overview") or "",
        "contentRating": row.get("contentRating"),
        "rating": row.get("rating") or row.get("audienceRating"),
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
    return item


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
        if not isinstance(item_raw, dict) or not item_raw.get("title"):
            return []
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
    if name in {"plex_search", "radarr_search", "sonarr_search", "overseerr_search"}:
        results = data.get("results") or []
        if not results:
            return []
        source = {
            "plex_search": "plex",
            "radarr_search": "radarr",
            "sonarr_search": "sonarr",
            "overseerr_search": "overseerr",
        }.get(name, "media")
        default_type = "movie" if "radarr" in name else "show"
        out = []
        for hit in results[:_SEARCH_HIT_CAP]:
            if not isinstance(hit, dict):
                continue
            row = {
                **hit,
                "type": hit.get("type") or hit.get("mediaType") or default_type,
            }
            item = _normalize_media_item(row, source=source)
            if item:
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


def _media_panel_copy(active: dict[str, Any], items: list[dict[str, Any]], *, mode: Any = None) -> tuple[str, str, str]:
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
    summary = (active.get("summary") or "").strip()
    if summary:
        detail_parts.append(summary[:220] + ("…" if len(summary) > 220 else ""))
    if active.get("player") and active.get("state"):
        detail_parts.append(f"{active['player']} · {active['state']}")
    elif active.get("player"):
        detail_parts.append(str(active["player"]))
    if active.get("skeleton"):
        detail_parts.append("looking up")
    if mode == "mock":
        detail_parts.append("mock")
    if len(items) > 1:
        detail_parts.append(f"{len(items)} on screen")
    return title, body, " · ".join(detail_parts)


def _media_widget(result: dict[str, Any]) -> Widget | None:
    name = str(result.get("name") or "media")
    data = result.get("data") or {}
    ok = bool(result.get("ok")) and data.get("ok") is not False
    incoming = _media_items_from_tool(name, data)
    if not incoming:
        # Empty search / nothing playing — no overlay (voice/transcript still answers).
        return None
    if result.get("needs_confirm"):
        incoming[0]["pending"] = True

    existing_widget = runtime.get_widget("media")
    existing_items: list[dict[str, Any]] = []
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
    title, body, detail = _media_panel_copy(active, items, mode=data.get("mode"))

    if not ok:
        return runtime.upsert_widget(
            Widget(
                id="media",
                kind="media",
                title=title,
                status="error",
                body=str(data.get("error") or "Could not load media."),
                detail="",
                data={
                    "tool": name,
                    "item": active,
                    "items": items,
                    "active_id": active_id,
                },
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
            data={
                "tool": name,
                "item": active,
                "items": items,
                "active_id": active_id,
            },
            # Soft-hide keeps this in memory for reappear; hard X still deletes.
            sticky=False,
        )
    )
