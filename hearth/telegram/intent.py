"""Lightweight gpt-4o hop for descriptive Telegram movie/TV asks.

Obvious titles stay on the deterministic Codex parser path. Plot, vibe, Dutch
descriptions, and actor-ish phrasing are resolved to one catalog title guess
here — the bot still asks before queueing (Get / yes). Never invent a grab.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact

log = logging.getLogger("hearth.telegram")

# Prefer gpt-4o for this hop. Honor OPENAI_MODEL when it is already a named
# non-mini model (house chat keeps its own default).
TELEGRAM_INTENT_MODEL = "gpt-4o"

_YEAR_PAREN = re.compile(r"\(\s*((?:19|20)\d{2})\s*\)")
_CATALOG_ID = re.compile(r"\btmdb\s*:\s*(?:movie|tv|film|show|series|serie\s*:)?\s*\d+", re.I)
_URLISH = re.compile(r"https?://", re.I)
_CONFIRM_YES = re.compile(
    r"^\s*(?:"
    r"y+e+s+|y+e+a+h+|y+e+p+|y+u+p+|su+re+|correct|right|"
    r"that(?:'s| is|s)?\s+(?:it|the\s+one|correct|right)|"
    r"j+a+|jawel|klopt|juist|"
    r"dat\s+is\s+(?:hem|die|het|correct)|"
    r"doe\s+(?:maar|die)|"
    r"go\s+ahead"
    r")\s*[.!?]*\s*$",
    re.I,
)
_CONFIRM_THUMBS = re.compile(r"^\s*👍[\U0001F3FB-\U0001F3FF]?\uFE0F?\s*[.!?]*\s*$")
_CONFIRM_NO = re.compile(
    r"^\s*(?:"
    r"n+o+|n+o+p+e+|n+a+h+|nee+|neen|"
    r"niet(?:\s+die)?|not\s+that|wrong|"
    r"geen\s+van\s+(?:deze|die)|"
    r"none(?:\s+of\s+(?:these|them|em|'em))?"
    r")\s*[.!?]*\s*$",
    re.I,
)
_CAST_CLAUSE = re.compile(
    r"^(?P<title>.+?)\s+(?:with|featuring|starring|feat\.?|ft\.?|met)\s+(?P<who>.+)$",
    re.I,
)
_ARTICLE_WHO = re.compile(r"^(?:the|a|an|de|het|een)\b", re.I)
_DESCRIPTIVE = re.compile(
    r"\b("
    r"about|waar|waarin|film\s+met|movie\s+about|series\s+about|"
    r"die\s+film|deze\s+film|that\s+movie|this\s+movie|looking\s+for|"
    r"someone\s+who|iemand\s+die|guy\s+with|girl\s+with|man\s+with|"
    r"woman\s+with|boy\s+with|kid\s+with|scar|litteken|wizard|tovenaar|"
    r"puzzel|spiegel|coolest|oldest|newest|classic\s+\w+\s+movie|"
    r"old\s+\w+\s+movie|horror\s+movie|sci-?fi|spaceship|space\s+ship|"
    r"you\s+can\s+f(?:i)?n[ds]|movie\s+on\s+a|film\s+on\s+a|"
    r"on\s+a\s+spaceship|vibe|like\s+that|something\s+like|"
    r"het\s+filmpje|die\s+serie"
    r")\b",
    re.I,
)
_PLOT_SHELL = re.compile(
    r"(?:(?:that|this|the|a|an|die|deze|dat|een)\s+)*"
    r"(?:movie|film|films|series|show|one|ones)?",
    re.I,
)

_SYSTEM = (
    "You interpret short Telegram messages for a house movie/TV download bot. "
    "Return JSON only with keys: search_title (string), year (int or null), "
    "media_kind (movie|tv|empty), confidence (0-1). "
    "The user sent a plot, vibe, appearance, actor, or Dutch/English description — "
    "not an exact catalog title. Guess the ONE best well-known catalog title. "
    "search_title must be the clean catalog name only (no plot words, no "
    "'with Actor' clauses). Include year when known. "
    "Never invent encyclopedia text. Never say to queue or download. "
    "If truly unsure, return search_title empty and confidence 0."
)


@dataclass(frozen=True, slots=True)
class CatalogGuess:
    search_title: str
    year: int | None = None
    media_kind: str = ""
    confidence: float = 0.0
    source: str = "model"


def telegram_intent_model() -> str:
    configured = (settings.openai_model or "").strip()
    if configured and "mini" not in configured.lower():
        return configured
    return TELEGRAM_INTENT_MODEL


def looks_like_confirm_yes(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return False
    if _CONFIRM_THUMBS.match(raw):
        return True
    return bool(_CONFIRM_YES.match(raw))


def looks_like_confirm_no(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return False
    return bool(_CONFIRM_NO.match(raw))


def _looks_like_actor_clue(text: str) -> bool:
    """True for short ``Title with Person`` asks — not ``Title with the Noun`` films."""
    match = _CAST_CLAUSE.match((text or "").strip())
    if not match:
        return False
    title = match.group("title").strip(" -–—|,.")
    who = match.group("who").strip(" -–—|,.")
    if not title or not who or _ARTICLE_WHO.match(who):
        return False
    who_words = who.split()
    if not 1 <= len(who_words) <= 4:
        return False
    if _DESCRIPTIVE.search(who) or _DESCRIPTIVE.search(title):
        return False
    return len(title.split()) <= 4 and bool(re.search(r"[A-Za-zÀ-ÿ]", who))


def looks_like_concrete_title(text: str) -> bool:
    """True for short near-exact title asks (not plot/actor sentences)."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _CATALOG_ID.search(raw) or _URLISH.search(raw):
        return False
    if looks_like_confirm_yes(raw) or looks_like_confirm_no(raw):
        return False
    if _YEAR_PAREN.search(raw):
        # Title (YYYY) is an explicit catalog ask — keep the Codex path.
        year_match = _YEAR_PAREN.search(raw)
        assert year_match is not None
        before = raw[: year_match.start()].strip(" -–—|,.")
        if before and re.search(r"[A-Za-zÀ-ÿ]", before) and len(before.split()) <= 8:
            return True
    cleaned = re.sub(
        r"\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|dv|dolby\s*vision)\b",
        " ",
        raw,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    # Actor-ish refinements ("Land with robin wright") use gpt-4o; leave
    # preposition titles like "Late Night with the Devil" on the Codex path.
    if _looks_like_actor_clue(cleaned):
        return False
    if _PLOT_SHELL.fullmatch(cleaned):
        return False
    words = cleaned.split()
    if len(words) > 8 or len(cleaned) > 80:
        return False
    if _DESCRIPTIVE.search(cleaned) or _DESCRIPTIVE.search(raw):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]", cleaned))


def _parse_guess_payload(data: dict[str, Any]) -> CatalogGuess | None:
    title = str(data.get("search_title") or data.get("title") or "").strip()[:200]
    if not title:
        return None
    # Refuse echoing a long descriptive sentence back as the search title.
    if len(title.split()) > 8 or _DESCRIPTIVE.search(title):
        return None
    year: int | None = None
    year_raw = data.get("year")
    if year_raw not in (None, ""):
        try:
            year_i = int(year_raw)
            if 1900 <= year_i <= 2100:
                year = year_i
        except (TypeError, ValueError):
            year = None
    kind = str(data.get("media_kind") or data.get("kind") or "").strip().lower()
    if kind not in {"movie", "tv"}:
        kind = ""
    try:
        confidence = float(data.get("confidence") if data.get("confidence") is not None else 0.6)
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    if confidence < 0.45:
        return None
    return CatalogGuess(
        search_title=title,
        year=year,
        media_kind=kind,
        confidence=confidence,
    )


async def guess_catalog_title(text: str) -> CatalogGuess | None:
    """Resolve a plot/vibe/actor ask to one catalog title via gpt-4o."""
    raw = (text or "").strip()
    if not raw or not settings.openai_configured:
        return None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        payload = {"user_message": redact(raw)[:240]}
        response = await client.chat.completions.create(
            model=telegram_intent_model(),
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
        if not drafted:
            return None
        data = json.loads(drafted)
        if not isinstance(data, dict):
            return None
        return _parse_guess_payload(data)
    except Exception:  # noqa: BLE001 — fall back to asking for a title
        log.exception("telegram catalog guess failed")
        return None


__all__ = [
    "TELEGRAM_INTENT_MODEL",
    "CatalogGuess",
    "guess_catalog_title",
    "looks_like_concrete_title",
    "looks_like_confirm_no",
    "looks_like_confirm_yes",
    "telegram_intent_model",
]
