"""Cheap movie-request intent layer for the Telegram inbox.

Uses ``settings.openai_model`` (default gpt-4o-mini) when OPENAI_API_KEY is set.
Falls back to small heuristics so follow-ups still work without a live call.
Also resolves vague plot/character descriptions to a catalog search title when
the model is configured. Never invents downloads: unsure → clarify; clear
catalog titles / links still passthrough.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from hearth.config import settings
from hearth.memory.redact import redact

log = logging.getLogger("hearth.telegram")

IntentAction = Literal[
    "passthrough",
    "ignore",
    "clarify",
    "pick",
    "pick_many",
    "search",
]

MAX_CANDIDATES = 12
MAX_BATCH = 10
MIN_RESOLVE_CONFIDENCE = 0.55

_FOLLOWUP_ALL = re.compile(
    r"\b("
    r"all(?:\s+of\s+(?:them|em|'em|it))?"
    r"|all\s+(?:the\s+)?(?:movies?|films?|ones?)"
    r"|every(?:one|thing)?"
    r"|the\s+whole\s+(?:\w+\s+){0,6}(?:series|franchise|lot|thing|saga|collection)"
    r"|the\s+entire\s+(?:\w+\s+){0,6}(?:series|franchise|saga|collection)"
    r"|whole\s+series"
    r"|entire\s+series"
    r"|the\s+trilogy"
    r"|full\s+series"
    r")\b",
    re.I,
)
_FOLLOWUP_FIRST = re.compile(
    r"\b((?:just\s+)?(?:the\s+)?first(?:\s+one)?|oldest|(?:the\s+)?1st(?:\s+one)?)\b",
    re.I,
)
_FOLLOWUP_NEW = re.compile(
    r"\b((?:the\s+)?(?:new|newest|latest|most\s+recent)(?:\s+one)?)\b",
    re.I,
)
_FOLLOWUP_LAST = re.compile(
    r"\b((?:the\s+)?last(?:\s+one)?|(?:the\s+)?final(?:\s+one)?)\b",
    re.I,
)
_ORDINAL = re.compile(
    r"^\s*(?:(?:just|only)\s+)?(?:the\s+)?"
    r"(?P<ord>first|second|third|1st|2nd|3rd|#?\s*[1-9]|one|two|three)"
    r"(?:\s+one)?\s*$",
    re.I,
)
_COLLECTION_SEARCH = re.compile(
    r"^\s*(?:"
    r"(?:download|get|grab|queue)\s+"
    r")?"
    r"(?:"
    r"(?P<all>all|every|the\s+whole|the\s+entire|the\s+full)\s+"
    r"(?:(?:of\s+)?(?:the\s+)?)?"
    r")?"
    r"(?P<title>.+?)"
    r"(?:\s+(?:movies?|films?|trilogy|series|franchise|saga|collection))?"
    r"\s*$",
    re.I,
)
_COLLECTION_HINT = re.compile(
    r"\b("
    r"trilogy|franchise|saga|collection|"
    r"whole\s+(?:\w+\s+){0,6}(?:series|franchise|saga|collection)|"
    r"entire\s+(?:\w+\s+){0,6}(?:series|franchise|saga|collection)|"
    r"all\s+(?:the\s+)?(?:movies?|films?|ones?)"
    r")\b",
    re.I,
)
_ORDINAL_MAP = {
    "first": 1,
    "1st": 1,
    "one": 1,
    "1": 1,
    "second": 2,
    "2nd": 2,
    "two": 2,
    "2": 2,
    "third": 3,
    "3rd": 3,
    "three": 3,
    "3": 3,
}

_DESCRIPTIVE_MARKERS = re.compile(
    r"\b("
    r"(?:movie|film|show|series|tv(?:\s+show)?)\s+"
    r"(?:about|with|where|featuring|involving|of)|"
    r"about\s+(?:a|an|the)\b|"
    r"(?:where|in\s+which)\s+(?:a|an|the|he|she|they|someone|people)\b|"
    r"(?:boy|girl|man|woman|kid|child|teenager|wizard|detective|robot|alien|"
    r"cop|doctor|teacher|student|orphan)\s+(?:with|who|that)\b|"
    r"who\s+(?:is|was|has|can|wears|lives)\b|"
    r"(?:based\s+on|remake\s+of|adaptation\s+of)\b|"
    r"(?:something|anything)\s+(?:like|about)\b|"
    r"(?:that|the)\s+(?:one|movie|film|show|series)\s+(?:about|with|where)\b"
    r")",
    re.I,
)
_YEAR_PAREN = re.compile(r"\(\s*(?:19|20)\d{2}\s*\)")
_SEASON_MARK = re.compile(r"\bS\d{1,2}E\d{1,3}\b|\bseason\s+\d+\b", re.I)
_CATALOG_ID = re.compile(r"\btt\d{7,}|\btmdb:\d+|\btvdb:\d+", re.I)
_URLISH = re.compile(r"https?://", re.I)
_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "about",
        "with",
        "who",
        "whom",
        "where",
        "which",
        "that",
        "from",
        "into",
        "over",
        "under",
        "between",
        "like",
        "for",
        "of",
        "in",
        "on",
        "to",
        "and",
        "or",
        "is",
        "was",
        "are",
        "were",
        "has",
        "have",
        "had",
        "can",
        "could",
        "would",
        "should",
        "movie",
        "film",
        "show",
        "series",
        "tv",
    }
)

_SYSTEM = (
    "You interpret short Telegram messages for a house movie/TV download bot. "
    "Return JSON only. Prefer clarify over wrong downloads. "
    "When candidates are listed: pick/pick_many with 1-based indices only — "
    "never invent titles or ids not listed in candidates. "
    "When candidates are empty and the message describes a plot, character, or "
    "premise instead of naming a title: set action=search with search_title to "
    "the well-known catalog title (franchise/series name is OK), and media_kind "
    "movie or tv when clear. If unsure which title, action=clarify with a short question. "
    "Do not echo the plot description as search_title. "
    "Actions: passthrough, ignore, clarify, pick, pick_many, search. "
    "search sets search_title (and select_all=true for whole series/trilogy). "
    "If the user clearly names a concrete title, passthrough."
)


@dataclass
class IntentDecision:
    action: IntentAction = "passthrough"
    indices: list[int] = field(default_factory=list)
    search_title: str = ""
    select_all: bool = False
    media_kind: str = ""
    clarify_question: str = ""
    confidence: float = 0.0
    source: str = "heuristic"


def looks_like_followup(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return False
    if _FOLLOWUP_ALL.search(raw) or _FOLLOWUP_FIRST.search(raw):
        return True
    if _FOLLOWUP_NEW.search(raw) or _FOLLOWUP_LAST.search(raw):
        return True
    if _ORDINAL.match(raw):
        return True
    lowered = raw.lower()
    return lowered in {"all", "both", "those", "these", "them", "yes", "yep", "yeah", "sure"}


def looks_like_collection_request(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 160:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", raw):
        return False
    if _FOLLOWUP_ALL.search(raw):
        return True
    return bool(_COLLECTION_HINT.search(raw))


def looks_like_descriptive_ask(text: str) -> bool:
    """True for plot/character descriptions — not concrete catalog titles/links."""
    raw = (text or "").strip()
    if not raw or len(raw) < 12 or len(raw) > 200:
        return False
    if looks_like_followup(raw):
        return False
    if _YEAR_PAREN.search(raw) or _SEASON_MARK.search(raw):
        return False
    if _CATALOG_ID.search(raw) or _URLISH.search(raw):
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", raw):
        return False
    if _DESCRIPTIVE_MARKERS.search(raw):
        return True
    words = re.findall(r"[A-Za-zÀ-ÿ']+", raw)
    if len(words) >= 6:
        function_hits = sum(1 for w in words if w.lower() in _FUNCTION_WORDS)
        if function_hits >= 3:
            return True
    return False


def _candidate_blob(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(candidates[:MAX_CANDIDATES], start=1):
        rows.append(
            {
                "i": idx,
                "title": str(row.get("title") or "")[:120],
                "year": row.get("year"),
                "tmdbId": row.get("tmdbId") or row.get("mediaId"),
                "tvdbId": row.get("tvdbId"),
            }
        )
    return rows


def _year_sort_key(row: dict[str, Any]) -> int:
    year = row.get("year")
    try:
        return int(year)
    except (TypeError, ValueError):
        return 0


def _heuristic_with_candidates(text: str, candidates: list[dict[str, Any]]) -> IntentDecision | None:
    raw = (text or "").strip()
    n = len(candidates)
    if n == 0:
        return None

    if _FOLLOWUP_ALL.search(raw) or raw.lower() in {"all", "both", "those", "these", "them"}:
        return IntentDecision(
            action="pick_many",
            indices=list(range(1, min(n, MAX_BATCH) + 1)),
            select_all=True,
            confidence=0.9,
            source="heuristic",
        )

    if _FOLLOWUP_NEW.search(raw):
        newest = max(range(n), key=lambda i: _year_sort_key(candidates[i]))
        return IntentDecision(action="pick", indices=[newest + 1], confidence=0.85)

    if _FOLLOWUP_LAST.search(raw) and not _FOLLOWUP_FIRST.search(raw):
        # "last" in franchise talk usually means final / newest; prefer newest year.
        newest = max(range(n), key=lambda i: _year_sort_key(candidates[i]))
        return IntentDecision(action="pick", indices=[newest + 1], confidence=0.7)

    if _FOLLOWUP_FIRST.search(raw):
        oldest = min(range(n), key=lambda i: _year_sort_key(candidates[i]) or 9999)
        return IntentDecision(action="pick", indices=[oldest + 1], confidence=0.85)

    ord_match = _ORDINAL.match(raw)
    if ord_match:
        token = re.sub(r"[^a-z0-9]", "", ord_match.group("ord").lower())
        idx = _ORDINAL_MAP.get(token)
        if idx is not None and 1 <= idx <= n:
            return IntentDecision(action="pick", indices=[idx], confidence=0.9)
        return IntentDecision(
            action="clarify",
            clarify_question=f"Pick a number from 1–{min(3, n)} (or say 'all of them').",
            confidence=0.6,
        )

    if raw.lower() in {"yes", "yep", "yeah", "sure"}:
        return IntentDecision(
            action="clarify",
            clarify_question=(
                f"Which one — reply 1–{min(3, n)}, "
                "'all of them', 'the first one', or 'the new one'?"
            ),
            confidence=0.7,
        )

    return None


def _heuristic_collection_search(text: str) -> IntentDecision | None:
    raw = (text or "").strip()
    if not looks_like_collection_request(raw):
        return None
    match = _COLLECTION_SEARCH.match(raw)
    if not match:
        return None
    title = (match.group("title") or "").strip(" \"'`-–—")
    title = re.sub(
        r"\b(movies?|films?|trilogy|series|franchise|saga|collection)\b",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip(" \"'`")
    if len(title) < 2:
        return None
    want_all = bool(match.group("all")) or bool(_FOLLOWUP_ALL.search(raw)) or bool(
        _COLLECTION_HINT.search(raw)
    )
    if not want_all:
        return None
    return IntentDecision(
        action="search",
        search_title=title[:200],
        select_all=True,
        confidence=0.8,
        source="heuristic",
    )


def heuristic_intent(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    pending_query: str = "",
) -> IntentDecision:
    """Local interpretation — used alone or as OpenAI fallback."""
    del pending_query  # reserved for richer context; candidates carry the set
    raw = (text or "").strip()
    if not raw:
        return IntentDecision(action="ignore", confidence=1.0)

    if candidates:
        decided = _heuristic_with_candidates(raw, candidates)
        if decided is not None:
            return decided
        # Unrelated new title while pending → let normal parser grab it.
        if looks_like_followup(raw):
            return IntentDecision(
                action="clarify",
                clarify_question=(
                    f"Not sure — reply 1–{min(3, len(candidates))}, "
                    "'all of them', or send a specific title."
                ),
                confidence=0.5,
            )
        return IntentDecision(action="passthrough", confidence=0.5)

    collection = _heuristic_collection_search(raw)
    if collection is not None:
        return collection
    # Without a model we must not invent a title from a plot description.
    if looks_like_descriptive_ask(raw):
        return IntentDecision(action="passthrough", confidence=0.35, source="heuristic")
    return IntentDecision(action="passthrough", confidence=0.4)


async def interpret_intent(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    pending_query: str = "",
    last_bot_reply: str = "",
    force: bool = False,
) -> IntentDecision:
    """Interpret a short user message. Cheap model; fail open to heuristic/clarify."""
    raw = (text or "").strip()
    if not raw:
        return IntentDecision(action="ignore", confidence=1.0)

    descriptive = looks_like_descriptive_ask(raw)
    should_run = (
        force
        or bool(candidates)
        or looks_like_followup(raw)
        or looks_like_collection_request(raw)
        or descriptive
    )
    if not should_run:
        return IntentDecision(action="passthrough", confidence=1.0, source="skip")

    heuristic = heuristic_intent(text, candidates=candidates, pending_query=pending_query)

    if not settings.openai_configured:
        return heuristic

    # High-confidence local follow-ups skip the model hop (cost/latency).
    if heuristic.source == "heuristic" and heuristic.confidence >= 0.85 and heuristic.action in {
        "pick",
        "pick_many",
        "ignore",
    }:
        return heuristic

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        payload = {
            "user_message": redact(raw)[:240],
            "pending_query": redact(pending_query)[:120] if pending_query else "",
            "last_bot_reply": redact(last_bot_reply)[:400] if last_bot_reply else "",
            "candidates": _candidate_blob(candidates or []),
            "descriptive_ask": descriptive and not candidates,
        }
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=180,
            temperature=0,
        )
        drafted = (response.choices[0].message.content or "").strip()
        parsed = _parse_model_json(
            drafted,
            candidate_count=len(candidates or []),
            descriptive_ask=descriptive and not candidates,
        )
        if parsed is None:
            return heuristic if heuristic.action != "passthrough" else IntentDecision(
                action="clarify" if candidates or descriptive else "passthrough",
                clarify_question=(
                    f"Which one — reply 1–{min(3, len(candidates or []))}, "
                    "'all of them', or a clearer title?"
                    if candidates
                    else (
                        "Which movie or series did you mean? Send the title if you know it."
                        if descriptive
                        else ""
                    )
                ),
                confidence=0.4,
                source="openai_fallback",
            )
        parsed.source = "openai"
        return parsed
    except Exception as exc:  # noqa: BLE001
        log.info("telegram intent openai failed: %s", redact(str(exc)))
        return heuristic


def _parse_model_json(
    raw: str,
    *,
    candidate_count: int,
    descriptive_ask: bool = False,
) -> IntentDecision | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "passthrough").strip().lower()
    if action not in {"passthrough", "ignore", "clarify", "pick", "pick_many", "search"}:
        return None
    indices_raw = data.get("indices") or data.get("picks") or []
    indices: list[int] = []
    if isinstance(indices_raw, list):
        for item in indices_raw:
            try:
                num = int(item)
            except (TypeError, ValueError):
                continue
            if candidate_count and 1 <= num <= candidate_count:
                indices.append(num)
            elif not candidate_count and 1 <= num <= MAX_BATCH:
                indices.append(num)
    # de-dupe preserve order
    seen: set[int] = set()
    uniq: list[int] = []
    for num in indices:
        if num not in seen:
            seen.add(num)
            uniq.append(num)
    indices = uniq[:MAX_BATCH]

    conf = data.get("confidence")
    try:
        confidence = float(conf) if conf is not None else 0.6
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))

    search_title = str(data.get("search_title") or data.get("title") or "").strip()[:200]
    select_all = bool(data.get("select_all"))
    clarify = str(data.get("clarify_question") or data.get("question") or "").strip()[:400]
    media_kind = str(data.get("media_kind") or data.get("kind") or "").strip().lower()
    if media_kind not in {"movie", "tv"}:
        media_kind = ""

    if action in {"pick", "pick_many"} and not indices and select_all and candidate_count:
        indices = list(range(1, min(candidate_count, MAX_BATCH) + 1))
        action = "pick_many"
    if action == "pick" and len(indices) > 1:
        action = "pick_many"
    if action == "pick" and not indices:
        return IntentDecision(
            action="clarify",
            clarify_question=clarify
            or (
                f"Which one — reply 1–{min(3, candidate_count)}?"
                if candidate_count
                else "Which title did you mean?"
            ),
            confidence=confidence,
        )
    if action == "pick_many" and not indices:
        return IntentDecision(
            action="clarify",
            clarify_question=clarify or "Which titles should I queue?",
            confidence=confidence,
        )
    if action == "search":
        if not search_title:
            return IntentDecision(
                action="clarify",
                clarify_question=clarify or "Which series or title should I search for?",
                confidence=confidence,
            )
        # Never treat an unresolved plot sentence as a catalog title.
        if looks_like_descriptive_ask(search_title) or (
            descriptive_ask and confidence < MIN_RESOLVE_CONFIDENCE
        ):
            return IntentDecision(
                action="clarify",
                clarify_question=clarify
                or "Which movie or series did you mean? Send the title if you know it.",
                confidence=confidence,
                media_kind=media_kind,
            )
    if action == "clarify" and not clarify:
        clarify = (
            f"Which one — reply 1–{min(3, candidate_count)}, 'all of them', or a clearer title?"
            if candidate_count
            else "Which movie or series did you mean?"
        )

    return IntentDecision(
        action=action,  # type: ignore[arg-type]
        indices=indices,
        search_title=search_title,
        select_all=select_all,
        media_kind=media_kind,
        clarify_question=clarify,
        confidence=confidence,
    )


__all__ = [
    "IntentDecision",
    "MAX_BATCH",
    "MAX_CANDIDATES",
    "MIN_RESOLVE_CONFIDENCE",
    "heuristic_intent",
    "interpret_intent",
    "looks_like_collection_request",
    "looks_like_descriptive_ask",
    "looks_like_followup",
]
