"""Telegram Movies conversational agent — OpenAI Chat Completions + tools.

Modes (session state, not vibes): idle → browse → offer → confirm → queued.
Correction stays in browse (never queues). Explain apologizes without re-search.

READ tools auto-run. WRITE ``queue_request`` is HITL: Python executes it only
after an inline-keyboard callback ``q:movie:<tmdbId>`` / ``q:tv:<tmdbId>`` or
an explicit yes bound to that pending tmdb_id — never from free-text
"all" / "3" / "those" / "all of them" / "de eerste".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

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
HISTORY_TURNS = 24

SessionMode = Literal["idle", "browse", "offer", "confirm", "queued", "explain"]

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


_ASKING_FOR_LIST = re.compile(
    r"(?:"
    r"\bi was asking for a few\b|"
    r"\basking for a (?:few|list|options)\b|"
    r"\bwant(?:ed)? a (?:few|list|options)\b|"
    r"\ba few\b.+\b(?:movies?|films?|shows?|series|titles?|options|sci-?fi|fantas)\b|"
    r"\b(?:give|name|show|list)\s+(?:me\s+)?(?:a\s+)?few\b"
    r")",
    re.I,
)

_ASKING_FOR_OTHERS = re.compile(
    r"(?:"
    r"\b(?:give|show|list|find|get)\s+(?:me\s+)?(?:some\s+)?others?\b|"
    r"\b(?:some|any)\s+(?:other|more)\b|"
    r"\byou(?:'ve| have)\s+just\s+mentioned\b|"
    r"\b(?:already|just)\s+(?:mentioned|showed|offered)\b|"
    r"\bnone of (?:these|those|them)\b|"
    r"\bnot (?:these|those)\b|"
    r"\bsame (?:titles?|ones?|movies?)\b|"
    r"\bdifferent (?:ones?|titles?|movies?|options)\b|"
    r"\bmore (?:options|titles|movies|films)\b"
    r")",
    re.I,
)

_EXHAUSTED_OFFER_REPLY = re.compile(
    r"(?:"
    r"trouble finding|"
    r"same titles? keep|"
    r"keep(?:s|ing)? appearing|"
    r"could(?:n't| not) find (?:new|other|more)|"
    r"no (?:new|other|more) (?:options|titles|movies)|"
    r"specify a different genre|"
    r"different genre or type|"
    r"nothing (?:else|new)|"
    r"ran out of"
    r")",
    re.I,
)

# Free-text that must NEVER trigger queue_request (second-brain leftovers).
_FREE_TEXT_QUEUE_BAN = re.compile(
    r"^\s*(?:"
    r"all(?:\s+of\s+(?:them|em|'em|it))?"
    r"|all\s+(?:the\s+)?(?:movies?|films?|ones?|sci-?fi|fantas\w*)?"
    r"|every(?:one|thing)?"
    r"|allemaal|alles"
    r"|#?\s*[1-9]"
    r"|(?:de\s+|het\s+)?eerste(?:\s+(?:een|één|one|film|movie))?"
    r"|(?:just\s+)?(?:the\s+)?first(?:\s+one)?"
    r"|those"
    r")\s*[.!?]*\s*$",
    re.I,
)

_CORRECTION = re.compile(
    r"(?:"
    r"those are all\s+sci-?fi|"
    r"all\s+(?:sci-?fi|scifi)|"
    r"fantas(?:y|ie).{0,40}sci-?fi|"
    r"sci-?fi.{0,40}fantas(?:y|ie)|"
    r"not\s+sci-?fi|"
    r"geen\s+sci-?fi|"
    r"dat\s+(?:zijn|is)\s+(?:allemaal\s+)?sci-?fi"
    r")",
    re.I,
)

_EXPLAIN_WHY = re.compile(
    r"(?:"
    r"why did you (?:do|queue|grab|add)|"
    r"waarom\s+(?:deed|doe)|"
    r"why\s+would\s+you"
    r")",
    re.I,
)


SYSTEM_PROMPT = """You are Hearth, a Telegram bot for movies and TV series (Dutch + English).
Users ask in a group chat; you help request titles via Overseerr.

Session modes: idle → browse → offer → confirm → queued.
- browse: searching / discovering (genre or title). Never queue here.
- offer: numbered list with Get buttons on screen. Wait for a button or yes to ONE id.
- confirm: single Did-you-mean / Get pending. Yes → queue that tmdb_id only.
- explain: user asked why something happened — apologize + say what happened. Do NOT
  re-search or re-offer the same wrong set unless they ask again.
Correction ("those are all sci-fi", "fantasy not sci-fi") stays in browse: acknowledge,
call discover_by_genre again with the right genre_ids, NEVER queue.

You have tools. Use them — do not invent catalog ids.

READ tools (auto-run):
- search_title: exact/prefix title lookup. Single-token seeds are exact
  (Land ≠ La La Land, Wild ≠ The Wild Robot). Use for named titles.
- discover_by_genre: TMDB discover via Overseerr. Pass genre_ids and optional
  exclude_genre_ids. Fantasy = 14 (ALWAYS exclude 878 Sci-Fi). Sci-Fi = 878.
  Horror = 27. "cool fantasy" MUST call discover_by_genre([14], exclude=[878]).
  Never invent Matrix/Arrival/Interstellar as a fantasy set.
  Discover is released-only (primary_release_date ≤ today) with a vote-count
  floor — never offer unreleased 2026 popularity vapor. Already-shown /
  rejected tmdb ids are excluded automatically; "others" / "none of these"
  must call discover_by_genre again (next pack), never re-attach the same
  Get buttons. If discover is exhausted the tool falls back to web_search.
- library_status / download_progress: status checks.

WRITE (HITL — Python only executes after Get button or explicit yes for that id):
- queue_request(tmdb_id, media_type): ONLY way to queue. Never call for free-text
  "all", "3", "those", "all of them", "de eerste", rejects, list asks, or corrections.

Conversation rules:
- Rejects (no/nah/nope/nee): never queue. Discover alternatives or ask what they want.
- Confirms (yes/yep/ja) of a single pending offer → queue_request with THAT pending tmdb_id.
- Genre / vibe list asks → discover_by_genre (not a single Did-you-mean).
- "None of these" / "give me some others" / "you've just mentioned these" →
  call discover_by_genre again for a NEW pack (exclusions applied). Never
  re-list the same three titles or re-attach the same Get buttons.
- If you cannot refresh the list, say so WITHOUT Get buttons — never attach
  Get 1/2/3 for titles you just said you could not replace.
- Exact Title (YYYY) / IMDb-TMDB URL: search_title then offer Get — do not assume queued.
- Ignore pure group chatter/emoji (empty reply).
- Prefer short Telegram replies. Numbered lists as "1. Title (year)\\n2. …".
- After a wrong genre: "You're right — those were sci-fi. Fantasy instead: …"
- Never say "Queued 3" / "Queued N via".
"""


def looks_like_correction(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 200:
        return False
    return bool(_CORRECTION.search(raw))


def looks_like_explain(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 160:
        return False
    return bool(_EXPLAIN_WHY.search(raw))


def looks_like_asking_for_others(text: str) -> bool:
    """True when the user wants a fresh pack, not the same Get buttons."""
    raw = (text or "").strip()
    if not raw or len(raw) > 240:
        return False
    return bool(_ASKING_FOR_OTHERS.search(raw))


def looks_like_exhausted_offer_reply(text: str) -> bool:
    """True when the assistant admits it could not refresh the offer list."""
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_EXHAUSTED_OFFER_REPLY.search(raw))


def should_refuse_queue(user_text: str) -> bool:
    """Safety rail: never execute queue_request on reject / list / correction / bare picks."""
    raw = (user_text or "").strip()
    if not raw:
        return False
    if looks_like_confirm_no(raw):
        return True
    if looks_like_list_ask(raw):
        return True
    if _ASKING_FOR_LIST.search(raw):
        return True
    if looks_like_correction(raw):
        return True
    if looks_like_explain(raw):
        return True
    if _FREE_TEXT_QUEUE_BAN.match(raw):
        return True
    # "Fantasy.. those are all scifi" and similar multi-clause corrections.
    if re.search(r"\bthose are all\b", raw, re.I):
        return True
    if re.search(r"\ball (?:of )?(?:them|sci-?fi|scifi|fantas)", raw, re.I):
        return True
    return False


TELEGRAM_CHAT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_title",
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
                    "year": {"type": "integer", "description": "Optional release year"},
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
            "name": "discover_by_genre",
            "description": (
                "TMDB discover by genre ids. Fantasy=14 (exclude 878), Sci-Fi=878, "
                "Horror=27. Use for 'cool fantasy', genre corrections, vibe lists, "
                "and 'others' / none-of-these follow-ups (excludes already-shown ids). "
                "Released-only with vote floor. Never queues."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "genre_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "TMDB genre ids to include (e.g. [14] for Fantasy)",
                    },
                    "exclude_genre_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "TMDB genre ids to exclude (e.g. [878] for Sci-Fi)",
                    },
                    "media_type": {
                        "type": "string",
                        "description": "movie or tv (default movie)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many options (2–4, default 4)",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Discover page (1+). Use next page for 'others'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional label for the offer, e.g. 'cool fantasy'",
                    },
                },
                "required": ["genre_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_titles",
            "description": (
                "Return 2–4 titled options when you already know specific names "
                "(not a genre browse). For genre/vibe asks prefer discover_by_genre."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit title list (2–4 names)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Label for the offer",
                    },
                    "media_type": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_request",
            "description": (
                "Queue one title via Overseerr. Only after Get button or explicit yes "
                "for that pending tmdb_id. Requires tmdb_id + media_type."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tmdb_id": {"type": "integer"},
                    "media_type": {
                        "type": "string",
                        "description": "movie or tv",
                    },
                    "title": {"type": "string"},
                    "year": {"type": "integer"},
                },
                "required": ["tmdb_id", "media_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "library_status",
            "description": "Check whether a title is already in the library or download queue.",
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
                "Retry a stalled or failed download from another *arr source."
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

# Back-compat alias used by older patches / docs.
TELEGRAM_CHAT_TOOLS_ALIASES = {
    "search_catalog": "search_title",
    "already_queued": "library_status",
}


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
    mode: SessionMode = "idle"
    reply_markup: dict[str, Any] | None = None


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _history_messages(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
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
    mode: str = "idle",
    shown_tmdb_ids: list[int] | None = None,
) -> str:
    payload = {
        "mode": mode,
        "pending": pending,
        "subject_title": redact(subject_title)[:120] if subject_title else "",
        "subject_media_kind": subject_media_kind or "",
        "rejected_titles": [redact(t)[:80] for t in (rejected_titles or [])[:12]],
        "offered": offered[:8],
        "shown_tmdb_ids": list(shown_tmdb_ids or [])[:32],
    }
    return (
        "Session context (JSON). mode is the explicit session state. "
        "pending/offered are live on-screen titles with tmdb ids; "
        "never re-offer rejected_titles or shown_tmdb_ids. "
        "For 'others' call discover_by_genre again. "
        "queue_request only for a confirmed pending id.\n"
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
    shown_tmdb_ids: list[int] | None = None,
    mode: SessionMode | str = "idle",
    model: str | None = None,
    queue_approved: bool = False,
) -> AgentTurnResult:
    """One user turn: Chat Completions with native function tools."""
    from openai import AsyncOpenAI

    if not settings.openai_api_key:
        return AgentTurnResult(reply="", mode=mode if mode in {
            "idle", "browse", "offer", "confirm", "queued", "explain"
        } else "idle")

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    use_model = model or telegram_intent_model()
    if use_model.endswith("-mini") and not model:
        use_model = TELEGRAM_INTENT_MODEL

    session_mode: SessionMode = (
        mode if mode in {"idle", "browse", "offer", "confirm", "queued", "explain"} else "idle"
    )

    extra_system = ""
    if looks_like_explain(user_text):
        session_mode = "explain"
        extra_system = (
            "EXPLAIN MODE: Apologize briefly and explain what happened. "
            "Do NOT call discover_by_genre or search_title unless the user asks "
            "for a new search. Do NOT call queue_request."
        )
    elif looks_like_correction(user_text):
        session_mode = "browse"
        extra_system = (
            "CORRECTION MODE: Stay in browse. Acknowledge the wrong genre. "
            "Call discover_by_genre with the corrected genre_ids "
            "(Fantasy=14 exclude 878). Never queue_request."
        )

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
                shown_tmdb_ids=list(shown_tmdb_ids or []),
                mode=session_mode,
            ),
        },
    ]
    if extra_system:
        messages.append({"role": "system", "content": extra_system})
    messages.extend(_history_messages(history))
    messages.append({"role": "user", "content": user_text})

    result = AgentTurnResult(mode=session_mode)
    refuse = should_refuse_queue(user_text) and not queue_approved

    # Alias map so handlers can be registered under either name.
    resolved_handlers = dict(handlers)
    for alias, canonical in TELEGRAM_CHAT_TOOLS_ALIASES.items():
        if alias in resolved_handlers and canonical not in resolved_handlers:
            resolved_handlers[canonical] = resolved_handlers[alias]
        if canonical in resolved_handlers and alias not in resolved_handlers:
            resolved_handlers[alias] = resolved_handlers[canonical]

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
                name = TELEGRAM_CHAT_TOOLS_ALIASES.get(tc["name"], tc["name"])
                args = _parse_args(tc["arguments"])
                payload = await _dispatch_tool(
                    name,
                    args,
                    handlers=resolved_handlers,
                    refuse_queue=refuse,
                    user_text=user_text,
                    queue_approved=queue_approved,
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
        if looks_like_exhausted_offer_reply(reply):
            result.reply_markup = None
        return result

    if not result.reply and result.grabbed:
        label = result.title or "that"
        if result.year:
            label = f"{label} ({result.year})"
        result.reply = f"Queued {label} via Overseerr."
    if result.reply and looks_like_exhausted_offer_reply(result.reply):
        result.reply_markup = None
    return result


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    handlers: dict[str, ToolHandler],
    refuse_queue: bool,
    user_text: str,
    queue_approved: bool = False,
) -> dict[str, Any]:
    if name == "queue_request" and refuse_queue and not queue_approved:
        log.info(
            "refused queue_request for reject/list/correction: %s",
            redact(user_text)[:80],
        )
        return {
            "ok": False,
            "refused": True,
            "error": (
                "Tool refused: free-text cannot queue. Wait for a Get button "
                "callback or an explicit yes bound to a pending tmdb_id. "
                "For genre mistakes call discover_by_genre."
            ),
        }
    handler = handlers.get(name)
    if handler is None:
        # Try alias.
        alias = next(
            (a for a, c in TELEGRAM_CHAT_TOOLS_ALIASES.items() if c == name),
            None,
        )
        handler = handlers.get(alias) if alias else None
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
        result.mode = "queued"
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
    if name in {
        "search_title",
        "search_catalog",
        "discover_by_genre",
        "suggest_titles",
        "retry_download",
        "download_progress",
        "library_status",
    }:
        st = payload.get("query") or payload.get("title") or ""
        if st:
            result.search_title = str(st)[:200]
        kind = payload.get("media_type") or payload.get("media_kind") or ""
        if kind in {"movie", "tv"}:
            result.media_kind = kind
        if payload.get("reply"):
            if not result.reply or name in {
                "retry_download",
                "suggest_titles",
                "discover_by_genre",
            }:
                result.reply = str(payload["reply"])
        if name in {"discover_by_genre", "suggest_titles"}:
            if payload.get("ok") and payload.get("reply_markup") and isinstance(
                payload["reply_markup"], dict
            ):
                result.reply_markup = payload["reply_markup"]
                result.mode = "offer"
            elif not payload.get("ok"):
                # Failed refresh must not keep prior Get buttons.
                result.reply_markup = None
        elif payload.get("reply_markup") and isinstance(payload["reply_markup"], dict):
            result.reply_markup = payload["reply_markup"]
        if name in {"search_title", "search_catalog"} and payload.get("count") == 1:
            result.mode = "confirm"
        elif name in {"search_title", "search_catalog"} and (payload.get("count") or 0) > 1:
            result.mode = "offer"
