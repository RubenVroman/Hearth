from __future__ import annotations

from typing import Any

from hearth.agent.registry import ToolSpec, registry
from hearth.memory.tools import register_memory_tools
from hearth.tools import files as workspace_files
from hearth.tools.arr import overseerr, radarr, sonarr
from hearth.tools.cos import cos_configured, escalate, not_configured_message
from hearth.tools.docker import docker
from hearth.tools.ha import ha
from hearth.tools.infuse import infuse
from hearth.tools.media import house_media_inventory, media_activity, media_control
from hearth.tools.plex import plex
from hearth.tools.skills import load_workspace_skills
from hearth.tools.thuisbezorgd import thuisbezorgd
from hearth.tools.videoland import videoland
from hearth.tools.weather import fetch_weather
from hearth.tools.suggest import suggest_titles
from hearth.tools.websearch import web_search


async def _ha_list(args: dict[str, Any]) -> dict[str, Any]:
    return await ha.list_states(args.get("domain"))


async def _ha_state(args: dict[str, Any]) -> dict[str, Any]:
    entity_id = str(args.get("entity_id") or "")
    if not entity_id:
        return {"ok": False, "error": "entity_id required"}
    return await ha.get_state(entity_id)


async def _ha_call(args: dict[str, Any]) -> dict[str, Any]:
    domain = str(args.get("domain") or "")
    service = str(args.get("service") or "")
    entity_id = str(args.get("entity_id") or "")
    data = args.get("data") if isinstance(args.get("data"), dict) else {}
    if not domain or not service or not entity_id:
        return {"ok": False, "error": "domain, service, and entity_id required"}
    return await ha.call_service(domain, service, entity_id, data)


async def _house_media(_args: dict[str, Any]) -> dict[str, Any]:
    return await house_media_inventory()


async def _house_network(args: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = int(args.get("limit") or 250)
    except (TypeError, ValueError):
        limit = 250
    return await ha.network_inventory(limit=limit)


async def _ha_device_control(args: dict[str, Any]) -> dict[str, Any]:
    device = str(args.get("device") or "")
    action = str(args.get("action") or "")
    if not device or not action:
        return {"ok": False, "error": "device and action required"}
    return await ha.control_entity(
        device,
        action,
        domain=str(args.get("domain") or "") or None,
        value=args.get("value"),
    )


async def _media_activity(args: dict[str, Any]) -> dict[str, Any]:
    return await media_activity(str(args.get("activity") or ""))


async def _ha_media(args: dict[str, Any]) -> dict[str, Any]:
    device = str(args.get("device") or "")
    action = str(args.get("action") or "")
    if not device or not action:
        return {"ok": False, "error": "device and action required"}
    volume = args.get("volume_level")
    muted = args.get("is_volume_muted")
    try:
        return await media_control(
            device,
            action,
            volume_level=float(volume) if volume is not None else None,
            source=str(args.get("source") or "") or None,
            media_content_id=str(args.get("media_content_id") or "") or None,
            media_content_type=str(args.get("media_content_type") or "") or None,
            is_volume_muted=bool(muted) if muted is not None else None,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _videoland_play(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("query") or args.get("title") or "").strip()
    profile = str(args.get("profile") or "").strip()
    prepare = args.get("prepare_path")
    return await videoland.play(
        title,
        profile=profile,
        prepare_path=True if prepare is None else bool(prepare),
    )


async def _plex_now(_args: dict[str, Any]) -> dict[str, Any]:
    return await plex.now_playing()


async def _plex_search(args: dict[str, Any]) -> dict[str, Any]:
    return await plex.search(str(args.get("query") or ""), int(args.get("limit") or 8))


async def _plex_clients(_args: dict[str, Any]) -> dict[str, Any]:
    return await plex.clients()


async def _plex_browse_genre(args: dict[str, Any]) -> dict[str, Any]:
    media_type = str(args.get("type") or args.get("media_type") or "movie")
    limit = args.get("limit")
    try:
        limit_n = int(limit) if limit is not None else 24
    except (TypeError, ValueError):
        limit_n = 24
    return await plex.browse_genre(
        str(args.get("genre") or ""),
        media_type=media_type,
        limit=limit_n,
    )


async def _plex_play(args: dict[str, Any]) -> dict[str, Any]:
    rating = args.get("ratingKey") or args.get("rating_key")
    offset = args.get("offset_ms") or args.get("offset") or 0
    try:
        offset_ms = int(offset)
    except (TypeError, ValueError):
        offset_ms = 0
    wait = args.get("wait_for_client")
    if wait is None:
        # First ask: no wait (instant guidance). Confirm / Try again: re-poll briefly.
        wait = bool(args.get("confirm"))
    wait_timeout = args.get("wait_timeout_s")
    try:
        wait_timeout_s = float(wait_timeout) if wait_timeout is not None else None
    except (TypeError, ValueError):
        wait_timeout_s = None
    return await plex.play(
        str(args.get("query") or ""),
        player=str(args.get("player") or "") or None,
        rating_key=rating,
        offset_ms=offset_ms,
        wait_for_client=bool(wait),
        wait_timeout_s=wait_timeout_s,
    )


async def _plex_play_preview(args: dict[str, Any]) -> dict[str, Any]:
    """Dry-run resolve: title + client + already-playing, without playMedia.

    Does not wait for offline clients — returns needs_client + keeps pending so
    confirm / Try again can re-poll once Plex is open on the Apple TV.
    """
    rating = args.get("ratingKey") or args.get("rating_key")
    offset = args.get("offset_ms") or args.get("offset") or 0
    try:
        offset_ms = int(offset)
    except (TypeError, ValueError):
        offset_ms = 0
    return await plex.resolve_play(
        str(args.get("query") or ""),
        player=str(args.get("player") or "") or None,
        rating_key=rating,
        offset_ms=offset_ms,
        wait_for_client=False,
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _infuse_play(args: dict[str, Any]) -> dict[str, Any]:
    return await infuse.play(
        str(args.get("query") or ""),
        tmdb_id=_optional_int(args.get("tmdbId") or args.get("tmdb_id")),
        rating_key=args.get("ratingKey") or args.get("rating_key"),
        season=_optional_int(args.get("season")),
        episode=_optional_int(args.get("episode")),
        play=bool(args.get("play", True)),
    )


async def _infuse_play_preview(args: dict[str, Any]) -> dict[str, Any]:
    return await infuse.resolve_play(
        str(args.get("query") or ""),
        tmdb_id=_optional_int(args.get("tmdbId") or args.get("tmdb_id")),
        rating_key=args.get("ratingKey") or args.get("rating_key"),
        season=_optional_int(args.get("season")),
        episode=_optional_int(args.get("episode")),
        play=bool(args.get("play", True)),
    )


async def _infuse_transport(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "")
    if not action:
        return {"ok": False, "error": "action required (pause, play, stop, skip, previous)"}
    return await infuse.transport(action)


async def _docker_ps(_args: dict[str, Any]) -> dict[str, Any]:
    return await docker.ps()


async def _docker_inspect(args: dict[str, Any]) -> dict[str, Any]:
    container = str(args.get("container") or "")
    if not container:
        return {"ok": False, "error": "container required"}
    return await docker.inspect(container)


async def _docker_stop(args: dict[str, Any]) -> dict[str, Any]:
    container = str(args.get("container") or "")
    if not container:
        return {"ok": False, "error": "container required"}
    return await docker.stop(container)


async def _ws_list(args: dict[str, Any]) -> dict[str, Any]:
    return workspace_files.list_dir(str(args.get("path") or "."))


async def _ws_read(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    if not path:
        return {"ok": False, "error": "path required"}
    return workspace_files.read_file(path)


async def _ws_write(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    content = str(args.get("content") or "")
    if not path:
        return {"ok": False, "error": "path required"}
    result = workspace_files.write_file(path, content)
    if path.startswith("skills/") and path.endswith(".py"):
        load_workspace_skills()
        result["skills_reloaded"] = True
    return result


async def _ws_delete(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    if not path:
        return {"ok": False, "error": "path required"}
    return workspace_files.delete_file(path)


async def _chief_of_staff(args: dict[str, Any]) -> dict[str, Any]:
    return await escalate(args)


async def _radarr_search(args: dict[str, Any]) -> dict[str, Any]:
    return await radarr.search(str(args.get("query") or ""))


async def _radarr_add(args: dict[str, Any]) -> dict[str, Any]:
    tmdb = args.get("tmdbId")
    return await radarr.add(str(args.get("query") or ""), tmdb_id=int(tmdb) if tmdb else None)


async def _radarr_queue(args: dict[str, Any]) -> dict[str, Any]:
    return await radarr.queue(str(args.get("query") or args.get("title") or ""))


async def _radarr_retry(args: dict[str, Any]) -> dict[str, Any]:
    return await radarr.retry_download(
        str(args.get("query") or args.get("title") or ""),
        force=True,
        reason="user:house",
    )


async def _sonarr_search(args: dict[str, Any]) -> dict[str, Any]:
    return await sonarr.search(str(args.get("query") or ""))


async def _sonarr_add(args: dict[str, Any]) -> dict[str, Any]:
    tvdb = args.get("tvdbId")
    return await sonarr.add(str(args.get("query") or ""), tvdb_id=int(tvdb) if tvdb else None)


async def _sonarr_queue(args: dict[str, Any]) -> dict[str, Any]:
    return await sonarr.queue(str(args.get("query") or args.get("title") or ""))


async def _sonarr_retry(args: dict[str, Any]) -> dict[str, Any]:
    return await sonarr.retry_download(
        str(args.get("query") or args.get("title") or ""),
        force=True,
        reason="user:house",
    )


async def _overseerr_search(args: dict[str, Any]) -> dict[str, Any]:
    return await overseerr.search(str(args.get("query") or ""))


async def _overseerr_request(args: dict[str, Any]) -> dict[str, Any]:
    media_id = args.get("mediaId")
    return await overseerr.request(
        str(args.get("query") or ""),
        media_id=int(media_id) if media_id else None,
        media_type=str(args.get("mediaType") or "") or None,
    )


async def _tb_restaurants(args: dict[str, Any]) -> dict[str, Any]:
    return await thuisbezorgd.restaurants(
        cuisine=str(args.get("cuisine") or ""),
        query=str(args.get("query") or ""),
    )


async def _tb_menu(args: dict[str, Any]) -> dict[str, Any]:
    restaurant_id = str(args.get("restaurant_id") or args.get("restaurantId") or "")
    return await thuisbezorgd.menu(restaurant_id)


async def _tb_cart(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "view").strip().lower()
    if action in {"view", "show", "get", "status"}:
        return thuisbezorgd.cart_view()
    if action in {"clear", "empty", "reset"}:
        return thuisbezorgd.cart_clear()
    if action in {"remove", "delete"}:
        item_id = str(args.get("item_id") or args.get("itemId") or "")
        if not item_id:
            return {"ok": False, "error": "item_id required to remove from cart"}
        return thuisbezorgd.cart_remove(item_id)
    if action in {"add", "put"}:
        restaurant_id = str(args.get("restaurant_id") or args.get("restaurantId") or "")
        item_id = str(args.get("item_id") or args.get("itemId") or "")
        if not restaurant_id or not item_id:
            return {"ok": False, "error": "restaurant_id and item_id required to add"}
        qty = args.get("quantity")
        return thuisbezorgd.cart_add(
            restaurant_id=restaurant_id,
            item_id=item_id,
            quantity=int(qty) if qty is not None else 1,
            notes=str(args.get("notes") or ""),
        )
    return {
        "ok": False,
        "error": f"unknown cart action {action!r}; use view, add, remove, or clear",
    }


async def _tb_auth(_args: dict[str, Any]) -> dict[str, Any]:
    return thuisbezorgd.auth_status()


async def _tb_order(_args: dict[str, Any]) -> dict[str, Any]:
    # confirm/dry-run enforced by ToolRegistry (destructive=True).
    return await thuisbezorgd.place_order()


def _tb_order_preview(_args: dict[str, Any]) -> dict[str, Any]:
    """Attach restaurant / items / price / address so Confirm UX is speakable."""
    address = thuisbezorgd.delivery_address()
    cart = thuisbezorgd.cart_view().get("cart") or {}
    return {
        "restaurant": cart.get("restaurant"),
        "items": cart.get("items") or [],
        "total": cart.get("total"),
        "total_cents": cart.get("total_cents"),
        "delivery_address": address.get("line") or None,
        "delivery_configured": address.get("configured"),
        "live_submit_ready": thuisbezorgd.live_submit_ready,
        "widget": "order_status",
        "summary": (
            f"{(cart.get('restaurant') or {}).get('name') or 'Restaurant'}: "
            f"{len(cart.get('items') or [])} item(s) for {cart.get('total') or '€0.00'} "
            f"→ {address.get('line') or '(set HEARTH_DELIVERY_* )'}"
        ),
    }

async def _get_weather(args: dict[str, Any]) -> dict[str, Any]:
    place = args.get("place")
    return await fetch_weather(place=str(place) if place else None)


async def _web_search(args: dict[str, Any]) -> dict[str, Any]:
    return await web_search(args)


async def _suggest_titles(args: dict[str, Any]) -> dict[str, Any]:
    return await suggest_titles(args)


async def _end_call(args: dict[str, Any]) -> dict[str, Any]:
    """Signal close-of-call. Sideband / client tear down the live WebRTC session."""
    reason = str(args.get("reason") or "close_of_call").strip() or "close_of_call"
    return {"ok": True, "ended": True, "reason": reason}


def register_builtin_tools() -> None:
    registry.register(
        ToolSpec(
            name="get_weather",
            description="Current weather near the house (temperature, condition, wind). Use when asked for the weather or forecast outside.",
            parameters={
                "type": "object",
                "properties": {
                    "place": {
                        "type": "string",
                        "description": "Optional label (defaults to the house place).",
                    }
                },
            },
            handler=_get_weather,
        )
    )
    registry.register(
        ToolSpec(
            name="web_search",
            description=(
                "Search the live public web and return a short speakable summary "
                "(title, snippet, source) of a few results. Use for current events, "
                "news, sports scores, where-to-watch / streaming availability, and "
                "anything that needs up-to-date internet — not the house Plex library, "
                "not Home Assistant, not secrets or local/internal URLs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. where to watch The Bear in Belgium",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many results to keep (1–5, default 4).",
                    },
                    "locale": {
                        "type": "string",
                        "description": "Optional locale like nl-BE or en-US (defaults near the house).",
                    },
                },
                "required": ["query"],
            },
            handler=_web_search,
        )
    )
    registry.register(
        ToolSpec(
            name="ha_list_entities",
            description="List Home Assistant entities (lights, scenes, media_player including Denon AVR-X3700H and LG webOS TV). Filter with domain.",
            parameters={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Optional domain filter: light, scene, media_player, switch.",
                    }
                },
            },
            handler=_ha_list,
        )
    )
    registry.register(
        ToolSpec(
            name="ha_get_state",
            description="Get a single Home Assistant entity state.",
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "e.g. light.living_room"}
                },
                "required": ["entity_id"],
            },
            handler=_ha_state,
        )
    )
    registry.register(
        ToolSpec(
            name="ha_call_service",
            description=(
                "Call a Home Assistant service (lights, scenes, Denon, LG TV). "
                "Runs immediately — no confirm step for routine house control."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "light, scene, media_player, switch"},
                    "service": {
                        "type": "string",
                        "description": "turn_on, turn_off, toggle, volume_set, select_source, …",
                    },
                    "entity_id": {"type": "string"},
                    "data": {"type": "object", "description": "Extra service data (brightness, volume_level, source)"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["domain", "service", "entity_id"],
            },
            handler=_ha_call,
        )
    )
    registry.register(
        ToolSpec(
            name="house_media",
            description=(
                "House media inventory the agent can speak: LG webOS TV and Denon AVR status "
                "via Home Assistant, plus Plex now-playing and which backends have API keys. "
                "Use for “what's on the TV”, “media status”, “is the AVR on”."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_house_media,
        )
    )
    registry.register(
        ToolSpec(
            name="house_network",
            description=(
                "Inventory every entity Home Assistant currently represents on the house network, "
                "including reachability, unavailable devices, controllable domains, and dedicated "
                "Denon/LG/Apple TV connection checks. Use for network/device/connectivity audits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum entity rows to return (summary always covers all).",
                    }
                },
            },
            handler=_house_network,
        )
    )
    registry.register(
        ToolSpec(
            name="ha_device_control",
            description=(
                "Resolve any routine Home Assistant device by entity id or friendly name and control "
                "it. Supports lights, switches, fans, covers, climate, media, remotes, scenes, "
                "scripts, buttons and vacuums. Never guess an entity id; ambiguous names are returned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Friendly name or entity_id."},
                    "action": {
                        "type": "string",
                        "description": (
                            "turn_on, turn_off, toggle, brightness, open, close, stop, "
                            "set_temperature, set_percentage, activate, press, start, return_to_base"
                        ),
                    },
                    "domain": {"type": "string", "description": "Optional domain disambiguation."},
                    "value": {
                        "type": "number",
                        "description": "Optional brightness %, temperature, or fan percentage.",
                    },
                },
                "required": ["device", "action"],
            },
            handler=_ha_device_control,
        )
    )
    registry.register(
        ToolSpec(
            name="media_activity",
            description=(
                "Prepare or stop the receiver-centric living-room chain. apple_tv wakes Denon, LG, "
                "selects the Denon Apple TV input, then wakes Apple TV; tv selects TV Audio; off "
                "powers Apple TV, LG, then Denon down. Runs immediately."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "activity": {
                        "type": "string",
                        "enum": ["apple_tv", "tv", "off"],
                    }
                },
                "required": ["activity"],
            },
            handler=_media_activity,
        )
    )
    registry.register(
        ToolSpec(
            name="ha_media_control",
            description=(
                "Control the LG webOS TV, Denon AVR, or Apple TV via Home Assistant "
                "media_player services. Prefer this over raw ha_call_service for TV/AVR/ATV. "
                "Runs immediately — no confirm step. "
                "device=tv|avr|apple_tv; action=turn_on|turn_off|volume_set|volume_mute|unmute|"
                "volume_up|volume_down|select_source|play_media|media_play|media_pause|"
                "media_stop|media_next_track|media_previous_track."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": (
                            "tv (LG webOS), avr (Denon), or apple_tv (HA Apple TV / Infuse transport). "
                            "Aliases: lg, denon, receiver, atv, infuse."
                        ),
                    },
                    "action": {"type": "string"},
                    "volume_level": {
                        "type": "number",
                        "description": "0.0–1.0 or 0–100 for volume_set",
                    },
                    "source": {"type": "string", "description": "Input/source name for select_source"},
                    "media_content_id": {"type": "string"},
                    "media_content_type": {"type": "string"},
                    "is_volume_muted": {"type": "boolean"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["device", "action"],
            },
            handler=_ha_media,
        )
    )
    registry.register(
        ToolSpec(
            name="videoland_play",
            description=(
                "Open Videoland on the living-room LG webOS TV via Home Assistant "
                "(media_player.select_source). Use for Dutch/English asks like "
                "“zet B&B Vol Liefde aan op Videoland”, “play X on Videoland”, "
                "“open Videoland”, or “open the Parel profile”. "
                "HA can launch the Videoland app but CANNOT start a specific title or "
                "select an in-app profile — there is no webOS/Videoland contentId or "
                "profile API through HA. Always speak the tool's limitation + workaround "
                "(pick the title/profile on the TV). Never claim playback or profile "
                "selection succeeded. Runs immediately — no confirm step. "
                "Do not escalate to Chief of Staff for Videoland title/profile asks."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Title to request in Videoland, e.g. B&B Vol Liefde. "
                            "Optional when only opening the app."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Alias for query.",
                    },
                    "profile": {
                        "type": "string",
                        "description": (
                            "Optional profile name (e.g. Parel). Accepted for the ask, "
                            "but HA cannot select Videoland profiles — speak that limit."
                        ),
                    },
                    "prepare_path": {
                        "type": "boolean",
                        "description": (
                            "Wake the receiver-centric TV path before launching "
                            "(default true)."
                        ),
                    },
                },
            },
            handler=_videoland_play,
        )
    )
    registry.register(
        ToolSpec(
            name="plex_now_playing",
            description="What is currently playing on Plex.",
            parameters={"type": "object", "properties": {}},
            handler=_plex_now,
        )
    )
    registry.register(
        ToolSpec(
            name="plex_search",
            description=(
                "Search the Plex library. Returns title, type, year, ratingKey, key, guid. "
                "Use before plex_play when you need to pick among matches. "
                "If the title is missing, say so — do not silently queue Radarr."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=_plex_search,
        )
    )
    registry.register(
        ToolSpec(
            name="plex_clients",
            description=(
                "List controllable Plex clients (Apple TV, LG webOS, Shield, …). "
                "Use when the user asks which players are available, or before plex_play "
                "if the target is ambiguous."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_plex_clients,
        )
    )
    registry.register(
        ToolSpec(
            name="plex_browse_genre",
            description=(
                "Browse the Plex movie (or show) library by genre. "
                "Use for Animation / Comedy / Horror / … lists — e.g. 'what animation "
                "movies do we have'. Returns a speakable summary (count + a few titles "
                "with years), not a huge dump. Omit genre (or pass list) to list available "
                "genres. Prefer this over plex_search when the ask is about a genre, not "
                "a specific title."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "genre": {
                        "type": "string",
                        "description": (
                            "Genre name, e.g. Animation, Comedy, Science Fiction. "
                            "Empty / 'list' lists available genres."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": "movie (default) or show",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max titles to return in results (spoken list stays short)",
                    },
                },
            },
            handler=_plex_browse_genre,
        )
    )
    registry.register(
        ToolSpec(
            name="plex_play",
            description=(
                "Start playback of a specific Plex library title on a Plex client "
                "(LG TV / Shield / living-room / explicit Plex player). Prefer infuse_play for "
                "Apple TV unless HEARTH_APPLE_TV_PLAYER=plex or the user asks for Plex. "
                "Searches the library (asks when multiple titles match), resolves the player "
                "(named hint, else active/recent session, else PLEX_DEFAULT_PLAYER, else "
                "Apple TV/LG/living room, else the only client), then playMedia via the PMS. "
                "Runs immediately — including switching away from whatever is already playing. "
                "If no clients are online, guides the user to open Plex and keeps the play "
                "ready for confirm / Try again (confirm re-polls briefly for the client). "
                "If the title is not in the library, say so clearly — do not silently grab it in Radarr."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Title to play, e.g. The Endless",
                    },
                    "player": {
                        "type": "string",
                        "description": "Optional client hint: Apple TV, LG, living room, …",
                    },
                    "ratingKey": {
                        "type": "string",
                        "description": "Optional Plex ratingKey when already known from plex_search",
                    },
                    "offset_ms": {
                        "type": "integer",
                        "description": "Start offset in milliseconds (default 0)",
                    },
                    "wait_for_client": {
                        "type": "boolean",
                        "description": (
                            "On confirm, re-poll for online clients briefly (default true). "
                            "Dry-run never waits."
                        ),
                    },
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["query"],
            },
            handler=_plex_play,
            preview_handler=_plex_play_preview,
        )
    )
    registry.register(
        ToolSpec(
            name="infuse_play",
            description=(
                "Play a library title in Infuse (Firecore) on the Apple TV. Prefer this over "
                "plex_play when the target is Apple TV / Infuse — Ruben uses Infuse, not the "
                "Plex tvOS app. Resolves title → TMDB id (Plex Guids, else Radarr/Overseerr), "
                "builds infuse://movie/{tmdb}?play (or series/season/episode), and launches it "
                "via Home Assistant's Apple TV media_player.play_media (type=url). "
                "Requires HA Apple TV paired (HA_APPLE_TV_ENTITY). Infuse has no now-playing API. "
                "Runs immediately — no confirm step. If HA Apple TV is missing, returns "
                "clear setup steps — do not silently no-op or fall back to opening Plex."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Title to play, e.g. The Endless",
                    },
                    "tmdbId": {
                        "type": "integer",
                        "description": "Optional TMDB id when already known",
                    },
                    "ratingKey": {
                        "type": "string",
                        "description": "Optional Plex ratingKey from plex_search",
                    },
                    "season": {"type": "integer"},
                    "episode": {"type": "integer"},
                    "play": {
                        "type": "boolean",
                        "description": "Append ?play to the Infuse deep link (default true)",
                    },
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["query"],
            },
            handler=_infuse_play,
            preview_handler=_infuse_play_preview,
        )
    )
    registry.register(
        ToolSpec(
            name="infuse_transport",
            description=(
                "Pause / play / stop / skip on the Apple TV while Infuse (or another app) is "
                "active. Uses Home Assistant Apple TV media_player remote commands — Infuse has "
                "no playback-state or transport API. Runs immediately — no confirm step. "
                "action=pause|play|stop|skip|previous."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "pause, play, stop, skip (next), or previous",
                    },
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["action"],
            },
            handler=_infuse_transport,
        )
    )
    registry.register(
        ToolSpec(
            name="docker_ps",
            description="List Docker containers on VAULT (inspect only).",
            parameters={"type": "object", "properties": {}},
            handler=_docker_ps,
        )
    )
    registry.register(
        ToolSpec(
            name="docker_inspect",
            description="Inspect one Docker container by name or id.",
            parameters={
                "type": "object",
                "properties": {"container": {"type": "string"}},
                "required": ["container"],
            },
            handler=_docker_inspect,
        )
    )
    registry.register(
        ToolSpec(
            name="docker_stop",
            description=(
                "Stop a Docker container. High-risk: dry-run unless confirm=true "
                "(voice/UI confirm)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "container": {"type": "string"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["container"],
            },
            handler=_docker_stop,
            destructive=True,
        )
    )
    registry.register(
        ToolSpec(
            name="workspace_list",
            description="List files in the sandboxed Hearth workspace (not the whole NAS).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path inside workspace"}},
            },
            handler=_ws_list,
        )
    )
    registry.register(
        ToolSpec(
            name="workspace_read",
            description="Read a file from the sandboxed workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=_ws_read,
        )
    )
    registry.register(
        ToolSpec(
            name="workspace_write",
            description=(
                "Write a file in the VAULT workspace sandbox only — not the git repo. "
                "For repo/PR/feature work call chief_of_staff. Runs immediately in the sandbox."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            handler=_ws_write,
        )
    )
    registry.register(
        ToolSpec(
            name="workspace_delete",
            description=(
                "Delete a workspace file. Irreversible: dry-run unless confirm=true. "
                "Cannot leave the workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["path"],
            },
            handler=_ws_delete,
            destructive=True,
        )
    )
    registry.register(
        ToolSpec(
            name="chief_of_staff",
            description=(
                "Escalate work Hearth cannot do to Chief of Staff: repo/code/PR/git, new features "
                "(Discord, integrations), Gridways/kanban/boards/tasks on a project, calendar, "
                "GitHub/GitLab org work, teammate agents. Hearth must NOT edit GitHub or pretend "
                "it connected. Call immediately when asked — no confirm step."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear instruction for Chief of Staff (what to change).",
                    },
                    "said": {
                        "type": "string",
                        "description": "Original user utterance, verbatim.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "owner/name. Defaults to RubenVroman/Hearth.",
                    },
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["task"],
            },
            handler=_chief_of_staff,
            configured=cos_configured,
            not_configured=not_configured_message(),
        )
    )
    registry.register(
        ToolSpec(
            name="radarr_search",
            description="Search Radarr for a movie to download. Use this (not Plex) when grabbing a film.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=_radarr_search,
        )
    )
    registry.register(
        ToolSpec(
            name="radarr_add",
            description=(
                "Add a movie to the Radarr download queue. Runs immediately — say you'll grab it "
                "in Radarr."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "tmdbId": {"type": "integer"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["query"],
            },
            handler=_radarr_add,
        )
    )
    registry.register(
        ToolSpec(
            name="radarr_queue",
            description=(
                "Radarr download progress: list active downloads or look up one title. "
                "Returns status (queued/downloading/paused/importing/stalled/completed/failed), "
                "percent complete, time left, quality/indexer when available. "
                "Use for “how far along is Annihilation”, “what's downloading”, "
                "“download progress for X”. Pass query/title to filter; omit for the full queue."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional movie title to look up in the download queue.",
                    },
                    "title": {"type": "string", "description": "Alias for query."},
                },
            },
            handler=_radarr_queue,
        )
    )
    registry.register(
        ToolSpec(
            name="radarr_retry",
            description=(
                "Retry a stalled/failed Radarr movie download from a different indexer/source. "
                "Blocklists the bad release and grabs an alternate *arr release for the SAME "
                "movie (does not delete the library entry; does not re-POST Overseerr). "
                "Use when the user says the download didn’t work, stalled, or wants another "
                "source / a new one for a title already downloading. Pass query/title."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Movie title already in the Radarr queue to retry.",
                    },
                    "title": {"type": "string", "description": "Alias for query."},
                },
                "required": ["query"],
            },
            handler=_radarr_retry,
        )
    )
    registry.register(
        ToolSpec(
            name="sonarr_search",
            description="Search Sonarr for a TV series. Use this (not Plex) when grabbing a show.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=_sonarr_search,
        )
    )
    registry.register(
        ToolSpec(
            name="sonarr_add",
            description=(
                "Add a series to the Sonarr download queue. Runs immediately — say you'll grab it "
                "in Sonarr."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "tvdbId": {"type": "integer"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["query"],
            },
            handler=_sonarr_add,
        )
    )
    registry.register(
        ToolSpec(
            name="sonarr_queue",
            description=(
                "Sonarr download progress for TV: list active downloads or look up one title. "
                "Same fields as radarr_queue. Use when the ask is clearly about a show/series."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional series/episode title to look up.",
                    },
                    "title": {"type": "string", "description": "Alias for query."},
                },
            },
            handler=_sonarr_queue,
        )
    )
    registry.register(
        ToolSpec(
            name="sonarr_retry",
            description=(
                "Retry a stalled/failed Sonarr episode download from a different indexer/source. "
                "Blocklists the bad release and grabs an alternate *arr release for the SAME "
                "episode. Use for TV when the user says the download didn’t work or wants "
                "another source. Pass query/title."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Series/episode title already in the Sonarr queue.",
                    },
                    "title": {"type": "string", "description": "Alias for query."},
                },
                "required": ["query"],
            },
            handler=_sonarr_retry,
        )
    )
    registry.register(
        ToolSpec(
            name="overseerr_search",
            description="Search Overseerr, the request front door for movies and TV on VAULT.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=_overseerr_search,
        )
    )
    registry.register(
        ToolSpec(
            name="suggest_titles",
            description=(
                "Show a list of suggested movie/TV titles on the house glass overlay "
                "(posters, year, short blurb, TMDB link). Use when recommending titles, "
                "after web-sourced movie ideas, or when asked to 'show them on the UI / screen'. "
                "Pass titles=[...] when you already have names; or query= for a freeform "
                "recommendation mood (e.g. 'mind-bending sci-fi'). Resolves metadata via "
                "Overseerr / Radarr / Sonarr server-side — never invent posters. Prefer this "
                "over speaking a long list with no overlay. For titles already in the Plex "
                "library by genre, prefer plex_browse_genre instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit title list, e.g. ['Dune: Part Two', 'Arrival']",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional mood/theme when titles are unknown, or a "
                            "comma/newline-separated title list"
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": "movie, tv, or any (default any)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max cards to show (default 4, max 6)",
                    },
                },
            },
            handler=_suggest_titles,
        )
    )
    registry.register(
        ToolSpec(
            name="overseerr_request",
            description=(
                "Request a movie or show via Overseerr (feeds Radarr/Sonarr). "
                "Runs immediately when the title (or mediaId+mediaType) is a confident "
                "match. Refuses mismatched search/fallback hits — returns choices to "
                "disambiguate instead of queueing the wrong film."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mediaId": {"type": "integer"},
                    "mediaType": {"type": "string", "description": "movie or tv"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["query"],
            },
            handler=_overseerr_request,
        )
    )
    registry.register(
        ToolSpec(
            name="thuisbezorgd_restaurants",
            description=(
                "Browse nearby Thuisbezorgd restaurants for the house delivery address "
                "(HEARTH_DELIVERY_*). Optional cuisine or name query. Returns a restaurant_list "
                "payload the UI may overlay later."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cuisine": {"type": "string", "description": "e.g. pizza, vietnamese"},
                    "query": {"type": "string", "description": "Restaurant name filter"},
                },
            },
            handler=_tb_restaurants,
        )
    )
    registry.register(
        ToolSpec(
            name="thuisbezorgd_menu",
            description="Fetch a Thuisbezorgd restaurant menu by restaurant_id from thuisbezorgd_restaurants.",
            parameters={
                "type": "object",
                "properties": {
                    "restaurant_id": {"type": "string"},
                },
                "required": ["restaurant_id"],
            },
            handler=_tb_menu,
        )
    )
    registry.register(
        ToolSpec(
            name="thuisbezorgd_cart",
            description=(
                "View or mutate the in-session Thuisbezorgd cart. "
                "action=view|add|remove|clear. For add: restaurant_id + item_id (+ optional quantity)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "view, add, remove, or clear",
                    },
                    "restaurant_id": {"type": "string"},
                    "item_id": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "notes": {"type": "string"},
                },
            },
            handler=_tb_cart,
        )
    )
    registry.register(
        ToolSpec(
            name="thuisbezorgd_auth_status",
            description=(
                "Thuisbezorgd auth/config status (partner key present?, session?, delivery address). "
                "Never returns credentials or tokens."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_tb_auth,
        )
    )
    registry.register(
        ToolSpec(
            name="thuisbezorgd_order",
            description=(
                "Place the current Thuisbezorgd cart. Spends money — always dry-run unless "
                "confirm=true. Before confirm, preview shows restaurant, items, price, and delivery "
                "address. No auto-reorder. Live paid submit only when THUISBEZORGD_API_KEY + session "
                "are configured; otherwise confirm places a fixture order only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
            },
            handler=_tb_order,
            destructive=True,
            preview=_tb_order_preview,
        )
    )
    registry.register(
        ToolSpec(
            name="end_call",
            description=(
                "End the live voice conversation and close the connection. Call this when the "
                "exchange is finished — explicit goodbye/done, nothing left to do, or a natural "
                "close-of-call. Say a brief farewell first, then call end_call in the same turn. "
                "Do not use between ordinary turns."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the call is ending, e.g. goodbye, done, natural_end.",
                    }
                },
            },
            handler=_end_call,
        )
    )
    load_workspace_skills()
    register_memory_tools()
