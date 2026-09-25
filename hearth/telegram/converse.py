"""Telegram turns that belong to the shared house agent, not a media lane.

Exact titles, franchises, editions, and in-thread media follow-ups stay on the
fast Overseerr path. Everything else the house router already understands —
weather, lights, memory, "also dim the lights", "and tomorrow?" — goes through
:class:`hearth.agent.loop.AgentLoop`, which is the same tool loop Ask-the-House
uses, including the one-call Jev gate.

Downloads stay on the Get button. This module never queues.
"""

from __future__ import annotations

import re

from hearth.agent.loop import route_intent
from hearth.jev import QUEUE_TOOLS
from hearth.telegram.heuristics import looks_like_concrete_title
from hearth.telegram.house import looks_like_house_control
from hearth.telegram.media.classify import classify_media_ask_sync
from hearth.telegram.media.editions import extract_edition
from hearth.telegram.media.followups import detect_follow_up
from hearth.telegram.media.memory import ChatContext
from hearth.telegram.media.play import looks_like_play_command
from hearth.telegram.media.types import MediaIntent
from hearth.telegram.thread import ChatThread

# The agent may plan these, but Telegram must not run them. Get is the queue.
HELD_TOOLS = frozenset(
    {
        *QUEUE_TOOLS,
        "radarr_retry",
        "sonarr_retry",
    }
)

_DISCOURSE = re.compile(
    r"^(?:(?:"
    r"and|also|then|plus|oh|ok|okay|alright|right|well|so|en|nou|please|pls|"
    r"can you|could you"
    r")[,!]?\s+)+",
    re.I,
)
_CONTINUE = re.compile(
    r"^\s*(?:and|also|then|plus|what about|how about|same for|me too)\b",
    re.I,
)
_LEADING_NO = re.compile(
    r"^(?:no|nah|nope|nee|not that|wrong)[,!]?\s+",
    re.I,
)
_ANAPHORA = re.compile(
    r"^(?:the|that|this|it|one|ones|version|cut|edition)?$",
    re.I,
)
_PLAY_REFERENCE = frozenset({"it", "that", "this", "something", ""})
# Lanes the media butler already answers. exact_title is excluded: short house
# asks ("what's the weather") look like titles until the house router claims them.
_MEDIA_LANES = frozenset(
    {
        "known_franchise",
        "series_all",
        "edition",
        "person",
        "mood",
        "similar",
        "batch",
        "follow_up",
        "house_pick",
        "describe",
        "chat_about",
    }
)
# Even a title-shaped ask stays on Telegram when the house plan is one of these.
# "movie night" is a catalog vibe here, and "what's X about" is chat_about.
_LEAVE_TO_MEDIA = frozenset(
    {
        "plex_search",
        "suggest_titles",
        "plex_browse_genre",
        "media_activity",
    }
)
_CATALOG_NIGHT = frozenset({"movie night", "film night", "cinema night"})


def strip_discourse(text: str) -> str:
    """Drop a leading "also" / "and" so the house router sees the instruction."""
    raw = " ".join((text or "").split())
    stripped = _DISCOURSE.sub("", raw).strip()
    return stripped or raw


def agent_utterance(text: str) -> str | None:
    """Text for the shared agent, or None when a media lane should keep it.

    The returned string may drop a discourse prefix ("also dim the lights" →
    "dim the lights") so the same router the glass UI uses can match it.
    """
    raw = " ".join((text or "").split())
    if not raw or detect_follow_up(raw) or looks_like_play_command(raw):
        return None
    # Rituals, climate, feeder, and purifier already have a Telegram reply
    # keyboard. Leave those on that path.
    if looks_like_house_control(raw):
        return None
    stripped = strip_discourse(raw)
    if stripped.casefold().strip(" .!?") in _CATALOG_NIGHT:
        return None
    plan = route_intent(stripped)
    if not plan:
        return None
    tool = str(plan.get("tool") or "")
    if tool in HELD_TOOLS:
        return None
    if tool in {"plex_play", "infuse_play"}:
        query = str((plan.get("args") or {}).get("query") or "").strip().casefold()
        if query in _PLAY_REFERENCE:
            return None
    # A media lane that already knows what to do wins over the house router.
    # Otherwise "what's Harry Potter about?" becomes a Plex search and
    # "what should we watch?" never reaches the house-picks cards.
    local = classify_media_ask_sync(raw)
    if local.kind in _MEDIA_LANES or (
        local.kind == "exact_title" and tool in _LEAVE_TO_MEDIA
    ):
        return None
    return stripped


def continue_thread(text: str, thread: ChatThread | None) -> bool:
    """True for a short glue follow-up that only makes sense after a house turn."""
    if thread is None or not thread.turns:
        return False
    raw = " ".join((text or "").split())
    if not raw or detect_follow_up(raw) or looks_like_play_command(raw):
        return False
    return bool(_CONTINUE.match(raw))


def edition_correction(text: str, context: ChatContext | None) -> MediaIntent | None:
    """Bind "no the extended cut" to the title already on screen.

    A correction that names its own title ("LOTR extended") returns None so the
    instant edition lane can search that title itself.
    """
    if context is None or not context.subject():
        return None
    edition = extract_edition(text)
    if edition is None or not edition.present:
        return None
    cleaned = _LEADING_NO.sub("", edition.clean_title).strip(" .!?,")
    cleaned = re.sub(
        r"^(?:the|that|this|it|one|version)\b",
        "",
        cleaned,
        count=1,
        flags=re.I,
    ).strip()
    if cleaned and looks_like_concrete_title(cleaned) and not _ANAPHORA.match(cleaned):
        return None
    top = context.top
    return MediaIntent(
        kind="edition",
        search_title=context.subject(),
        year=top.year if top is not None else None,
        media_type=(top.media_type if top is not None else context.media_type) or "",
        edition_key=edition.key,
        edition_label=edition.label,
        raw_text=(text or "").strip(),
        note="thread_edition",
        source="context",
    )


def describe_context(
    context: ChatContext | None,
    pending: dict | None,
    thread: ChatThread,
) -> str:
    """Short note the agent reads so "that" and "the second one" stay grounded."""
    lines: list[str] = []
    if context is not None and context.hits:
        labels = ", ".join(f"{index}. {hit.label}" for index, hit in enumerate(context.hits, 1))
        lines.append(f"Last media offered (not queued): {labels}.")
    if pending:
        title = str(pending.get("title") or "").strip()
        if title:
            lines.append(f"Pending confirm, not yet queued: {title}.")
    if thread.last_house:
        lines.append(f"Last house action: {thread.last_house}")
    for role, text in thread.turns[-6:]:
        lines.append(f"{role}: {text}")
    if not lines:
        return ""
    return (
        "Thread context for follow-ups. Do not queue a download from this note.\n"
        + "\n".join(lines)
    )


def idle_line(thread: ChatThread) -> str:
    """Butler line when the router has no tool and no model is configured."""
    for role, text in reversed(thread.turns):
        if role == "assistant" and text.strip():
            last = text.strip().split("\n", 1)[0]
            return f"Still with you — {last} What next?"
    if thread.last_house:
        return f"Still with you — {thread.last_house} What next?"
    return "I'm here. Lights, weather, what's playing, or a title."


def house_summary(out: dict) -> str:
    """One line to remember from a tool turn, so the next message can refer to it."""
    reply = str(out.get("reply") or "").strip().split("\n", 1)[0]
    tools = out.get("tools") or []
    if any(isinstance(tool, dict) and tool.get("name") for tool in tools):
        return reply[:180]
    return ""


def awaiting_confirm(out: dict) -> bool:
    tools = out.get("tools") or []
    return any(isinstance(tool, dict) and tool.get("needs_confirm") for tool in tools)


__all__ = [
    "HELD_TOOLS",
    "agent_utterance",
    "awaiting_confirm",
    "continue_thread",
    "describe_context",
    "edition_correction",
    "house_summary",
    "idle_line",
    "strip_discourse",
]
