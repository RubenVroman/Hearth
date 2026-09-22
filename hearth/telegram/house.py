"""Deterministic Telegram commands for routine Home Assistant control."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from hearth.agent.registry import ToolRegistry, registry
from hearth.jev import evaluate_message, log_shadow_outcome
from hearth.telegram.models import BotReply

HouseCommandKind = Literal[
    "house_status",
    "list_lights",
    "control_light",
    "list_scenes",
    "activate_scene",
    "list_covers",
    "control_cover",
]

TELEGRAM_COMMANDS: list[dict[str, str]] = [
    {"command": "house", "description": "What is on, climate, covers, last-fed"},
    {"command": "lights", "description": "List lights or control one"},
    {"command": "scenes", "description": "List Home Assistant scenes"},
    {"command": "scene", "description": "Activate a scene by name"},
    {"command": "covers", "description": "List covers and positions"},
    {"command": "cover", "description": "Open, close, stop, or position a cover"},
    {"command": "search", "description": "Find a movie or series"},
    {"command": "status", "description": "Check Telegram media services"},
    {"command": "help", "description": "Show examples and recovery help"},
]

_COMMAND = re.compile(
    r"^/(?P<name>house|home|lights|scenes|scene|covers|cover)"
    r"(?:@[a-z0-9_]+)?(?:\s+(?P<argument>.*))?$",
    re.IGNORECASE,
)
_LIGHT_ACTION_END = re.compile(r"^(?P<target>.+?)\s+(?P<action>on|off|toggle)$", re.I)
_LIGHT_ACTION_START = re.compile(r"^(?P<action>on|off|toggle)\s+(?P<target>.+)$", re.I)
_LIGHT_LEVEL = re.compile(
    r"^(?P<target>.+?)\s+(?:(?:to|at|brightness)\s+)?(?P<value>\d{1,3})%?$",
    re.I,
)
_COVER_ACTION_END = re.compile(
    r"^(?P<target>.+?)\s+(?P<action>open|close|stop|up|down)$",
    re.I,
)
_COVER_ACTION_START = re.compile(
    r"^(?P<action>open|close|stop|up|down)\s+(?P<target>.+)$",
    re.I,
)
_COVER_POSITION = re.compile(
    r"^(?P<target>.+?)\s+(?:(?:to|at|position)\s+)?(?P<value>\d{1,3})%?$",
    re.I,
)

_LIGHT_USAGE = (
    "Use /lights to list lights, or /lights <name> on|off|toggle|0-100. "
    "Example: /lights kitchen 40."
)
_SCENE_USAGE = (
    "Use /scenes to list scenes, then /scene <name>. "
    "Example: /scene movie night."
)
_COVER_USAGE = (
    "Use /covers to list covers, or /cover <name> open|close|stop|0-100. "
    "Example: /cover living room blind 50."
)


@dataclass(frozen=True, slots=True)
class HouseCommand:
    kind: HouseCommandKind
    tool: str
    args: dict[str, Any]
    error: str = ""


def _target(value: str) -> str:
    return re.sub(r"^(?:the|my|our)\s+", "", value.strip(), flags=re.I).strip(" .?!")


def parse_house_command(text: str) -> HouseCommand | None:
    """Parse only explicit house slash commands; media parsing stays untouched."""
    match = _COMMAND.match((text or "").strip())
    if match is None:
        return None
    name = match.group("name").casefold()
    argument = (match.group("argument") or "").strip()

    if name in {"house", "home"}:
        if argument.casefold() not in {"", "status", "now"}:
            return HouseCommand("house_status", "house_status", {}, "Use /house for a snapshot.")
        return HouseCommand("house_status", "house_status", {})

    if name == "lights":
        if not argument:
            return HouseCommand("list_lights", "ha_list_entities", {"domain": "light"})
        parsed = _parse_light(argument)
        if parsed is None:
            return HouseCommand("control_light", "ha_device_control", {}, _LIGHT_USAGE)
        target, action, value = parsed
        args: dict[str, Any] = {"device": target, "domain": "light", "action": action}
        if value is not None:
            args["value"] = value
        return HouseCommand("control_light", "ha_device_control", args)

    if name in {"scene", "scenes"}:
        if not argument and name == "scenes":
            return HouseCommand("list_scenes", "ha_list_entities", {"domain": "scene"})
        scene = re.sub(
            r"^(?:activate|run|turn\s+on)\s+",
            "",
            argument,
            flags=re.I,
        )
        scene = _target(scene)
        if not scene:
            return HouseCommand("activate_scene", "ha_device_control", {}, _SCENE_USAGE)
        return HouseCommand(
            "activate_scene",
            "ha_device_control",
            {"device": scene, "domain": "scene", "action": "activate"},
        )

    if name in {"cover", "covers"}:
        if not argument and name == "covers":
            return HouseCommand("list_covers", "ha_list_entities", {"domain": "cover"})
        parsed = _parse_cover(argument)
        if parsed is None:
            return HouseCommand("control_cover", "ha_device_control", {}, _COVER_USAGE)
        target, action, value = parsed
        args = {"device": target, "domain": "cover", "action": action}
        if value is not None:
            args["value"] = value
        return HouseCommand("control_cover", "ha_device_control", args)

    return None


def _parse_light(argument: str) -> tuple[str, str, int | None] | None:
    for pattern in (_LIGHT_ACTION_END, _LIGHT_ACTION_START):
        match = pattern.match(argument)
        if match:
            target = _target(match.group("target"))
            if target:
                action = match.group("action").casefold()
                tool_action = f"turn_{action}" if action in {"on", "off"} else "toggle"
                return target, tool_action, None
    level = _LIGHT_LEVEL.match(argument)
    if level:
        target = _target(level.group("target"))
        value = int(level.group("value"))
        if target and 0 <= value <= 100:
            return target, "brightness", value
    return None


def _parse_cover(argument: str) -> tuple[str, str, int | None] | None:
    for pattern in (_COVER_ACTION_END, _COVER_ACTION_START):
        match = pattern.match(argument)
        if match:
            target = _target(match.group("target"))
            action = match.group("action").casefold()
            action = {"up": "open", "down": "close"}.get(action, action)
            if target:
                return target, action, None
    position = _COVER_POSITION.match(argument)
    if position:
        target = _target(position.group("target"))
        value = int(position.group("value"))
        if target and 0 <= value <= 100:
            return target, "set_position", value
    return None


class TelegramHouseCommands:
    """Jev-gated bridge from Telegram commands to the shared tool registry."""

    def __init__(self, tools: ToolRegistry | None = None) -> None:
        self.tools = tools or registry

    async def handle(self, text: str) -> BotReply | None:
        command = parse_house_command(text)
        if command is None:
            return None
        if command.error:
            return BotReply(command.error)

        verdict = await evaluate_message(text)
        if verdict.action == "block_cancel":
            log_shadow_outcome(
                verdict,
                channel="telegram_house",
                tools=[],
                outcome="blocked_cancel",
            )
            return BotReply(
                "I didn't change the house because that looked like a cancel or refusal. "
                "Send the full command again if you meant to run it."
            )
        if verdict.action == "escalate_cos":
            log_shadow_outcome(
                verdict,
                channel="telegram_house",
                tools=[],
                outcome="redirected_cos",
            )
            return BotReply(
                "That doesn't look like a Home Assistant command. Use /help for house examples."
            )

        result = await self.tools.call(command.tool, command.args)
        log_shadow_outcome(
            verdict,
            channel="telegram_house",
            tools=[command.tool],
            outcome="ok" if result.ok else "failed",
        )
        return BotReply(_format_result(command, result.ok, result.data))


def _format_result(command: HouseCommand, ok: bool, data: dict[str, Any]) -> str:
    if command.kind == "house_status":
        return _format_house_status(data)
    if not ok:
        return _format_failure(command, data)
    if command.kind in {"list_lights", "list_scenes", "list_covers"}:
        return _format_entity_list(command.kind, data.get("states") or [])

    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    name = str(
        (state.get("attributes") or {}).get("friendly_name")
        or data.get("entity_id")
        or command.args.get("device")
        or "Device"
    )
    if command.kind == "activate_scene":
        return f"Scene {name} activated."
    if command.kind == "control_cover":
        position = (state.get("attributes") or {}).get("current_position")
        suffix = f" at {position}%" if position is not None else ""
        return f"{name} is {state.get('state') or 'updated'}{suffix}."
    if command.kind == "control_light":
        brightness = _brightness_pct(state)
        suffix = (
            f" at {brightness}%"
            if brightness is not None and state.get("state") == "on"
            else ""
        )
        return f"{name} is {state.get('state') or 'updated'}{suffix}."
    return "Done."


def _format_failure(command: HouseCommand, data: dict[str, Any]) -> str:
    matches = data.get("matches") or []
    if data.get("ambiguous") and matches:
        names = [
            str((row.get("attributes") or {}).get("friendly_name") or row.get("entity_id"))
            for row in matches[:5]
        ]
        return (
            "More than one entity matches. Use the exact entity id: "
            + ", ".join(names)
            + "."
        )
    error = str(data.get("error") or "Home Assistant did not accept the command")
    if data.get("mode") == "live" or "home assistant" in error.casefold():
        return (
            f"Home Assistant isn't reachable or rejected that command: {error}. "
            "Nothing was reported as changed; check HA and try again."
        )
    recovery = {
        "control_light": " Run /lights to see available names.",
        "activate_scene": " Run /scenes to see available names.",
        "control_cover": " Run /covers to see available names.",
    }.get(command.kind, "")
    return f"I couldn't run that house command: {error}.{recovery}"


def _format_entity_list(kind: HouseCommandKind, states: list[dict[str, Any]]) -> str:
    labels = {
        "list_lights": ("lights", "/lights <name> on|off|toggle|0-100"),
        "list_scenes": ("scenes", "/scene <name>"),
        "list_covers": ("covers", "/cover <name> open|close|stop|0-100"),
    }
    noun, example = labels[kind]
    if not states:
        return (
            f"No {noun} are exposed by Home Assistant yet. Pair them in HA, "
            f"then retry /{noun}."
        )
    lines = [noun.capitalize() + ":"]
    for row in sorted(states, key=_state_name):
        name = _state_name(row)
        state = str(row.get("state") or "unknown")
        attrs = row.get("attributes") or {}
        detail = ""
        if kind == "list_lights":
            brightness = _brightness_pct(row)
            if brightness is not None and state == "on":
                detail = f", {brightness}%"
        elif kind == "list_covers" and attrs.get("current_position") is not None:
            detail = f", {attrs['current_position']}%"
        lines.append(f"• {name}: {state}{detail}")
    lines.append(f"Control: {example}")
    return "\n".join(lines)


def _format_house_status(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return str(data.get("speak") or "I couldn't read Home Assistant. Try /house again.")
    summary = data.get("summary") or {}
    lines = ["House status"]
    lights = data.get("lights") or []
    lights_on = [row for row in lights if row.get("state") == "on"]
    if lights:
        names = ", ".join(str(row.get("name") or row.get("entity_id")) for row in lights_on)
        lines.append(
            f"Lights: {len(lights_on)}/{len(lights)} on"
            + (f" — {names}" if names else "")
        )
    climate = data.get("climate") or []
    if climate:
        bits = []
        for row in climate[:3]:
            current = row.get("current_temperature")
            target = row.get("target_temperature")
            unit = row.get("unit") or "°C"
            bit = str(row.get("name") or row.get("entity_id"))
            if current is not None:
                bit += f" {current}{unit}"
            if target is not None:
                bit += f" → {target}{unit}"
            bits.append(bit)
        lines.append("Climate: " + "; ".join(bits))
    covers = data.get("covers") or []
    if covers:
        bits = [
            f"{row.get('name') or row.get('entity_id')} {row.get('state')}"
            + (f" {row.get('position')}%" if row.get("position") is not None else "")
            for row in covers[:5]
        ]
        lines.append("Covers: " + "; ".join(bits))
    feeder = data.get("feeder")
    if isinstance(feeder, dict):
        lines.append(
            f"Last fed: {feeder.get('last_fed')} ({feeder.get('name') or 'feeder'})"
        )
    unavailable = int(summary.get("unavailable") or 0)
    if unavailable:
        lines.append(f"Attention: {unavailable} HA entities unavailable")
    if len(lines) == 1:
        lines.append(str(data.get("speak") or "No house entities found."))
    return "\n".join(lines)


def _state_name(state: dict[str, Any]) -> str:
    return str(
        (state.get("attributes") or {}).get("friendly_name")
        or state.get("name")
        or state.get("entity_id")
        or "Unknown"
    )


def _brightness_pct(state: dict[str, Any]) -> int | None:
    attrs = state.get("attributes") or {}
    value = attrs.get("brightness_pct")
    if value is not None:
        try:
            return round(float(value))
        except (TypeError, ValueError):
            return None
    value = attrs.get("brightness")
    if value is None:
        return None
    try:
        return round(float(value) / 255.0 * 100)
    except (TypeError, ValueError):
        return None


__all__ = [
    "HouseCommand",
    "TELEGRAM_COMMANDS",
    "TelegramHouseCommands",
    "parse_house_command",
]
