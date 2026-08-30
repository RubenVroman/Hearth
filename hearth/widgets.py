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



def _pick_media_item(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if name == "plex_now_playing":
        sessions = data.get("sessions") or []
        if not sessions:
            return None
        row = sessions[0]
        art = _art_fields(row)
        return {
            "title": row.get("title"),
            "type": row.get("type") or "movie",
            "year": row.get("year"),
            "show": row.get("show") or row.get("grandparentTitle"),
            "summary": row.get("summary") or "",
            "player": row.get("player"),
            "state": row.get("state"),
            **art,
            "source": "plex",
        }
    if name == "plex_play":
        item = data.get("item") or {}
        if not item.get("title"):
            return None
        client = data.get("client") or {}
        art = _art_fields(item)
        return {
            "title": item.get("title"),
            "type": item.get("type") or "movie",
            "year": item.get("year"),
            "show": item.get("grandparentTitle"),
            "summary": item.get("summary") or "",
            "contentRating": item.get("contentRating"),
            "rating": item.get("rating") or item.get("audienceRating"),
            **art,
            "player": client.get("name"),
            "source": "plex",
            "pending": bool(data.get("needs_confirm") or data.get("would_call_with")),
        }
    if name in {"plex_search", "radarr_search", "sonarr_search", "overseerr_search"}:
        results = data.get("results") or []
        if not results:
            return None
        hit = results[0]
        source = {
            "plex_search": "plex",
            "radarr_search": "radarr",
            "sonarr_search": "sonarr",
            "overseerr_search": "overseerr",
        }.get(name, "media")
        # Overseerr uses mediaId as the TMDB id.
        if hit.get("tmdbId") is None and hit.get("mediaId") is not None:
            hit = {**hit, "tmdbId": hit.get("mediaId")}
        art = _art_fields(hit)
        return {
            "title": hit.get("title") or hit.get("name"),
            "type": hit.get("type") or hit.get("mediaType") or ("movie" if "radarr" in name else "show"),
            "year": hit.get("year"),
            "show": hit.get("grandparentTitle"),
            "summary": hit.get("summary") or hit.get("overview") or "",
            "contentRating": hit.get("contentRating"),
            "rating": hit.get("rating") or hit.get("audienceRating"),
            **art,
            "source": source,
            "result_count": len(results),
        }
    return None


def _media_widget(result: dict[str, Any]) -> Widget | None:
    name = str(result.get("name") or "media")
    data = result.get("data") or {}
    ok = bool(result.get("ok")) and data.get("ok") is not False
    item = _pick_media_item(name, data)
    if item is None:
        # Empty search / nothing playing — no overlay (voice/transcript still answers).
        return None
    if result.get("needs_confirm"):
        item["pending"] = True
    title = str(item.get("title") or "Untitled")
    year = item.get("year")
    media_type = str(item.get("type") or "movie")
    if not ok:
        return runtime.upsert_widget(
            Widget(
                id="media",
                kind="media",
                title=title,
                status="error",
                body=str(data.get("error") or "Could not load media."),
                detail="",
                data={"tool": name, "item": item, **{k: v for k, v in data.items() if k != "results"}},
            )
        )
    bits = [media_type]
    if year:
        bits.append(str(year))
    if item.get("show"):
        bits.append(str(item["show"]))
    body = " · ".join(bits)
    detail_parts = []
    summary = (item.get("summary") or "").strip()
    if summary:
        detail_parts.append(summary[:220] + ("…" if len(summary) > 220 else ""))
    if item.get("player") and item.get("state"):
        detail_parts.append(f"{item['player']} · {item['state']}")
    elif item.get("player"):
        detail_parts.append(str(item["player"]))
    if data.get("mode") == "mock":
        detail_parts.append("mock")
    n = item.get("result_count")
    if isinstance(n, int) and n > 1:
        detail_parts.append(f"{n} matches")
    return runtime.upsert_widget(
        Widget(
            id="media",
            kind="media",
            title=title,
            status="done",
            body=body,
            detail=" · ".join(detail_parts),
            data={"tool": name, "item": item},
            # Soft-hide keeps this in memory for reappear; hard X still deletes.
            sticky=False,
        )
    )
