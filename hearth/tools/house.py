"""Whole-home rituals and comfort controls.

Composes Home Assistant services already in the house (scenes, lights, climate,
fans, buttons, the Denon/LG path). There is no Tuya client here — if a device
is not represented in HA, the tool says so and does not invent a result.
"""

from __future__ import annotations

import re
from typing import Any

from hearth.config import settings
from hearth.tools.ha import ha

RITUALS = ("sleep", "morning", "movie")

_RITUAL_LABELS = {
    "sleep": "House sleep",
    "morning": "Good morning",
    "movie": "Movie night",
}

_AIR_CLASSES = {
    "pm25": "PM2.5",
    "pm10": "PM10",
    "aqi": "AQI",
    "carbon_dioxide": "CO₂",
    "volatile_organic_compounds": "VOC",
    "nitrogen_dioxide": "NO₂",
}

_PURIFIER_WORDS = ("purifier", "luchtreiniger", "air_purifier", "hepa")
_FEEDER_WORDS = ("feeder", "voeder", "voerbak", "pet_feeder", "cat_feeder", "dog_feeder")
_SLEEP_SCENES = ("good_night", "goodnight", "house_sleep", "welterusten", "lights_out")
_MORNING_SCENES = ("good_morning", "goodmorning", "morning_lights", "wake_up")
_MOVIE_SCENES = ("movie_night", "cinema", "filmavond", "cinema_mode")


def voice_plan(text: str) -> dict[str, Any] | None:
    """Local-router plan for ritual / climate / feeder / purifier / air asks."""
    raw = _clean(text)
    if not raw:
        return None
    ritual = _voice_ritual(raw)
    if ritual:
        return {"tool": "house_ritual", "args": {"ritual": ritual}}
    climate = _voice_climate(raw)
    if climate:
        return {"tool": "house_climate", "args": climate}
    if _voice_feeder(raw):
        return {"tool": "house_feeder", "args": {"action": "feed"}}
    purifier = _voice_purifier(raw)
    if purifier:
        return {"tool": "house_purifier", "args": purifier}
    if _voice_comfort(raw):
        return {"tool": "house_comfort", "args": {}}
    return None


async def run_ritual(ritual: str) -> dict[str, Any]:
    key = _ritual_key(ritual)
    if key is None:
        return {
            "ok": False,
            "error": "ritual must be sleep, morning, or movie",
            "speak": "I can do house sleep, good morning, or movie night.",
        }
    snapshot = await _snapshot()
    if not snapshot.get("ok"):
        return {
            "ok": False,
            "mode": snapshot.get("mode"),
            "error": snapshot.get("error") or "Home Assistant is unreachable",
            "speak": f"Home Assistant is unreachable: {snapshot.get('error') or 'no reply'}.",
        }
    rows = snapshot["rows"]
    if key == "sleep":
        return await _sleep(rows, snapshot.get("mode"))
    if key == "morning":
        return await _morning(rows, snapshot.get("mode"))
    return await _movie(rows, snapshot.get("mode"))


async def climate_control(
    action: str,
    *,
    temperature: float | None = None,
    entity: str | None = None,
) -> dict[str, Any]:
    snapshot = await _snapshot()
    if not snapshot.get("ok"):
        return _ha_down(snapshot)
    chosen, error = _pick_configured(
        _climate_rows(snapshot["rows"]),
        entity or settings.ha_climate_entity,
        noun="climate",
    )
    if error:
        return error
    assert chosen is not None
    entity_id = str(chosen["entity_id"])
    name = _name(chosen)
    verb = (action or "status").strip().lower().replace("-", "_")
    steps: list[dict[str, Any]] = []
    if verb in {"status", "state", "read"}:
        return _climate_status(chosen, snapshot.get("mode"))
    if verb in {"warmer", "up", "heat_up"}:
        target = _setpoint(chosen)
        if target is None:
            return _no_setpoint(name)
        steps.extend(await _nudge_climate(chosen, target + 0.5, heat_if_off=True))
    elif verb in {"cooler", "down", "cool_down"}:
        target = _setpoint(chosen)
        if target is None:
            return _no_setpoint(name)
        steps.extend(await _nudge_climate(chosen, target - 0.5, heat_if_off=False))
    elif verb in {"set", "set_temperature", "temperature"}:
        if temperature is None:
            return {
                "ok": False,
                "error": "temperature required",
                "speak": f"Tell me a temperature for {name}.",
            }
        steps.extend(await _nudge_climate(chosen, float(temperature), heat_if_off=False))
    elif verb in {"off", "heat", "cool", "auto", "dry", "fan_only"}:
        steps.append(
            await _service("climate", "set_hvac_mode", entity_id, {"hvac_mode": verb})
        )
    else:
        return {
            "ok": False,
            "error": f"unknown climate action {action!r}",
            "speak": (
                "I can read the climate, nudge it warmer or cooler, "
                "set a temperature, or turn it off."
            ),
        }
    fresh = await ha.get_state(entity_id)
    state = fresh.get("state") if fresh.get("ok") else chosen
    speak = _climate_sentence(_name(state or chosen), state or chosen)
    if any(step.get("ok") is False for step in steps):
        speak = f"{speak} Home Assistant did not take every step."
    return _finish(
        steps,
        snapshot.get("mode"),
        speak=speak,
        extra={"action": verb, "entity_id": entity_id, "climate": _climate_card(state or chosen)},
    )


async def feeder_control(action: str = "feed", *, entity: str | None = None) -> dict[str, Any]:
    snapshot = await _snapshot()
    if not snapshot.get("ok"):
        return _ha_down(snapshot)
    feeders = _feeder_rows(snapshot["rows"])
    verb = (action or "feed").strip().lower()
    if verb in {"status", "state", "list"}:
        if not feeders:
            return _missing(
                "feeder",
                "No feeder is on Home Assistant. Pair it there and I’ll press it.",
            )
        names = ", ".join(_name(row) for row in feeders)
        return {
            "ok": True,
            "mode": snapshot.get("mode"),
            "feeders": [_device_card(row) for row in feeders],
            "speak": f"Feeders on Home Assistant: {names}.",
        }
    chosen, error = _pick_configured(feeders, entity or settings.ha_feeder_entity, noun="feeder")
    if error:
        return error
    assert chosen is not None
    entity_id = str(chosen["entity_id"])
    domain = entity_id.split(".", 1)[0]
    if domain == "button":
        step = await _service("button", "press", entity_id)
    else:
        step = await _service(domain, "turn_on", entity_id)
    name = _name(chosen)
    speak = f"Pressed {name}." if step.get("ok") else f"Could not feed via {name}."
    return _finish(
        [step],
        snapshot.get("mode"),
        speak=speak,
        extra={"action": "feed", "entity_id": entity_id},
    )


async def purifier_control(
    action: str = "status",
    *,
    entity: str | None = None,
    percentage: float | None = None,
) -> dict[str, Any]:
    snapshot = await _snapshot()
    if not snapshot.get("ok"):
        return _ha_down(snapshot)
    units = _purifier_rows(snapshot["rows"])
    verb = (action or "status").strip().lower()
    if verb in {"status", "state", "list"} and not (entity or settings.ha_purifier_entity):
        if not units:
            return _missing(
                "purifier",
                "No air purifier is on Home Assistant. Pair it there and I’ll switch it.",
            )
        bits = ", ".join(f"{_name(row)} is {row.get('state')}" for row in units)
        return {
            "ok": True,
            "mode": snapshot.get("mode"),
            "purifiers": [_device_card(row) for row in units],
            "speak": bits + ".",
        }
    chosen, error = _pick_configured(units, entity or settings.ha_purifier_entity, noun="purifier")
    if error:
        return error
    assert chosen is not None
    entity_id = str(chosen["entity_id"])
    domain = entity_id.split(".", 1)[0]
    if verb in {"status", "state"}:
        return {
            "ok": True,
            "mode": snapshot.get("mode"),
            "entity_id": entity_id,
            "purifier": _device_card(chosen),
            "speak": f"{_name(chosen)} is {chosen.get('state')}.",
        }
    if verb in {"on", "turn_on"}:
        data = {}
        if percentage is not None and domain == "fan":
            data["percentage"] = max(0.0, min(100.0, float(percentage)))
        step = await _service(domain, "turn_on", entity_id, data or None)
    elif verb in {"off", "turn_off"}:
        step = await _service(domain, "turn_off", entity_id)
    elif verb == "toggle":
        step = await _service(domain, "toggle", entity_id)
    else:
        return {
            "ok": False,
            "error": f"unknown purifier action {action!r}",
            "speak": "I can turn the purifier on, off, or tell you its state.",
        }
    fresh = await ha.get_state(entity_id)
    state = (fresh.get("state") or {}).get("state") if fresh.get("ok") else None
    label = state or ("on" if verb in {"on", "turn_on", "toggle"} else "off")
    speak = (
        f"{_name(chosen)} is {label}."
        if step.get("ok")
        else f"Could not change {_name(chosen)}."
    )
    return _finish(
        [step],
        snapshot.get("mode"),
        speak=speak,
        extra={"action": verb, "entity_id": entity_id, "state": label},
    )


async def comfort_snapshot() -> dict[str, Any]:
    """Climate + indoor air for the dashboard and a spoken comfort line."""
    snapshot = await _snapshot()
    if not snapshot.get("ok"):
        return {
            "ok": False,
            "mode": snapshot.get("mode"),
            "error": snapshot.get("error"),
            "rituals": _ritual_chips(),
            "climate": [],
            "air": [],
            "purifiers": [],
            "feeders": [],
            "speak": f"Home Assistant is unreachable: {snapshot.get('error') or 'no reply'}.",
        }
    rows = snapshot["rows"]
    climate = [_climate_card(row) for row in _climate_rows(rows)]
    air = [_air_card(row) for row in _air_rows(rows)]
    purifiers = [_device_card(row) for row in _purifier_rows(rows)]
    feeders = [_device_card(row) for row in _feeder_rows(rows)]
    parts: list[str] = []
    parts.extend(_climate_sentence(card["name"], _row_from_card(card)) for card in climate)
    if air:
        readings = ", ".join(
            f"{card['label']} {card['state']} {card['unit']}".strip() for card in air
        )
        parts.append(readings + ".")
    if purifiers:
        parts.append(", ".join(f"{card['name']} is {card['state']}" for card in purifiers) + ".")
    if not parts:
        parts.append("No climate or air-quality entities are on Home Assistant yet.")
    return {
        "ok": True,
        "mode": snapshot.get("mode"),
        "rituals": _ritual_chips(),
        "climate": climate,
        "air": air,
        "purifiers": purifiers,
        "feeders": feeders,
        "speak": " ".join(parts),
    }


async def _sleep(rows: list[dict[str, Any]], mode: Any) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    scene = _match_scene(rows, _SLEEP_SCENES)
    if scene:
        steps.append(await _service("scene", "turn_on", str(scene["entity_id"])))
    else:
        lights = _light_rows(rows)
        for light in lights:
            steps.append(await _service("light", "turn_off", str(light["entity_id"])))
    media = await _media_power(rows, "off")
    steps.extend(media)
    failed = [step for step in steps if step.get("ok") is False and not step.get("skipped")]
    ran = [step for step in steps if not step.get("skipped")]
    if not ran:
        return _missing("ritual", "Nothing in Home Assistant matches house sleep yet.")
    lights_bit = f"The {_name(scene)} scene is on" if scene else "Lights are off"
    if any(step.get("kind") == "media" and not step.get("skipped") for step in steps):
        media_bit = "The cinema is off." if not failed else "The cinema only partly powered down."
    else:
        media_bit = "No TV or receiver is paired, so I left that path alone."
    speak = f"House is down. {lights_bit}. {media_bit}"
    return _finish(steps, mode, speak=speak, extra={"ritual": "sleep"})


async def _morning(rows: list[dict[str, Any]], mode: Any) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    scene = _match_scene(rows, _MORNING_SCENES)
    if scene:
        steps.append(await _service("scene", "turn_on", str(scene["entity_id"])))
        lights_bit = f"The {_name(scene)} scene is on"
    else:
        lights = _morning_lights(rows)
        for light in lights:
            steps.append(
                await _service(
                    "light",
                    "turn_on",
                    str(light["entity_id"]),
                    {"brightness_pct": 70},
                )
            )
        lights_bit = _joined_names(lights, "Lights") + " at a morning level" if lights else ""
    ran = [step for step in steps if not step.get("skipped")]
    if not ran:
        return _missing("ritual", "No morning scene or lights are on Home Assistant yet.")
    speak = f"Good morning. {lights_bit}. The cinema stays dark."
    return _finish(steps, mode, speak=speak, extra={"ritual": "morning"})


async def _movie(rows: list[dict[str, Any]], mode: Any) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    scene = _match_scene(rows, _MOVIE_SCENES)
    if scene:
        steps.append(await _service("scene", "turn_on", str(scene["entity_id"])))
        lights_bit = f"The {_name(scene)} scene is on"
    else:
        living = _named_lights(rows, ("living_room", "living"))
        others = [light for light in _light_rows(rows) if light not in living]
        for light in living:
            steps.append(
                await _service(
                    "light",
                    "turn_on",
                    str(light["entity_id"]),
                    {"brightness_pct": 12},
                )
            )
        for light in others:
            steps.append(await _service("light", "turn_off", str(light["entity_id"])))
        lights_bit = "Lights are down"
    media = await _media_power(rows, "movie")
    steps.extend(media)
    ran = [step for step in steps if not step.get("skipped")]
    if not ran:
        return _missing("ritual", "Nothing in Home Assistant matches movie night yet.")
    if any(step.get("kind") == "media" and step.get("ok") for step in media):
        media_bit = "The Denon and TV are up."
    elif any(step.get("kind") == "media" for step in media):
        media_bit = "The cinema path did not fully come up."
    else:
        media_bit = "No TV or receiver is paired, so I only set the lights."
    speak = f"Movie night. {lights_bit}. {media_bit}"
    return _finish(steps, mode, speak=speak, extra={"ritual": "movie"})


async def _media_power(rows: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """AVR + TV (+ Apple TV when it exists). Skips roles HA does not represent."""
    present = {
        role: _has_role(rows, role)
        for role in ("apple_tv", "tv", "avr")
    }
    if not present["tv"] and not present["avr"]:
        return []
    steps: list[dict[str, Any]] = []
    if mode == "off":
        for role in ("apple_tv", "tv", "avr"):
            if not present[role]:
                continue
            result = await ha.media_control(role, "turn_off")
            steps.append(_media_step(role, "turn_off", result))
        return steps
    if present["tv"] and present["avr"]:
        target = "apple_tv" if present["apple_tv"] else "tv"
        result = await ha.activate_media_path(target)
        steps.append(
            {
                "ok": bool(result.get("ok")),
                "skipped": False,
                "kind": "media",
                "action": target,
                "entity_id": None,
                "mode": None,
                "error": None if result.get("ok") else result.get("speak"),
            }
        )
        return steps
    role = "tv" if present["tv"] else "avr"
    result = await ha.media_control(role, "turn_on")
    steps.append(_media_step(role, "turn_on", result))
    return steps


def _media_step(role: str, action: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(result.get("ok")),
        "skipped": False,
        "kind": "media",
        "action": f"{role}.{action}",
        "entity_id": result.get("entity_id"),
        "mode": result.get("mode"),
        "error": result.get("error"),
    }


async def _service(
    domain: str,
    service: str,
    entity_id: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = await ha.call_service(domain, service, entity_id, data)
    return {
        "ok": bool(result.get("ok")),
        "skipped": False,
        "kind": f"{domain}.{service}",
        "entity_id": entity_id,
        "mode": result.get("mode"),
        "error": result.get("error"),
    }


async def _nudge_climate(
    row: dict[str, Any],
    target: float,
    *,
    heat_if_off: bool,
) -> list[dict[str, Any]]:
    entity_id = str(row["entity_id"])
    bounded = max(5.0, min(30.0, round(float(target) * 2) / 2))
    steps: list[dict[str, Any]] = []
    if heat_if_off and str(row.get("state") or "").lower() == "off":
        steps.append(
            await _service("climate", "set_hvac_mode", entity_id, {"hvac_mode": "heat"})
        )
    steps.append(
        await _service("climate", "set_temperature", entity_id, {"temperature": bounded})
    )
    return steps


async def _snapshot() -> dict[str, Any]:
    result = await ha.list_states()
    if not result.get("ok"):
        return {"ok": False, "mode": result.get("mode"), "error": result.get("error"), "rows": []}
    return {"ok": True, "mode": result.get("mode"), "rows": list(result.get("states") or [])}


def _finish(
    steps: list[dict[str, Any]],
    mode: Any,
    *,
    speak: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempted = [step for step in steps if not step.get("skipped")]
    failed = [step for step in attempted if step.get("ok") is False]
    modes = {step.get("mode") for step in attempted if step.get("mode")}
    if not modes:
        rolled = mode or "mock"
    elif modes == {"live"} or modes == {"mock"}:
        rolled = next(iter(modes))
    else:
        rolled = "mixed"
    payload = {
        "ok": bool(attempted) and not failed,
        "mode": rolled,
        "steps": steps,
        "failed_steps": len(failed),
        "speak": speak,
    }
    if extra:
        payload.update(extra)
    return payload


def _ha_down(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": snapshot.get("mode"),
        "error": snapshot.get("error") or "Home Assistant is unreachable",
        "speak": f"Home Assistant is unreachable: {snapshot.get('error') or 'no reply'}.",
    }


def _missing(kind: str, speak: str) -> dict[str, Any]:
    return {"ok": False, "configured": False, "kind": kind, "error": speak, "speak": speak}


def _no_setpoint(name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "no setpoint",
        "speak": f"{name} has no temperature setpoint on Home Assistant.",
    }


def _climate_status(row: dict[str, Any], mode: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "mode": mode,
        "entity_id": row.get("entity_id"),
        "climate": _climate_card(row),
        "speak": _climate_sentence(_name(row), row),
    }


def _pick_configured(
    rows: list[dict[str, Any]],
    preferred: str,
    *,
    noun: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    wanted = (preferred or "").strip()
    if wanted:
        match = next((row for row in rows if str(row.get("entity_id")) == wanted), None)
        if match is None:
            # Configured id might be absent from the filtered list (unavailable).
            return None, _missing(
                noun,
                f"{wanted} is not available on Home Assistant.",
            )
        return match, None
    if not rows:
        return None, _missing(
            noun,
            f"No {noun} is on Home Assistant. Pair it there and I’ll control it.",
        )
    if len(rows) == 1:
        return rows[0], None
    living = [
        row
        for row in rows
        if "living" in _slug(str(row.get("entity_id"))) or "living" in _slug(_name(row))
    ]
    if len(living) == 1:
        return living[0], None
    names = ", ".join(f"{_name(row)} ({row.get('entity_id')})" for row in rows[:6])
    return None, {
        "ok": False,
        "ambiguous": True,
        "matches": [_device_card(row) for row in rows[:6]],
        "error": f"More than one {noun} matches",
        "speak": f"Which {noun}? {names}.",
    }


def _climate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if _domain(row) == "climate" and _controllable(row)]


def _light_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if _domain(row) == "light" and _controllable(row)]


def _purifier_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for row in rows:
        if _domain(row) not in {"fan", "switch", "humidifier"}:
            continue
        if not _controllable(row):
            continue
        if _looks_like(row, _PURIFIER_WORDS) or _device_class(row) in {"air_purifier", "purifier"}:
            found.append(row)
    return found


def _feeder_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for row in rows:
        if _domain(row) not in {"button", "switch"}:
            continue
        state = str(row.get("state") or "").lower()
        if state == "unavailable":
            continue
        if _looks_like(row, _FEEDER_WORDS):
            found.append(row)
    return found


def _air_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for row in rows:
        if _domain(row) != "sensor" or not _controllable(row):
            continue
        device_class = _device_class(row)
        blob = _blob(row)
        if device_class in _AIR_CLASSES or "air_quality" in blob or "pm2" in blob:
            found.append(row)
    return found


def _match_scene(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0
    for row in rows:
        if _domain(row) != "scene" or not _controllable(row):
            continue
        object_id = _slug(str(row.get("entity_id") or "").split(".", 1)[-1])
        name = _slug(_name(row))
        score = 0
        for key in keys:
            token = _slug(key)
            if object_id == token or name == token:
                score = max(score, 100)
            elif token and (token in object_id or token in name):
                score = max(score, 70)
        if score > best_score:
            best = row
            best_score = score
    return best


def _morning_lights(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    named = _named_lights(rows, ("kitchen", "living_room", "living"))
    if named:
        return named
    return _light_rows(rows)[:2]


def _named_lights(rows: list[dict[str, Any]], tokens: tuple[str, ...]) -> list[dict[str, Any]]:
    wanted = {_slug(token) for token in tokens}
    found: list[dict[str, Any]] = []
    for light in _light_rows(rows):
        blob = _slug(str(light.get("entity_id"))) + "_" + _slug(_name(light))
        if any(token in blob for token in wanted):
            found.append(light)
    return found


def _has_role(rows: list[dict[str, Any]], role: str) -> bool:
    configured = {
        "tv": settings.ha_tv_entity,
        "avr": settings.ha_avr_entity,
        "apple_tv": settings.ha_apple_tv_entity,
    }.get(role, "")
    for row in rows:
        if _domain(row) != "media_player" or not _controllable(row):
            continue
        if configured and str(row.get("entity_id")) == configured:
            return True
        blob = _blob(row)
        if role == "tv" and "apple" not in blob and ("webos" in blob or "lg" in blob):
            return True
        if role == "avr" and ("denon" in blob or "avr" in blob or "receiver" in blob):
            return True
        if role == "apple_tv" and "apple" in blob and "tv" in blob:
            return True
    return False


def _climate_card(row: dict[str, Any]) -> dict[str, Any]:
    attrs = row.get("attributes") or {}
    return {
        "entity_id": row.get("entity_id"),
        "name": _name(row),
        "state": row.get("state"),
        "current": attrs.get("current_temperature"),
        "target": attrs.get("temperature"),
        "unit": attrs.get("temperature_unit") or "°C",
        "action": attrs.get("hvac_action"),
    }


def _air_card(row: dict[str, Any]) -> dict[str, Any]:
    device_class = _device_class(row)
    attrs = row.get("attributes") or {}
    label = _AIR_CLASSES.get(device_class) or _name(row)
    return {
        "entity_id": row.get("entity_id"),
        "name": _name(row),
        "label": label,
        "state": row.get("state"),
        "unit": attrs.get("unit_of_measurement") or "",
        "device_class": device_class,
        "tone": _air_tone(device_class, row.get("state")),
    }


def _device_card(row: dict[str, Any]) -> dict[str, Any]:
    attrs = row.get("attributes") or {}
    return {
        "entity_id": row.get("entity_id"),
        "name": _name(row),
        "state": row.get("state"),
        "percentage": attrs.get("percentage"),
        "device_class": _device_class(row),
    }


def _row_from_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_id": card.get("entity_id"),
        "state": card.get("state"),
        "attributes": {
            "friendly_name": card.get("name"),
            "current_temperature": card.get("current"),
            "temperature": card.get("target"),
            "temperature_unit": card.get("unit"),
            "hvac_action": card.get("action"),
        },
    }


def _climate_sentence(name: str, row: dict[str, Any]) -> str:
    attrs = row.get("attributes") or {}
    unit = attrs.get("temperature_unit") or "°C"
    current = attrs.get("current_temperature")
    target = attrs.get("temperature")
    state = row.get("state") or "unknown"
    if current is not None and target is not None:
        return f"{name} is {current}{unit}, set to {target}{unit} ({state})."
    if target is not None:
        return f"{name} is set to {target}{unit} ({state})."
    return f"{name} is {state}."


def _air_tone(device_class: str, value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "info"
    if device_class == "pm25":
        return "good" if number <= 12 else "fair" if number <= 35 else "poor"
    if device_class == "pm10":
        return "good" if number <= 20 else "fair" if number <= 50 else "poor"
    if device_class == "carbon_dioxide":
        return "good" if number < 800 else "fair" if number < 1200 else "poor"
    if device_class == "aqi":
        return "good" if number <= 50 else "fair" if number <= 100 else "poor"
    return "info"


def _ritual_chips() -> list[dict[str, str]]:
    return [{"id": key, "label": _RITUAL_LABELS[key]} for key in RITUALS]


def _ritual_key(value: str) -> str | None:
    key = _slug(value)
    aliases = {
        "sleep": "sleep",
        "house_sleep": "sleep",
        "good_night": "sleep",
        "goodnight": "sleep",
        "lights_out": "sleep",
        "morning": "morning",
        "good_morning": "morning",
        "movie": "movie",
        "movie_night": "movie",
        "cinema": "movie",
        "cinema_mode": "movie",
    }
    return aliases.get(key)


def _joined_names(rows: list[dict[str, Any]], fallback: str) -> str:
    names = [_name(row) for row in rows]
    if not names:
        return fallback
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _voice_ritual(raw: str) -> str | None:
    if re.search(r"\b(play|watch|recommend|suggest|download|grab|queue)\b", raw):
        return None
    # Spoken "movie night" (including movie night mode) is media_activity.
    # "activate/start movie night" is the scene command. Cinema mode and
    # filmavond stay on this ritual.
    if re.fullmatch(
        r"(?:(?:set|start|prepare|activate|it'?s)\s+)?(?:movie|film|cinema)\s+night(?:\s+mode)?",
        raw,
    ):
        return None
    if re.fullmatch(r"(cinema mode|filmavond|house movie night)", raw):
        return "movie"
    if re.fullmatch(
        r"(house sleep|good night|goodnight|lights out|welterusten|put the house to sleep|"
        r"time for bed|good morning|goedemorgen)",
        raw,
    ):
        if raw in {"good morning", "goedemorgen"}:
            return "morning"
        return "sleep"
    return None


def _voice_climate(raw: str) -> dict[str, Any] | None:
    if re.search(r"\b(outside|weather|forecast|raining|snow)\b", raw):
        return None
    setpoint = re.fullmatch(
        r"(?:please )?(?:set )?(?:the )?(?:heat|heating|thermostat|climate) to (\d{1,2}(?:\.\d)?)",
        raw,
    )
    if setpoint:
        return {"action": "set", "temperature": float(setpoint.group(1))}
    if re.fullmatch(r"(warmer|make it warmer|turn the heat up|heat up)", raw):
        return {"action": "warmer"}
    if re.fullmatch(r"(cooler|make it cooler|turn the heat down|heat down)", raw):
        return {"action": "cooler"}
    if re.fullmatch(r"(climate off|turn the heat off|thermostat off|heating off)", raw):
        return {"action": "off"}
    if re.fullmatch(
        r"(what'?s the climate|how'?s the (?:heat|heating|thermostat)|climate status)",
        raw,
    ):
        return {"action": "status"}
    return None


def _voice_feeder(raw: str) -> bool:
    return bool(
        re.fullmatch(
            r"(feed|feed the (?:cat|cats|dog|dogs|pets|feeder)|feeder|voeder|voeren)",
            raw,
        )
    )


def _voice_purifier(raw: str) -> dict[str, Any] | None:
    if re.fullmatch(r"(?:turn )?(?:the )?(?:air )?purifier on|purifier on|air purifier on", raw):
        return {"action": "on"}
    if re.fullmatch(r"(?:turn )?(?:the )?(?:air )?purifier off|purifier off|air purifier off", raw):
        return {"action": "off"}
    if re.fullmatch(r"(?:the )?air purifier|purifier|luchtreiniger", raw):
        return {"action": "status"}
    return None


def _voice_comfort(raw: str) -> bool:
    return bool(
        re.fullmatch(
            r"(how'?s the air|air quality|what'?s the air quality|house comfort|comfort)",
            raw,
        )
    )


def _clean(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip().lower())
    raw = raw.strip(" .!?")
    raw = re.sub(r"^(please |can you |could you )", "", raw)
    return raw


def _controllable(row: dict[str, Any]) -> bool:
    return str(row.get("state") or "").lower() not in {"unavailable", "unknown"}


def _looks_like(row: dict[str, Any], words: tuple[str, ...]) -> bool:
    blob = _blob(row).replace(" ", "_")
    return any(word in blob for word in words)


def _device_class(row: dict[str, Any]) -> str:
    return str((row.get("attributes") or {}).get("device_class") or "").lower()


def _setpoint(row: dict[str, Any]) -> float | None:
    value = (row.get("attributes") or {}).get("temperature")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _domain(row: dict[str, Any]) -> str:
    entity_id = str(row.get("entity_id") or "")
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _name(row: dict[str, Any]) -> str:
    attrs = row.get("attributes") or {}
    return str(attrs.get("friendly_name") or row.get("entity_id") or "device")


def _blob(row: dict[str, Any]) -> str:
    return f"{row.get('entity_id') or ''} {_name(row)}".lower()


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (value or "").lower())).strip("_")
