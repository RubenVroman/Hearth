from __future__ import annotations

from typing import Any

from hearth.agent.registry import ToolSpec, registry
from hearth.tools import files as workspace_files
from hearth.tools.docker import docker
from hearth.tools.ha import ha
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


async def _plex_now(_args: dict[str, Any]) -> dict[str, Any]:
    return await plex.now_playing()


async def _plex_search(args: dict[str, Any]) -> dict[str, Any]:
    return await plex.search(str(args.get("query") or ""), int(args.get("limit") or 8))


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
            name="plex_now_playing",
            description="What is currently playing on Plex.",
            parameters={"type": "object", "properties": {}},
            handler=_plex_now,
        )
    )
    registry.register(
        ToolSpec(
            name="plex_search",
            description="Search the Plex library.",
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
            description="Write a file in the workspace. Use skills/name.py to add a tool. Destructive: dry-run unless confirm=true.",
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
    load_workspace_skills()
