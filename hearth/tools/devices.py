"""Finding the house devices in Home Assistant: feeder, airco, air purifier.

``hearth.tools.house`` controls these devices. This module answers the question
that comes first and, on a half-built house, comes up constantly: *which Home
Assistant entity is the thing you are talking about — and is it even there yet?*

Tuya object ids depend on how a device was paired, so nothing here hardcodes
one. Each role owns an ordered candidate list from ``.env`` and falls back to
keyword discovery over live HA state. When several entities fit, the answer is
the list of candidates, never a guess: switching the wrong Tuya relay is worse
than asking.

The discovery report also separates the failure modes that look identical from
the outside and need completely different fixes:

* Home Assistant is unreachable.
* ``tuya_local`` is not loaded — the files can be on disk via HACS, but no
  device has been added, so HA has no entities to find.
* The integration is loaded but this particular role is still unpaired.
* Several entities match and Hearth needs to be told which one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable

from hearth.config import settings
from hearth.tools.ha import ha
from hearth.tools.tuya_lan import probe_tuya_lan

# Custom integrations that actually adopt this hardware. Presence in HA's loaded
# component list means at least one config entry exists.
TUYA_INTEGRATIONS = ("tuya_local", "tuya", "localtuya", "smartlife")

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
    # The single-id setting to pin once discovery has found the real entity.
    pin_var: str = ""
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
        pin_var="HA_FEEDER_ENTITY",
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
        pin_var="HA_CLIMATE_ENTITY",
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
        pin_var="HA_PURIFIER_ENTITY",
        pairing_hint=(
            "Pair the KPT air purifier in Home Assistant (Tuya Local usually "
            "exposes it as a fan entity), then set HA_AIR_PURIFIER_ENTITIES."
        ),
    ),
}

def _candidates(pinned: str, fallback: list[str]) -> list[str]:
    """A pinned HA_*_ENTITY wins; the candidate list is the search order after it."""
    chosen = [entity_id for entity_id in [pinned.strip()] if entity_id]
    for entity_id in fallback:
        if entity_id not in chosen:
            chosen.append(entity_id)
    return chosen


_ROLE_CONFIG: dict[str, Callable[[], list[str]]] = {
    "pet_feeder": lambda: _candidates(
        settings.ha_feeder_entity, settings.pet_feeder_entity_list
    ),
    "airco": lambda: _candidates(settings.ha_climate_entity, settings.airco_entity_list),
    "air_purifier": lambda: _candidates(
        settings.ha_purifier_entity, settings.air_purifier_entity_list
    ),
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
    check_lan: bool | None = None,
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
        if resolved.get("ok"):
            env_suggestions[role.pin_var] = str(resolved["entity_id"])
        else:
            suggestion = present or [str(c["entity_id"]) for c in candidates[:3]]
            if suggestion:
                env_suggestions[role.env_var] = ",".join(suggestion)
        in_domains = [
            row
            for row in rows
            if _domain_of(str(row.get("entity_id") or "")) in role.domains
        ]
        if resolved.get("ok"):
            status, next_step = "ready", ""
        elif resolved.get("ambiguous"):
            status = "ambiguous"
            next_step = (
                f"Set {role.pin_var} to whichever of these is the "
                f"{role.label.lower()}."
            )
        elif not in_domains:
            # Nothing of the right kind exists at all — the device has not been
            # added to Home Assistant, whatever is installed on disk.
            status = "no_entities_in_domain"
            next_step = (
                f"Home Assistant has no {' / '.join(role.domains)} entity at all. "
                f"{role.pairing_hint}"
            )
        else:
            status = "not_paired"
            next_step = (
                f"Home Assistant has {len(in_domains)} "
                f"{' / '.join(role.domains)} entity(ies), but none look like the "
                f"{role.label.lower()}. Set {role.pin_var} if one of them is."
            )
        roles[key] = {
            "label": role.label,
            "status": status,
            "domains": list(role.domains),
            "entities_in_domains": len(in_domains),
            "env_var": role.env_var,
            "configured": configured,
            "configured_present": present,
            "candidates": candidates,
            "resolved_entity_id": resolved.get("entity_id") if resolved.get("ok") else None,
            "resolved_via": resolved.get("resolved") if resolved.get("ok") else None,
            "ambiguous": bool(resolved.get("ambiguous")),
            "next_step": next_step,
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

    integration = await _integration_status()
    found = [
        f"{info['label'].lower()} → {info['resolved_entity_id']}"
        for info in roles.values()
        if info["resolved_entity_id"]
    ]
    unresolved = [info for info in roles.values() if not info["resolved_entity_id"]]

    # Probing the LAN only earns its cost when Home Assistant came up short:
    # it is what turns "nothing found" into "the hardware is there, unpaired".
    want_lan = bool(unresolved) if check_lan is None else bool(check_lan)
    lan = await probe_tuya_lan() if want_lan else None

    parts: list[str] = []
    if found:
        parts.append("Resolved " + ", ".join(found) + ".")
    if unresolved:
        names = ", ".join(info["label"].lower() for info in unresolved)
        ambiguous = [info for info in unresolved if info["status"] == "ambiguous"]
        if ambiguous:
            parts.append(f"More than one entity could be the {names} — tell me which.")
        elif integration["adopted"]:
            parts.append(f"Not on Home Assistant yet: {names}.")
        else:
            # The decisive case on a half-built house: the integration exists but
            # has adopted nothing, so there is nothing for keywords to match.
            parts.append(
                f"Not on Home Assistant yet: {names}. {integration['speak']}"
            )
        if lan is not None and lan.get("open_count"):
            parts.append(
                f"{lan['open_count']} Tuya device(s) do answer on the LAN "
                f"(port {lan['port']}), so the hardware is there and waiting to "
                "be added — it is a pairing step, not a broken device."
            )
        elif lan is not None and lan.get("configured"):
            parts.append(lan["speak"])
    if want_tuya:
        parts.append(f"{len(tuya)} entity(ies) look like Tuya or Smart Life hardware.")
    if not parts:
        parts.append("No feeder, airco, or purifier entities found in Home Assistant yet.")

    return {
        "ok": True,
        "mode": mode,
        "total_entities": len(rows),
        "domains": _domain_counts(rows),
        "integration": integration,
        "lan": lan,
        "roles": roles,
        "tuya": tuya,
        "keyword_matches": keyword_matches,
        "env_suggestions": env_suggestions,
        "next_steps": [
            info["next_step"] for info in roles.values() if info.get("next_step")
        ],
        "speak": " ".join(parts),
    }


def _domain_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        domain = _domain_of(str(row.get("entity_id") or ""))
        if domain:
            counts[domain] = counts.get(domain, 0) + 1
    return dict(sorted(counts.items()))


async def _integration_status() -> dict[str, Any]:
    """Has a Tuya integration actually adopted anything in Home Assistant?

    Home Assistant only loads a custom integration once it has a config entry,
    so "not loaded" is the signal that the files are installed (HACS) but no
    device has been added. That is a completely different fix to "the
    integration is missing", and the two are indistinguishable from entities
    alone — both simply produce nothing to match.
    """
    config = await ha.loaded_integrations()
    if not config.get("known"):
        return {
            "known": False,
            "adopted": False,
            "loaded": [],
            "speak": (
                "I cannot read the Home Assistant integration list, so I cannot "
                "tell whether Tuya has adopted anything."
            ),
        }
    components = set(config.get("components") or [])
    loaded = [name for name in TUYA_INTEGRATIONS if name in components]
    if loaded:
        return {
            "known": True,
            "adopted": True,
            "loaded": loaded,
            "ha_version": config.get("version"),
            "speak": (
                f"{', '.join(loaded)} is loaded in Home Assistant, so the devices "
                "it adopted should appear above."
            ),
        }
    return {
        "known": True,
        "adopted": False,
        "loaded": [],
        "ha_version": config.get("version"),
        "speak": (
            "No Tuya integration is loaded in Home Assistant. Installing "
            "tuya_local through HACS only copies the files — each device still "
            "has to be added under Settings, Devices & Services, Add Integration, "
            "Tuya Local. See docs/devices.md for the exact steps."
        ),
    }


