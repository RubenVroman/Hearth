"""Telegram Movies conversational agent — OpenAI Chat Completions + tools.

Every media ask is a model turn. The only way to queue is ``queue_request``.
Python never auto-queues on short replies (Yep/No/Nah/a few…).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.intent import (
    TELEGRAM_INTENT_MODEL,
    looks_like_confirm_no,
    looks_like_list_ask,
    telegram_intent_model,
)

log = logging.getLogger("hearth.telegram")

MAX_TOOL_TURNS = 6
HISTORY_TURNS = 8

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


_ASKING_FOR_LIST = re.compile(
    r"(?:"
    r"\bi was asking for a few\b|"
    r"\basking for a (?:few|list|options)\b|"
    r"\bwant(?:ed)? a (?:few|list|options)\b|"
    r"\ba few\b.+\b(?:movies?|films?|shows?|series|titles?|options|sci-?fi)\b|"
    r"\b(?:give|name|show|list)\s+(?:me\s+)?(?:a\s+)?few\b"
    r")",
    re.I,
)


SYSTEM_PROMPT = """You are Hearth, a Telegram bot for movies and TV series (Dutch + English).
Users ask in a group chat; you help request titles via Overseerr.

You have tools. Use them — do not invent catalog ids or queue without tools.

Tools:
- search_catalog: exact/prefix title lookup on Overseerr/TMDB. Single-token seeds are exact
  (Land ≠ La La Land, Wild ≠ The Wild Robot). Use for named titles and plot guesses.
- suggest_titles: return 2–4 titled+year options. MUST use for list/vibe asks
  ("a few", "name a few more", "cool space sci-fi", "give me options", "I was asking for a few").
  Never reply with a single "Did you mean …?" for a list ask. Never queue on a list ask.
- queue_request: Overseerr grab. The ONLY way anything gets queued. Call only when the user
  clearly confirms a pending title (yes/yep/duh/ja) or names an exact Title (year)/id to grab.
- already_queued / download_progress: optional status checks.

Conversation rules:
- Every user media message is a turn. Use recent history (~8 turns / 30 min) and pending/offered.
- Rejects (no/nah/nope/nee/not that): never queue that title. Call suggest_titles for alternatives
  or ask what they want. rejected_titles must not be re-offered.
- Confirms (yes/yep/yeah/ja/duh) of a single pending Did-you-mean → queue_request with that
  title + year + tmdb_id from pending/offered context only.
- Plot/vibe single guesses → search_catalog then ask "Did you mean Title (year)?" — do not queue yet.
- Concrete exact titles with one catalog hit may queue_request after search_catalog.
- Ignore pure group chatter/emoji with no media ask (empty reply).
- Prefer short Telegram replies. Numbered lists as "1. Title (year)\\n2. …".
"""


def should_refuse_queue(user_text: str) -> bool:
    """Safety rail: never execute queue_request on clear reject / list asks."""
    raw = (user_text or "").strip()
    if not raw:
        return False
    if looks_like_confirm_no(raw):
        return True
    if looks_like_list_ask(raw):
        return True
    if _ASKING_FOR_LIST.search(raw):
        return True
    return False


TELEGRAM_CHAT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": (
                "Search Overseerr/TMDB for an exact or prefix movie/TV title. "
                "Single-token seeds match exactly (Land ≠ La La Land)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Catalog title to look up (not a plot sentence).",
                    },
                    "year": {
                        "type": "integer",
                        "description": "Optional release year",
                    },
                    "media_type": {
                        "type": "string",
                        "description": "movie, tv, or any",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_titles",
            "description": (
                "Return 2–4 titled+year options for vibe/list asks "
                "('a few', 'name a few more', 'cool space sci-fi'). "
                "Never queues. Prefer over a single Did-you-mean."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit title list when you already know 2–4 names",
                    },
                    "query": {
                        "type": "string",
                        "description": "Mood/vibe when titles are unknown, e.g. 'cool space sci-fi'",
                    },
                    "media_type": {
                        "type": "string",
                        "description": "movie, tv, or any",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many options (2–4, default 4)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_request",
            "description": (
                "Queue one movie/TV title via Overseerr. Only call on clear user confirm "
                "of a pending offer, or an unambiguous Title (year)/id grab. "
                "Requires title; include year and tmdb_id when known."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": "integer"},
                    "tmdb_id": {"type": "integer"},
                    "media_type": {
                        "type": "string",
                        "description": "movie or tv",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "already_queued",
            "description": "Check whether a title is already downloading or in the library queue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "media_type": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_progress",
            "description": "Check download progress for a title currently grabbing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "media_type": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_download",
            "description": (
                "Retry a stalled or failed download from another *arr source. "
                "Use when the user says the download didn't work / try another source."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "media_type": {"type": "string"},
                },
            },
        },
    },
]


@dataclass
class AgentTurnResult:
    reply: str = ""
    grabbed: bool = False
    title: str = ""
    year: int | None = None
    titles: list[str] = field(default_factory=list)
    tools_used: list[dict[str, Any]] = field(default_factory=list)
    search_title: str = ""
    media_kind: str = ""


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _history_messages(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Map ChatMemory history_blob turns into OpenAI chat messages."""
    out: list[dict[str, Any]] = []
    for turn in (history or [])[-HISTORY_TURNS:]:
        role = "assistant" if turn.get("role") == "bot" else "user"
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        out.append({"role": role, "content": text[:500]})
    return out


def _context_block(
    *,
    pending: dict[str, Any] | None,
    subject_title: str,
    subject_media_kind: str,
    rejected_titles: list[str],
    offered: list[dict[str, Any]],
) -> str:
    payload = {
        "pending": pending,
        "subject_title": redact(subject_title)[:120] if subject_title else "",
        "subject_media_kind": subject_media_kind or "",
        "rejected_titles": [redact(t)[:80] for t in (rejected_titles or [])[:12]],
        "offered": offered[:8],
    }
    return (
        "Session context (JSON). pending/offered are the live on-screen titles; "
        "never re-offer rejected_titles.\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


async def run_telegram_agent(
    user_text: str,
    *,
    handlers: dict[str, ToolHandler],
    history: list[dict[str, Any]] | None = None,
    pending: dict[str, Any] | None = None,
    subject_title: str = "",
    subject_media_kind: str = "",
    rejected_titles: list[str] | None = None,
    offered: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> AgentTurnResult:
    """One user turn: Chat Completions with native function tools."""
    from openai import AsyncOpenAI

    if not settings.openai_api_key:
        return AgentTurnResult(reply="")

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    use_model = model or telegram_intent_model()
    # Prefer gpt-4o for Telegram conversation unless a non-mini override is set.
    if use_model.endswith("-mini") and not model:
        use_model = TELEGRAM_INTENT_MODEL

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": _context_block(
                pending=pending,
                subject_title=subject_title,
                subject_media_kind=subject_media_kind,
                rejected_titles=list(rejected_titles or []),
                offered=list(offered or []),
            ),
        },
        *_history_messages(history),
        {"role": "user", "content": user_text},
    ]

    result = AgentTurnResult()
    refuse = should_refuse_queue(user_text)

    for _ in range(MAX_TOOL_TURNS):
        response = await client.chat.completions.create(
            model=use_model,
            messages=messages,
            tools=TELEGRAM_CHAT_TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
        try:
            from hearth.openai_usage import record_chat_usage

            record_chat_usage(response, model=use_model, kind="telegram")
        except Exception:  # noqa: BLE001
            pass

        choice = response.choices[0]
        msg = choice.message
        tool_calls = list(msg.tool_calls or [])

        if tool_calls:
            normalized = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function") or {}
                    normalized.append(
                        {
                            "id": tc.get("id") or f"call_{len(normalized)}",
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "{}",
                        }
                    )
                else:
                    normalized.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    )
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in normalized
                    ],
                }
            )
            for tc in normalized:
                name = tc["name"]
                args = _parse_args(tc["arguments"])
                payload = await _dispatch_tool(
                    name,
                    args,
                    handlers=handlers,
                    refuse_queue=refuse,
                    user_text=user_text,
                )
                result.tools_used.append(
                    {"name": name, "args": args, "result": payload}
                )
                _absorb_tool_side_effects(result, name, payload)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(payload, default=str)[:4000],
                    }
                )
            continue

        reply = (msg.content or "").strip()
        result.reply = reply
        return result

    if not result.reply and result.grabbed:
        label = result.title or "that"
        if result.year:
            label = f"{label} ({result.year})"
        result.reply = f"Queued {label} via Overseerr."
    return result


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    handlers: dict[str, ToolHandler],
    refuse_queue: bool,
    user_text: str,
) -> dict[str, Any]:
    if name == "queue_request" and refuse_queue:
        log.info(
            "refused queue_request for reject/list ask: %s",
            redact(user_text)[:80],
        )
        return {
            "ok": False,
            "refused": True,
            "error": (
                "Tool refused: the latest user message is a reject or list ask. "
                "Do not queue. Call suggest_titles for 2–4 alternatives or ask "
                "what they want instead."
            ),
        }
    handler = handlers.get(name)
    if handler is None:
        return {"ok": False, "error": f"unknown tool {name}"}
    try:
        data = await handler(args)
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram tool %s failed: %s", name, redact(str(exc)))
        return {"ok": False, "error": str(exc)}
    return data if isinstance(data, dict) else {"ok": True, "result": data}


def _absorb_tool_side_effects(
    result: AgentTurnResult, name: str, payload: dict[str, Any]
) -> None:
    if not isinstance(payload, dict):
        return
    if name == "queue_request" and payload.get("ok") and payload.get("grabbed"):
        result.grabbed = True
        result.title = str(payload.get("title") or result.title or "")
        year = payload.get("year")
        try:
            result.year = int(year) if year not in (None, "") else result.year
        except (TypeError, ValueError):
            pass
        if payload.get("reply"):
            result.reply = str(payload["reply"])
        titles = payload.get("titles")
        if isinstance(titles, list):
            result.titles.extend(str(t) for t in titles if t)
    if name in {"search_catalog", "suggest_titles", "retry_download", "download_progress"}:
        st = payload.get("query") or payload.get("title") or ""
        if st:
            result.search_title = str(st)[:200]
        kind = payload.get("media_type") or payload.get("media_kind") or ""
        if kind in {"movie", "tv"}:
            result.media_kind = kind
        if payload.get("reply"):
            # Prefer model prose, but keep tool-formatted list / retry copy.
            if not result.reply or name in {"retry_download", "suggest_titles"}:
                result.reply = str(payload["reply"])
