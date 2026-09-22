"""What’s already on Plex: continue watching, new arrivals, last finished."""

from __future__ import annotations

import asyncio
from typing import Any

from hearth.tools.ha import ha
from hearth.tools.plex import plex


def _label(item: dict[str, Any]) -> str:
    return str(item.get("label") or item.get("title") or "Untitled")


def _continue_bit(item: dict[str, Any]) -> str:
    label = _label(item)
    percent = item.get("progress_pct")
    if isinstance(percent, int) and percent > 0:
        return f"{label} ({percent}% in)"
    return label


def speak_shelf(
    *,
    continue_watching: list[dict[str, Any]],
    recently_added: list[dict[str, Any]],
    last_played: dict[str, Any] | None,
    now: list[dict[str, Any]],
    mode: str,
    error: str | None,
) -> str:
    """One spoken picture of the shelf. Never invents a title that wasn’t returned."""
    if error:
        lead = "Plex didn’t answer, so this is the last picture I have of the shelf — not a live read. "
    elif mode == "mock":
        lead = "Plex isn’t connected, so this is the fixture shelf. "
    else:
        lead = ""

    parts: list[str] = []
    if now:
        session = now[0]
        title = session.get("show") or session.get("title") or "something"
        if session.get("show") and session.get("title") and session.get("title") != session.get("show"):
            title = f"{session.get('show')} · {session.get('title')}"
        state = session.get("state") or "playing"
        player = session.get("player") or "a player"
        parts.append(f"On right now: {title}, {state} on {player}.")
    if continue_watching:
        bits = "; ".join(_continue_bit(item) for item in continue_watching[:3])
        parts.append(f"Continue watching: {bits}.")
    elif not now:
        parts.append("Nothing half-finished on the shelf.")
    if recently_added:
        names = ", ".join(_label(item) for item in recently_added[:3])
        parts.append(f"Already on Plex and new: {names}.")
    if last_played and not now:
        parts.append(f"Last finished: {_label(last_played)}.")
    if not parts:
        return (
            lead
            + "The shelf is quiet — nothing half-watched, and nothing new landed. Name a title and I’ll look."
        ).strip()
    body = " ".join(parts)
    if not continue_watching and not recently_added and not now:
        body += " Name a title if you want me to look further."
    return (lead + body).strip()


def _take(result: Any, *, key: str = "items") -> tuple[list[dict[str, Any]], str, str | None]:
    if isinstance(result, Exception):
        return [], "error", str(result)
    if not isinstance(result, dict):
        return [], "error", "unexpected shelf payload"
    rows = result.get(key) or []
    if not isinstance(rows, list):
        rows = []
    clean = [row for row in rows if isinstance(row, dict)]
    mode = str(result.get("mode") or "unknown")
    error = str(result["error"]) if result.get("error") else None
    return clean, mode, error


async def shelf_snapshot(*, limit: int = 6) -> dict[str, Any]:
    """Continue watching + recently added + last play + what’s on right now."""
    deck, recent, history, playing = await asyncio.gather(
        plex.on_deck(limit=limit),
        plex.recently_added(limit=limit),
        plex.play_history(limit=3),
        plex.now_playing(),
        return_exceptions=True,
    )
    continue_watching, deck_mode, deck_error = _take(deck)
    recently_added, recent_mode, recent_error = _take(recent)
    history_rows, history_mode, history_error = _take(history)
    if isinstance(playing, Exception):
        now: list[dict[str, Any]] = []
        now_mode = "error"
        now_error: str | None = str(playing)
    else:
        now, now_mode, now_error = _take(playing, key="sessions")

    modes = [deck_mode, recent_mode, history_mode, now_mode]
    mode = "live" if "live" in modes and "mock" not in modes and "error" not in modes else (
        "mock" if "mock" in modes else "error"
    )
    error = deck_error or recent_error or history_error or now_error
    last_played = history_rows[0] if history_rows else None
    speak = speak_shelf(
        continue_watching=continue_watching,
        recently_added=recently_added,
        last_played=last_played,
        now=now,
        mode=mode,
        error=error,
    )
    ok = error is None or bool(continue_watching or recently_added or now or last_played)
    return {
        "ok": ok,
        "mode": mode,
        "error": error,
        "continue_watching": continue_watching,
        "recently_added": recently_added,
        "last_played": last_played,
        "now": now,
        "speak": speak,
    }


async def house_pulse() -> dict[str, Any]:
    """Web-shell snapshot: shelf plus which scene presets Home Assistant can run."""
    from hearth.butler.scenes import preset_status

    shelf, scenes = await asyncio.gather(
        shelf_snapshot(),
        ha.list_states("scene"),
        return_exceptions=True,
    )
    if isinstance(shelf, Exception):
        shelf_payload: dict[str, Any] = {
            "ok": False,
            "mode": "error",
            "error": str(shelf),
            "continue_watching": [],
            "recently_added": [],
            "last_played": None,
            "now": [],
            "speak": "The shelf didn’t answer. Try again in a moment.",
        }
    else:
        shelf_payload = shelf
    if isinstance(scenes, Exception):
        scene_payload: dict[str, Any] = {"ok": False, "mode": "error", "error": str(scenes), "states": []}
    else:
        scene_payload = scenes
    states = scene_payload.get("states") or []
    presets = preset_status(states if isinstance(states, list) else [])
    active = next((row["preset"] for row in presets if row.get("state") == "on"), None)
    return {
        "ok": bool(shelf_payload.get("ok")) and bool(scene_payload.get("ok", True)),
        "plex": {
            "live": plex.live,
            "mode": shelf_payload.get("mode"),
            "error": shelf_payload.get("error"),
        },
        "ha": {
            "live": ha.live,
            "mode": scene_payload.get("mode"),
            "ok": bool(scene_payload.get("ok", False)),
            "error": scene_payload.get("error"),
        },
        "now": (shelf_payload.get("now") or [None])[0],
        "continue_watching": shelf_payload.get("continue_watching") or [],
        "recently_added": shelf_payload.get("recently_added") or [],
        "last_played": shelf_payload.get("last_played"),
        "presets": presets,
        "active_preset": active,
        "speak": shelf_payload.get("speak") or "",
    }


__all__ = ["house_pulse", "shelf_snapshot", "speak_shelf"]
