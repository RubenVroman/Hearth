"""Cheap Jev decision gate before the expensive agent / Telegram tool loop.

Shadow mode (default when enabled): log typed answers; never change behavior.
Enforce mode: high-confidence cancel blocks queue tools; high-confidence
escalate_cos prefers Chief of Staff; API errors and low confidence fail open.
"""

from __future__ import annotations

import logging
from typing import Any

from hearth.config import settings
from hearth.jev.client import SystemOneClient, build_system_one_client
from hearth.jev.schema import (
    EnforceAction,
    JevAnswers,
    JevVerdict,
    QUEUE_TOOLS,
    hearth_system_one_questions,
)
from hearth.memory.redact import redact

log = logging.getLogger("hearth.jev")

_client: SystemOneClient | None = None


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
            questions=hearth_system_one_questions(),
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
    "build_state",
    "evaluate_message",
    "get_client",
    "log_shadow_outcome",
    "noul_high",
    "reset_client",
    "set_client",
    "suggest_action",
]
