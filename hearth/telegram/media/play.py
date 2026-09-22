"""Remote “play it on the TV” for Telegram — real HA / Infuse / Plex paths only.

Prefers Infuse on Apple TV when ``HEARTH_APPLE_TV_PLAYER=infuse`` (house default):
wake the Denon → LG → Apple TV chain via Home Assistant, then send the Infuse
deep link. Falls back to Plex ``playMedia`` for an explicit Plex client.
Never invents success — failures become an honest spoken line.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from hearth.config import settings

log = logging.getLogger("hearth.telegram.media.play")

_PLAY_PHRASE = re.compile(
    r"^\s*(?:"
    r"(?:put|play|throw|send)\s+(?:it|that|this)\s+on\s+(?:the\s+)?(?:tv|television|plex|screen)|"
    r"(?:play\s+it(?:\s+on\s+(?:the\s+)?(?:tv|plex))?|play\s+on\s+(?:the\s+)?tv)|"
    r"(?:op\s+de\s+tv\s+(?:zetten|afspelen)|zet\s+(?:hem|het)\s+op\s+de\s+tv)"
    r")\s*[.!?]*\s*$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class PlayOutcome:
    ok: bool
    message: str
    path: str = ""  # infuse | plex | none
    detail: dict[str, Any] | None = None


def play_lane_enabled() -> bool:
    return bool(getattr(settings, "telegram_play_lane", True))


def looks_like_play_command(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 80:
        return False
    return bool(_PLAY_PHRASE.match(raw))


def _player_preference() -> str:
    return str(getattr(settings, "apple_tv_player", "infuse") or "infuse").strip().lower()


def _honest_failure(reason: str, *, path: str = "none") -> PlayOutcome:
    return PlayOutcome(ok=False, message=reason, path=path)


async def play_on_tv(
    *,
    title: str,
    tmdb_id: int | None = None,
    media_type: str = "movie",
    year: int | None = None,
    season: int | None = None,
) -> PlayOutcome:
    """Start playback on the living-room path. Never fakes success."""
    if not play_lane_enabled():
        return _honest_failure(
            "Play-from-Telegram is turned off (HEARTH_TELEGRAM_PLAY_LANE=false)."
        )

    label = f"{title} ({year})" if year else (title or "that title")
    prefer = _player_preference()

    if prefer in {"infuse", "firecore", ""}:
        outcome = await _play_infuse(
            title=title,
            tmdb_id=tmdb_id,
            season=season,
            label=label,
        )
        if outcome.ok or outcome.path == "infuse":
            return outcome

    return await _play_plex(title=title, label=label, media_type=media_type)


async def _play_infuse(
    *,
    title: str,
    tmdb_id: int | None,
    season: int | None,
    label: str,
) -> PlayOutcome:
    try:
        from hearth.tools.infuse import infuse
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram play: infuse import failed: %s", exc)
        return _honest_failure(
            f"I can't reach Infuse from here ({exc}).",
            path="infuse",
        )

    try:
        result = await infuse.play(
            title,
            tmdb_id=tmdb_id,
            season=season,
            play=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("telegram play: infuse.play failed")
        return _honest_failure(
            f"Infuse play failed for {label}: {exc}",
            path="infuse",
        )

    if not isinstance(result, dict):
        return _honest_failure(
            f"Infuse returned nothing useful for {label}.",
            path="infuse",
        )
    if result.get("ok") and result.get("played"):
        target = (
            str(result.get("entity_id") or "")
            or "Apple TV"
        )
        return PlayOutcome(
            ok=True,
            message=f"Playing {label} on {target} via Infuse.",
            path="infuse",
            detail=result,
        )
    if result.get("ok") and result.get("launched"):
        return PlayOutcome(
            ok=True,
            message=str(result.get("speak") or f"Opened {label} in Infuse."),
            path="infuse",
            detail=result,
        )
    error = str(result.get("error") or result.get("speak") or "unknown error")
    return PlayOutcome(
        ok=False,
        message=str(result.get("speak") or f"Couldn't start {label} on Infuse — {error}"),
        path="infuse",
        detail=result,
    )


async def _play_plex(*, title: str, label: str, media_type: str) -> PlayOutcome:
    try:
        from hearth.tools.plex import plex
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram play: plex import failed: %s", exc)
        return _honest_failure(
            f"Plex isn't importable here ({exc}).",
            path="plex",
        )

    player = str(getattr(settings, "plex_default_player", "") or "").strip() or None
    try:
        result = await plex.play(title, player=player)
    except Exception as exc:  # noqa: BLE001
        log.exception("telegram play: plex.play failed")
        return _honest_failure(
            f"Plex play failed for {label}: {exc}",
            path="plex",
        )

    if not isinstance(result, dict):
        return _honest_failure(
            f"Plex returned nothing useful for {label}.",
            path="plex",
        )
    if result.get("ok") and result.get("played"):
        client = (result.get("client") or {}).get("name") or "the Plex client"
        return PlayOutcome(
            ok=True,
            message=f"Playing {label} on {client}.",
            path="plex",
            detail=result,
        )
    if result.get("ok") and not result.get("played"):
        # resolve-only / waiting for client
        speak = str(result.get("speak") or "").strip()
        return PlayOutcome(
            ok=False,
            message=speak
            or (
                f"{label} is on Plex, but no player is online to take playMedia. "
                "Open Plex on the TV and try again."
            ),
            path="plex",
            detail=result,
        )
    error = str(result.get("error") or result.get("speak") or "no Plex client")
    return PlayOutcome(
        ok=False,
        message=f"Couldn't play {label} via Plex — {error}",
        path="plex",
        detail=result,
    )


__all__ = [
    "PlayOutcome",
    "looks_like_play_command",
    "play_lane_enabled",
    "play_on_tv",
]
