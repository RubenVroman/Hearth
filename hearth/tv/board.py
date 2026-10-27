"""Snapshot and button actions for the living-room TV shell.

Every control delegates to the same tools as chat and the phone command
center (scenes, receiver activities, ``ha_media_control``, Videoland, Infuse).
The TV app does not invent a second Home Assistant client.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from hearth.agent.registry import registry
from hearth.butler.shelf import house_pulse
from hearth.config import settings
from hearth.jev import tool_turn
from hearth.tools.ha import ha
from hearth.tools.media import house_media_inventory
from hearth.tools.videoland import match_videoland_source

_SECRET_RE = re.compile(
    r"(?i)(bearer\s+\S+|x-hearth-token\S*|x-auth-token\S*|token=[^&\s]+|api[_-]?key[=:]\S+)"
)

# This shell is built for the living-room set. It is a label, not a probe.
TARGET_TV = "OLED65G1RLA"

SCENES = ("movie_night", "quiet_hours", "good_night")
ACTIVITIES = {
    "movie_night": {
        "label": "Movie night",
        "hint": "Lights and Apple TV",
        "said": "movie night",
    },
    "apple_tv": {
        "label": "Apple TV",
        "hint": "Denon Media Player",
        "said": "switch to Apple TV",
    },
    "tv": {
        "label": "TV Audio",
        "hint": "Denon TV Audio",
        "said": "switch the receiver to TV audio",
    },
    "off": {
        "label": "Media off",
        "hint": "Apple TV, LG, Denon",
        "said": "turn the media off",
    },
}
TRANSPORT = {
    "pause": "pause",
    "play": "play",
    "stop": "stop",
}

# Friendly webOS source names. Title playback inside these apps is not an HA API.
_SOURCE_LAUNCHES: dict[str, dict[str, Any]] = {
    "netflix": {
        "label": "Netflix",
        "hint": "LG app",
        "candidates": ("Netflix",),
        "pairing": (
            "Needs the LG webOS integration paired in Home Assistant "
            "(HA_TV_ENTITY). Hearth calls media_player.select_source with the "
            "friendly name from the TV source_list. It cannot start a title "
            "inside Netflix, and it does not modify the app."
        ),
    },
    "prime": {
        "label": "Prime Video",
        "hint": "LG app",
        "candidates": ("Amazon Prime Video", "Prime Video", "Amazon"),
        "pairing": (
            "Same HA webOS pairing as Netflix. The source name on this TV is "
            "usually Amazon Prime Video, Prime Video, or Amazon — Hearth uses "
            "whichever is actually on the source_list. No title deep link."
        ),
    },
}


class TvActionError(ValueError):
    """The TV sent a button id this shell does not know."""


def _public_text(value: Any) -> str:
    return _SECRET_RE.sub("[redacted]", str(value or ""))


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (value or "").lower())).strip("_")


def _attrs(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    attributes = state.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _source_list(state: dict[str, Any] | None) -> list[str]:
    raw = _attrs(state).get("source_list") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def _volume_pct(state: dict[str, Any] | None) -> int | None:
    level = _attrs(state).get("volume_level")
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    return max(0, min(100, round(float(level) * 100)))


def _player_card(block: dict[str, Any] | None, *, role: str) -> dict[str, Any]:
    block = block or {}
    state = block.get("state") if isinstance(block.get("state"), dict) else None
    attrs = _attrs(state)
    name = str(attrs.get("friendly_name") or block.get("entity_id") or role)
    return {
        "ok": bool(block.get("ok")),
        "role": role,
        "entity_id": block.get("entity_id"),
        "name": name,
        "state": None if state is None else state.get("state"),
        "source": attrs.get("source") or attrs.get("app_name") or None,
        "media_title": attrs.get("media_title") or None,
        "volume_pct": _volume_pct(state),
        "muted": bool(attrs.get("is_volume_muted")),
        "speak": _public_text(block.get("speak") or ""),
        "error": _public_text(block.get("error") or "") or None,
    }


def _progress_pct(offset: Any, duration: Any) -> int | None:
    try:
        total = int(duration or 0)
        done = int(offset or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0 or done <= 0:
        return None
    return max(1, min(99, round(100 * done / total)))


def _now_card(sessions: list[Any]) -> dict[str, Any] | None:
    session = next((row for row in sessions if isinstance(row, dict)), None)
    if session is None:
        return None
    title = str(session.get("title") or "Untitled")
    show = session.get("show") or None
    summary = str(session.get("summary") or "").strip()
    return {
        "title": title,
        "show": show,
        "type": session.get("type"),
        "year": session.get("year"),
        "state": session.get("state"),
        "player": session.get("player"),
        "progress_pct": _progress_pct(session.get("progress_ms"), session.get("duration_ms")),
        "summary": summary[:180],
        "ratingKey": session.get("ratingKey"),
    }


def _continue_card(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": str(item.get("label") or item.get("title") or "Untitled"),
        "title": item.get("title"),
        "show": item.get("show") or item.get("grandparentTitle"),
        "type": item.get("type"),
        "year": item.get("year"),
        "ratingKey": item.get("ratingKey"),
        "progress_pct": item.get("progress_pct"),
        "season": item.get("season"),
        "episode": item.get("episode"),
    }


def _match_named_source(source_list: list[str], candidates: tuple[str, ...]) -> str | None:
    wanted = [_slug(name) for name in candidates if name]
    for source in source_list:
        if _slug(source) in wanted:
            return source
    for source in source_list:
        slug = _slug(source)
        if any(name and name in slug for name in wanted):
            return source
    return None


def _source_launch(launch_id: str, spec: dict[str, Any], sources: list[str]) -> dict[str, Any]:
    matched = _match_named_source(sources, tuple(spec["candidates"]))
    if matched:
        status = "ready"
        available = True
        source = matched
    elif not sources:
        status = "unverified_source_list"
        available = True
        source = spec["candidates"][0]
    else:
        status = "not_in_source_list"
        available = False
        source = None
    return {
        "id": launch_id,
        "label": spec["label"],
        "hint": spec["hint"],
        "kind": "source",
        "available": available,
        "status": status,
        "source": source,
        "source_list": sources if status == "not_in_source_list" else None,
        "pairing": spec["pairing"],
        "plays_title": False,
    }


def _launch_rows(tv_state: dict[str, Any] | None, *, apple_tv_ok: bool) -> list[dict[str, Any]]:
    sources = _source_list(tv_state)
    videoland_source = match_videoland_source(sources)
    videoland_on_list = bool(sources) and any("videoland" in _slug(name) for name in sources)
    if not sources:
        videoland_status = "unverified_source_list"
        videoland_available = True
    elif videoland_on_list:
        videoland_status = "ready"
        videoland_available = True
    else:
        videoland_status = "not_in_source_list"
        videoland_available = False
        videoland_source = None
    rows = [
        {
            "id": "infuse",
            "label": "Infuse",
            "hint": "Apple TV",
            "kind": "infuse",
            "available": apple_tv_ok,
            "status": "ready" if apple_tv_ok else "needs_apple_tv_pairing",
            "source": None,
            "source_list": None,
            "pairing": (
                "Needs the Apple TV integration paired in Home Assistant "
                "(HA_APPLE_TV_ENTITY) and Infuse installed on the Apple TV. "
                "Hearth wakes the Denon "
                f"({settings.ha_avr_apple_tv_source}) and asks HA to launch "
                f"{settings.infuse_app_id}. This opens the app; continue watching "
                "starts a library title. Infuse has no now-playing API."
            ),
            "plays_title": False,
        },
        {
            "id": "videoland",
            "label": "Videoland",
            "hint": "LG app",
            "kind": "videoland",
            "available": videoland_available,
            "status": videoland_status,
            "source": videoland_source,
            "source_list": sources if videoland_status == "not_in_source_list" else None,
            "pairing": (
                "Needs HA webOS pairing (HA_TV_ENTITY). Hearth launches the "
                "Videoland source, then tells you honestly that titles and "
                "in-app profiles still need the remote. VIDEOLAND_SOURCE / "
                "VIDEOLAND_APP_ID only change that launch."
            ),
            "plays_title": False,
        },
    ]
    for launch_id, spec in _SOURCE_LAUNCHES.items():
        rows.append(_source_launch(launch_id, spec, sources))
    return rows


def _overseerr_stub() -> dict[str, Any]:
    configured = bool(settings.overseerr_configured)
    return {
        "configured": configured,
        "available": False,
        "requests": [],
        "stub": True,
        "todo": (
            "Continue watching on this screen is Plex On Deck. "
            "Hearth can search and request through Overseerr from chat and Telegram, "
            "but this TV shell has no request-queue read yet — so it does not invent one. "
            + (
                "Overseerr is configured on VAULT."
                if configured
                else "Overseerr is not configured (OVERSEERR_URL / OVERSEERR_API_KEY)."
            )
        ),
    }


async def build_board() -> dict[str, Any]:
    """One poll for the 10-foot UI. No tokens, no backend URLs."""
    media_result, pulse_result = await asyncio.gather(
        house_media_inventory(),
        house_pulse(),
        return_exceptions=True,
    )
    if isinstance(media_result, Exception):
        media: dict[str, Any] = {"ok": False, "error": str(media_result), "plex": {"sessions": []}}
    else:
        media = media_result
    if isinstance(pulse_result, Exception):
        pulse: dict[str, Any] = {
            "ok": False,
            "error": str(pulse_result),
            "continue_watching": [],
            "presets": [],
        }
    else:
        pulse = pulse_result

    tv = _player_card(media.get("tv"), role="tv")
    avr = _player_card(media.get("avr"), role="avr")
    apple = _player_card(media.get("apple_tv"), role="apple_tv")
    sessions = (media.get("plex") or {}).get("sessions") or []
    if not isinstance(sessions, list):
        sessions = []
    continue_rows = pulse.get("continue_watching") or []
    if not isinstance(continue_rows, list):
        continue_rows = []
    presets = pulse.get("presets") or []
    if not isinstance(presets, list):
        presets = []

    return {
        "ok": bool(media.get("ok", True)) and bool(pulse.get("ok", True)),
        "house": settings.house_name,
        "owner": settings.owner,
        "target_tv": TARGET_TV,
        "mode": media.get("mode") or "unknown",
        "receiver_centric": bool(settings.receiver_centric),
        "avr_sources": {
            "apple_tv": settings.ha_avr_apple_tv_source,
            "tv": settings.ha_avr_tv_source,
        },
        "now": _now_card(sessions),
        "players": {"tv": tv, "avr": avr, "apple_tv": apple},
        "continue_watching": [
            _continue_card(row) for row in continue_rows if isinstance(row, dict)
        ][:6],
        "presets": [
            {
                "preset": row.get("preset"),
                "label": row.get("label"),
                "available": bool(row.get("available")),
                "entity_id": row.get("entity_id"),
            }
            for row in presets
            if isinstance(row, dict)
        ],
        "activities": [
            {"id": key, "label": spec["label"], "hint": spec["hint"]}
            for key, spec in ACTIVITIES.items()
        ],
        "launches": _launch_rows(
            (media.get("tv") or {}).get("state") if isinstance(media.get("tv"), dict) else None,
            apple_tv_ok=bool(apple.get("ok")),
        ),
        "overseerr": _overseerr_stub(),
        "reach": {
            "lan_only": True,
            "hint": (
                "Use the VAULT LAN address or Tailscale from this TV. "
                "Do not port-forward Hearth."
            ),
        },
        "error": _public_text(media.get("error") or pulse.get("error") or "") or None,
    }


def _speak_of(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        text = data.get("speak") or data.get("error") or ""
        if text:
            return _public_text(text)
    if payload.get("ok"):
        return "Done."
    return "That didn't work."


async def _tool(name: str, args: dict[str, Any], said: str) -> dict[str, Any]:
    result = await registry.call(name, args, said=said, explicit_confirm=True)
    payload = result.as_dict()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "name": name,
        "ok": bool(payload.get("ok")),
        "speak": _speak_of({"ok": payload.get("ok"), "data": data}),
    }


def _finish(kind: str, action_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    ok = all(step.get("ok") for step in steps) if steps else False
    speak = " ".join(step["speak"] for step in steps if step.get("speak")).strip()
    if not speak:
        speak = "Done." if ok else "That didn't work."
    return {
        "ok": ok,
        "kind": kind,
        "id": action_id,
        "speak": _public_text(speak),
        "tools": [{"name": step["name"], "ok": step["ok"]} for step in steps],
    }


async def _tv_sources() -> tuple[dict[str, Any], list[str]]:
    tv = await ha.resolve_device_state("tv")
    state = tv.get("state") if isinstance(tv.get("state"), dict) else None
    return tv, _source_list(state)


async def _launch(action_id: str, said: str) -> dict[str, Any]:
    if action_id == "infuse":
        steps = [
            await _tool("media_activity", {"activity": "apple_tv"}, said),
            await _tool(
                "ha_media_control",
                {
                    "device": "apple_tv",
                    "action": "play_media",
                    "media_content_id": settings.infuse_app_id,
                    "media_content_type": "app",
                },
                said,
            ),
        ]
        return _finish("launch", action_id, steps)

    if action_id == "videoland":
        _tv, sources = await _tv_sources()
        if sources and not any("videoland" in _slug(name) for name in sources):
            listed = ", ".join(sources)
            return {
                "ok": False,
                "kind": "launch",
                "id": action_id,
                "speak": (
                    "Videoland isn't in this TV's Home Assistant source list "
                    f"({listed}). Pair webOS and confirm the app name. "
                    "Hearth won't guess a source."
                ),
                "tools": [],
            }
        step = await _tool("videoland_play", {"prepare_path": True}, said)
        return _finish("launch", action_id, [step])

    spec = _SOURCE_LAUNCHES.get(action_id)
    if spec is None:
        raise TvActionError(f"unknown launch {action_id}")
    _tv, sources = await _tv_sources()
    matched = _match_named_source(sources, tuple(spec["candidates"]))
    if sources and matched is None:
        listed = ", ".join(sources)
        return {
            "ok": False,
            "kind": "launch",
            "id": action_id,
            "speak": (
                f"{spec['label']} isn't in this TV's Home Assistant source list "
                f"({listed}). Pair the LG webOS integration and confirm the app "
                "name on the source list. Hearth won't guess a source, and it "
                "cannot start a title inside the app."
            ),
            "tools": [],
        }
    source = matched or spec["candidates"][0]
    steps = [
        await _tool("media_activity", {"activity": "tv"}, said),
        await _tool(
            "ha_media_control",
            {"device": "tv", "action": "select_source", "source": source},
            said,
        ),
    ]
    finished = _finish("launch", action_id, steps)
    if finished["ok"]:
        finished["speak"] = (
            f"Opened {spec['label']} on the LG ({source}). "
            "Home Assistant cannot start a title inside that app."
        )
    return finished


async def run_action(
    *,
    kind: str,
    action_id: str = "",
    title: str = "",
    season: int | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """Run one TV button. Unknown ids are errors; HA misses stay honest."""
    kind_key = (kind or "").strip().lower()
    ident = (action_id or "").strip()
    said = _said(kind_key, ident, title)
    with tool_turn(said, channel="tv"):
        if kind_key == "scene":
            if ident not in SCENES:
                raise TvActionError("scene must be movie_night, quiet_hours, or good_night")
            step = await _tool("house_scene", {"preset": ident}, said)
            return _finish("scene", ident, [step])
        if kind_key == "activity":
            if ident not in ACTIVITIES:
                raise TvActionError("activity must be movie_night, apple_tv, tv, or off")
            step = await _tool("media_activity", {"activity": ident}, said)
            return _finish("activity", ident, [step])
        if kind_key == "volume":
            action = {"up": "volume_up", "down": "volume_down", "mute": "mute", "unmute": "unmute"}.get(ident)
            if action is None:
                raise TvActionError("volume must be up, down, mute, or unmute")
            step = await _tool(
                "ha_media_control",
                {"device": "avr", "action": action},
                said,
            )
            return _finish("volume", ident, [step])
        if kind_key == "transport":
            action = TRANSPORT.get(ident)
            if action is None:
                raise TvActionError("transport must be pause, play, or stop")
            step = await _tool("infuse_transport", {"action": action}, said)
            return _finish("transport", ident, [step])
        if kind_key == "launch":
            return await _launch(ident, said)
        if kind_key == "continue":
            if not ident.isdigit():
                raise TvActionError("continue needs a numeric Plex ratingKey")
            query = (title or "").strip()
            if not query:
                raise TvActionError("continue needs a title")
            args: dict[str, Any] = {"query": query, "ratingKey": ident, "play": True}
            if season is not None:
                args["season"] = season
            if episode is not None:
                args["episode"] = episode
            step = await _tool("infuse_play", args, said)
            return _finish("continue", ident, [step])
    raise TvActionError("kind must be scene, activity, volume, transport, launch, or continue")


def _said(kind: str, action_id: str, title: str) -> str:
    if kind == "scene":
        return f"activate {action_id.replace('_', ' ')}"
    if kind == "activity":
        spec = ACTIVITIES.get(action_id)
        return spec["said"] if spec else f"media activity {action_id}"
    if kind == "volume":
        return f"denon volume {action_id}"
    if kind == "transport":
        return f"{action_id} on the apple tv"
    if kind == "launch":
        return f"open {action_id} on the tv"
    if kind == "continue":
        return f"play {title} in infuse"
    return "tv command"
