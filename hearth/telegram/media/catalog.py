"""gpt-4o helpers for descriptive Telegram media asks and title Q&A.

Queue/download never happens here — callers still require Get / yes confirm.
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

TELEGRAM_INTENT_MODEL = "gpt-4o"

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
    r"het\s+filmpje|die\s+serie|glasses|bril"
    r")\b",
    re.I,
)

_GUESS_SYSTEM = (
    "You interpret short Telegram messages for a house movie/TV download bot. "
    "Return JSON only with keys: "
    "candidates (array of up to 3 objects with search_title, year, media_kind, "
    "confidence), and optionally primary (same shape as one candidate). "
    "The user sent a plot, vibe, appearance, actor, riddle, or Dutch/English "
    "description — not an exact catalog title. Guess the best well-known "
    "catalog title(s). search_title must be the clean catalog name only "
    "(no plot words, no 'with Actor' clauses, no edition words). "
    "Include year when known. media_kind is movie|tv|empty. "
    "Never invent encyclopedia text. Never say to queue or download. "
    "If truly unsure, return candidates as []."
)

_CHAT_SYSTEM = (
    "You answer brief factual questions about movies/TV for a house Telegram bot. "
    "Return JSON only with keys: answer (string, max 2 short sentences), "
    "search_title (string, catalog title if identifiable, else empty), "
    "year (int or null), media_kind (movie|tv|empty). "
    "Use the provided catalog_context when present. "
    "Do not offer to download, queue, or grab. Do not invent streaming links. "
    "If unsure, say so briefly."
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


def _parse_one_guess(data: dict[str, Any]) -> CatalogGuess | None:
    title = str(data.get("search_title") or data.get("title") or "").strip()[:200]
    if not title:
        return None
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
        confidence = float(
            data.get("confidence") if data.get("confidence") is not None else 0.6
        )
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


def _parse_guess_payload(data: dict[str, Any]) -> list[CatalogGuess]:
    out: list[CatalogGuess] = []
    seen: set[str] = set()
    buckets: list[Any] = []
    primary = data.get("primary")
    if isinstance(primary, dict):
        buckets.append(primary)
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        buckets.extend(candidates)
    # Backward-compatible single-object shape from older prompts.
    if not buckets and (data.get("search_title") or data.get("title")):
        buckets.append(data)
    for item in buckets:
        if not isinstance(item, dict):
            continue
        guess = _parse_one_guess(item)
        if guess is None:
            continue
        key = guess.search_title.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(guess)
        if len(out) >= 3:
            break
    return out


async def guess_catalog_titles(text: str) -> list[CatalogGuess]:
    """Resolve a plot/vibe/actor ask to one or more catalog title guesses."""
    raw = (text or "").strip()
    if not raw or not settings.openai_configured:
        return []
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        payload = {"user_message": redact(raw)[:240]}
        response = await client.chat.completions.create(
            model=telegram_intent_model(),
            messages=[
                {"role": "system", "content": _GUESS_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=280,
            temperature=0,
        )
        drafted = (response.choices[0].message.content or "").strip()
        if not drafted:
            return []
        data = json.loads(drafted)
        if not isinstance(data, dict):
            return []
        return _parse_guess_payload(data)
    except Exception:  # noqa: BLE001 — fall back to asking for a title
        log.exception("telegram catalog guess failed")
        return []


async def guess_catalog_title(text: str) -> CatalogGuess | None:
    """Resolve to the single best catalog title guess (compat wrapper)."""
    guesses = await guess_catalog_titles(text)
    return guesses[0] if guesses else None


async def answer_catalog_question(
    text: str,
    *,
    catalog_context: str = "",
) -> dict[str, Any] | None:
    """Answer a title Q&A without offering a download."""
    raw = (text or "").strip()
    if not raw or not settings.openai_configured:
        return None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        payload = {
            "user_message": redact(raw)[:240],
            "catalog_context": redact(catalog_context or "")[:600],
        }
        response = await client.chat.completions.create(
            model=telegram_intent_model(),
            messages=[
                {"role": "system", "content": _CHAT_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=220,
            temperature=0,
        )
        drafted = (response.choices[0].message.content or "").strip()
        if not drafted:
            return None
        data = json.loads(drafted)
        if not isinstance(data, dict):
            return None
        answer = str(data.get("answer") or "").strip()
        if not answer:
            return None
        return {
            "answer": answer[:500],
            "search_title": str(data.get("search_title") or "").strip()[:200],
            "year": data.get("year"),
            "media_kind": str(data.get("media_kind") or "").strip().lower(),
        }
    except Exception:  # noqa: BLE001
        log.exception("telegram catalog Q&A failed")
        return None


__all__ = [
    "TELEGRAM_INTENT_MODEL",
    "CatalogGuess",
    "answer_catalog_question",
    "guess_catalog_title",
    "guess_catalog_titles",
    "telegram_intent_model",
]
