"""Jev-routed tool calling: every house tool call is decided by System One.

Hearth used to ask Jev one governance question per turn and then let regex, gpt,
or a Telegram lane pick the tools. This module makes the typed gate the actual
router:

* **which tool** — a ``tool_lane`` Choice narrows the turn to one family of house
  tools. The local router prefers that lane; the lane's arguments are still
  derived deterministically, never from prose.
* **allow / deny** — a ``tool_allow`` Noul plus the ``is_cancel`` Noul and the
  ``risk`` Score decide whether a state-changing tool may run at all.
* **whether an LLM is needed** — the ``needs_llm`` Noul rides along so callers
  can skip or force a gpt hop.

Three invariants hold everywhere:

1. **One call per turn.** A turn opens a scope (:func:`turn`), and the first
   gated tool call fetches (or inherits) a single System One answer set. Every
   later tool call in that turn is decided locally from it, so an eight-tool
   OpenAI turn costs one typed call, not eight.
2. **Fail open.** Jev off, no API key, empty state, API error, timeout, or an
   unparseable answer set all return ``allow``. A house that cannot reach its
   gate behaves exactly like a house without one.
3. **Shadow never changes behavior.** ``HEARTH_JEV_SHADOW=true`` still computes
   and logs the decision it *would* have taken, so enforcement can be reviewed
   from logs before it is switched on.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from hearth.config import settings
from hearth.jev.client import SystemOneClient
from hearth.jev.gate import evaluate_message
from hearth.jev.schema import (
    TOOL_LANE_CRITERIA,
    JevAnswers,
    JevVerdict,
    ToolAction,
    tool_gate_questions,
)

log = logging.getLogger("hearth.jev")

# Lane → the registry tools that belong to it. Lanes are coarse on purpose: a
# 14-label Choice is cheap and stable, while a 50-label one is neither.
TOOL_LANES: dict[str, tuple[str, ...]] = {
    "lights": (
        "ha_list_entities",
        "ha_get_state",
        "ha_call_service",
        "ha_device_control",
        "ha_discover_entities",
        "house_scene",
        "house_ritual",
        "house_climate",
        "house_feeder",
        "house_purifier",
        "house_comfort",
        "house_status",
    ),
    "media_playback": (
        "plex_play",
        "infuse_play",
        "infuse_transport",
        "videoland_play",
        "ha_media_control",
        "media_activity",
    ),
    "media_library": (
        "plex_search",
        "plex_now_playing",
        "plex_clients",
        "plex_browse_genre",
        "house_media",
        "house_shelf",
        "suggest_titles",
    ),
    "media_queue": (
        "overseerr_request",
        "radarr_add",
        "sonarr_add",
        "radarr_retry",
        "sonarr_retry",
        "radarr_grab_release",
    ),
    "media_status": (
        "radarr_queue",
        "sonarr_queue",
        "radarr_search",
        "sonarr_search",
        "overseerr_search",
        "radarr_list_releases",
    ),
    "food": (
        "thuisbezorgd_restaurants",
        "thuisbezorgd_menu",
        "thuisbezorgd_cart",
        "thuisbezorgd_auth_status",
        "thuisbezorgd_order",
    ),
    "weather": ("get_weather",),
    "web": ("web_search",),
    "files": (
        "workspace_list",
        "workspace_read",
        "workspace_write",
        "workspace_delete",
        "docker_ps",
        "docker_inspect",
        "docker_stop",
    ),
    "memory_read": ("memory_list", "memory_search"),
    "memory_write": (
        "memory_remember",
        "memory_forget",
        "memory_export",
        "memory_purge",
    ),
    "network": ("house_network", "tuya_lan_probe"),
    "escalate_cos": ("chief_of_staff",),
    "no_tool": ("end_call",),
}

_TOOL_TO_LANE: dict[str, str] = {
    tool: lane for lane, tools in TOOL_LANES.items() for tool in tools
}

# Tools that change house state, spend money, queue a download, or leave the
# house. Only these can be denied or confirm-gated; reads always fail open so a
# misread gate can never stop Hearth from *answering*.
WRITE_TOOLS = frozenset(
    {
        "ha_call_service",
        "ha_device_control",
        "ha_media_control",
        "media_activity",
        "house_scene",
        "house_ritual",
        "house_climate",
        "house_feeder",
        "house_purifier",
        "plex_play",
        "infuse_play",
        "infuse_transport",
        "videoland_play",
        "overseerr_request",
        "radarr_add",
        "sonarr_add",
        "radarr_retry",
        "sonarr_retry",
        "radarr_grab_release",
        "thuisbezorgd_cart",
        "thuisbezorgd_order",
        "workspace_write",
        "workspace_delete",
        "docker_stop",
        "memory_remember",
        "memory_forget",
        "memory_export",
        "memory_purge",
        "chief_of_staff",
    }
)

# Reads that must never be gated away, even when a tool is not in TOOL_LANES.
READ_ONLY_TOOLS = frozenset(
    tool for tools in TOOL_LANES.values() for tool in tools if tool not in WRITE_TOOLS
)

_DENY_TEXT: dict[str, str] = {
    "refused": "I'm not going to do that.",
    "high_risk": (
        "That is risky enough that I won't run it on my own. Say it again with a "
        "clear confirm if you really mean it."
    ),
    "cancelled": "Okay — I won't run that.",
    "tool_not_allowed": (
        "I'd rather answer that than act on it. Tell me plainly what to do and I'll run it."
    ),
    "lane_mismatch": "That didn't read like an instruction to run that. Say it more directly.",
    "no_tool_lane": "That read as a question rather than an instruction, so I didn't run anything.",
}


def lane_for_tool(tool: str) -> str:
    """Lane label for a registry tool, or ``""`` when it is not classified.

    Workspace skills are registered at runtime from ``workspace/skills/``, so
    they are resolved through the registry rather than the static map.
    """
    name = str(tool or "").strip()
    lane = _TOOL_TO_LANE.get(name)
    if lane is not None:
        return lane
    if not name:
        return ""
    try:
        from hearth.agent.registry import registry

        spec = registry.get(name)
    except Exception:  # noqa: BLE001 — classification must not raise into a call
        return ""
    if spec is not None and str(getattr(spec, "source", "")).startswith("workspace:"):
        return "files"
    return ""


def tools_for_lane(lane: str) -> tuple[str, ...]:
    return TOOL_LANES.get(str(lane or "").strip(), ())


def is_write_tool(tool: str) -> bool:
    """Whether a tool has side effects worth gating.

    Known reads are never treated as writes. Anything Hearth cannot classify is
    treated as a write so a newly registered tool is gated by default rather
    than silently ungoverned.
    """
    name = str(tool or "").strip()
    if not name:
        return False
    if name in WRITE_TOOLS:
        return True
    if name in READ_ONLY_TOOLS:
        return False
    try:
        from hearth.agent.registry import registry

        spec = registry.get(name)
    except Exception:  # noqa: BLE001 — classification must not raise into a call
        spec = None
    if spec is not None and getattr(spec, "destructive", False):
        return True
    return True


@dataclass(frozen=True, slots=True)
class ToolDecision:
    """What the gate decided about one tool call, and what it would have decided."""

    tool: str
    lane: str
    write: bool
    enabled: bool
    shadow: bool
    ok: bool
    # What enforce mode would do (always computed, also in shadow).
    suggested: ToolAction = "allow"
    # What the caller must actually do this turn.
    action: ToolAction = "allow"
    reason: str = ""
    chosen_lane: str = ""
    lane_confidence: float = 0.0
    risk_level: str = ""
    needs_llm: bool = False
    error: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def denied(self) -> bool:
        return self.action == "deny"

    @property
    def needs_confirm(self) -> bool:
        return self.action == "confirm"

    @property
    def enforced(self) -> bool:
        """True when this decision actually changed what Hearth did."""
        return self.action != "allow"

    @property
    def message(self) -> str:
        """House-voice sentence explaining a deny."""
        base = _DENY_TEXT.get(self.reason, "I didn't run that.")
        if self.reason == "lane_mismatch" and self.chosen_lane:
            readable = self.chosen_lane.replace("_", " ")
            return f"{base} I read it as {readable}."
        return base

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "lane": self.lane or None,
            "write": self.write,
            "enabled": self.enabled,
            "shadow": self.shadow,
            "ok": self.ok,
            "suggested": self.suggested,
            "action": self.action,
            "reason": self.reason,
            "chosen_lane": self.chosen_lane or None,
            "lane_confidence": round(self.lane_confidence, 4) or None,
            "risk": self.risk_level or None,
            "needs_llm": self.needs_llm,
            "error": self.error or None,
        }


def _allow(
    tool: str,
    *,
    reason: str,
    lane: str = "",
    write: bool = False,
    ok: bool = True,
    error: str = "",
) -> ToolDecision:
    return ToolDecision(
        tool=tool,
        lane=lane,
        write=write,
        enabled=bool(settings.jev_enabled),
        shadow=bool(settings.jev_shadow),
        ok=ok,
        suggested="allow",
        action="allow",
        reason=reason,
        error=error,
    )


@dataclass
class ToolTurn:
    """Per-turn scope: the shared answer set plus the decisions taken from it."""

    said: str
    channel: str
    recent: tuple[str, ...] = ()
    verdict: JevVerdict | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def answers(self) -> JevAnswers | None:
        if self.verdict is None or not self.verdict.ok:
            return None
        return self.verdict.answers

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "mode": settings.jev_mode,
            "decisions": list(self.decisions),
        }


_turn: contextvars.ContextVar[ToolTurn | None] = contextvars.ContextVar(
    "hearth_jev_tool_turn",
    default=None,
)


def current_turn() -> ToolTurn | None:
    return _turn.get()


@contextmanager
def tool_turn(
    said: str,
    *,
    channel: str,
    recent: Sequence[str] | None = None,
) -> Iterator[ToolTurn]:
    """Open a tool-gate scope so one Jev call covers the whole turn."""
    scope = ToolTurn(
        said=(said or "").strip(),
        channel=channel,
        recent=tuple(str(item) for item in (recent or ()) if str(item).strip()),
    )
    token = _turn.set(scope)
    try:
        yield scope
    finally:
        if scope.decisions:
            log.info("jev.tool_turn %s", scope.as_log_dict())
        _turn.reset(token)


def adopt_verdict(verdict: JevVerdict | None) -> None:
    """Share a turn verdict the caller already paid for with the tool gate."""
    scope = _turn.get()
    if scope is None or verdict is None:
        return
    if verdict.ok and verdict.answers is not None:
        scope.verdict = verdict


def turn_lane(*, confidence_min: float | None = None) -> tuple[str, float] | None:
    """Jev's tool lane for the open turn, read locally with no network call."""
    scope = _turn.get()
    if scope is None:
        return None
    return lane_choice(scope.answers, confidence_min=confidence_min)


def lane_choice(
    answers: JevAnswers | None,
    *,
    confidence_min: float | None = None,
) -> tuple[str, float] | None:
    """``(lane, confidence)`` when Jev's tool_lane Choice clears the threshold."""
    if answers is None or answers.tool_lane is None:
        return None
    floor = (
        settings.jev_tool_lane_confidence if confidence_min is None else float(confidence_min)
    )
    lane = str(answers.tool_lane.choice or "").strip()
    confidence = float(answers.tool_lane.confidence)
    if lane not in TOOL_LANE_CRITERIA or confidence < floor:
        return None
    return lane, confidence


def suggest_tool_action(
    answers: JevAnswers | None,
    *,
    tool: str,
    lane: str | None = None,
    write: bool | None = None,
    explicit_confirm: bool = False,
    domain_confidence: float | None = None,
    cancel_threshold: float | None = None,
    allow_threshold: float | None = None,
    lane_confidence: float | None = None,
    risk_confidence: float | None = None,
) -> tuple[ToolAction, str]:
    """Map a typed answer set onto allow / deny / confirm for one tool.

    Pure and synchronous so the policy can be unit-tested without a network or a
    turn scope. Fail-open by construction: every branch that cannot reach a
    confident conclusion returns ``allow``.
    """
    if answers is None:
        return "allow", "no_answers"

    tool_lane = lane if lane is not None else lane_for_tool(tool)
    is_write = is_write_tool(tool) if write is None else bool(write)

    domain_min = (
        settings.jev_domain_confidence if domain_confidence is None else float(domain_confidence)
    )
    cancel_min = (
        settings.jev_cancel_threshold if cancel_threshold is None else float(cancel_threshold)
    )
    allow_min = (
        settings.jev_tool_allow_threshold if allow_threshold is None else float(allow_threshold)
    )
    lane_min = (
        settings.jev_tool_lane_confidence if lane_confidence is None else float(lane_confidence)
    )
    risk_min = settings.jev_risk_confidence if risk_confidence is None else float(risk_confidence)

    # Hard stops apply to every tool, read or write, tap or typed.
    if (
        answers.domain is not None
        and answers.domain.choice == "refuse"
        and answers.domain.confidence >= domain_min
    ):
        return "deny", "refused"
    if (
        answers.risk is not None
        and answers.risk.level == "do_not_auto_run"
        and answers.risk.confidence >= risk_min
    ):
        return "deny", "high_risk"

    # A button tap or an explicit yes already is the confirm.
    if explicit_confirm:
        return "allow", "explicit_confirm"
    if not is_write:
        return "allow", "read_only"

    if answers.is_cancel is not None and answers.is_cancel.noul >= cancel_min:
        return "deny", "cancelled"
    if answers.tool_allow is not None and answers.tool_allow.noul < allow_min:
        return "deny", "tool_not_allowed"

    picked = lane_choice(answers, confidence_min=lane_min)
    if picked is not None and tool_lane:
        chosen, _ = picked
        if chosen == "no_tool":
            return "deny", "no_tool_lane"
        if chosen != tool_lane:
            return "deny", "lane_mismatch"

    if (
        answers.risk is not None
        and answers.risk.level == "needs_confirm"
        and answers.risk.confidence >= risk_min
    ):
        return "confirm", "needs_confirm"

    return "allow", "pass"


async def _turn_verdict(
    *,
    said: str,
    recent: Sequence[str] | None,
    client: SystemOneClient | None,
) -> JevVerdict:
    """Fetch (once per turn) the answer set the gate decides from."""
    scope = _turn.get()
    if scope is None:
        return await evaluate_message(
            said,
            recent=list(recent or ()),
            client=client,
            questions=tool_gate_questions(),
        )

    if scope.verdict is not None:
        return scope.verdict
    # One in-flight call per turn even when tools run concurrently.
    async with scope.lock:
        if scope.verdict is not None:
            return scope.verdict
        verdict = await evaluate_message(
            said,
            recent=list(recent if recent is not None else scope.recent),
            client=client,
            questions=tool_gate_questions(),
        )
        scope.verdict = verdict
        return verdict


async def authorize_tool(
    tool: str,
    args: Mapping[str, Any] | None = None,
    *,
    said: str | None = None,
    recent: Sequence[str] | None = None,
    channel: str = "",
    explicit_confirm: bool = False,
    client: SystemOneClient | None = None,
) -> ToolDecision:
    """Decide one tool call. Never raises; every failure path allows the call."""
    name = str(tool or "").strip()
    lane = lane_for_tool(name)
    write = is_write_tool(name)

    if not settings.jev_enabled:
        return _allow(name, reason="disabled", lane=lane, write=write)
    if not settings.jev_tool_gate:
        return _allow(name, reason="gate_off", lane=lane, write=write)
    if not settings.typesafe_configured:
        return _allow(name, reason="missing_api_key", lane=lane, write=write, ok=False)

    scope = _turn.get()
    text = (said if said is not None else (scope.said if scope is not None else "")).strip()
    if not text and (scope is None or scope.verdict is None):
        # No state to reason about — gating here would be guessing, not governing.
        return _allow(name, reason="no_state", lane=lane, write=write, ok=False)

    try:
        verdict = await _turn_verdict(said=text, recent=recent, client=client)
    except Exception as exc:  # noqa: BLE001 — fail open to today's behavior
        log.warning("jev.tool_gate fail-open: %s", type(exc).__name__)
        return _allow(
            name,
            reason="gate_error",
            lane=lane,
            write=write,
            ok=False,
            error=type(exc).__name__,
        )

    answers = verdict.answers if verdict.ok else None
    suggested, reason = suggest_tool_action(
        answers,
        tool=name,
        lane=lane,
        write=write,
        explicit_confirm=explicit_confirm,
    )
    if not verdict.ok:
        reason = verdict.reason or "fail_open"
    picked = lane_choice(answers)
    decision = ToolDecision(
        tool=name,
        lane=lane,
        write=write,
        enabled=True,
        shadow=bool(verdict.shadow),
        ok=bool(verdict.ok),
        suggested=suggested,
        action=suggested if verdict.enforcing else "allow",
        reason=reason,
        chosen_lane=picked[0] if picked else "",
        lane_confidence=picked[1] if picked else 0.0,
        risk_level=(answers.risk.level if answers is not None and answers.risk else ""),
        needs_llm=bool(
            answers is not None and answers.needs_llm is not None and answers.needs_llm.yes
        ),
        error=verdict.error,
    )

    payload = decision.as_log_dict()
    payload["channel"] = channel or (scope.channel if scope is not None else "")
    payload["arg_keys"] = sorted(str(key) for key in (args or {}))
    if scope is not None:
        scope.decisions.append(payload)
    log.info("jev.tool_gate %s", payload)
    return decision


async def choose_tool_lane(
    said: str,
    *,
    recent: Sequence[str] | None = None,
    client: SystemOneClient | None = None,
) -> tuple[str, float] | None:
    """Ask Jev which tool family should serve this message (``None`` = unsure)."""
    if not settings.jev_active:
        return None
    text = (said or "").strip()
    if not text:
        return None
    try:
        verdict = await _turn_verdict(said=text, recent=recent, client=client)
    except Exception:  # noqa: BLE001 — fail open to the local router
        log.warning("jev.tool_lane fail-open", exc_info=True)
        return None
    if not verdict.ok:
        return None
    return lane_choice(verdict.answers)


def status_snapshot() -> dict[str, Any]:
    """Operator-facing view of the gate. Never includes the API key."""
    return {
        "enabled": bool(settings.jev_enabled),
        "active": bool(settings.jev_active),
        "mode": settings.jev_mode,
        "tool_gate": bool(settings.jev_tool_gate),
        "route_local_tools": bool(settings.jev_route_local_tools),
        "key_configured": bool(settings.typesafe_configured),
        "model": settings.jev_model,
        "timeout_seconds": float(settings.jev_timeout_seconds),
        "lanes": list(TOOL_LANES),
        "thresholds": {
            "tool_allow": float(settings.jev_tool_allow_threshold),
            "tool_lane_confidence": float(settings.jev_tool_lane_confidence),
            "risk_confidence": float(settings.jev_risk_confidence),
            "cancel": float(settings.jev_cancel_threshold),
            "confirm": float(settings.jev_confirm_threshold),
            "domain_confidence": float(settings.jev_domain_confidence),
            "media_ask_confidence": float(settings.jev_media_ask_confidence),
            "needs_llm": float(settings.jev_needs_llm_threshold),
        },
    }


__all__ = [
    "READ_ONLY_TOOLS",
    "TOOL_LANES",
    "WRITE_TOOLS",
    "ToolDecision",
    "ToolTurn",
    "adopt_verdict",
    "authorize_tool",
    "choose_tool_lane",
    "current_turn",
    "is_write_tool",
    "lane_choice",
    "lane_for_tool",
    "status_snapshot",
    "suggest_tool_action",
    "tool_turn",
    "tools_for_lane",
    "turn_lane",
]
