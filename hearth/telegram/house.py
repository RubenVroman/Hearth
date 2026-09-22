"""Telegram house commands and comfort quick replies.

Slash and natural commands cover lights, scenes, covers, and house status.
Ritual, climate, feeder, and purifier phrases use a one-tap reply keyboard
when Home Assistant actually has those devices. Bare "movie night" stays
with the media bot. Jev gates every house call before a service runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from hearth.agent.registry import ToolRegistry, registry
from hearth.jev import authorize_tool
from hearth.telegram.models import BotReply
from hearth.tools.device_intent import match_device_phrase
from hearth.tools.house import (
    climate_control,
    comfort_snapshot,
    feeder_control,
    purifier_control,
    run_ritual,
)

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
_NATURAL_HOUSE_STATUS = re.compile(
    r"^(?:"
    r"(?:house|home)\s+(?:status|snapshot|check)|"
    r"status\s+of\s+(?:the\s+)?(?:house|home)|"
    r"how(?:'s| is)\s+(?:the\s+)?(?:house|home)|"
    r"what(?:'s| is)\s+on\s+(?:in|around|at)\s+(?:the\s+)?(?:house|home)"
    r")\s*[.?!]*$",
    re.I,
)
_NATURAL_LIGHT_LIST = re.compile(
    r"^(?:lights?|list\s+(?:the\s+)?lights?|show\s+(?:me\s+)?(?:the\s+)?lights?|"
    r"what\s+lights?\s+are\s+on|which\s+lights?\s+are\s+on)\s*[.?!]*$",
    re.I,
)
_NATURAL_LIGHT_ACTION = re.compile(
    r"^(?:(?:turn|switch)\s+(?P<power>on|off)|(?P<toggle>toggle))\s+"
    r"(?:the\s+)?(?P<target>.+\blights?)\s*[.?!]*$",
    re.I,
)
_NATURAL_LIGHT_LEVEL = re.compile(
    r"^(?:dim|set)\s+(?:the\s+)?(?P<target>.+\blights?)\s+"
    r"(?:to|at)\s+(?P<value>\d{1,3})%?\s*[.?!]*$",
    re.I,
)
_NATURAL_SCENE_LIST = re.compile(
    r"^(?:scenes?|list\s+(?:the\s+)?scenes?|show\s+(?:me\s+)?(?:the\s+)?scenes?)"
    r"\s*[.?!]*$",
    re.I,
)
_NATURAL_SCENE_ACTION = re.compile(
    r"^(?:activate|run|start|turn\s+on)\s+(?:the\s+)?(?:"
    r"scene\s+(?P<prefix>.+?)|(?P<suffix>.+?)\s+scene|"
    r"(?P<known>movie\s+night|good\s+night)"
    r")\s*[.?!]*$",
    re.I,
)
_NATURAL_COVER_LIST = re.compile(
    r"^(?:covers?|blinds?|shades?|curtains?|shutters?|"
    r"(?:list|show)(?:\s+me)?\s+(?:the\s+)?"
    r"(?:covers?|blinds?|shades?|curtains?|shutters?))\s*[.?!]*$",
    re.I,
)
_NATURAL_COVER_ACTION = re.compile(
    r"^(?P<action>open|close|stop)\s+(?:the\s+)?"
    r"(?P<target>.+?(?:cover|blind|blinds|shade|shades|curtain|curtains|shutter|shutters))"
    r"\s*[.?!]*$",
    re.I,
)
_NATURAL_COVER_POWER = re.compile(
    r"^(?:turn|switch)\s+(?P<action>on|off)\s+(?:the\s+)?"
    r"(?P<target>.+?(?:cover|blind|blinds|shade|shades|curtain|curtains|shutter|shutters))"
    r"\s*[.?!]*$",
    re.I,
)
_NATURAL_COVER_POSITION = re.compile(
    r"^(?:set|move)\s+(?:the\s+)?"
    r"(?P<target>.+?(?:cover|blind|blinds|shade|shades|curtain|curtains|shutter|shutters))"
    r"\s+(?:to|at)\s+(?P<value>\d{1,3})%?\s*[.?!]*$",
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
    """Parse strict house commands before the media router sees the message."""
    raw = (text or "").strip()
    match = _COMMAND.match(raw)
    if match is None:
        return _parse_natural_house_command(raw)
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


def _parse_natural_house_command(text: str) -> HouseCommand | None:
    if not text:
        return None
    text = re.sub(
        r"^(?:please\s+|(?:can|could|would)\s+you\s+)",
        "",
        text,
        flags=re.I,
    ).strip()
    if _NATURAL_HOUSE_STATUS.fullmatch(text):
        return HouseCommand("house_status", "house_status", {})
    if _NATURAL_LIGHT_LIST.fullmatch(text):
        return HouseCommand("list_lights", "ha_list_entities", {"domain": "light"})
    if _NATURAL_SCENE_LIST.fullmatch(text):
        return HouseCommand("list_scenes", "ha_list_entities", {"domain": "scene"})
    if _NATURAL_COVER_LIST.fullmatch(text):
        return HouseCommand("list_covers", "ha_list_entities", {"domain": "cover"})

    light = _NATURAL_LIGHT_ACTION.fullmatch(text)
    if light:
        target = _target(light.group("target"))
        power = str(light.group("power") or "").casefold()
        action = f"turn_{power}" if power else "toggle"
        return HouseCommand(
            "control_light",
            "ha_device_control",
            {"device": target, "domain": "light", "action": action},
        )
    light_level = _NATURAL_LIGHT_LEVEL.fullmatch(text)
    if light_level:
        value = int(light_level.group("value"))
        if value <= 100:
            return HouseCommand(
                "control_light",
                "ha_device_control",
                {
                    "device": _target(light_level.group("target")),
                    "domain": "light",
                    "action": "brightness",
                    "value": value,
                },
            )
        return HouseCommand("control_light", "ha_device_control", {}, _LIGHT_USAGE)

    scene = _NATURAL_SCENE_ACTION.fullmatch(text)
    if scene:
        target = next((group for group in scene.groups() if group), "")
        return HouseCommand(
            "activate_scene",
            "ha_device_control",
            {"device": _target(target), "domain": "scene", "action": "activate"},
        )

    cover = _NATURAL_COVER_ACTION.fullmatch(text)
    if cover:
        return HouseCommand(
            "control_cover",
            "ha_device_control",
            {
                "device": _target(cover.group("target")),
                "domain": "cover",
                "action": cover.group("action").casefold(),
            },
        )
    cover_power = _NATURAL_COVER_POWER.fullmatch(text)
    if cover_power:
        return HouseCommand(
            "control_cover",
            "ha_device_control",
            {
                "device": _target(cover_power.group("target")),
                "domain": "cover",
                "action": "open"
                if cover_power.group("action").casefold() == "on"
                else "close",
            },
        )
    cover_position = _NATURAL_COVER_POSITION.fullmatch(text)
    if cover_position:
        value = int(cover_position.group("value"))
        if value <= 100:
            return HouseCommand(
                "control_cover",
                "ha_device_control",
                {
                    "device": _target(cover_position.group("target")),
                    "domain": "cover",
                    "action": "set_position",
                    "value": value,
                },
            )
        return HouseCommand("control_cover", "ha_device_control", {}, _COVER_USAGE)
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
        # Rituals, climate, feeder, and purifier before slash/scene parsing.
        # Bare "movie night" is not a house-control phrase, so the media bot
        # still owns that vibe.
        if looks_like_house_control(text):
            return await house_control_reply(text)
        command = parse_house_command(text)
        if command is None:
            return None
        if command.error:
            return BotReply(command.error)

        # The command already names its tool, so it is authorized by the shared
        # tool gate rather than by a second, coarser Jev call of its own.
        decision = await authorize_tool(
            command.tool,
            command.args,
            said=text,
            channel="telegram_house",
        )
        if decision.denied:
            return BotReply(
                f"{decision.message} The house is unchanged — "
                "send the full command again if you meant it."
            )

        result = await self.tools.call(command.tool, command.args)
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
    "house_control_reply",
    "looks_like_house_control",
    "parse_house_command",
    "telegram_plan",
]


def looks_like_house_control(text: str) -> bool:
    return telegram_plan(text) is not None


def telegram_plan(text: str) -> dict[str, Any] | None:
    """Parse a house-control phrase. None means leave it to the media bot."""
    raw = _clean(text)
    if not raw:
        return None
    if raw in {
        "house sleep",
        "good night",
        "goodnight",
        "lights out",
        "welterusten",
        "put the house to sleep",
        "time for bed",
    }:
        return {"tool": "house_ritual", "args": {"ritual": "sleep"}}
    if raw in {"good morning", "goedemorgen"}:
        return {"tool": "house_ritual", "args": {"ritual": "morning"}}
    # Bare "movie night" is a catalog vibe. Require an explicit control shape.
    if raw in {
        "movie night mode",
        "cinema mode",
        "filmavond",
        "start movie night",
        "set movie night",
        "house movie night",
    }:
        return {"tool": "house_ritual", "args": {"ritual": "movie"}}
    if raw in {"warmer", "make it warmer", "turn the heat up"}:
        return {"tool": "house_climate", "args": {"action": "warmer"}}
    if raw in {"cooler", "make it cooler", "turn the heat down"}:
        return {"tool": "house_climate", "args": {"action": "cooler"}}
    if raw in {"climate off", "turn the heat off", "thermostat off", "heating off"}:
        return {"tool": "house_climate", "args": {"action": "off"}}
    if raw in {"what's the climate", "how's the heat", "how's the heating", "climate status"}:
        return {"tool": "house_climate", "args": {"action": "status"}}
    setpoint = re.fullmatch(
        r"(?:set )?(?:the )?(?:heat|heating|thermostat|climate) to (\d{1,2}(?:\.\d)?)",
        raw,
    )
    if setpoint:
        return {
            "tool": "house_climate",
            "args": {"action": "set", "temperature": float(setpoint.group(1))},
        }
    if raw in {"feed", "feeder", "voeder", "voeren"} or re.fullmatch(
        r"feed the (?:cat|cats|dog|dogs|pets|feeder)",
        raw,
    ):
        return {"tool": "house_feeder", "args": {"action": "feed"}}
    if raw in {
        "purifier on",
        "air purifier on",
        "turn the purifier on",
        "turn the air purifier on",
    }:
        return {"tool": "house_purifier", "args": {"action": "on"}}
    if raw in {
        "purifier off",
        "air purifier off",
        "turn the purifier off",
        "turn the air purifier off",
    }:
        return {"tool": "house_purifier", "args": {"action": "off"}}
    if raw in {"purifier", "air purifier", "luchtreiniger", "purifier status"}:
        return {"tool": "house_purifier", "args": {"action": "status"}}
    if raw in {
        "house",
        "house controls",
        "control the house",
        "control everything",
        "comfort",
        "how's the air",
        "air quality",
        "what's the air quality",
    }:
        return {"tool": "house_comfort", "args": {}}
    # Same wider net the voice router uses, so a phrase does not mean one thing
    # spoken and another in Telegram.
    device = match_device_phrase(text)
    return device.as_plan(text) if device is not None else None


async def house_control_reply(text: str) -> BotReply:
    """Run one house tool after the shared Jev gate, then offer a one-tap keyboard."""
    plan = telegram_plan(text) or {"tool": "house_comfort", "args": {}}
    tool = str(plan.get("tool") or "")
    args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
    decision = await authorize_tool(tool, args, said=text, channel="telegram_house")
    if decision.denied:
        return BotReply(f"{decision.message} The house is unchanged.")

    result = await _run(plan)
    speak = str(result.get("speak") or result.get("error") or "Done.")
    keyboard = await _quick_keyboard()
    return BotReply(speak, keyboard)


async def _run(plan: dict[str, Any]) -> dict[str, Any]:
    tool = str(plan.get("tool") or "")
    args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
    if tool == "house_ritual":
        return await run_ritual(str(args.get("ritual") or ""))
    if tool == "house_climate":
        temperature = args.get("temperature")
        return await climate_control(
            str(args.get("action") or "status"),
            temperature=float(temperature) if temperature is not None else None,
            fan_mode=str(args.get("fan_mode") or "") or None,
        )
    if tool == "house_feeder":
        portions = args.get("portions")
        return await feeder_control(
            str(args.get("action") or "feed"),
            portions=int(portions) if portions is not None else None,
            force=bool(args.get("force")),
        )
    if tool == "house_purifier":
        percentage = args.get("percentage")
        return await purifier_control(
            str(args.get("action") or "status"),
            percentage=float(percentage) if percentage is not None else None,
            preset_mode=str(args.get("preset_mode") or "") or None,
        )
    if tool == "ha_discover_entities":
        from hearth.tools.devices import discover_entities

        return await discover_entities(str(args.get("kind") or ""))
    return await comfort_snapshot()


async def _quick_keyboard() -> dict[str, Any]:
    """Reply keyboard — only rows whose Home Assistant path actually exists."""
    snapshot = await comfort_snapshot()
    rows: list[list[str]] = [["House sleep", "Good morning", "Movie night mode"]]
    if snapshot.get("climate"):
        rows.append(["Warmer", "Cooler", "Climate off"])
    device_row: list[str] = []
    if snapshot.get("feeders"):
        device_row.append("Feed")
    purifiers = snapshot.get("purifiers") or []
    if purifiers:
        state = str(purifiers[0].get("state") or "").lower()
        device_row.append("Purifier off" if state == "on" else "Purifier on")
    if device_row:
        rows.append(device_row)
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "is_persistent": False,
        "input_field_placeholder": "House, climate, or a title",
    }


def _clean(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip().lower())
    raw = raw.strip(" .!?")
    raw = re.sub(r"^(please |can you |could you )", "", raw)
    return raw
