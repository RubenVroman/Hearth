"""Deterministic phrases for the house-device tools (English + Dutch).

Shared by the agent's local router (no OpenAI key) and the Telegram lane so
"feed the cats" means the same thing however it arrives. Matching is
intentionally narrow: on Telegram anything that is not recognised here falls
through to the Overseerr media search, so a bare "Cats" or "Feed" has to stay a
film title rather than becoming a meal.

The ``kind`` on each plan lines up with Jev's ``device_ask`` Choice options, so
the two classifications can be compared instead of quietly disagreeing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PET = r"(?:cats?|kittens?|kitt(?:y|ies)|pets?|animals?|katten|kat|poezen|poes|dieren|beesten)"
# Bare "ac" is left out on purpose — "AC/DC" is a band, not the air conditioning.
_AIRCO = r"(?:airco(?:s)?|aircon(?:ditioning|ditioner)?|air[\s-]?co(?:nditioning|nditioner)?|a/c|klimaat)"
_PURIFIER = r"(?:air[\s-]?purifier|purifier|air[\s-]?cleaner|luchtreiniger|luchtfilter)"

_ON = r"(?:on|aan)"
_OFF = r"(?:off|out|uit)"


@dataclass(frozen=True, slots=True)
class DevicePlan:
    """One recognised house-device command and the tool call it becomes."""

    tool: str
    kind: str
    args: dict[str, Any] = field(default_factory=dict)

    def as_plan(self, said: str = "") -> dict[str, Any]:
        """Registry-shaped ``{tool, args}``; ``said`` lets Jev judge the sentence."""
        args = dict(self.args)
        if said.strip():
            args["said"] = said.strip()
        return {"tool": self.tool, "args": args}


_DISCOVER = re.compile(
    r"\b(?:"
    r"(?:which|what|list|find|show me|discover)\s+(?:home\s?assistant\s+|ha\s+)?"
    r"(?:entit(?:y|ies)|devices?)\s+(?:for|do we have for)\s+"
    r"(?:the\s+)?(?:feeder|pet\s?feeder|airco|air\s?conditioning|purifier|air\s?purifier)"
    r"|(?:tuya|smart\s?life)\s+(?:devices?|entit(?:y|ies))"
    r"|(?:feeder|pet\s?feeder|airco|purifier|air\s?purifier)\s+entit(?:y|ies)"
    r"|discover\s+(?:the\s+)?(?:feeder|airco|purifier)"
    r")\b",
    re.I,
)

_FEEDER_SCHEDULE = re.compile(
    r"\b(?:"
    r"auto(?:matic)?[\s-]?feed(?:ing)?"
    r"|(?:feed(?:ing)?|feeder)\s+(?:schedule|timer)"
    r"|scheduled\s+feed(?:ing)?"
    r"|voerschema|voedingsschema|automatisch\s+voeren"
    r")\b",
    re.I,
)

_FEEDER_STATUS = re.compile(
    r"\b(?:"
    rf"(?:did|have|has)\s+(?:the\s+|de\s+)?{_PET}\s+(?:get|got|been|already\s+been)?\s*fed"
    r"|(?:pet\s?)?feeder\s+(?:status|state)"
    rf"|hebben\s+de\s+{_PET}\s+(?:al\s+)?(?:gegeten|eten\s+gehad)"
    r")\b",
    re.I,
)

_FEED_NOW = re.compile(
    r"\b(?:"
    rf"feed\s+(?:the\s+|my\s+|our\s+|de\s+|onze\s+)?{_PET}"
    r"|feed\s+(?:them|him|her|'?em)"
    rf"|give\s+(?:the\s+|my\s+|our\s+)?(?:{_PET}|them)\s+(?:some\s+|a\s+|their\s+)?"
    r"(?:food|dinner|breakfast|portion|supper|something\s+to\s+eat)"
    rf"|(?:voer|voeren)\s+(?:de\s+|mijn\s+|onze\s+)?{_PET}"
    rf"|{_PET}\s+voeren"
    rf"|geef\s+(?:de\s+|mijn\s+|onze\s+)?{_PET}\s+(?:wat\s+)?(?:eten|voer|te\s+eten)"
    rf"|eten\s+voor\s+(?:de\s+|onze\s+)?{_PET}"
    r")\b",
    re.I,
)

_FEED_PORTIONS = re.compile(
    r"\b(\d{1,2})\s*(?:portions?|servings?|scoops?|porties?|keer|meals?)\b",
    re.I,
)
_FEED_DOUBLE = re.compile(r"\b(?:twice|double|dubbele?|two\s+portions?)\b", re.I)
_FEED_FORCE = re.compile(
    r"\b(?:anyway|toch|again|opnieuw|nog\s+(?:een|eens)|extra|"
    r"another\s+(?:portion|one|round)|second\s+(?:portion|one))\b",
    re.I,
)

_AIRCO_STATUS = re.compile(
    rf"(?:\b(?:how(?:'s| is)|hoe\s+staat|what(?:'s| is))\s+(?:the\s+|de\s+)?{_AIRCO}\b"
    rf"|\bis\s+(?:the\s+|de\s+)?{_AIRCO}\s+(?:{_ON}|{_OFF})\b"
    rf"|\bstaat\s+de\s+{_AIRCO}\s+(?:{_ON}|{_OFF})\b"
    rf"|\b{_AIRCO}\s+(?:status|state)\b)",
    re.I,
)
_AIRCO_TEMPERATURE = re.compile(
    rf"(?:\b{_AIRCO}\s*(?:to|op|naar|at|@)?\s*(\d{{1,2}})(?:\s*(?:°|degrees?|graden|deg|c\b))?"
    rf"|\b(?:set|zet|put)\s+(?:the\s+|de\s+)?{_AIRCO}\s+(?:to|op|naar|at)\s+(\d{{1,2}}))",
    re.I,
)
_AIRCO_FAN = re.compile(
    rf"\b{_AIRCO}\s+(?:fan|ventilator|blower)\s+(?:to\s+|op\s+|speed\s+)?"
    r"(low|medium|high|auto|laag|midden|hoog|stil|silent)\b",
    re.I,
)
_AIRCO_MODE = re.compile(
    rf"\b{_AIRCO}\s+(?:to\s+|op\s+|in\s+|mode\s+|naar\s+)?"
    r"(cool(?:ing)?|heat(?:ing)?|dry|dehumidify|fan[\s-]?only|auto(?:matic)?|"
    r"koel(?:en|ing)?|verwarm(?:en|ing)?|warm|drogen|ontvochtigen|ventileren)\b",
    re.I,
)
_AIRCO_POWER = re.compile(
    rf"(?:\b(?:turn|switch|zet|doe|put)\s+(?:the\s+|de\s+)?{_AIRCO}\s+({_ON}|{_OFF})\b"
    rf"|\b(?:turn|switch)\s+({_ON}|{_OFF})\s+(?:the\s+)?{_AIRCO}\b"
    rf"|\b{_AIRCO}\s+({_ON}|{_OFF})\b"
    rf"|\b({_ON}|{_OFF})\s+(?:met\s+)?(?:de\s+)?{_AIRCO}\b)",
    re.I,
)

_PURIFIER_STATUS = re.compile(
    rf"(?:\b(?:how(?:'s| is)|hoe\s+staat|what(?:'s| is))\s+(?:the\s+|de\s+)?{_PURIFIER}\b"
    rf"|\bis\s+(?:the\s+|de\s+)?{_PURIFIER}\s+(?:{_ON}|{_OFF}|running)\b"
    rf"|\bstaat\s+de\s+{_PURIFIER}\s+(?:{_ON}|{_OFF})\b"
    rf"|\b{_PURIFIER}\s+(?:status|state)\b)",
    re.I,
)
_PURIFIER_SPEED = re.compile(
    rf"\b{_PURIFIER}\s*(?:to|op|at|speed|snelheid|@)?\s*(\d{{1,3}})\s*%?",
    re.I,
)
_PURIFIER_PRESET = re.compile(
    rf"\b{_PURIFIER}\s+(?:to\s+|op\s+|in\s+|mode\s+|naar\s+)?"
    r"(auto(?:matic)?|sleep|night|slaap|nacht|turbo|boost|manual|handmatig|"
    r"low|medium|high|silent|stil|laag|hoog)\b",
    re.I,
)
_PURIFIER_POWER = re.compile(
    rf"(?:\b(?:turn|switch|zet|doe|put)\s+(?:the\s+|de\s+)?{_PURIFIER}\s+({_ON}|{_OFF})\b"
    rf"|\b(?:turn|switch)\s+({_ON}|{_OFF})\s+(?:the\s+)?{_PURIFIER}\b"
    rf"|\b{_PURIFIER}\s+({_ON}|{_OFF})\b"
    rf"|\b({_ON}|{_OFF})\s+(?:met\s+)?(?:de\s+)?{_PURIFIER}\b)",
    re.I,
)

_DEVICE_STATUS = re.compile(
    r"\b(?:house\s+devices?|device\s+status|huisapparaten|"
    r"status\s+of\s+the\s+(?:devices?|appliances?))\b",
    re.I,
)

# Telegram affordances. Expanded into the plain phrases above so the slash and
# spoken forms cannot drift apart.
_SLASH = re.compile(r"^/(feed|airco|purifier|devices)(?:@[\w_]+)?\b\s*(.*)$", re.I | re.S)


def _expand_slash(raw: str) -> str | None:
    match = _SLASH.match(raw)
    if match is None:
        return None
    command = match.group(1).lower()
    rest = (match.group(2) or "").strip()
    if command == "devices":
        return "house devices"
    if command == "feed":
        if rest.isdigit():
            return f"feed the cats {rest} portions"
        return f"feed the cats {rest}".strip()
    return f"{command} {rest}" if rest else f"{command} status"


_MODE_CANONICAL = {
    "cooling": "cool",
    "koel": "cool",
    "koelen": "cool",
    "koeling": "cool",
    "heating": "heat",
    "warm": "heat",
    "verwarm": "heat",
    "verwarmen": "heat",
    "verwarming": "heat",
    "dehumidify": "dry",
    "drogen": "dry",
    "ontvochtigen": "dry",
    "fan only": "fan_only",
    "fan-only": "fan_only",
    "fanonly": "fan_only",
    "ventileren": "fan_only",
    "automatic": "auto",
    "laag": "low",
    "midden": "medium",
    "hoog": "high",
    "stil": "silent",
    "slaap": "sleep",
    "nacht": "night",
    "handmatig": "manual",
    "boost": "turbo",
}


def _canonical(word: str) -> str:
    key = (word or "").strip().lower()
    return _MODE_CANONICAL.get(key, key)


def _first_group(match: re.Match[str]) -> str:
    return next((group for group in match.groups() if group), "")


def _is_on(word: str) -> bool:
    return (word or "").strip().lower() in {"on", "aan"}


def match_device_phrase(text: str) -> DevicePlan | None:
    """Recognise a feeder / airco / purifier command, or return None."""
    raw = (text or "").strip()
    if not raw:
        return None
    raw = _expand_slash(raw) or raw

    if _DISCOVER.search(raw):
        return DevicePlan(tool="ha_discover_entities", kind="discover_entities", args={})

    schedule = _FEEDER_SCHEDULE.search(raw)
    if schedule:
        if re.search(rf"\b(?:{_OFF}|disable|stop|pauze|pause)\b", raw, re.I):
            action = "off"
        elif re.search(rf"\b(?:{_ON}|enable|start|resume|hervat)\b", raw, re.I):
            action = "on"
        else:
            action = "status"
        return DevicePlan(
            tool="pet_feeder_schedule",
            kind="feeder_schedule",
            args={"action": action},
        )

    if _FEEDER_STATUS.search(raw):
        return DevicePlan(tool="house_devices", kind="device_status", args={})

    if _FEED_NOW.search(raw):
        args: dict[str, Any] = {}
        portions = _FEED_PORTIONS.search(raw)
        if portions:
            args["portions"] = int(portions.group(1))
        elif _FEED_DOUBLE.search(raw):
            args["portions"] = 2
        if _FEED_FORCE.search(raw):
            args["force"] = True
        return DevicePlan(tool="pet_feeder_feed", kind="feed_pets", args=args)

    airco = _match_airco(raw)
    if airco is not None:
        return airco

    purifier = _match_purifier(raw)
    if purifier is not None:
        return purifier

    if _DEVICE_STATUS.search(raw):
        return DevicePlan(tool="house_devices", kind="device_status", args={})
    return None


def _match_airco(raw: str) -> DevicePlan | None:
    # Status first: "is the airco on?" is a question, not a power command.
    if _AIRCO_STATUS.search(raw):
        return DevicePlan(tool="airco_control", kind="airco", args={"action": "status"})

    temperature = _AIRCO_TEMPERATURE.search(raw)
    if temperature:
        return DevicePlan(
            tool="airco_control",
            kind="airco",
            args={"action": "set_temperature", "temperature": int(_first_group(temperature))},
        )

    # Fan speed before mode: "fan" is also an hvac mode name.
    fan = _AIRCO_FAN.search(raw)
    if fan:
        return DevicePlan(
            tool="airco_control",
            kind="airco",
            args={"action": "set_fan_mode", "fan_mode": _canonical(fan.group(1))},
        )

    mode = _AIRCO_MODE.search(raw)
    if mode:
        return DevicePlan(
            tool="airco_control",
            kind="airco",
            args={"action": "set_mode", "mode": _canonical(mode.group(1))},
        )

    power = _AIRCO_POWER.search(raw)
    if power:
        return DevicePlan(
            tool="airco_control",
            kind="airco",
            args={"action": "on" if _is_on(_first_group(power)) else "off"},
        )
    return None


def _match_purifier(raw: str) -> DevicePlan | None:
    if _PURIFIER_STATUS.search(raw):
        return DevicePlan(
            tool="air_purifier_control", kind="air_purifier", args={"action": "status"}
        )

    speed = _PURIFIER_SPEED.search(raw)
    if speed:
        return DevicePlan(
            tool="air_purifier_control",
            kind="air_purifier",
            args={"action": "set_speed", "percentage": int(speed.group(1))},
        )

    preset = _PURIFIER_PRESET.search(raw)
    if preset:
        return DevicePlan(
            tool="air_purifier_control",
            kind="air_purifier",
            args={"action": "set_mode", "preset_mode": _canonical(preset.group(1))},
        )

    power = _PURIFIER_POWER.search(raw)
    if power:
        return DevicePlan(
            tool="air_purifier_control",
            kind="air_purifier",
            args={"action": "on" if _is_on(_first_group(power)) else "off"},
        )
    return None


__all__ = ["DevicePlan", "match_device_phrase"]
