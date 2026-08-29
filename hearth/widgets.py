"""Map house turns and tool results into contextual UI widgets."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from hearth.runtime import Widget, runtime

# Tools that deserve a dedicated in-progress / result panel.
_ACTION_TOOLS = {
    "radarr_add",
    "radarr_search",
    "sonarr_add",
    "sonarr_search",
    "overseerr_request",
    "overseerr_search",
    "ha_call_service",
    "chief_of_staff",
    "docker_stop",
    "workspace_write",
    "workspace_delete",
}

_TURN_ID = "turn-active"


def new_id(prefix: str = "w") -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def start_turn(message: str) -> Widget:
    """Show a compact working panel as soon as Ruben asks something."""
    preview = re.sub(r"\s+", " ", (message or "").strip())
    if len(preview) > 72:
        preview = preview[:69] + "…"
    body = preview or "Working on it."
    return runtime.upsert_widget(
        Widget(
            id=_TURN_ID,
            kind="action",
            title="House",
            status="running",
            body=body,
            detail="Thinking…",
            data={"phase": "turn"},
            dismissible=True,
            sticky=False,
        )
    )


def finish_turn(*, ok: bool = True, detail: str = "") -> Widget | None:
    existing = runtime.get_widget(_TURN_ID)
    if existing is None:
        return None
    return runtime.upsert_widget(
        Widget(
            id=_TURN_ID,
            kind="action",
            title=existing.title,
            status="done" if ok else "error",
            body=existing.body,
            detail=detail or ("Done." if ok else "Something went wrong."),
            data={**existing.data, "phase": "done"},
            dismissible=True,
            sticky=False,
        )
    )


def publish_tool(result: dict[str, Any]) -> Widget | None:
    """Upsert a widget from a tool result dict (ToolResult.as_dict())."""
    name = str(result.get("name") or "")
    if not name:
        return None
    if name == "get_weather":
        return _weather_widget(result)
    if name in _ACTION_TOOLS or result.get("needs_confirm"):
        return _action_widget(result)
    return _generic_widget(result)


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


def _action_title(name: str) -> str:
    labels = {
        "radarr_add": "Grabbing movie",
        "radarr_search": "Searching movies",
        "sonarr_add": "Grabbing show",
        "sonarr_search": "Searching shows",
        "overseerr_request": "Requesting media",
        "overseerr_search": "Searching Overseerr",
        "ha_call_service": "House action",
        "chief_of_staff": "Chief of Staff",
        "docker_stop": "Docker",
        "workspace_write": "Workspace",
        "workspace_delete": "Workspace",
    }
    return labels.get(name, name.replace("_", " ").title())


def _action_body(name: str, data: dict[str, Any], *, needs_confirm: bool) -> tuple[str, str]:
    preview = data.get("would_call_with") or {}
    if needs_confirm:
        if name in {"radarr_add", "sonarr_add", "overseerr_request"}:
            query = preview.get("query") or data.get("query") or "that"
            return f"{query}", "Waiting for confirm"
        if name == "ha_call_service":
            entity = preview.get("entity_id") or "device"
            service = preview.get("service") or "act"
            return f"{service} · {entity}", "Waiting for confirm"
        if name == "chief_of_staff":
            task = preview.get("task") or preview.get("said") or "that"
            return str(task)[:90], "Waiting for confirm"
        return str(preview or name)[:90], "Waiting for confirm"

    if name == "radarr_add":
        added = data.get("added") or {}
        return str(added.get("title") or preview.get("query") or "Movie"), "Queued in Radarr"
    if name == "sonarr_add":
        added = data.get("added") or {}
        return str(added.get("title") or preview.get("query") or "Show"), "Queued in Sonarr"
    if name == "overseerr_request":
        item = data.get("requested") or {}
        return str(item.get("title") or preview.get("query") or "Request"), "Sent to Overseerr"
    if name in {"radarr_search", "sonarr_search", "overseerr_search"}:
        results = data.get("results") or []
        n = len(results)
        first = (results[0].get("title") if results else None) or "No matches"
        return str(first), f"{n} result{'s' if n != 1 else ''}"
    if name == "ha_call_service":
        entity = (data.get("entity") or {})
        label = entity.get("entity_id") or data.get("entity_id") or "entity"
        state = entity.get("state") or "updated"
        return f"{label}", f"State: {state}"
    if name == "chief_of_staff":
        if data.get("configured") is False:
            return str(data.get("error") or "Not configured"), "Needs setup"
        repo = data.get("repo") or "repo"
        return f"Escalated · {repo}", "Handed off"
    if data.get("error"):
        return str(data.get("error"))[:120], "Failed"
    return name.replace("_", " "), "Done"


def _action_widget(result: dict[str, Any]) -> Widget:
    name = str(result.get("name") or "action")
    data = result.get("data") or {}
    needs_confirm = bool(result.get("needs_confirm"))
    ok = bool(result.get("ok"))
    if needs_confirm:
        status = "pending"
    elif not ok or data.get("ok") is False:
        status = "error"
    else:
        status = "done"
    body, detail = _action_body(name, data, needs_confirm=needs_confirm)
    if data.get("mode") == "mock" and detail and "mock" not in detail.lower():
        detail = f"{detail} · mock"
    return runtime.upsert_widget(
        Widget(
            id=f"action-{name}",
            kind="action",
            title=_action_title(name),
            status=status,
            body=body,
            detail=detail,
            data={"tool": name, **({k: v for k, v in data.items() if k != "would_call_with"})},
            sticky=needs_confirm,
        )
    )


def _generic_widget(result: dict[str, Any]) -> Widget:
    name = str(result.get("name") or "tool")
    data = result.get("data") or {}
    ok = bool(result.get("ok")) and data.get("ok") is not False
    title = name.replace("_", " ").title()
    if not ok:
        body = str(data.get("error") or data)[:140]
        status = "error"
        detail = "Failed"
    else:
        # Compact summary for generic tools (plex, docker, lights, …)
        if name == "plex_now_playing":
            sessions = data.get("sessions") or []
            if sessions:
                body = str(sessions[0].get("title") or "Playing")
                detail = str(sessions[0].get("player") or sessions[0].get("state") or "")
            else:
                body = "Nothing playing"
                detail = ""
        elif name == "docker_ps":
            containers = data.get("containers") or []
            body = f"{len(containers)} containers"
            detail = ", ".join(str(c.get("name") or c.get("id")) for c in containers[:4])
        elif name == "ha_list_entities":
            states = data.get("states") or []
            body = f"{len(states)} entities"
            detail = ""
        else:
            body = "Finished"
            detail = name
        status = "done"
        if data.get("mode") == "mock" and detail:
            detail = f"{detail} · mock"
        elif data.get("mode") == "mock":
            detail = "mock"
    return runtime.upsert_widget(
        Widget(
            id=f"tool-{name}",
            kind="generic",
            title=title,
            status=status,
            body=body,
            detail=detail,
            data={"tool": name},
        )
    )
