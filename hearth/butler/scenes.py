"""Movie night, quiet hours, and good night — Home Assistant scenes when they exist."""

from __future__ import annotations

import re
from typing import Any

from hearth.tools.ha import ha

_UNAVAILABLE = frozenset({"unavailable", "unknown"})

PRESETS: dict[str, dict[str, Any]] = {
    "movie_night": {
        "label": "Movie night",
        "entities": ("scene.movie_night", "scene.cinema", "scene.film_night"),
        "names": ("movie night", "film night", "cinema night", "filmavond"),
    },
    "quiet_hours": {
        "label": "Quiet hours",
        "entities": ("scene.quiet_hours", "scene.quiet", "scene.do_not_disturb", "scene.hush"),
        "names": ("quiet hours", "hush", "stilte", "stille uren"),
    },
    "good_night": {
        "label": "Good night",
        "entities": ("scene.good_night", "scene.goodnight", "scene.bedtime"),
        "names": ("good night", "bedtime", "lights out", "slaap lekker"),
    },
}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _friendly(row: dict[str, Any]) -> str:
    attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    return str(attributes.get("friendly_name") or row.get("entity_id") or "")


def resolve_scene(preset: str, states: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the HA scene that matches a preset. Exact entity id, then exact name."""
    spec = PRESETS.get(preset)
    if spec is None:
        return None
    entity_ids = {str(entity).casefold() for entity in spec["entities"]}
    names = {_norm(name) for name in spec["names"]}
    by_name: dict[str, Any] | None = None
    for row in states:
        if not isinstance(row, dict):
            continue
        entity = str(row.get("entity_id") or "")
        if not entity.startswith("scene."):
            continue
        if entity.casefold() in entity_ids:
            return row
        friendly = _norm(_friendly(row))
        if friendly in names or any(friendly.startswith(f"{name} ") for name in names):
            by_name = row
    return by_name


def preset_status(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which presets the current scene list can actually run."""
    rows: list[dict[str, Any]] = []
    for preset, spec in PRESETS.items():
        match = resolve_scene(preset, states)
        state = str(match.get("state") or "") if match else ""
        rows.append(
            {
                "preset": preset,
                "label": spec["label"],
                "available": match is not None and state not in _UNAVAILABLE,
                "entity_id": match.get("entity_id") if match else None,
                "state": state or None,
            }
        )
    return rows


def _scene_names(states: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in states:
        if not isinstance(row, dict):
            continue
        entity = str(row.get("entity_id") or "")
        if not entity.startswith("scene."):
            continue
        label = _friendly(row)
        if label:
            names.append(label)
    return names


async def activate_preset(preset: str) -> dict[str, Any]:
    """Turn on the matching scene. A missing scene is a spoken miss, not a fake success."""
    key = (preset or "").strip().casefold().replace("-", "_").replace(" ", "_")
    spec = PRESETS.get(key)
    if spec is None:
        return {
            "ok": False,
            "preset": key,
            "speak": "I know movie night, quiet hours, and good night.",
        }
    label = str(spec["label"])
    listed = await ha.list_states("scene")
    states = listed.get("states") or []
    if not isinstance(states, list):
        states = []
    mode = listed.get("mode") or ("live" if ha.live else "mock")
    if listed.get("ok") is False:
        detail = str(listed.get("error") or "no response")
        return {
            "ok": False,
            "mode": mode,
            "preset": key,
            "speak": (
                f"Home Assistant didn’t answer, so I didn’t change the lights ({detail}). "
                f"Try {label.lower()} again in a moment."
            ),
        }
    match = resolve_scene(key, states)
    if match is None:
        known = _scene_names(states)
        if known:
            listing = ", ".join(known[:8])
            speak = (
                f"I don’t see a {label} scene in Home Assistant. "
                f"Scenes I can run: {listing}."
            )
        else:
            speak = (
                f"I don’t see a {label} scene, and Home Assistant didn’t list any scenes. "
                "Add one with that name and I’ll use it."
            )
        return {
            "ok": False,
            "mode": mode,
            "preset": key,
            "scenes": known,
            "speak": speak,
        }
    entity_id = str(match.get("entity_id") or "")
    state = str(match.get("state") or "")
    if state in _UNAVAILABLE:
        return {
            "ok": False,
            "mode": mode,
            "preset": key,
            "entity_id": entity_id,
            "speak": (
                f"{label} is unavailable in Home Assistant right now. "
                "I left the lights alone — try again once that scene is back."
            ),
        }
    called = await ha.call_service("scene", "turn_on", entity_id)
    if called.get("ok") is False:
        detail = str(called.get("error") or "it didn’t accept the call")
        return {
            "ok": False,
            "mode": called.get("mode") or mode,
            "preset": key,
            "entity_id": entity_id,
            "speak": (
                f"Home Assistant didn’t run {label} ({detail}). "
                "The lights should be unchanged — try the scene again."
            ),
        }
    ran = called.get("mode") or mode
    speak = f"{label} is on."
    if ran == "mock":
        speak = f"{label} is on. Home Assistant isn’t connected, so this is the fixture house."
    return {
        "ok": True,
        "mode": ran,
        "preset": key,
        "entity_id": entity_id,
        "label": _friendly(match) or label,
        "speak": speak,
    }


__all__ = ["PRESETS", "activate_preset", "preset_status", "resolve_scene"]
