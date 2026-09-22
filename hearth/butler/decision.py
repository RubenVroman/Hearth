"""Jev decides shelf and scene tools. The language model does not.

``evaluate_message`` already runs the shared System One gate. ``butler_ask`` on
that same call is the tool choice. High confidence wins. Disabled Jev, a missing
key, an API error, or low confidence fails open to the local phrase router.
A confident ``other`` does not run the tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from hearth.config import settings
from hearth.butler.phrases import classify_house_phrase, house_route
from hearth.jev.schema import BUTLER_ASK_CRITERIA, JevAnswers, JevVerdict

# Offered to the house runtime, hidden from OpenAI tool lists.
JEV_GATED_TOOL_NAMES = frozenset({"house_shelf", "house_scene"})

_TOOL_FOR_CHOICE: dict[str, tuple[str, dict[str, str]]] = {
    "shelf": ("house_shelf", {}),
    "movie_night": ("house_scene", {"preset": "movie_night"}),
    "quiet_hours": ("house_scene", {"preset": "quiet_hours"}),
    "good_night": ("house_scene", {"preset": "good_night"}),
}

PRESET_PHRASES = {
    "movie_night": "movie night",
    "quiet_hours": "quiet hours",
    "good_night": "good night",
}


@dataclass(frozen=True, slots=True)
class ButlerDecision:
    """Whether a butler tool may run, and which one Jev or the fail-open picked."""

    run: bool
    tool: str = ""
    args: dict[str, str] | None = None
    source: str = "none"
    blocked_by_jev: bool = False

    def as_args(self) -> dict[str, str]:
        return dict(self.args or {})


def butler_ask_choice(
    answers: JevAnswers | None,
    *,
    confidence_min: float | None = None,
) -> tuple[str, float] | None:
    """Return ``(butler_ask choice, confidence)`` when it clears the floor."""
    if answers is None or answers.butler_ask is None:
        return None
    floor = (
        settings.jev_domain_confidence if confidence_min is None else float(confidence_min)
    )
    choice = str(answers.butler_ask.choice or "").strip()
    confidence = float(answers.butler_ask.confidence)
    if choice not in BUTLER_ASK_CRITERIA or confidence < floor:
        return None
    return choice, confidence


def decide_butler_tool(text: str, verdict: JevVerdict | None) -> ButlerDecision:
    """Pick house_shelf / house_scene from the shared gate, or fail open locally."""
    local = house_route(text)
    if verdict is not None and verdict.action == "block_cancel":
        return ButlerDecision(run=False, blocked_by_jev=True, source="jev_cancel")

    confident = None
    if (
        verdict is not None
        and verdict.enabled
        and verdict.ok
        and verdict.answers is not None
    ):
        confident = butler_ask_choice(verdict.answers)

    if confident is not None:
        choice, _confidence = confident
        if choice == "other":
            return ButlerDecision(run=False, blocked_by_jev=True, source="jev_other")
        tool, args = _TOOL_FOR_CHOICE[choice]
        return ButlerDecision(run=True, tool=tool, args=dict(args), source="jev")

    if local is not None:
        return ButlerDecision(
            run=True,
            tool=str(local["tool"]),
            args=dict(local.get("args") or {}),
            source="fail_open",
        )
    return ButlerDecision(run=False, source="none")


def phrase_for_preset(preset: str) -> str:
    """Canonical utterance for a shell button so the gate sees the same ask."""
    key = (preset or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return PRESET_PHRASES.get(key, key.replace("_", " "))


def hide_from_llm(tools: list[dict]) -> list[dict]:
    """Drop Jev-gated tools so the model cannot choose them."""
    visible: list[dict] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        name = str((function or tool).get("name") or "")
        if name in JEV_GATED_TOOL_NAMES:
            continue
        visible.append(tool)
    return visible


def is_butler_phrase(text: str) -> bool:
    return classify_house_phrase(text) is not None


__all__ = [
    "JEV_GATED_TOOL_NAMES",
    "ButlerDecision",
    "butler_ask_choice",
    "decide_butler_tool",
    "hide_from_llm",
    "is_butler_phrase",
    "phrase_for_preset",
]
