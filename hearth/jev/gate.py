"""Cheap Jev decision gate before the expensive agent / Telegram tool loop.

Shadow mode keeps cancel/confirm/CoS governance advisory. Media lane routing
and the shared allow/which-tool gate are first-class whenever Jev answers with
enough confidence. Disabled/unavailable/error/low-confidence Jev fails open.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from hearth.config import settings
from hearth.jev.client import SystemOneClient, build_system_one_client
from hearth.jev.schema import (
    EnforceAction,
    JevAnswers,
    JevVerdict,
    NO_TOOL,
    QUEUE_TOOLS,
    hearth_system_one_questions,
    telegram_media_system_one_questions,
    tool_call_system_one_questions,
)
from hearth.memory.redact import redact

log = logging.getLogger("hearth.jev")

_client: SystemOneClient | None = None


@dataclass(frozen=True, slots=True)
class ToolGateDecision:
    """Authoritative Jev allow/which-tool result, or an explicit fail-open."""

    allowed: bool
    requested_tool: str
    selected_tool: str
    fail_open: bool
    reason: str
    verdict: JevVerdict

    @property
    def rerouted(self) -> bool:
        return bool(
            self.allowed
            and self.selected_tool
            and self.selected_tool != self.requested_tool
        )

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requested_tool": self.requested_tool,
            "selected_tool": self.selected_tool or None,
            "rerouted": self.rerouted,
            "fail_open": self.fail_open,
            "reason": self.reason,
            "jev": self.verdict.as_log_dict(),
        }


_tool_gate_context: ContextVar[ToolGateDecision | None] = ContextVar(
    "hearth_tool_gate",
    default=None,
)


def current_tool_gate() -> ToolGateDecision | None:
    """Decision authorizing the currently executing registry tool, if any."""
    return _tool_gate_context.get()


@contextmanager
def tool_gate_scope(
    decision: ToolGateDecision | None,
) -> Iterator[ToolGateDecision | None]:
    """Propagate one gate decision through nested backend/service calls."""
    if decision is None:
        yield None
        return
    token = _tool_gate_context.set(decision)
    try:
        yield decision
    finally:
        _tool_gate_context.reset(token)


def reset_client() -> None:
    """Drop the cached client (tests / config reload)."""
    global _client
    _client = None


def get_client() -> SystemOneClient:
    global _client
    if _client is None:
        _client = build_system_one_client()
    return _client


def set_client(client: SystemOneClient | None) -> None:
    """Inject a mock client for tests."""
    global _client
    _client = client


def build_state(user_text: str, *, recent: list[str] | None = None) -> dict[str, Any]:
    """Minimal state: latest user message + optional short recent context."""
    message = redact((user_text or "").strip())[:500]
    state: dict[str, Any] = {"user_message": message}
    if recent:
        clipped = [redact(str(item).strip())[:160] for item in recent if str(item).strip()]
        if clipped:
            state["recent_context"] = clipped[-4:]
    return state


def suggest_action(
    answers: JevAnswers,
    *,
    domain_confidence: float | None = None,
    cancel_threshold: float | None = None,
    confirm_threshold: float | None = None,
) -> tuple[EnforceAction, str]:
    """Map typed answers + thresholds → suggested enforce action (fail-open)."""
    domain_min = (
        settings.jev_domain_confidence
        if domain_confidence is None
        else float(domain_confidence)
    )
    cancel_min = (
        settings.jev_cancel_threshold if cancel_threshold is None else float(cancel_threshold)
    )
    # confirm_threshold is used by the Telegram pending-guess path, not agent routing.
    _ = confirm_threshold if confirm_threshold is not None else settings.jev_confirm_threshold

    if answers.is_cancel is not None and answers.is_cancel.noul >= cancel_min:
        return "block_cancel", "high_confidence_cancel"

    if answers.domain is not None and answers.domain.choice == "escalate_cos":
        if answers.domain.confidence >= domain_min:
            return "escalate_cos", "high_confidence_escalate_cos"

    if answers.domain is not None and answers.domain.choice == "refuse":
        if answers.domain.confidence >= domain_min:
            return "block_cancel", "high_confidence_refuse"

    return "continue", "pass"


async def evaluate_message(
    user_text: str,
    *,
    recent: list[str] | None = None,
    client: SystemOneClient | None = None,
    questions: dict[str, dict[str, Any]] | None = None,
    state_extra: Mapping[str, Any] | None = None,
) -> JevVerdict:
    """Run one parallel System One call (all questions in one request).

    Disabled / missing key → continue without calling the network.
    Errors → fail open (continue) with ok=False.
    """
    enabled = bool(settings.jev_enabled)
    shadow = bool(settings.jev_shadow)
    if not enabled:
        return JevVerdict(
            enabled=False,
            shadow=shadow,
            ok=True,
            suggested="continue",
            action="continue",
            reason="disabled",
        )
    if not settings.typesafe_configured:
        return JevVerdict(
            enabled=True,
            shadow=shadow,
            ok=False,
            suggested="continue",
            action="continue",
            reason="missing_api_key",
            error="TYPESAFE_API_KEY not set",
        )

    text = (user_text or "").strip()
    if not text:
        return JevVerdict(
            enabled=True,
            shadow=shadow,
            ok=True,
            suggested="continue",
            action="continue",
            reason="empty_message",
        )

    try:
        active = client or get_client()
        state = build_state(text, recent=recent)
        if state_extra:
            for key, value in state_extra.items():
                name = str(key).strip()
                if not name:
                    continue
                if isinstance(value, str):
                    serialized = value
                else:
                    try:
                        serialized = json.dumps(value, default=str, sort_keys=True)
                    except (TypeError, ValueError):
                        serialized = str(value)
                state[name] = redact(serialized)[:500]
        answers = await active.system_one(
            state=state,
            questions=questions or hearth_system_one_questions(),
            model=settings.jev_model,
        )
        suggested, reason = suggest_action(answers)
        action: EnforceAction = suggested if (enabled and not shadow) else "continue"
        verdict = JevVerdict(
            enabled=True,
            shadow=shadow,
            ok=True,
            answers=answers,
            suggested=suggested,
            action=action,
            reason=reason,
        )
        log.info("jev.gate %s", verdict.as_log_dict())
        return verdict
    except Exception as exc:  # noqa: BLE001 — fail open to today's behavior
        log.warning("jev.gate fail-open: %s", type(exc).__name__)
        return JevVerdict(
            enabled=True,
            shadow=shadow,
            ok=False,
            suggested="continue",
            action="continue",
            reason="api_error",
            error=type(exc).__name__,
        )


async def evaluate_tool_call(
    user_text: str,
    *,
    proposed_tool: str,
    args: Mapping[str, Any] | None = None,
    allowed_tools: Mapping[str, str],
    channel: str = "tool",
    recent: list[str] | None = None,
    client: SystemOneClient | None = None,
) -> ToolGateDecision:
    """Ask Jev whether a tool may run and which listed tool owns the turn.

    Tool routing is first-class even when governance is in shadow mode. Disabled
    Jev, missing credentials, API errors, missing answers, and low-confidence
    choices fail open to ``proposed_tool``.
    """
    requested = str(proposed_tool or "").strip()
    tools = {
        str(name).strip(): str(description or f"Run the {name} Hearth tool.")
        for name, description in allowed_tools.items()
        if str(name).strip()
    }
    if requested and requested not in tools:
        tools[requested] = f"Run the proposed {requested} Hearth tool."

    verdict = await evaluate_message(
        user_text or requested,
        recent=recent,
        client=client,
        questions=tool_call_system_one_questions(tools),
        state_extra={
            "channel": channel,
            "proposed_tool": requested,
            "tool_arguments": dict(args or {}),
        },
    )

    def finish(
        *,
        allowed: bool,
        selected_tool: str,
        fail_open: bool,
        reason: str,
    ) -> ToolGateDecision:
        decision = ToolGateDecision(
            allowed=allowed,
            requested_tool=requested,
            selected_tool=selected_tool,
            fail_open=fail_open,
            reason=reason,
            verdict=verdict,
        )
        log.info("jev.tool_gate %s", decision.as_log_dict())
        return decision

    if not verdict.enabled:
        return finish(
            allowed=True,
            selected_tool=requested,
            fail_open=True,
            reason="disabled",
        )
    if not verdict.ok or verdict.answers is None:
        return finish(
            allowed=True,
            selected_tool=requested,
            fail_open=True,
            reason=verdict.reason or "unavailable",
        )

    allow_answer = verdict.answers.allow_tool
    which_answer = verdict.answers.which_tool
    if allow_answer is None or which_answer is None:
        return finish(
            allowed=True,
            selected_tool=requested,
            fail_open=True,
            reason="missing_tool_gate_answers",
        )
    if float(allow_answer.noul) < float(settings.jev_tool_allow_threshold):
        return finish(
            allowed=False,
            selected_tool="",
            fail_open=False,
            reason="jev_denied_tool",
        )

    selected = str(which_answer.choice or "").strip()
    if selected == NO_TOOL:
        return finish(
            allowed=False,
            selected_tool="",
            fail_open=False,
            reason="jev_selected_no_tool",
        )
    if float(which_answer.confidence) < float(settings.jev_tool_confidence):
        return finish(
            allowed=True,
            selected_tool=requested,
            fail_open=True,
            reason="low_tool_confidence",
        )
    if selected not in tools:
        return finish(
            allowed=True,
            selected_tool=requested,
            fail_open=True,
            reason="unknown_tool_choice",
        )
    return finish(
        allowed=True,
        selected_tool=selected,
        fail_open=False,
        reason="jev_selected_tool",
    )


async def evaluate_telegram_media(
    user_text: str,
    *,
    recent: list[str] | None = None,
    client: SystemOneClient | None = None,
) -> JevVerdict:
    """Jev-first Telegram media router (media_ask + needs_llm + confirm/cancel).

    Same fail-open contract as ``evaluate_message``. Callers use ``media_ask``
    for routing whenever the verdict is ok and confidence clears the media
    threshold — this is first-class product routing, not shadow-only logging.
    Cancel/confirm enforcement still respects ``jev_shadow``.
    """
    return await evaluate_message(
        user_text,
        recent=recent,
        client=client,
        questions=telegram_media_system_one_questions(),
    )


def media_ask_choice(
    answers: JevAnswers | None,
    *,
    confidence_min: float | None = None,
) -> tuple[str, float] | None:
    """Return ``(media_ask choice, confidence)`` when above threshold."""
    if answers is None or answers.media_ask is None:
        return None
    floor = (
        settings.jev_media_ask_confidence
        if confidence_min is None
        else float(confidence_min)
    )
    choice = str(answers.media_ask.choice or "").strip()
    conf = float(answers.media_ask.confidence)
    if not choice or conf < floor:
        return None
    return choice, conf


def needs_llm_resolve(
    answers: JevAnswers | None,
    *,
    media_choice: str | None = None,
    threshold: float | None = None,
) -> bool:
    """True when Jev says an LLM hop is required (or media_ask is a riddle/Q&A)."""
    if media_choice in {"descriptive_riddle", "chat_about_title"}:
        return True
    if answers is None:
        return False
    floor = (
        settings.jev_needs_llm_threshold if threshold is None else float(threshold)
    )
    if answers.needs_llm is not None and float(answers.needs_llm.noul) >= floor:
        return True
    return False


def log_shadow_outcome(
    verdict: JevVerdict,
    *,
    channel: str,
    tools: list[str] | None = None,
    outcome: str = "",
) -> None:
    """Structured compare of Jev suggestion vs what Hearth actually did."""
    if not verdict.enabled:
        return
    tool_names = [str(t) for t in (tools or []) if t]
    queued = [name for name in tool_names if name in QUEUE_TOOLS]
    log.info(
        "jev.shadow_outcome %s",
        {
            "channel": channel,
            "shadow": verdict.shadow,
            "ok": verdict.ok,
            "suggested": verdict.suggested,
            "action_taken": verdict.action,
            "reason": verdict.reason,
            "tools": tool_names,
            "queued_tools": queued,
            "outcome": outcome or None,
            "answers": verdict.answers.as_log_dict() if verdict.answers else None,
        },
    )


def noul_high(answers: JevAnswers | None, field: str, threshold: float) -> bool:
    if answers is None:
        return False
    value = getattr(answers, field, None)
    if value is None:
        return False
    return float(value.noul) >= float(threshold)


__all__ = [
    "QUEUE_TOOLS",
    "ToolGateDecision",
    "build_state",
    "current_tool_gate",
    "evaluate_message",
    "evaluate_tool_call",
    "evaluate_telegram_media",
    "get_client",
    "log_shadow_outcome",
    "media_ask_choice",
    "needs_llm_resolve",
    "noul_high",
    "reset_client",
    "set_client",
    "suggest_action",
    "tool_gate_scope",
]
