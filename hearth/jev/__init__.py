"""TypeSafe Jev (System One) decision / governance sandbox for Hearth.

Jev is not an LLM and does not generate text. It returns typed Choice / Noul /
Score answers used as a cheap gate before the OpenAI agent tool loop, and as
the **first-class Telegram media intent router** (exact title vs riddle vs
series vs edition vs chat-about).
"""

from __future__ import annotations

from hearth.jev.gate import (
    ToolGate,
    current_utterance,
    device_ask_choice,
    evaluate_house_device,
    evaluate_message,
    evaluate_telegram_media,
    guard_tool_call,
    log_shadow_outcome,
    media_ask_choice,
    needs_llm_resolve,
    noul_high,
    reset_client,
    set_client,
    set_utterance,
)
from hearth.jev.schema import (
    DEVICE_ASK_CRITERIA,
    DEVICE_ASK_KINDS,
    DEVICE_TOOLS,
    MEDIA_ASK_CRITERIA,
    MEDIA_ASK_KINDS,
    JevAnswers,
    JevVerdict,
    QUEUE_TOOLS,
)

__all__ = [
    "DEVICE_ASK_CRITERIA",
    "DEVICE_ASK_KINDS",
    "DEVICE_TOOLS",
    "MEDIA_ASK_CRITERIA",
    "MEDIA_ASK_KINDS",
    "JevAnswers",
    "JevVerdict",
    "QUEUE_TOOLS",
    "ToolGate",
    "current_utterance",
    "device_ask_choice",
    "evaluate_house_device",
    "evaluate_message",
    "evaluate_telegram_media",
    "guard_tool_call",
    "log_shadow_outcome",
    "media_ask_choice",
    "needs_llm_resolve",
    "noul_high",
    "reset_client",
    "set_client",
    "set_utterance",
]
