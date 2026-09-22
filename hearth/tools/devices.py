"""Non-media house devices: PetZero pet feeders, Tuya airco, Tuya air purifier.

The house runs PetZero feeders plus Tuya OEM hardware (the My AI Apps / Smart
Life stack, including the KPT Air Purifier and the air conditioner). Home
Assistant stays the device layer — pair them with Tuya Local so control is a LAN
call instead of a cloud round trip — and Hearth drives HA over REST exactly as
it does for media.

Nothing here hardcodes a single entity id. Tuya object ids depend on how a
device was paired, so each role owns an ordered candidate list from ``.env``
(``HA_PET_FEEDER_ENTITIES``, ``HA_AIRCO_ENTITIES``, ``HA_AIR_PURIFIER_ENTITIES``)
and falls back to keyword discovery over live HA state. When discovery is
ambiguous the tools say which entities matched instead of picking one:
switching the wrong Tuya relay is worse than asking.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from hearth.config import settings
from hearth.tools.ha import ha

# Vendor fingerprints that show up in entity ids / friendly names once a Tuya
# OEM device is adopted (Tuya cloud, Tuya Local, or a Smart Life rebadge).
_TUYA_MARKERS = (
    "tuya",
    "smart_life",
    "smartlife",
    "my_ai_apps",
    "myaiapps",
    "localtuya",
    "kpt",
    "petzero",
)

_UNREACHABLE = frozenset({"unavailable", "unknown"})

# Tuya aircos advertise wildly different mode spellings. Map what a human says
# onto whatever the entity actually reports in hvac_modes.
_HVAC_ALIASES: dict[str, tuple[str, ...]] = {
    "cool": ("cool", "cooling", "koel", "koelen", "kouder", "ac"),
    "heat": ("heat", "heating", "warm", "warmte", "verwarmen", "verwarming"),
    "dry": ("dry", "dehumidify", "drogen", "ontvochtigen", "vocht"),
    "fan_only": ("fan_only", "fan", "ventilate", "ventilator", "ventileren", "blow"),
    "heat_cool": ("heat_cool", "auto_heat_cool", "range"),
    "auto": ("auto", "automatic", "automatisch"),
    "off": ("off", "uit", "stop"),
}

# Order Hearth reaches for when asked to simply turn the airco on and the
# entity offers several running modes. Overridable with HA_AIRCO_DEFAULT_MODE.
_HVAC_ON_PREFERENCE = ("cool", "heat_cool", "auto", "heat", "dry", "fan_only")


@dataclass(frozen=True, slots=True)
class DeviceRole:
    """One controllable house role and how to recognise it in Home Assistant."""

    key: str
    label: str
    domains: tuple[str, ...]
    # Decisive words ("purifier"), then supporting ones ("hepa", "kpt").
    strong: tuple[str, ...]
    weak: tuple[str, ...] = ()
    # Companion entities that share the device name but are not the control.
    exclude: tuple[str, ...] = ()
    env_var: str = ""
    pairing_hint: str = ""

    def configured(self) -> list[str]:
        return _ROLE_CONFIG[self.key]()


ROLES: dict[str, DeviceRole] = {
    "pet_feeder": DeviceRole(
        key="pet_feeder",
        label="Pet feeder",
        domains=("button", "switch"),
        strong=("feeder", "petzero", "pet_zero", "voerautomaat", "voerbak"),
        weak=("feed", "pet", "cat", "kat", "kitty", "dispenser", "voer"),
        exclude=(
            "schedule",
            "auto_feed",
            "portion",
            "child_lock",
            "indicator",
            "buzzer",
            "sound",
            "volume",
            "reset",
            "calibrat",
            "light",
            "led",
        ),
        env_var="HA_PET_FEEDER_ENTITIES",
        pairing_hint=(
            "Pair the PetZero feeder in Home Assistant (Tuya Local on the LAN, "
            "or the Tuya cloud integration), then set HA_PET_FEEDER_ENTITIES to "
            "the manual-feed button or switch entity id."
        ),
    ),
    "airco": DeviceRole(
        key="airco",
        label="Airco",
        domains=("climate",),
        strong=("airco", "air_conditioner", "airconditioner", "aircon", "klimaat"),
        weak=("ac", "climate", "cool", "koel", "heat_pump", "hvac", "split"),
        exclude=("water_heater", "boiler", "floor_heating"),
        env_var="HA_AIRCO_ENTITIES",
        pairing_hint=(
            "Pair the air conditioner in Home Assistant (Tuya Local exposes it "
            "as a climate entity), then set HA_AIRCO_ENTITIES to its entity id."
        ),
    ),
    "air_purifier": DeviceRole(
        key="air_purifier",
        label="Air purifier",
        domains=("fan", "humidifier", "switch"),
        strong=("purifier", "luchtreiniger", "air_cleaner", "luchtfilter"),
        weak=("kpt", "hepa", "ionizer", "air", "filter"),
        exclude=("child_lock", "buzzer", "led", "light", "filter_reset", "indicator"),
        env_var="HA_AIR_PURIFIER_ENTITIES",
        pairing_hint=(
            "Pair the KPT air purifier in Home Assistant (Tuya Local usually "
            "exposes it as a fan entity), then set HA_AIR_PURIFIER_ENTITIES."
        ),
    ),
}

_ROLE_CONFIG: dict[str, Callable[[], list[str]]] = {
    "pet_feeder": lambda: settings.pet_feeder_entity_list,
    "airco": lambda: settings.airco_entity_list,
    "air_purifier": lambda: settings.air_purifier_entity_list,
}

_ROLE_ALIASES: dict[str, str] = {
    "feeder": "pet_feeder",
    "pet": "pet_feeder",
    "pets": "pet_feeder",
    "pet_feeder": "pet_feeder",
    "petzero": "pet_feeder",
    "cat": "pet_feeder",
    "cats": "pet_feeder",
    "airco": "airco",
    "ac": "airco",
    "aircon": "airco",
    "air_conditioner": "airco",
    "climate": "airco",
    "purifier": "air_purifier",
    "air_purifier": "air_purifier",
    "luchtreiniger": "air_purifier",
    "kpt": "air_purifier",
}

# Dispensed food cannot be recalled, so the last successful feed per entity is
# remembered in-process and a repeat inside the cooldown needs force=True.
_FEED_HISTORY: dict[str, float] = {}


def reset_feed_history() -> None:
    """Drop the anti-double-feed cooldown (tests / restarts)."""
    _FEED_HISTORY.clear()


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (value or "").lower())).strip("_")


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _reachable(state: dict[str, Any]) -> bool:
    status = str(state.get("state") or "").lower()
    # A button that has never been pressed legitimately reports "unknown";
    # only an explicit "unavailable" means Home Assistant lost the device.
    if _domain_of(str(state.get("entity_id") or "")) == "button":
        return status != "unavailable"
    return status not in _UNREACHABLE


def _friendly(row: dict[str, Any]) -> str:
    attrs = row.get("attributes") or {}
    return str(attrs.get("friendly_name") or row.get("entity_id") or "device")


def _blob(row: dict[str, Any]) -> str:
    attrs = row.get("attributes") or {}
    return _slug(
        f"{row.get('entity_id') or ''} {attrs.get('friendly_name') or ''} "
        f"{attrs.get('device_class') or ''}"
    )


def resolve_role_key(name: str) -> str:
    """Map a spoken device word onto a role key ('' when unknown)."""
    key = _slug(name)
    if key in ROLES:
        return key
    return _ROLE_ALIASES.get(key, "")


def _score_row(role: DeviceRole, row: dict[str, Any]) -> int:
    entity_id = str(row.get("entity_id") or "")
    if _domain_of(entity_id) not in role.domains:
        return 0
    blob = _blob(row)
    if any(_slug(bad) in blob for bad in role.exclude):
        return 0
    score = 0
    for word in role.strong:
        if _slug(word) in blob:
            score += 60
    for word in role.weak:
        if _slug(word) in blob:
            score += 15
    if score and any(marker in blob for marker in _TUYA_MARKERS):
        score += 5
    if score and not _reachable(row):
        score -= 5
    return score


def _describe(row: dict[str, Any], *, score: int | None = None) -> dict[str, Any]:
    attrs = row.get("attributes") or {}
    entity_id = str(row.get("entity_id") or "")
    out: dict[str, Any] = {
        "entity_id": entity_id,
        "domain": _domain_of(entity_id),
        "friendly_name": attrs.get("friendly_name"),
        "state": row.get("state"),
        "reachable": _reachable(row),
        "device_class": attrs.get("device_class"),
    }
    if score is not None:
        out["score"] = score
    return out


async def _snapshot(states: list[dict[str, Any]] | None) -> dict[str, Any]:
    if states is not None:
        return {"ok": True, "mode": "live" if ha.live else "mock", "states": states}
    return await ha.list_states()


def _pairing_error(
    role: DeviceRole,
    mode: str,
    *,
    matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    configured = role.configured()
    if matches:
        names = ", ".join(str(m.get("entity_id")) for m in matches[:6])
        message = (
            f"More than one Home Assistant entity could be the {role.label.lower()}: {names}. "
            f"Set {role.env_var} to the right one."
        )
    else:
        message = (
            f"No Home Assistant entity looks like the {role.label.lower()}. {role.pairing_hint}"
        )
    out: dict[str, Any] = {
        "ok": False,
        "role": role.key,
        "label": role.label,
        "mode": mode,
        "configured": configured,
        "env_var": role.env_var,
        "error": message,
        "speak": message,
        "hint": role.pairing_hint,
    }
    if matches:
        out["ambiguous"] = True
        out["matches"] = matches[:6]
    return out


async def resolve_role(
    role_key: str,
    *,
    hint: str = "",
    states: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Find the HA entity for a house role: config first, then discovery.

    A caller-supplied ``hint`` ("the kitchen feeder") wins over configuration so
    a second device can be addressed by name without touching ``.env``.
    """
    key = resolve_role_key(role_key) or role_key
    role = ROLES.get(key)
    if role is None:
        return {"ok": False, "error": f"unknown house device role {role_key!r}"}

    if hint.strip():
        found = await ha.resolve_entity(hint, domains=role.domains)
        if not found.get("ok"):
            return {
                **found,
                "ok": False,
                "role": role.key,
                "label": role.label,
                "speak": str(found.get("error") or f"No {role.label.lower()} matches {hint!r}."),
            }
        state = found.get("state") or {}
        entity_id = str(found.get("entity_id") or "")
        return {
            "ok": True,
            "role": role.key,
            "label": _friendly(state) if state else role.label,
            "entity_id": entity_id,
            "domain": _domain_of(entity_id),
            "state": state,
            "reachable": bool(found.get("reachable")),
            "resolved": "hint",
            "mode": found.get("mode"),
        }

    snapshot = await _snapshot(states)
    if not snapshot.get("ok"):
        error = str(snapshot.get("error") or "Home Assistant is unreachable")
        return {
            "ok": False,
            "role": role.key,
            "label": role.label,
            "mode": snapshot.get("mode"),
            "error": error,
            "speak": f"Home Assistant is unreachable: {error}",
        }
    mode = str(snapshot.get("mode") or ("live" if ha.live else "mock"))
    rows = snapshot.get("states") or []
    by_id = {str(row.get("entity_id") or "").lower(): row for row in rows}

    for candidate in role.configured():
        row = by_id.get(candidate.strip().lower())
        if row is not None:
            return {
                "ok": True,
                "role": role.key,
                "label": _friendly(row),
                "entity_id": str(row.get("entity_id")),
                "domain": _domain_of(str(row.get("entity_id"))),
                "state": row,
                "reachable": _reachable(row),
                "resolved": "config",
                "mode": mode,
            }

    scored = sorted(
        ((_score_row(role, row), row) for row in rows),
        key=lambda item: (item[0], _reachable(item[1])),
        reverse=True,
    )
    scored = [item for item in scored if item[0] > 0]
    if scored:
        top_score, top = scored[0]
        tied = [row for score, row in scored if score == top_score]
        if len(tied) > 1:
            return _pairing_error(role, mode, matches=[_describe(row) for row in tied])
        return {
            "ok": True,
            "role": role.key,
            "label": _friendly(top),
            "entity_id": str(top.get("entity_id")),
            "domain": _domain_of(str(top.get("entity_id"))),
            "state": top,
            "reachable": _reachable(top),
            "resolved": "discovery",
            "mode": mode,
        }

    # A house with exactly one climate entity has exactly one airco, whatever
    # the installer named it. Only safe for single-domain roles.
    if len(role.domains) == 1:
        only = [
            row
            for row in rows
            if _domain_of(str(row.get("entity_id") or "")) == role.domains[0]
        ]
        if len(only) == 1:
            row = only[0]
            return {
                "ok": True,
                "role": role.key,
                "label": _friendly(row),
                "entity_id": str(row.get("entity_id")),
                "domain": role.domains[0],
                "state": row,
                "reachable": _reachable(row),
                "resolved": "only_in_domain",
                "mode": mode,
            }

    return _pairing_error(role, mode)


async def discover_entities(
    kind: str = "",
    *,
    keywords: Sequence[str] | None = None,
    domain: str = "",
    limit: int = 40,
) -> dict[str, Any]:
    """List the HA entities that could be a feeder / airco / purifier.

    This is the wiring aid: it never controls anything, and it reports both what
    ``.env`` currently points at and what Home Assistant actually has, so the
    real entity ids can be copied in after pairing instead of guessed.
    """
    snapshot = await ha.list_states()
    if not snapshot.get("ok"):
        error = str(snapshot.get("error") or "Home Assistant is unreachable")
        return {
            "ok": False,
            "mode": snapshot.get("mode"),
            "error": error,
            "speak": f"Home Assistant is unreachable: {error}",
            "roles": {},
        }
    mode = str(snapshot.get("mode") or ("live" if ha.live else "mock"))
    rows = snapshot.get("states") or []
    if domain:
        prefix = domain.rstrip(".") + "."
        rows = [row for row in rows if str(row.get("entity_id") or "").startswith(prefix)]
    cap = max(1, min(int(limit or 40), 200))

    requested = _slug(kind)
    want_tuya = requested in {"tuya", "smart_life", "smartlife"}
    role_key = "" if want_tuya else resolve_role_key(kind)
    wanted_roles = [role_key] if role_key else list(ROLES)

    roles: dict[str, Any] = {}
    env_suggestions: dict[str, str] = {}
    for key in wanted_roles:
        role = ROLES[key]
        scored = sorted(
            ((_score_row(role, row), row) for row in rows),
            key=lambda item: (item[0], _reachable(item[1])),
            reverse=True,
        )
        candidates = [_describe(row, score=score) for score, row in scored if score > 0][:cap]
        configured = role.configured()
        present = [
            entity_id
            for entity_id in configured
            if any(str(row.get("entity_id") or "").lower() == entity_id.lower() for row in rows)
        ]
        resolved = await resolve_role(key, states=rows)
        suggestion = present or [str(c["entity_id"]) for c in candidates[:3]]
        if suggestion:
            env_suggestions[role.env_var] = ",".join(suggestion)
        roles[key] = {
            "label": role.label,
            "domains": list(role.domains),
            "env_var": role.env_var,
            "configured": configured,
            "configured_present": present,
            "candidates": candidates,
            "resolved_entity_id": resolved.get("entity_id") if resolved.get("ok") else None,
            "resolved_via": resolved.get("resolved") if resolved.get("ok") else None,
            "ambiguous": bool(resolved.get("ambiguous")),
            "pairing_hint": role.pairing_hint,
        }

    tuya = [
        _describe(row)
        for row in rows
        if any(marker in _blob(row) for marker in _TUYA_MARKERS)
    ][:cap]

    keyword_matches: list[dict[str, Any]] = []
    needles = [_slug(word) for word in (keywords or []) if str(word).strip()]
    if not needles and requested and not want_tuya and not role_key:
        needles = [requested]
    if needles:
        keyword_matches = [
            _describe(row)
            for row in rows
            if any(needle in _blob(row) for needle in needles)
        ][:cap]

    found = [
        f"{info['label'].lower()} → {info['resolved_entity_id']}"
        for info in roles.values()
        if info["resolved_entity_id"]
    ]
    missing = [info["label"].lower() for info in roles.values() if not info["resolved_entity_id"]]
    parts: list[str] = []
    if found:
        parts.append("Resolved " + ", ".join(found) + ".")
    if missing:
        parts.append(
            "Still unpaired or ambiguous: " + ", ".join(missing) + "."
        )
    if want_tuya:
        parts.append(f"{len(tuya)} entity(ies) look like Tuya or Smart Life hardware.")
    if not parts:
        parts.append("No feeder, airco, or purifier entities found in Home Assistant yet.")

    return {
        "ok": True,
        "mode": mode,
        "total_entities": len(rows),
        "roles": roles,
        "tuya": tuya,
        "keyword_matches": keyword_matches,
        "env_suggestions": env_suggestions,
        "speak": " ".join(parts),
    }


def _expect_state(*values: str) -> Callable[[dict[str, Any]], bool]:
    wanted = {value.lower() for value in values}
    return lambda state: str(state.get("state") or "").lower() in wanted


def _expect_on() -> Callable[[dict[str, Any]], bool]:
    return lambda state: str(state.get("state") or "").lower() not in (_UNREACHABLE | {"off"})


def _expect_attr(
    name: str,
    value: Any,
    *,
    tolerance: float = 0.0,
) -> Callable[[dict[str, Any]], bool]:
    def check(state: dict[str, Any]) -> bool:
        actual = (state.get("attributes") or {}).get(name)
        if actual is None:
            return False
        if tolerance and isinstance(value, (int, float)):
            try:
                return abs(float(actual) - float(value)) <= tolerance
            except (TypeError, ValueError):
                return False
        return _slug(str(actual)) == _slug(str(value))

    return check


def _refuse(role: str, entity_id: str, message: str) -> dict[str, Any]:
    """A tool-shaped 'no' that speaks for itself."""
    return {
        "ok": False,
        "role": role,
        "entity_id": entity_id,
        "error": message,
        "speak": message,
    }


def _attr(state: dict[str, Any] | None, name: str, default: Any = None) -> Any:
    if not state:
        return default
    value = (state.get("attributes") or {}).get(name)
    return default if value is None else value


def _match_option(requested: str, options: Iterable[Any]) -> str:
    """Pick the entity's own spelling for a requested mode / speed."""
    available = [str(option) for option in options or []]
    if not available:
        return requested
    needle = _slug(requested)
    for option in available:
        if _slug(option) == needle:
            return option
    canonical = ""
    for name, aliases in _HVAC_ALIASES.items():
        if needle == name or needle in {_slug(alias) for alias in aliases}:
            canonical = name
            break
    if canonical:
        for option in available:
            if _slug(option) == canonical:
                return option
        for option in available:
            if _slug(option) in {_slug(alias) for alias in _HVAC_ALIASES[canonical]}:
                return option
    for option in available:
        if needle and (needle in _slug(option) or _slug(option) in needle):
            return option
    return ""


# ---------------------------------------------------------------------------
# Pet feeders
# ---------------------------------------------------------------------------


async def feed_pets(
    *,
    portions: int | None = None,
    feeder: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Dispense a meal now on the PetZero feeder.

    Feeding is one-way, and voice plus Telegram make an accidental second meal
    easy, so a repeat inside ``HA_PET_FEEDER_COOLDOWN_SECONDS`` is refused with
    the remaining wait unless ``force`` is set.
    """
    resolved = await resolve_role("pet_feeder", hint=feeder)
    if not resolved.get("ok"):
        return resolved
    entity_id = str(resolved["entity_id"])
    domain = str(resolved["domain"])
    label = str(resolved.get("label") or "Pet feeder")
    mode = resolved.get("mode")

    if not resolved.get("reachable"):
        message = f"{label} is unavailable in Home Assistant — nothing was dispensed."
        return {
            "ok": False,
            "role": "pet_feeder",
            "entity_id": entity_id,
            "mode": mode,
            "error": message,
            "speak": message,
        }

    wanted = settings.ha_pet_feeder_default_portions if portions is None else int(portions)
    cap = max(1, int(settings.ha_pet_feeder_max_portions))
    count = max(1, min(cap, wanted))
    capped = count != wanted

    cooldown = max(0.0, float(settings.ha_pet_feeder_cooldown_seconds))
    last = _FEED_HISTORY.get(entity_id)
    if not force and cooldown and last is not None:
        waited = time.monotonic() - last
        if waited < cooldown:
            remaining = int(round(cooldown - waited))
            minutes = max(1, round(remaining / 60))
            message = (
                f"{label} already dispensed a portion just now. Ask again in about "
                f"{minutes} minute(s), or say feed them anyway for a second one."
            )
            return {
                "ok": False,
                "role": "pet_feeder",
                "entity_id": entity_id,
                "mode": mode,
                "cooldown_active": True,
                "cooldown_remaining_s": remaining,
                "error": message,
                "speak": message,
            }

    steps: list[dict[str, Any]] = []
    portion_entity = await _resolve_first(settings.pet_feeder_portion_entity_list)
    portion_via = ""
    presses = count
    if count > 1 and portion_entity:
        # A number entity means the feeder dispenses N portions per trigger.
        step = await ha.call_and_verify(
            _domain_of(portion_entity),
            "set_value",
            portion_entity,
            {"value": float(count)},
            expect=_expect_state(str(count), f"{float(count):.1f}"),
        )
        steps.append(step)
        if step.get("accepted"):
            portion_via = portion_entity
            presses = 1

    if domain == "button":
        service, data, expect = "press", None, None
    elif domain == "switch":
        service, data, expect = "turn_on", None, _expect_state("on")
    else:
        message = f"{label} is a {domain} entity; Hearth can only trigger button or switch feeders."
        return {
            "ok": False,
            "role": "pet_feeder",
            "entity_id": entity_id,
            "mode": mode,
            "error": message,
            "speak": message,
        }

    for _ in range(presses):
        steps.append(
            await ha.call_and_verify(domain, service, entity_id, data, expect=expect)
        )

    failed = [step for step in steps if step.get("ok") is False]
    ok = not failed
    if ok:
        _FEED_HISTORY[entity_id] = time.monotonic()

    meal = "one portion" if count == 1 else f"{count} portions"
    if ok:
        speak = f"Fed the pets — {meal} via {label}."
        if capped:
            speak += f" Capped at {cap} portions."
    else:
        speak = str(failed[0].get("error") or f"{label} did not confirm the feed.")

    return {
        "ok": ok,
        "role": "pet_feeder",
        "entity_id": entity_id,
        "label": label,
        "mode": mode,
        "resolved": resolved.get("resolved"),
        "portions": count,
        "portions_capped": capped,
        "presses": presses,
        "portion_entity_id": portion_via or None,
        "steps": steps,
        "forced": bool(force),
        "speak": speak,
        "error": None if ok else speak,
    }


async def _resolve_first(candidates: Sequence[str]) -> str:
    """First configured helper entity that actually exists in HA."""
    for entity_id in candidates:
        probe = await ha.get_state(entity_id)
        if probe.get("ok") and probe.get("state") is not None:
            return entity_id
    return ""


async def feeder_schedule(action: str = "status", *, feeder: str = "") -> dict[str, Any]:
    """Read or flip the feeder's built-in schedule when HA exposes one.

    Most Tuya feeders keep their timetable on the device and only surface an
    on/off switch for it. Hearth reports that honestly rather than pretending it
    can write meal times it cannot see.
    """
    wanted = _slug(action) or "status"
    schedule_entity = await _resolve_first(settings.pet_feeder_schedule_entity_list)
    if not schedule_entity:
        message = (
            "This feeder does not expose its schedule to Home Assistant — only "
            "manual feeding is available. Set the timetable in the feeder's own "
            "integration, or point HA_PET_FEEDER_SCHEDULE_ENTITIES at the "
            "schedule switch if one exists."
        )
        return {
            "ok": False,
            "role": "pet_feeder",
            "supported": False,
            "action": wanted,
            "error": message,
            "speak": message,
            "hint": ROLES["pet_feeder"].pairing_hint,
        }

    snapshot = await ha.get_state(schedule_entity)
    state = snapshot.get("state") or {}
    label = _friendly(state) if state else "Feeder schedule"
    if wanted in {"status", "state", "read", "get"}:
        status = str(state.get("state") or "unknown")
        return {
            "ok": True,
            "role": "pet_feeder",
            "supported": True,
            "action": "status",
            "entity_id": schedule_entity,
            "mode": snapshot.get("mode"),
            "state": state,
            "enabled": status == "on",
            "speak": f"{label} is {status}.",
        }

    if wanted in {"on", "enable", "enabled", "turn_on", "start", "resume"}:
        service, expect, spoken = "turn_on", _expect_state("on"), "on"
    elif wanted in {"off", "disable", "disabled", "turn_off", "stop", "pause"}:
        service, expect, spoken = "turn_off", _expect_state("off"), "off"
    else:
        message = f"Unknown schedule action {action!r}; use status, on, or off."
        return {"ok": False, "role": "pet_feeder", "error": message, "speak": message}

    result = await ha.call_and_verify(
        _domain_of(schedule_entity), service, schedule_entity, expect=expect
    )
    ok = bool(result.get("ok"))
    speak = (
        f"Scheduled feeding is {spoken}."
        if ok
        else str(result.get("error") or "The feeder schedule did not change.")
    )
    return {
        "ok": ok,
        "role": "pet_feeder",
        "supported": True,
        "action": spoken,
        "entity_id": schedule_entity,
        "mode": result.get("mode"),
        "state": result.get("state"),
        "verified": result.get("verified"),
        "speak": speak,
        "error": None if ok else speak,
    }


# ---------------------------------------------------------------------------
# Airco (Tuya climate)
# ---------------------------------------------------------------------------


def _clamp_temperature(value: float, state: dict[str, Any] | None) -> tuple[float, bool]:
    low = float(settings.ha_airco_min_temperature)
    high = float(settings.ha_airco_max_temperature)
    entity_low = _attr(state, "min_temp")
    entity_high = _attr(state, "max_temp")
    try:
        if entity_low is not None:
            low = max(low, float(entity_low))
        if entity_high is not None:
            high = min(high, float(entity_high))
    except (TypeError, ValueError):
        pass
    if low > high:
        low, high = high, low
    clamped = max(low, min(high, float(value)))
    return clamped, clamped != float(value)


def _speak_climate(label: str, state: dict[str, Any] | None) -> str:
    if not state:
        return f"{label} state is unknown."
    status = str(state.get("state") or "unknown")
    if status == "off":
        return f"{label} is off."
    bits = [f"{label} is {status}"]
    target = _attr(state, "temperature")
    if target is not None:
        bits.append(f"set to {_number(target)}°")
    current = _attr(state, "current_temperature")
    if current is not None:
        bits.append(f"room {_number(current)}°")
    fan = _attr(state, "fan_mode")
    if fan:
        bits.append(f"fan {fan}")
    return ", ".join(bits) + "."


def _number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


async def climate_control(
    action: str = "status",
    *,
    temperature: float | None = None,
    mode: str = "",
    fan_mode: str = "",
    target: str = "",
) -> dict[str, Any]:
    """Control the airco: power, target temperature, hvac mode, fan speed.

    Setting a temperature on a unit that is off also starts it — "airco 21"
    means make it 21 in here, not arm a target for later.
    """
    resolved = await resolve_role("airco", hint=target)
    if not resolved.get("ok"):
        return resolved
    entity_id = str(resolved["entity_id"])
    label = str(resolved.get("label") or "Airco")
    state = resolved.get("state") or {}
    mode_name = resolved.get("mode")
    wanted = _slug(action) or "status"

    if wanted in {"status", "state", "read", "get"}:
        return {
            "ok": True,
            "role": "airco",
            "action": "status",
            "entity_id": entity_id,
            "label": label,
            "mode": mode_name,
            "state": state,
            "speak": _speak_climate(label, state),
        }

    if temperature is not None and wanted in {"on", "turn_on"}:
        wanted = "set_temperature"
    if wanted in {"temperature", "set_temperature", "temp", "set_temp"} and temperature is None:
        return _refuse("airco", entity_id, "Give me a temperature, for example airco 21.")

    steps: list[dict[str, Any]] = []
    # Set by the power-on step when it already put the unit in the asked-for mode,
    # so "airco 21 on heat" does not send set_hvac_mode twice.
    powered_mode = ""
    if wanted in {"off", "turn_off", "stop"}:
        result = await ha.call_and_verify(
            "climate",
            "set_hvac_mode",
            entity_id,
            {"hvac_mode": "off"},
            expect=_expect_state("off"),
        )
        steps.append(result)
        return _climate_result(label, entity_id, mode_name, "turn_off", steps, f"{label} is off.")

    if wanted in {"on", "turn_on"} or (
        wanted in {"set_temperature", "set_fan_mode", "fan_mode"}
        and str(state.get("state") or "").lower() == "off"
    ):
        power = await _airco_power_on(entity_id, state, requested_mode=mode)
        steps.append(power)
        if mode and power.get("ok"):
            powered_mode = str(power.get("applied_mode") or "")
        if not power.get("ok"):
            return _climate_result(
                label,
                entity_id,
                mode_name,
                "turn_on",
                steps,
                str(power.get("error") or f"{label} did not turn on."),
            )
        if wanted in {"on", "turn_on"} and temperature is None and not fan_mode:
            after = power.get("state") or {}
            return _climate_result(
                label, entity_id, mode_name, "turn_on", steps, _speak_climate(label, after)
            )

    wants_mode = wanted in {"mode", "set_mode", "hvac_mode", "set_hvac_mode"}
    if wants_mode or (mode and not powered_mode):
        requested = mode or action
        modes = _attr(state, "hvac_modes", []) or []
        chosen = _match_option(requested, modes)
        if not chosen:
            available = ", ".join(str(m) for m in modes) or "none reported"
            return _refuse(
                "airco",
                entity_id,
                f"{label} has no {requested!r} mode. Available: {available}.",
            )
        result = await ha.call_and_verify(
            "climate",
            "set_hvac_mode",
            entity_id,
            {"hvac_mode": chosen},
            expect=_expect_state(chosen),
        )
        steps.append(result)
        if wanted != "set_temperature" and temperature is None and not fan_mode:
            return _climate_result(
                label,
                entity_id,
                mode_name,
                "set_mode",
                steps,
                _speak_climate(label, result.get("state")),
            )

    if temperature is not None:
        clamped, adjusted = _clamp_temperature(float(temperature), state)
        result = await ha.call_and_verify(
            "climate",
            "set_temperature",
            entity_id,
            {"temperature": clamped},
            expect=_expect_attr("temperature", clamped, tolerance=0.51),
        )
        steps.append(result)
        speak = f"{label} set to {_number(clamped)} degrees."
        if adjusted:
            speak += f" {_number(temperature)} is outside its range."
        return _climate_result(
            label, entity_id, mode_name, "set_temperature", steps, speak, temperature=clamped
        )

    if fan_mode:
        fan_modes = _attr(state, "fan_modes", []) or []
        chosen = _match_option(fan_mode, fan_modes)
        if not chosen:
            available = ", ".join(str(m) for m in fan_modes) or "none reported"
            return _refuse(
                "airco",
                entity_id,
                f"{label} has no {fan_mode!r} fan speed. Available: {available}.",
            )
        result = await ha.call_and_verify(
            "climate",
            "set_fan_mode",
            entity_id,
            {"fan_mode": chosen},
            expect=_expect_attr("fan_mode", chosen),
        )
        steps.append(result)
        return _climate_result(
            label, entity_id, mode_name, "set_fan_mode", steps, f"{label} fan set to {chosen}."
        )

    if steps:
        return _climate_result(
            label,
            entity_id,
            mode_name,
            wanted,
            steps,
            _speak_climate(label, steps[-1].get("state")),
        )
    return _refuse(
        "airco",
        entity_id,
        f"Unknown airco action {action!r}; use status, on, off, set_temperature, "
        "set_mode, or set_fan_mode.",
    )


async def _airco_power_on(
    entity_id: str,
    state: dict[str, Any],
    *,
    requested_mode: str = "",
) -> dict[str, Any]:
    """Start the unit in a real running mode, never an ambiguous 'on'."""
    modes = [str(m) for m in (_attr(state, "hvac_modes", []) or [])]
    chosen = ""
    if requested_mode:
        chosen = _match_option(requested_mode, modes)
    if not chosen:
        preference = (settings.ha_airco_default_mode or "").strip()
        if preference:
            chosen = _match_option(preference, modes)
    if not chosen:
        for candidate in _HVAC_ON_PREFERENCE:
            match = _match_option(candidate, modes)
            if match and _slug(match) != "off":
                chosen = match
                break
    if not chosen:
        # No mode list published — fall back to the plain climate.turn_on service.
        return await ha.call_and_verify("climate", "turn_on", entity_id, expect=_expect_on())
    result = await ha.call_and_verify(
        "climate",
        "set_hvac_mode",
        entity_id,
        {"hvac_mode": chosen},
        expect=_expect_state(chosen),
    )
    return {**result, "applied_mode": chosen}


def _climate_result(
    label: str,
    entity_id: str,
    mode: Any,
    action: str,
    steps: list[dict[str, Any]],
    speak: str,
    *,
    temperature: float | None = None,
) -> dict[str, Any]:
    failed = [step for step in steps if step.get("ok") is False]
    ok = not failed
    if not ok:
        speak = str(failed[0].get("error") or speak)
    out: dict[str, Any] = {
        "ok": ok,
        "role": "airco",
        "action": action,
        "entity_id": entity_id,
        "label": label,
        "mode": mode,
        "steps": steps,
        "state": steps[-1].get("state") if steps else None,
        "verified": steps[-1].get("verified") if steps else None,
        "speak": speak,
        "error": None if ok else speak,
    }
    if temperature is not None:
        out["temperature"] = temperature
    return out


# ---------------------------------------------------------------------------
# Air purifier (Tuya fan / humidifier / switch)
# ---------------------------------------------------------------------------


def _speak_purifier(label: str, state: dict[str, Any] | None) -> str:
    if not state:
        return f"{label} state is unknown."
    status = str(state.get("state") or "unknown")
    if status == "off":
        return f"{label} is off."
    bits = [f"{label} is {status}"]
    percentage = _attr(state, "percentage")
    if percentage is not None:
        bits.append(f"{_number(percentage)}% speed")
    preset = _attr(state, "preset_mode")
    if preset:
        bits.append(f"{preset} mode")
    pm25 = _attr(state, "pm25")
    if pm25 is not None:
        bits.append(f"PM2.5 {_number(pm25)}")
    filter_life = _attr(state, "filter_life_remaining")
    if filter_life is not None:
        bits.append(f"filter {_number(filter_life)}%")
    return ", ".join(bits) + "."


async def purifier_control(
    action: str = "status",
    *,
    percentage: float | None = None,
    preset_mode: str = "",
    target: str = "",
) -> dict[str, Any]:
    """Control the KPT air purifier: power, fan speed, preset mode.

    Tuya ships this device as a ``fan`` under Tuya Local but as a ``humidifier``
    or plain ``switch`` in some OEM builds, so speed and preset are only offered
    when the resolved entity really supports them.
    """
    resolved = await resolve_role("air_purifier", hint=target)
    if not resolved.get("ok"):
        return resolved
    entity_id = str(resolved["entity_id"])
    domain = str(resolved["domain"])
    label = str(resolved.get("label") or "Air purifier")
    state = resolved.get("state") or {}
    mode_name = resolved.get("mode")
    wanted = _slug(action) or "status"

    if wanted in {"status", "state", "read", "get"}:
        return {
            "ok": True,
            "role": "air_purifier",
            "action": "status",
            "entity_id": entity_id,
            "label": label,
            "mode": mode_name,
            "state": state,
            "speak": _speak_purifier(label, state),
        }

    if percentage is not None and wanted in {"on", "turn_on"}:
        wanted = "set_speed"
    if preset_mode and wanted in {"on", "turn_on"}:
        wanted = "set_mode"

    if wanted in {"on", "turn_on", "off", "turn_off", "toggle"}:
        service = "toggle" if wanted == "toggle" else (
            "turn_on" if wanted in {"on", "turn_on"} else "turn_off"
        )
        expect = (
            _expect_state("off")
            if service == "turn_off"
            else (_expect_on() if service == "turn_on" else None)
        )
        result = await ha.call_and_verify(domain, service, entity_id, expect=expect)
        speak = (
            _speak_purifier(label, result.get("state"))
            if result.get("ok")
            else str(result.get("error") or f"{label} did not respond.")
        )
        return _purifier_result(label, entity_id, mode_name, service, result, speak)

    if wanted in {"speed", "set_speed", "percentage", "set_percentage"}:
        if percentage is None:
            return _refuse(
                "air_purifier",
                entity_id,
                "Give me a speed percentage, for example purifier 40%.",
            )
        if domain != "fan":
            return _refuse(
                "air_purifier",
                entity_id,
                f"{label} is a {domain} entity in Home Assistant, so it only does on "
                "and off. Re-pair it with Tuya Local to get fan speeds.",
            )
        value = max(0.0, min(100.0, float(percentage)))
        result = await ha.call_and_verify(
            "fan",
            "set_percentage",
            entity_id,
            {"percentage": value},
            expect=_expect_attr("percentage", value, tolerance=1.0),
        )
        speak = (
            f"{label} at {_number(value)}%."
            if result.get("ok")
            else str(result.get("error") or f"{label} did not change speed.")
        )
        return _purifier_result(label, entity_id, mode_name, "set_percentage", result, speak)

    if wanted in {"mode", "set_mode", "preset", "preset_mode", "set_preset_mode"}:
        if not preset_mode:
            return _refuse(
                "air_purifier",
                entity_id,
                "Which mode? For example auto, sleep, or turbo.",
            )
        available = _attr(state, "preset_modes", []) or []
        chosen = _match_option(preset_mode, available)
        if not chosen:
            names = ", ".join(str(m) for m in available) or "none reported"
            return _refuse(
                "air_purifier",
                entity_id,
                f"{label} has no {preset_mode!r} mode. Available: {names}.",
            )
        result = await ha.call_and_verify(
            domain,
            "set_preset_mode",
            entity_id,
            {"preset_mode": chosen},
            expect=_expect_attr("preset_mode", chosen),
        )
        speak = (
            f"{label} in {chosen} mode."
            if result.get("ok")
            else str(result.get("error") or f"{label} did not change mode.")
        )
        return _purifier_result(label, entity_id, mode_name, "set_preset_mode", result, speak)

    return _refuse(
        "air_purifier",
        entity_id,
        f"Unknown purifier action {action!r}; use status, on, off, set_speed, or set_mode.",
    )


def _purifier_result(
    label: str,
    entity_id: str,
    mode: Any,
    action: str,
    result: dict[str, Any],
    speak: str,
) -> dict[str, Any]:
    ok = bool(result.get("ok"))
    return {
        "ok": ok,
        "role": "air_purifier",
        "action": action,
        "entity_id": entity_id,
        "label": label,
        "mode": mode,
        "state": result.get("state"),
        "verified": result.get("verified"),
        "result": result,
        "speak": speak,
        "error": None if ok else speak,
    }


# ---------------------------------------------------------------------------
# Combined snapshot
# ---------------------------------------------------------------------------


async def house_devices_status() -> dict[str, Any]:
    """One speakable snapshot of feeder, airco, and purifier from one HA read."""
    snapshot = await ha.list_states()
    if not snapshot.get("ok"):
        error = str(snapshot.get("error") or "Home Assistant is unreachable")
        return {
            "ok": False,
            "mode": snapshot.get("mode"),
            "error": error,
            "speak": f"Home Assistant is unreachable: {error}",
            "devices": {},
        }
    rows = snapshot.get("states") or []
    mode = str(snapshot.get("mode") or ("live" if ha.live else "mock"))

    devices: dict[str, Any] = {}
    spoken: list[str] = []
    for key, role in ROLES.items():
        resolved = await resolve_role(key, states=rows)
        if not resolved.get("ok"):
            devices[key] = {
                "label": role.label,
                "ok": False,
                "configured": role.configured(),
                "env_var": role.env_var,
                "ambiguous": bool(resolved.get("ambiguous")),
                "matches": resolved.get("matches"),
                "error": resolved.get("error"),
                "speak": resolved.get("speak"),
            }
            spoken.append(f"{role.label} is not paired yet.")
            continue
        state = resolved.get("state") or {}
        if key == "airco":
            speak = _speak_climate(str(resolved.get("label") or role.label), state)
        elif key == "air_purifier":
            speak = _speak_purifier(str(resolved.get("label") or role.label), state)
        else:
            speak = f"{resolved.get('label') or role.label} is {state.get('state') or 'unknown'}."
        devices[key] = {
            "label": resolved.get("label") or role.label,
            "ok": True,
            "entity_id": resolved.get("entity_id"),
            "domain": resolved.get("domain"),
            "resolved": resolved.get("resolved"),
            "reachable": resolved.get("reachable"),
            "state": state,
            "speak": speak,
        }
        spoken.append(speak)

    return {
        "ok": True,
        "mode": mode,
        "devices": devices,
        "speak": " ".join(spoken),
    }
