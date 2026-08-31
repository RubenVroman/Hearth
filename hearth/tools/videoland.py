"""Videoland on the living-room LG webOS TV via Home Assistant.

What HA/webOS can do
--------------------
Home Assistant's LG webOS integration can launch installed apps with
``media_player.select_source`` (source name from the TV ``source_list``,
typically ``Videoland``). It can also send ``webostv.command`` /
``webostv.button``. Hearth talks to HA over REST only — it does not speak
webOS/SSAP itself.

What it cannot do
-----------------
Videoland's Netflix-style **profile picker** and **in-app title playback**
are not first-class HA/webOS APIs. There is no documented ``contentId`` /
deep-link format that starts a named show (e.g. "B&B Vol Liefde") inside
the Videoland LG app, and no API to list or select household profiles
("Parel", …). Guessed D-pad sequences are intentionally not used: the
profile grid and home layout change, so a brittle remote path would fake
success.

Hearth path
-----------
1. Optionally prepare the receiver-centric TV media path.
2. Launch / focus Videoland on the LG when it is not already the source.
3. Accept a title and/or profile name so the agent can name the ask.
4. Return ``played=False`` / ``profile_selected=False`` with bilingual
   speak text and a practical workaround (pick the title or profile on
   the TV remote). Never claim playback or profile selection succeeded.
"""

from __future__ import annotations

import re
from typing import Any

from hearth.config import settings
from hearth.tools.ha import ha

# Friendly source names HA exposes after webOS pairing (case-insensitive match).
_DEFAULT_SOURCE_CANDIDATES = ("Videoland", "videoland")

_LIMITATION_EN = (
    "Home Assistant can open Videoland on the LG webOS TV, but it cannot "
    "start a specific title or select an in-app profile — Videoland does "
    "not expose that through HA/webOS."
)
_LIMITATION_NL = (
    "Home Assistant kan Videoland openen op de LG webOS-tv, maar kan geen "
    "specifieke titel starten of een in-app-profiel kiezen — Videoland "
    "biedt dat niet via HA/webOS."
)


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (value or "").lower())).strip("_")


def _source_candidates() -> list[str]:
    configured = (settings.videoland_source or "").strip()
    out: list[str] = []
    if configured:
        out.append(configured)
    for name in _DEFAULT_SOURCE_CANDIDATES:
        if name not in out:
            out.append(name)
    return out


def match_videoland_source(source_list: list[Any] | None) -> str | None:
    """Pick the TV source_list entry that is Videoland, if present."""
    sources = [str(s) for s in (source_list or []) if str(s).strip()]
    candidates = [_slug(c) for c in _source_candidates()]
    for source in sources:
        if _slug(source) in candidates:
            return source
    for source in sources:
        slug = _slug(source)
        if "videoland" in slug:
            return source
    # Fall back to configured / default name even when source_list is empty
    # (TV off / fixtures) — select_source still accepts the friendly name.
    return _source_candidates()[0]


def _title_hint(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    return cleaned


def _speak_result(
    *,
    launched: bool,
    already_open: bool,
    title: str,
    profile: str,
    launch_error: str | None = None,
) -> str:
    """Bilingual honest status — never claims played/profile-selected."""
    bits_en: list[str] = []
    bits_nl: list[str] = []

    if launch_error:
        bits_en.append(f"I couldn't open Videoland on the LG: {launch_error}.")
        bits_nl.append(f"Ik kon Videoland niet openen op de LG: {launch_error}.")
    elif already_open:
        bits_en.append("Videoland is already open on the LG webOS TV.")
        bits_nl.append("Videoland staat al open op de LG webOS-tv.")
    elif launched:
        bits_en.append("I opened Videoland on the LG webOS TV.")
        bits_nl.append("Ik heb Videoland geopend op de LG webOS-tv.")
    else:
        bits_en.append("I could not confirm Videoland on the LG.")
        bits_nl.append("Ik kon Videoland op de LG niet bevestigen.")

    bits_en.append(_LIMITATION_EN)
    bits_nl.append(_LIMITATION_NL)

    if title:
        bits_en.append(
            f'Please pick "{title}" yourself on the TV remote — I did not start playback.'
        )
        bits_nl.append(
            f'Kies "{title}" zelf op de TV-afstandsbediening — ik heb het niet afgespeeld.'
        )
    if profile:
        bits_en.append(
            f'Please select the "{profile}" profile on the TV — I did not switch profiles.'
        )
        bits_nl.append(
            f'Kies zelf het profiel "{profile}" op de tv — ik heb geen profiel gewisseld.'
        )
    if not title and not profile and not launch_error:
        bits_en.append(
            "When you want a show, open it in Videoland on the TV; "
            "I can only bring the app up."
        )
        bits_nl.append(
            "Wil je een programma? Open het in Videoland op de tv; "
            "ik kan alleen de app openen."
        )

    return " ".join(bits_en) + " / " + " ".join(bits_nl)


class Videoland:
    """Launch Videoland on the LG; never fake title play or profile select."""

    async def play(
        self,
        title: str = "",
        *,
        profile: str = "",
        prepare_path: bool = True,
    ) -> dict[str, Any]:
        """Open Videoland (if needed) and return an honest limitation for title/profile."""
        title_clean = _title_hint(title)
        profile_clean = _title_hint(profile)

        steps: list[dict[str, Any]] = []
        if prepare_path and settings.receiver_centric:
            path = await ha.activate_media_path("tv")
            steps.append({"step": "media_path", **path})

        tv = await ha.resolve_device_state("tv")
        if not tv.get("ok"):
            error = str(tv.get("error") or "LG webOS TV not found in Home Assistant")
            return {
                "ok": False,
                "played": False,
                "profile_selected": False,
                "launched": False,
                "already_open": False,
                "title": title_clean or None,
                "profile": profile_clean or None,
                "limitation": "no_title_or_profile_api",
                "error": error,
                "steps": steps,
                "speak": _speak_result(
                    launched=False,
                    already_open=False,
                    title=title_clean,
                    profile=profile_clean,
                    launch_error=error,
                ),
            }

        entity_id = str(tv.get("entity_id") or settings.ha_tv_entity)
        state = tv.get("state") or {}
        attrs = state.get("attributes") or {}
        current_source = str(attrs.get("source") or attrs.get("app_name") or "")
        source = match_videoland_source(attrs.get("source_list"))
        already_open = bool(current_source) and (
            _slug(current_source) == _slug(source or "")
            or "videoland" in _slug(current_source)
        )

        launch: dict[str, Any] | None = None
        launched = False
        launch_error: str | None = None
        if already_open:
            launched = True
        else:
            launch = await ha.media_control("tv", "select_source", source=source)
            steps.append({"step": "select_source", "source": source, **launch})
            launched = bool(launch.get("ok"))
            if not launched:
                launch_error = str(
                    launch.get("error") or launch.get("warning") or "select_source failed"
                )

        # Optional undocumented deep-link attempt only when an app id is configured.
        # Videoland has no verified contentId schema on webOS — never treat as played.
        deep_link: dict[str, Any] | None = None
        app_id = (settings.videoland_app_id or "").strip()
        if launched and title_clean and app_id:
            deep_link = await ha.call_service(
                "webostv",
                "command",
                entity_id,
                {
                    "command": "system.launcher/launch",
                    "payload": {
                        "id": app_id,
                        # Best-effort only: title string is not a known Videoland contentId.
                        "contentId": title_clean,
                    },
                },
            )
            steps.append(
                {
                    "step": "deep_link_attempt",
                    "verified": False,
                    "note": (
                        "Optional webostv.command launch with contentId=title. "
                        "No documented Videoland contentId exists; not treated as playback."
                    ),
                    **deep_link,
                }
            )

        ok = launched and launch_error is None
        return {
            "ok": ok,
            # Honest flags — never true via current HA/webOS APIs.
            "played": False,
            "profile_selected": False,
            "launched": launched,
            "already_open": already_open,
            "title": title_clean or None,
            "profile": profile_clean or None,
            "source": source,
            "entity_id": entity_id,
            "limitation": "no_title_or_profile_api",
            "workaround": {
                "en": (
                    f'Select "{title_clean}" in Videoland on the TV remote.'
                    if title_clean
                    else "Use the TV remote inside Videoland for titles and profiles."
                ),
                "nl": (
                    f'Kies "{title_clean}" in Videoland op de afstandsbediening.'
                    if title_clean
                    else "Gebruik de afstandsbediening in Videoland voor titels en profielen."
                ),
            },
            "limitation_text": {"en": _LIMITATION_EN, "nl": _LIMITATION_NL},
            "deep_link_attempted": deep_link is not None,
            "deep_link_verified": False,
            "steps": steps,
            "mode": tv.get("mode"),
            "speak": _speak_result(
                launched=launched and not already_open,
                already_open=already_open,
                title=title_clean,
                profile=profile_clean,
                launch_error=launch_error,
            ),
        }


videoland = Videoland()

__all__ = [
    "Videoland",
    "match_videoland_source",
    "videoland",
]
