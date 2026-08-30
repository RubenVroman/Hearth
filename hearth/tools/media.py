"""House media inventory — TV / AVR (via HA) + Plex, speakable for the agent."""

from __future__ import annotations

import asyncio
from typing import Any

from hearth.config import settings
from hearth.tools.ha import ha, speak_player
from hearth.tools.plex import plex


def _device_role(device: str) -> str:
    key = (device or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in {"tv", "lg", "webos", "lg_tv", "lg_webos_tv", "television"}:
        return "tv"
    if key in {"avr", "denon", "receiver", "amp", "denon_avr", "denon_avr_x3700h"}:
        return "avr"
    if key in {"apple_tv", "appletv", "atv", "infuse"}:
        return "apple_tv"
    return key or "unknown"


def _speak_plex(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return "Nothing playing on Plex."
    lines: list[str] = []
    for session in sessions:
        title = session.get("title") or "Untitled"
        show = session.get("show")
        label = f"{show} — {title}" if show else title
        player = session.get("player") or "a player"
        state = session.get("state") or "idle"
        lines.append(f"{label} on {player} ({state})")
    return "Plex: " + "; ".join(lines) + "."


def _pipeline_status() -> dict[str, Any]:
    return {
        "radarr": {"configured": settings.radarr_configured, "url": settings.radarr_url},
        "sonarr": {"configured": settings.sonarr_configured, "url": settings.sonarr_url},
        "overseerr": {"configured": settings.overseerr_configured, "url": settings.overseerr_url},
        "plex": {"configured": settings.plex_configured, "url": settings.plex_url},
        "home_assistant": {"configured": settings.ha_configured, "url": settings.ha_url},
    }


async def house_media_inventory() -> dict[str, Any]:
    """Single snapshot: LG TV, Denon AVR, Apple TV, Plex now-playing, backend wiring."""
    tv, avr, apple_tv, playing = await asyncio.gather(
        ha.resolve_device_state("tv"),
        ha.resolve_device_state("avr"),
        ha.resolve_device_state("apple_tv"),
        plex.now_playing(),
    )
    sessions = playing.get("sessions") or []

    tv_state = tv.get("state") if tv.get("ok") else None
    avr_state = avr.get("state") if avr.get("ok") else None
    atv_state = apple_tv.get("state") if apple_tv.get("ok") else None
    tv_speak = speak_player(tv_state, label="LG webOS TV")
    avr_speak = speak_player(avr_state, label="Denon AVR")
    atv_speak = speak_player(atv_state, label="Apple TV")
    plex_speak = _speak_plex(sessions)

    modes = {tv.get("mode"), avr.get("mode"), apple_tv.get("mode"), playing.get("mode")}
    modes.discard(None)
    mode = "live" if modes == {"live"} else ("mock" if "mock" in modes else "mixed")

    speak_parts = [tv_speak, avr_speak, atv_speak, plex_speak]
    pipeline = _pipeline_status()
    missing = [
        name
        for name, info in (
            ("Home Assistant", pipeline["home_assistant"]),
            ("Plex", pipeline["plex"]),
            ("Radarr", pipeline["radarr"]),
            ("Sonarr", pipeline["sonarr"]),
            ("Overseerr", pipeline["overseerr"]),
        )
        if not info["configured"]
    ]
    if missing:
        speak_parts.append(
            "Backends without keys (fixtures until set): " + ", ".join(missing) + "."
        )

    return {
        "ok": True,
        "mode": mode,
        "tv": {
            "ok": bool(tv.get("ok")),
            "entity_id": tv.get("entity_id") or settings.ha_tv_entity,
            "resolved": tv.get("resolved"),
            "state": tv_state,
            "speak": tv_speak,
            "error": tv.get("error"),
        },
        "avr": {
            "ok": bool(avr.get("ok")),
            "entity_id": avr.get("entity_id") or settings.ha_avr_entity,
            "resolved": avr.get("resolved"),
            "state": avr_state,
            "speak": avr_speak,
            "error": avr.get("error"),
        },
        "apple_tv": {
            "ok": bool(apple_tv.get("ok")),
            "entity_id": apple_tv.get("entity_id") or settings.ha_apple_tv_entity,
            "resolved": apple_tv.get("resolved"),
            "state": atv_state,
            "speak": atv_speak,
            "error": apple_tv.get("error"),
            "default_player": settings.apple_tv_player,
        },
        "plex": {
            "mode": playing.get("mode"),
            "sessions": sessions,
            "speak": plex_speak,
            "error": playing.get("error"),
        },
        "pipeline": pipeline,
        "entities": {
            "tv": settings.ha_tv_entity,
            "avr": settings.ha_avr_entity,
            "apple_tv": settings.ha_apple_tv_entity,
        },
        "speak": " ".join(speak_parts),
    }


async def media_control(
    device: str,
    action: str,
    *,
    volume_level: float | None = None,
    source: str | None = None,
    media_content_id: str | None = None,
    media_content_type: str | None = None,
    is_volume_muted: bool | None = None,
) -> dict[str, Any]:
    requested_role = _device_role(device)
    role = requested_role
    volume_actions = {
        "volume",
        "volume_set",
        "set_volume",
        "volume_mute",
        "mute",
        "volume_unmute",
        "unmute",
        "volume_up",
        "volume_down",
    }
    # The Denon owns living-room audio. Route TV/Apple-TV volume commands to it
    # so a natural "turn the TV down" does not hit a disabled television output.
    if settings.receiver_centric and role in {"tv", "apple_tv"} and action.lower() in volume_actions:
        role = "avr"
    result = await ha.media_control(
        role,
        action,
        volume_level=volume_level,
        source=source,
        media_content_id=media_content_id,
        media_content_type=media_content_type,
        is_volume_muted=is_volume_muted,
    )
    state = result.get("state")
    if requested_role == "apple_tv":
        label = "Apple TV"
    elif requested_role == "tv":
        label = "LG webOS TV"
    else:
        label = "Denon AVR"
    result["device"] = requested_role
    if role != requested_role:
        result["routed_via"] = "avr"
        result["controlled_entity_id"] = result.get("entity_id")
    result["speak"] = speak_player(state, label=label) if state else (
        f"{label}: {action} via {result.get('entity_id')}."
        if result.get("ok")
        else str(result.get("error") or f"{label} control failed.")
    )
    return result


async def media_activity(activity: str) -> dict[str, Any]:
    """Receiver-aware living-room activities used by voice and playback."""
    key = (activity or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in {"off", "power_off", "all_off", "good_night"}:
        return await ha.power_off_media_path()
    if key in {"apple_tv", "appletv", "atv", "infuse", "watch_apple_tv"}:
        return await ha.activate_media_path("apple_tv")
    if key in {"tv", "watch_tv", "television", "lg"}:
        return await ha.activate_media_path("tv")
    return {
        "ok": False,
        "error": "activity must be apple_tv, tv, or off",
        "speak": "I can prepare Apple TV, television, or turn the media chain off.",
    }
