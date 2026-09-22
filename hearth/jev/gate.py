"""Cheap Jev decision gate before the expensive agent / Telegram tool loop.

Shadow mode (default when enabled): log typed answers; never change behavior.
Enforce mode: high-confidence cancel blocks queue tools; high-confidence
escalate_cos prefers Chief of Staff; API errors and low confidence fail open.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from hearth.config import settings
from hearth.jev.client import SystemOneClient, build_system_one_client
from hearth.jev.schema import (
    DEVICE_TOOLS,
    EnforceAction,
    JevAnswers,
    JevVerdict,
    QUEUE_TOOLS,
    hearth_system_one_questions,
    house_device_system_one_questions,
    telegram_media_system_one_questions,
)
from hearth.memory.redact import redact

log = logging.getLogger("hearth.jev")

_client: SystemOneClient | None = None

# The utterance that caused the current turn. Surfaces that own the user's words
# (chat, voice, Telegram) publish it here so a tool gate deep in the registry can
# judge the real sentence instead of the tool arguments it was flattened into.
_utterance: ContextVar[str] = ContextVar("hearth_jev_utterance", default="")


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


def set_utterance(text: str) -> None:
    """Record the user sentence driving this turn (chat / voice / Telegram)."""
    _utterance.set((text or "").strip())


def current_utterance() -> str:
    return _utterance.get()


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
        answers = await active.system_one(
            state=build_state(text, recent=recent),
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


async def evaluate_house_device(
    user_text: str,
    *,
    recent: list[str] | None = None,
    client: SystemOneClient | None = None,
) -> JevVerdict:
    """Gate in front of the physical device layer (feeder / airco / purifier).

    Same fail-open contract as ``evaluate_message``: disabled Jev, a missing key,
    or an API error all return ``continue``. Enforce mode is what turns a
    high-confidence cancel into a refusal to dispense food or start the airco.
    """
    return await evaluate_message(
        user_text,
        recent=recent,
        client=client,
        questions=house_device_system_one_questions(),
    )


@dataclass(frozen=True, slots=True)
class ToolGate:
    """What the shared gate decided about one house-device tool call."""

    allowed: bool
    reason: str = "pass"
    message: str = ""
    verdict: JevVerdict | None = None

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "verdict": self.verdict.as_log_dict() if self.verdict else None,
        }


_GATE_BLOCK_MESSAGE = (
    "Okay — I won't touch that device. Say it again plainly if you did mean it."
)


async def guard_tool_call(
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    said: str = "",
    client: SystemOneClient | None = None,
) -> ToolGate:
    """Run the shared Jev gate before a physical house-device tool executes.

    Called from the tool registry for every ``jev_gated`` spec, so chat, voice,
    Telegram, and ``/api/invoke`` all pass through the same decision instead of
    each surface inventing its own guard. Fails open in every ambiguous case —
    a flaky System One call must not leave the pets unfed.
    """
    if tool not in DEVICE_TOOLS:
        return ToolGate(allowed=True, reason="not_gated")
    if not settings.jev_enabled:
        return ToolGate(allowed=True, reason="disabled")

    payload = args or {}
    text = (said or str(payload.get("said") or "") or current_utterance()).strip()
    if not text:
        # No sentence to judge (a bare API/tool invocation). The caller is
        # already trusted by auth; Jev has nothing to add.
        return ToolGate(allowed=True, reason="no_utterance")

    verdict = await evaluate_house_device(text, client=client)
    log_shadow_outcome(verdict, channel="house_device", tools=[tool], outcome=tool)
    if not verdict.enforcing or not verdict.ok:
        return ToolGate(allowed=True, reason=verdict.reason or "fail_open", verdict=verdict)

    answers = verdict.answers
    if noul_high(answers, "is_cancel", settings.jev_cancel_threshold):
        return ToolGate(
            allowed=False,
            reason="high_confidence_cancel",
            message=_GATE_BLOCK_MESSAGE,
            verdict=verdict,
        )
    if (
        answers is not None
        and answers.risk is not None
        and answers.risk.level == "do_not_auto_run"
        and answers.risk.confidence >= settings.jev_device_confidence
    ):
        return ToolGate(
            allowed=False,
            reason="high_risk",
            message=(
                "That reads as something I should not run on the house hardware "
                "without a clearer instruction."
            ),
            verdict=verdict,
        )
    return ToolGate(allowed=True, reason="pass", verdict=verdict)


def device_ask_choice(
    answers: JevAnswers | None,
    *,
    confidence_min: float | None = None,
) -> tuple[str, float] | None:
    """Return ``(device_ask choice, confidence)`` when above threshold."""
    if answers is None or answers.device_ask is None:
        return None
    floor = (
        settings.jev_device_confidence if confidence_min is None else float(confidence_min)
    )
    choice = str(answers.device_ask.choice or "").strip()
    conf = float(answers.device_ask.confidence)
    if not choice or conf < floor:
        return None
    return choice, conf


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
    "DEVICE_TOOLS",
    "QUEUE_TOOLS",
    "ToolGate",
    "build_state",
    "current_utterance",
    "device_ask_choice",
    "evaluate_house_device",
    "evaluate_message",
    "evaluate_telegram_media",
    "get_client",
    "guard_tool_call",
    "log_shadow_outcome",
    "media_ask_choice",
    "needs_llm_resolve",
    "noul_high",
    "reset_client",
    "set_client",
    "set_utterance",
    "suggest_action",
]
