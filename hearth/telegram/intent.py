"""Telegram movie-request intent — conversation-first.

Instant path (no model) only when there is no reasonable doubt:
catalog id/URL, explicit ``Title (YYYY)``, or a live numbered pick
(``1``/``2``/``3``, ``all of them``, ``de eerste`` while a list is on screen).

Everything else always calls a smart/fast model with the last ~8 turns of
this chat. No keyword gates, no franchise/actor maps. Unsure → clarify;
never invent a grab.
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

# Telegram conversation hop: prefer gpt-4o. Honor OPENAI_MODEL when it is
# already set to a named model other than mini (house UI keeps its own default).
TELEGRAM_INTENT_MODEL = "gpt-4o"

_FOLLOWUP_ALL = re.compile(
    r"\b("
    r"all(?:\s+of\s+(?:them|em|'em|it))?"
    r"|all\s+(?:the\s+)?(?:movies?|films?|ones?)"
    r"|every(?:one|thing)?"
    r"|allemaal|alles"
    r")\b",
    re.I,
)
_FOLLOWUP_FIRST = re.compile(
    r"^\s*(?:"
    r"(?:just\s+)?(?:the\s+)?first(?:\s+one)?|"
    r"(?:de\s+|het\s+)?eerste(?:\s+(?:een|één|one|film|movie))?"
    r")\s*[.!?]*\s*$",
    re.I,
)
_YEAR_PAREN = re.compile(r"\(\s*((?:19|20)\d{2})\s*\)")
_CATALOG_ID = re.compile(r"\btt\d{7,}|\btmdb:\d+|\btvdb:\d+", re.I)
_URLISH = re.compile(r"https?://", re.I)
_ORDINAL_PICK = re.compile(r"^\s*#?\s*([1-9])\s*$")

_SYSTEM = (
    "You interpret short Telegram messages for a house movie/TV download bot. "
    "Return JSON only. Never invent a grab. "
    "DEFAULT for chit-chat, emoji, reactions, meta talk about the bot, thanks, "
    "and off-topic chatter (NL+EN) is action=ignore (empty — no reply). "
    "Examples of ignore: 🙈, 'ga ik fixen', 'jaartallen kloppen niet', lol, "
    "thanks, ok, cool, short jokes about the bot. Do NOT ask which movie. "
    "You are in a multi-turn conversation: use recent_history (NL+EN) to resolve "
    "plot descriptions, pronouns, corrections, actor/artist clues, and misspellings. "
    "subject_title is the last resolved title — drop it when the user rejects it. "
    "rejected_titles must NEVER be re-offered as search_title or implied picks. "
    "When the user rejects the current/last suggestion (nee/niet die/not that/"
    "no not X) and adds new clues, return action=search with a NEW search_title "
    "from the original plot + new clues; do not ask them to pick 1–2 for a "
    "rejected film. Bare nee/no with no new info → action=clarify with a short "
    "useful question (not a 1–2 list). "
    "When candidates are listed and the user clearly picks among them, use "
    "pick/pick_many with 1-based indices only — never invent ids. "
    "When candidates are empty: resolve plot/character/premise to search_title "
    "(catalog title; franchise name OK) and year + media_kind when known. "
    "Do not echo the plot as search_title. "
    "Actor/artist names and misspellings are clues for you — never refuse because "
    "of spelling. If unsure which title, action=clarify once with a useful question. "
    "Actions: passthrough, ignore, clarify, pick, pick_many, search. "
    "search sets search_title (and year when known; select_all=true for whole "
    "series/trilogy). Catalog year wins later — still include your best year. "
    "search_title must be the catalog title only — strip trailing 'with Actor' / "
    "'featuring' / 'starring' / 'met …' clauses (those are clues for you, not "
    "part of the Overseerr query). media_kind is movie or tv when known; omit "
    "when unsure (Overseerr returns both). "
    "If the user clearly names a concrete catalog title with no ambiguity, "
    "action=search with that title (or passthrough)."
)


@dataclass
class IntentDecision:
    action: IntentAction = "passthrough"
    indices: list[int] = field(default_factory=list)
    search_title: str = ""
    year: int | None = None
    select_all: bool = False
    media_kind: str = ""
    clarify_question: str = ""
    confidence: float = 0.0
    source: str = "heuristic"


_EMOJI_ONLY = re.compile(
    r"^[\s"
    r"\U0001F300-\U0001FAFF"
    r"\U00002700-\U000027BF"
    r"\U0001F000-\U0001F02F"
    r"\U00002600-\U000026FF"
    r"\U0000FE00-\U0000FE0F"
    r"\U0000200D"
    r"\U00002122"
    r"\U0000231A-\U0000231B"
    r"\U000023E9-\U000023F3"
    r"\U000025AA-\U000025FE"
    r"\U00002B50"
    r"\U0001F1E0-\U0001F1FF"
    r"!?.…,~*\-_/\\'\"()]+$"
)


def looks_like_chatter(text: str) -> bool:
    """True for emoji/reactions/meta chatter that must not ask 'which movie'."""
    raw = (text or "").strip()
    if not raw:
        return True
    if len(raw) <= 2 and not re.search(r"[A-Za-zÀ-ÿ0-9]", raw):
        return True
    if _EMOJI_ONLY.match(raw) and not re.search(r"[A-Za-zÀ-ÿ0-9]", raw):
        return True
    lowered = re.sub(r"\s+", " ", raw).strip().lower()
    # Pure acknowledgements / thanks — not catalog asks.
    # Note: yes/nee/no stay out — they need conversation context (lists / rejects).
    chatter = {
        "ok",
        "okay",
        "k",
        "kk",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "lol",
        "haha",
        "cool",
        "nice",
        "great",
        "bot too dumb",
        "te dom",
    }
    if lowered in chatter:
        return True
    # Meta / self-talk without a title ask (keep short — model still sees longer plots).
    if len(lowered) <= 80 and not _YEAR_PAREN.search(raw) and not _CATALOG_ID.search(raw):
        meta_bits = (
            "ga ik fixen",
            "ik ga fixen",
            "jaartallen kloppen",
            "bot te",
            "too dumb",
            "niet slim",
            "kan geen gesprek",
            "hold a conversation",
        )
        if any(bit in lowered for bit in meta_bits):
            return True
    return False


def looks_like_concrete_title(text: str) -> bool:
    """True for short near-exact title asks (not plot sentences)."""
    raw = (text or "").strip()
    if not raw or looks_like_chatter(raw):
        return False
    if _CATALOG_ID.search(raw) or _URLISH.search(raw):
        return False
    if is_explicit_title_year(raw):
        return True
    # Strip quality tokens for length checks.
    cleaned = re.sub(
        r"\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|dv|dolby\s*vision)\b",
        " ",
        raw,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    words = cleaned.split()
    if len(words) > 8 or len(cleaned) > 80:
        return False
    # Plot / descriptive cues → conversation hop, not catalog-first.
    descriptive = re.compile(
        r"\b("
        r"about|waar|waarin|film\s+met|movie\s+about|series\s+about|"
        r"die\s+film|zoek|looking\s+for|someone\s+who|iemand\s+die|"
        r"puzzel|spiegel|tovenaar|wizard|harige|voeten"
        r")\b",
        re.I,
    )
    if descriptive.search(cleaned):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]", cleaned))


def telegram_intent_model() -> str:
    """Model for the Telegram conversation hop (smart + fast, no new env key)."""
    configured = (settings.openai_model or "").strip()
    if configured and "mini" not in configured.lower():
        return configured
    return TELEGRAM_INTENT_MODEL


def is_explicit_title_year(text: str) -> bool:
    """True for unambiguous ``Title (YYYY)`` (optional trailing quality)."""
    raw = (text or "").strip()
    if not raw or len(raw) > 200:
        return False
    if _CATALOG_ID.search(raw) or _URLISH.search(raw):
        return False
    match = _YEAR_PAREN.search(raw)
    if not match:
        return False
    # Year must be the disambiguator — title text before the paren.
    before = raw[: match.start()].strip(" -–—|,.")
    after = raw[match.end() :].strip()
    if not before or not re.search(r"[A-Za-zÀ-ÿ]", before):
        return False
    # Allow optional quality tokens after the year; reject extra clauses.
    if after and not re.fullmatch(
        r"(?:2160p|1080p|720p|480p|4k|uhd|hdr|dv|dolby\s*vision\s*)+",
        after,
        flags=re.I,
    ):
        return False
    return True


def instant_pick_decision(
    text: str,
    candidates: list[dict[str, Any]] | None,
) -> IntentDecision | None:
    """Live numbered-list shortcuts — only while candidates are on screen."""
    raw = (text or "").strip()
    if not raw or not candidates:
        return None
    n = len(candidates)
    if n == 0:
        return None

    digit = _ORDINAL_PICK.match(raw)
    if digit:
        idx = int(digit.group(1))
        if 1 <= idx <= n:
            return IntentDecision(
                action="pick",
                indices=[idx],
                confidence=1.0,
                source="instant",
            )
        return IntentDecision(
            action="clarify",
            clarify_question=f"Pick a number from 1–{min(3, n)} (or say 'all of them').",
            confidence=0.9,
            source="instant",
        )

    if _FOLLOWUP_ALL.search(raw) and len(raw) <= 40:
        return IntentDecision(
            action="pick_many",
            indices=list(range(1, min(n, MAX_BATCH) + 1)),
            select_all=True,
            confidence=1.0,
            source="instant",
        )

    if _FOLLOWUP_FIRST.match(raw):
        oldest = min(
            range(n),
            key=lambda i: _year_sort_key(candidates[i]) or 9999,
        )
        return IntentDecision(
            action="pick",
            indices=[oldest + 1],
            confidence=1.0,
            source="instant",
        )

    return None


def _year_sort_key(row: dict[str, Any]) -> int:
    year = row.get("year")
    try:
        return int(year)
    except (TypeError, ValueError):
        return 0


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


def _offline_fallback(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None,
    rejected_titles: list[str] | None,
) -> IntentDecision:
    """No API key: never invent titles from plots — ignore chatter, else clarify."""
    raw = (text or "").strip()
    if not raw or looks_like_chatter(raw):
        return IntentDecision(action="ignore", confidence=1.0, source="offline")
    instant = instant_pick_decision(raw, candidates)
    if instant is not None:
        return instant
    if candidates:
        return IntentDecision(
            action="clarify",
            clarify_question=(
                "Which movie did you mean? Send the title, or reply with a number "
                f"from the list (1–{min(3, len(candidates))})."
            ),
            confidence=0.4,
            source="offline",
        )
    rejected = ", ".join((rejected_titles or [])[:4])
    extra = f" (not {rejected})" if rejected else ""
    return IntentDecision(
        action="clarify",
        clarify_question=(
            f"Which movie or series did you mean{extra}? "
            "Send the title if you know it, or a bit more detail."
        ),
        confidence=0.4,
        source="offline",
    )


async def interpret_intent(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    pending_query: str = "",
    last_bot_reply: str = "",
    force: bool = False,
    history: list[dict[str, Any]] | None = None,
    subject_title: str = "",
    subject_media_kind: str = "",
    rejected_titles: list[str] | None = None,
) -> IntentDecision:
    """Interpret a user message. Always uses the model when configured.

    ``force`` is retained for callers; non-empty text with history/candidates/
    conversational content always hits the model. Instant catalog/year/pick
    shortcuts are decided by the inbox *before* calling this.
    """
    del force  # inbox decides instant vs AI; this hop always models when keyed
    raw = (text or "").strip()
    if not raw:
        return IntentDecision(action="ignore", confidence=1.0)

    # Clear chatter never needs a model hop — empty reply, no "which movie".
    # Skip when candidates are on screen (yes/1/all still need that context).
    if looks_like_chatter(raw) and not candidates:
        return IntentDecision(action="ignore", confidence=1.0, source="chatter")

    # Live-list shortcuts stay local even when the model is available — they are
    # the instant path (1/2/3, all of them, de eerste).
    instant = instant_pick_decision(raw, candidates)
    if instant is not None and instant.action in {"pick", "pick_many"}:
        return instant

    if not settings.openai_configured:
        return _offline_fallback(
            raw, candidates=candidates, rejected_titles=rejected_titles
        )

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        rejected = [
            redact(str(t))[:120]
            for t in (rejected_titles or [])
            if str(t).strip()
        ][:12]
        payload = {
            "user_message": redact(raw)[:240],
            "pending_query": redact(pending_query)[:120] if pending_query else "",
            "last_bot_reply": redact(last_bot_reply)[:400] if last_bot_reply else "",
            "candidates": _candidate_blob(candidates or []),
            "rejected_titles": rejected,
            "recent_history": history or [],
            "subject_title": redact(subject_title)[:120] if subject_title else "",
            "subject_media_kind": (
                subject_media_kind if subject_media_kind in {"movie", "tv"} else ""
            ),
        }
        model = telegram_intent_model()
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=200,
            temperature=0,
        )
        drafted = (response.choices[0].message.content or "").strip()
        parsed = _parse_model_json(
            drafted,
            candidate_count=len(candidates or []),
            rejected_titles=rejected,
        )
        if parsed is None:
            return IntentDecision(
                action="clarify",
                clarify_question=(
                    "Which movie or series did you mean? Send the title if you know it."
                ),
                confidence=0.4,
                source="openai_fallback",
            )
        parsed.source = "openai"
        return parsed
    except Exception as exc:  # noqa: BLE001
        log.info("telegram intent openai failed: %s", redact(str(exc)))
        return _offline_fallback(
            raw, candidates=candidates, rejected_titles=rejected_titles
        )


def _parse_model_json(
    raw: str,
    *,
    candidate_count: int,
    rejected_titles: list[str] | None = None,
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
    year_raw = data.get("year")
    year: int | None = None
    if year_raw not in (None, ""):
        try:
            year_i = int(year_raw)
            if 1900 <= year_i <= 2100:
                year = year_i
        except (TypeError, ValueError):
            year = None

    rejected_norm = {
        re.sub(r"\s+", " ", t).strip().lower()
        for t in (rejected_titles or [])
        if str(t).strip()
    }

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
        if search_title.strip().lower() in rejected_norm:
            return IntentDecision(
                action="clarify",
                clarify_question=clarify
                or "Which movie did you mean instead? Send another title or clue.",
                confidence=confidence,
                media_kind=media_kind,
            )
        if confidence < MIN_RESOLVE_CONFIDENCE:
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
        year=year,
        select_all=select_all,
        media_kind=media_kind,
        clarify_question=clarify,
        confidence=confidence,
    )


# --- Compatibility shims (not used as gates; kept so older imports/tests soft-fail) ---


def looks_like_followup(text: str) -> bool:
    """Deprecated as a gate — instant picks use ``instant_pick_decision``."""
    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return False
    if _FOLLOWUP_ALL.search(raw) or _FOLLOWUP_FIRST.match(raw):
        return True
    return raw.lower() in {
        "all",
        "both",
        "yes",
        "yep",
        "yeah",
        "sure",
        "ja",
        "jawel",
        "nee",
        "no",
        "nope",
        "allemaal",
        "alles",
    }


def looks_like_contextual_followup(text: str) -> bool:
    """Deprecated as a gate."""
    raw = (text or "").strip()
    return bool(raw) and len(raw) <= 80


def looks_like_collection_request(text: str) -> bool:
    """Deprecated as a gate."""
    raw = (text or "").strip()
    return bool(raw) and bool(_FOLLOWUP_ALL.search(raw))


def looks_like_descriptive_ask(text: str) -> bool:
    """Deprecated as a gate — plots always go to the model now."""
    raw = (text or "").strip()
    return bool(raw) and len(raw) >= 12 and not is_explicit_title_year(raw)


def heuristic_intent(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    pending_query: str = "",
) -> IntentDecision:
    """Deprecated offline helper — prefer ``interpret_intent`` / instant picks."""
    del pending_query
    instant = instant_pick_decision(text, candidates)
    if instant is not None:
        return instant
    return _offline_fallback(text, candidates=candidates, rejected_titles=None)


__all__ = [
    "IntentDecision",
    "MAX_BATCH",
    "MAX_CANDIDATES",
    "MIN_RESOLVE_CONFIDENCE",
    "TELEGRAM_INTENT_MODEL",
    "heuristic_intent",
    "instant_pick_decision",
    "interpret_intent",
    "is_explicit_title_year",
    "looks_like_chatter",
    "looks_like_concrete_title",
    "looks_like_collection_request",
    "looks_like_contextual_followup",
    "looks_like_descriptive_ask",
    "looks_like_followup",
    "telegram_intent_model",
]
