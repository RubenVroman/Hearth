"""House media inventory — TV / AVR (via HA) + Plex, speakable for the agent."""

from __future__ import annotations

from typing import Any

from hearth.config import settings
from hearth.tools.ha import ha, speak_player
from hearth.tools.plex import plex


def _device_role(device: str) -> str:
    key = (device or "").strip().lower()
    if key in {"tv", "lg", "webos", "lg_tv", "lg webos tv", "television"}:
        return "tv"
    if key in {"avr", "denon", "receiver", "amp", "denon_avr", "denon avr"}:
        return "avr"
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
    """Single snapshot: LG TV, Denon AVR, Plex now-playing, backend wiring."""
    tv = await ha.resolve_device_state("tv")
    avr = await ha.resolve_device_state("avr")
    playing = await plex.now_playing()
    sessions = playing.get("sessions") or []

    tv_state = tv.get("state") if tv.get("ok") else None
    avr_state = avr.get("state") if avr.get("ok") else None
    tv_speak = speak_player(tv_state, label="LG webOS TV")
    avr_speak = speak_player(avr_state, label="Denon AVR")
    plex_speak = _speak_plex(sessions)

    modes = {tv.get("mode"), avr.get("mode"), playing.get("mode")}
    modes.discard(None)
    mode = "live" if modes == {"live"} else ("mock" if "mock" in modes else "mixed")

    speak_parts = [tv_speak, avr_speak, plex_speak]
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
    role = _device_role(device)
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
    label = "LG webOS TV" if role == "tv" else "Denon AVR"
    result["device"] = role
    result["speak"] = speak_player(state, label=label) if state else (
        f"{label}: {action} via {result.get('entity_id')}."
        if result.get("ok")
        else str(result.get("error") or f"{label} control failed.")
    )
    return result
