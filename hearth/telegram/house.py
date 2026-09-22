"""House-device lane for Telegram: pet feeder, airco, air purifier.

Sits in front of the Overseerr media router. A message only enters this lane
when the shared deterministic matcher recognises it, so ordinary titles keep
going to the catalog — "Cats" stays a film and "feed the cats" becomes a meal.

Every command is executed through the tool registry rather than by calling the
device functions directly. That is deliberate: the registry is where the shared
``hearth/jev`` gate lives, so a Telegram message reaches the hardware through
exactly the same governance decision as chat and voice.
"""

from __future__ import annotations

import logging

from hearth.agent.registry import registry
from hearth.jev import set_utterance
from hearth.telegram.models import BotReply
from hearth.tools.device_intent import DevicePlan, match_device_phrase

log = logging.getLogger("hearth.telegram.house")

HOUSE_HELP = (
    "House devices: “feed the cats”, “airco 21”, “purifier on”, “is the airco on”. "
    "Also /feed, /airco, /purifier, /devices."
)


def detect_house_device(text: str) -> DevicePlan | None:
    """Recognise a feeder / airco / purifier command, or None to stay on media."""
    return match_device_phrase(text)


async def run_house_device(plan: DevicePlan, said: str) -> BotReply:
    """Execute one device command and phrase the result for Telegram."""
    # The gate inside the registry reads this when the tool arguments alone are
    # not enough to judge what was asked.
    set_utterance(said)
    call = plan.as_plan(said)
    result = await registry.call(call["tool"], call["args"])
    data = result.data if isinstance(result.data, dict) else {}

    if data.get("blocked_by") == "jev":
        return BotReply(str(data.get("speak") or "Okay — leaving the house hardware alone."))

    spoken = str(data.get("speak") or "").strip()
    if spoken:
        if not result.ok and data.get("hint") and str(data["hint"]) not in spoken:
            return BotReply(f"{spoken}\n\n{data['hint']}")
        return BotReply(spoken)

    if result.ok:
        return BotReply("Done.")
    error = str(data.get("error") or "That did not work.")
    log.info("telegram.house_device_failed %s", {"tool": call["tool"], "error": error})
    return BotReply(error)


async def house_device_reply(text: str) -> BotReply | None:
    """Full lane: match, run, reply — or None when this is not a device ask."""
    plan = detect_house_device(text)
    if plan is None:
        return None
    return await run_house_device(plan, text)


__all__ = ["HOUSE_HELP", "detect_house_device", "house_device_reply", "run_house_device"]
