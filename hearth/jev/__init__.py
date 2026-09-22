"""TypeSafe Jev (System One) decision / governance layer for Hearth.

Jev is not an LLM and does not generate text. It returns typed Choice / Noul /
Score answers, and Hearth uses them for **every tool-calling decision**:

* :func:`authorize_tool` gates each house tool call — allow, deny, or escalate to
  a confirm — from one typed answer set per turn.
* :func:`choose_tool_lane` / :func:`turn_lane` pick which family of tools should
  run, so the local router follows Jev instead of regex precedence alone.
* :func:`evaluate_telegram_media` is the first-class Telegram media intent router
  (exact title vs riddle vs series vs edition vs chat-about).

Everything fails open: no key, no state, an API error, or a timeout leaves
Hearth behaving exactly as it did before the gate existed.
"""

from __future__ import annotations

from hearth.jev.gate import (
    evaluate_message,
    evaluate_telegram_media,
    log_shadow_outcome,
    media_ask_choice,
    needs_llm_resolve,
    noul_high,
    reset_client,
    set_client,
)
from hearth.jev.schema import (
    MEDIA_ASK_CRITERIA,
    MEDIA_ASK_KINDS,
    TOOL_LANE_CRITERIA,
    TOOL_LANES,
    JevAnswers,
    JevVerdict,
    QUEUE_TOOLS,
)
from hearth.jev.tools import (
    WRITE_TOOLS,
    ToolDecision,
    adopt_verdict,
    authorize_tool,
    choose_tool_lane,
    is_write_tool,
    lane_for_tool,
    status_snapshot,
    suggest_tool_action,
    tool_turn,
    turn_lane,
)

__all__ = [
    "MEDIA_ASK_CRITERIA",
    "MEDIA_ASK_KINDS",
    "TOOL_LANES",
    "TOOL_LANE_CRITERIA",
    "WRITE_TOOLS",
    "JevAnswers",
    "JevVerdict",
    "QUEUE_TOOLS",
    "ToolDecision",
    "adopt_verdict",
    "authorize_tool",
    "choose_tool_lane",
    "evaluate_message",
    "evaluate_telegram_media",
    "is_write_tool",
    "lane_for_tool",
    "log_shadow_outcome",
    "media_ask_choice",
    "needs_llm_resolve",
    "noul_high",
    "reset_client",
    "set_client",
    "status_snapshot",
    "suggest_tool_action",
    "tool_turn",
    "turn_lane",
]
