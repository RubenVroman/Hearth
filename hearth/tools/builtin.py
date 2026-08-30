from __future__ import annotations

from typing import Any

from hearth.agent.registry import ToolSpec, registry
from hearth.tools import files as workspace_files
from hearth.tools.arr import overseerr, radarr, sonarr
from hearth.tools.cos import cos_configured, escalate, not_configured_message
from hearth.tools.docker import docker
from hearth.tools.ha import ha
from hearth.tools.infuse import infuse
from hearth.tools.media import house_media_inventory, media_control
from hearth.tools.plex import plex
from hearth.tools.skills import load_workspace_skills


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


async def _plex_now(_args: dict[str, Any]) -> dict[str, Any]:
    return await plex.now_playing()


async def _plex_search(args: dict[str, Any]) -> dict[str, Any]:
    return await plex.search(str(args.get("query") or ""), int(args.get("limit") or 8))


async def _plex_clients(_args: dict[str, Any]) -> dict[str, Any]:
    return await plex.clients()


async def _plex_play(args: dict[str, Any]) -> dict[str, Any]:
    rating = args.get("ratingKey") or args.get("rating_key")
    offset = args.get("offset_ms") or args.get("offset") or 0
    try:
        offset_ms = int(offset)
    except (TypeError, ValueError):
        offset_ms = 0
    wait = args.get("wait_for_client")
    if wait is None:
        wait = True
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


async def _sonarr_search(args: dict[str, Any]) -> dict[str, Any]:
    return await sonarr.search(str(args.get("query") or ""))


async def _sonarr_add(args: dict[str, Any]) -> dict[str, Any]:
    tvdb = args.get("tvdbId")
    return await sonarr.add(str(args.get("query") or ""), tvdb_id=int(tvdb) if tvdb else None)


async def _overseerr_search(args: dict[str, Any]) -> dict[str, Any]:
    return await overseerr.search(str(args.get("query") or ""))


async def _overseerr_request(args: dict[str, Any]) -> dict[str, Any]:
    media_id = args.get("mediaId")
    return await overseerr.request(
        str(args.get("query") or ""),
        media_id=int(media_id) if media_id else None,
        media_type=str(args.get("mediaType") or "") or None,
    )


def register_builtin_tools() -> None:
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
            description="Call a Home Assistant service (lights, scenes, Denon, LG TV). Destructive: defaults to dry-run unless confirm=true.",
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
            destructive=True,
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
            name="ha_media_control",
            description=(
                "Control the LG webOS TV, Denon AVR, or Apple TV via Home Assistant "
                "media_player services. Prefer this over raw ha_call_service for TV/AVR/ATV. "
                "Destructive: dry-run unless confirm=true. "
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
            destructive=True,
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
            name="plex_play",
            description=(
                "Start playback of a specific Plex library title on a Plex client "
                "(Apple TV / LG TV / living-room player) without tapping Play on the client. "
                "Searches the library (asks when multiple titles match), resolves the player "
                "(named hint, else active/recent session, else PLEX_DEFAULT_PLAYER, else "
                "Apple TV/LG/living room, else the only client), then playMedia via the PMS. "
                "If no clients are online, guides the user to open Plex and keeps the play "
                "ready for confirm / Try again (confirm re-polls briefly for the client). "
                "Destructive: dry-run unless confirm=true. Confirm also covers switching away "
                "from whatever is already playing. If the title is not in the library, say so "
                "clearly — do not silently grab it in Radarr."
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
            destructive=True,
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
                "Destructive: dry-run unless confirm=true. If HA Apple TV is missing, returns "
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
            destructive=True,
        )
    )
    registry.register(
        ToolSpec(
            name="infuse_transport",
            description=(
                "Pause / play / stop / skip on the Apple TV while Infuse (or another app) is "
                "active. Uses Home Assistant Apple TV media_player remote commands — Infuse has "
                "no playback-state or transport API. Destructive: dry-run unless confirm=true. "
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
            destructive=True,
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
            description="Stop a Docker container. Destructive: dry-run unless confirm=true.",
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
            description="Write a file in the VAULT workspace sandbox only — not the git repo. For repo/PR/feature work call chief_of_staff. Destructive: dry-run unless confirm=true.",
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
            destructive=True,
        )
    )
    registry.register(
        ToolSpec(
            name="workspace_delete",
            description="Delete a workspace file. Destructive: dry-run unless confirm=true. Cannot leave the workspace.",
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
                "it connected. Destructive: dry-run unless confirm=true (voice/UI confirm is enough)."
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
            destructive=True,
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
            description="Add a movie to the Radarr download queue. Destructive: dry-run unless confirm=true. Say you'll grab it in Radarr.",
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
            destructive=True,
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
            description="Add a series to the Sonarr download queue. Destructive: dry-run unless confirm=true. Say you'll grab it in Sonarr.",
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
            destructive=True,
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
            name="overseerr_request",
            description="Request a movie or show via Overseerr (feeds Radarr/Sonarr). Destructive: dry-run unless confirm=true.",
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
            destructive=True,
        )
    )
    load_workspace_skills()
