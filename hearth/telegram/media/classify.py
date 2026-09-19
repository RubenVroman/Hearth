"""Jev-first classifier for Telegram media asks.

Happy path: every media-ish turn hits TypeSafe System One (parallel Choice /
Noul / Score) via ``evaluate_telegram_media``. OpenAI is only used when Jev
says ``descriptive_riddle`` / ``needs_llm`` (or confidence is too low).

Fail-open: missing key, disabled Jev, or API errors → local heuristics (today's
deterministic path), never invent a queue.
"""

from __future__ import annotations

import logging
import re

from hearth.config import settings
from hearth.jev import (
    evaluate_telegram_media,
    log_shadow_outcome,
    media_ask_choice,
    needs_llm_resolve,
)
from hearth.jev.schema import JevVerdict, MEDIA_ASK_KINDS
from hearth.telegram.heuristics import (
    looks_like_concrete_title,
    looks_like_confirm_no,
    looks_like_confirm_yes,
)
from hearth.telegram.media.editions import extract_edition
from hearth.telegram.media.types import MediaAskKind, MediaIntent
from hearth.telegram.models import MediaQuery

log = logging.getLogger("hearth.telegram")

_YEAR_PAREN = re.compile(r"\(\s*((?:19|20)\d{2})\s*\)")

_SERIES_ALL = re.compile(
    r"\b("
    r"all\s+(?:the\s+)?(?:movies|films|parts|ones|of\s+them)|"
    r"all\s+(?:the\s+)?(?:harry\s+potters?|lotr|lord\s+of\s+the\s+rings)|"
    r"(?:the\s+)?whole\s+(?:series|franchise|saga|trilogy|collection)|"
    r"(?:every|alle)\s+(?:movie|film|part|one)|"
    r"complete\s+(?:series|collection|saga|trilogy)|"
    r"alle\s+(?:films|delen|movies)|"
    r"hele\s+(?:reeks|serie|franchise|trilogie)|"
    r"full\s+(?:series|franchise|saga|trilogy)"
    r")\b",
    re.I,
)
_ALL_PREFIX = re.compile(
    r"^\s*all\s+(?:of\s+|the\s+)?(?P<title>.+?)(?:\s+movies|\s+films)?\s*$",
    re.I,
)
_TITLE_ALL_SUFFIX = re.compile(
    r"^(?P<title>.+?)(?:,\s*|\s+)"
    r"(?:all(?:\s+(?:the\s+)?(?:movies|films|parts|ones))?|"
    r"the\s+whole\s+(?:series|franchise|saga|trilogy)|"
    r"complete\s+(?:series|collection))\s*$",
    re.I,
)


def _normalize_franchise_seed(seed: str) -> str:
    cleaned = re.sub(
        r"\b(?:movies|films|parts|ones|series|franchise|saga|trilogy)\b",
        " ",
        seed,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    # "Harry Potters" / "Avengerses" → drop a trailing plural s on the last token.
    tokens = cleaned.split()
    if tokens and len(tokens[-1]) > 3 and tokens[-1].casefold().endswith("s"):
        last = tokens[-1]
        if not last.casefold().endswith(("ss", "us", "is", "ones")):
            tokens[-1] = last[:-1]
            cleaned = " ".join(tokens)
    return cleaned
_CHAT_ABOUT = re.compile(
    r"(?:"
    r"what(?:'s|\s+is|\s+are)\s+(?:that|it|this|the\s+\w[\w' -]{0,40})\s+about\b|"
    r"what(?:'s|\s+is)\s+.+\s+about\b|"
    r"(?:tell\s+me\s+)?(?:about|the\s+plot\s+of|plot\s+of|synopsis\s+of|summary\s+of)\b|"
    r"\b(?:plot|synopsis|summary)\s+(?:of|for)\b|"
    r"who\s+(?:directed|stars|starred|wrote|plays|played)\b|"
    r"when\s+(?:did|was|came|released)\b|"
    r"(?:what|which)\s+year\b|"
    r"waar\s+(?:gaat|is)\b.+\bover\b|"
    r"\bwaarover\b|"
    r"vertel\s+(?:me\s+)?(?:meer\s+)?over\b"
    r")",
    re.I,
)
_GRAB_INTENT = re.compile(
    r"\b("
    r"download|grab|snatch|request|queue|"
    r"get\s+(?:me\s+)?(?:the|it|that|this|all)|"
    r"haal|vraag|zoek\s+(?:op|naar)"
    r")\b",
    re.I,
)
_LIST_ASK = re.compile(
    r"^\s*(?:"
    r"what(?:'s|\s+is)\s+(?:on|in)\s+(?:the\s+)?(?:list|queue)|"
    r"list\s+(?:movies|films|shows|requests)|"
    r"show\s+(?:me\s+)?(?:the\s+)?(?:list|queue)|"
    r"any\s+recommendations?"
    r")\s*[.!?]*\s*$",
    re.I,
)

# Well-known franchise seeds that should present multiple films even without "all".
_KNOWN_FRANCHISE_SEEDS = frozenset(
    {
        "harry potter",
        "lord of the rings",
        "lotr",
        "hobbit",
        "the hobbit",
        "star wars",
        "marvel",
        "avengers",
        "fast and furious",
        "mission impossible",
        "john wick",
        "matrix",
        "the matrix",
        "jurassic park",
        "jurassic world",
        "pirates of the caribbean",
        "indiana jones",
        "spider-man",
        "spiderman",
        "batman",
        "transformers",
    }
)

_JEV_TO_KIND: dict[str, MediaAskKind] = {
    "exact_title": "exact_title",
    "known_franchise": "known_franchise",
    "series_all": "series_all",
    "edition_aware": "edition",
    "descriptive_riddle": "describe",
    "chat_about_title": "chat_about",
    "not_media": "other",
}


def _clean_title_bits(text: str) -> tuple[str, int | None]:
    raw = re.sub(r"\s+", " ", (text or "").strip(" -–—|,."))
    year: int | None = None
    match = _YEAR_PAREN.search(raw)
    if match:
        try:
            year = int(match.group(1))
        except (TypeError, ValueError):
            year = None
        raw = (raw[: match.start()] + " " + raw[match.end() :]).strip(" -–—|,.")
        raw = re.sub(r"\s+", " ", raw)
    return raw, year


def _series_seed(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if not (
        _SERIES_ALL.search(raw) or _ALL_PREFIX.match(raw) or _TITLE_ALL_SUFFIX.match(raw)
    ):
        return None
    for pattern in (_TITLE_ALL_SUFFIX, _ALL_PREFIX):
        match = pattern.match(raw)
        if match:
            seed, _ = _clean_title_bits(match.group("title"))
            seed = _normalize_franchise_seed(seed)
            if len(seed) >= 2:
                return seed
    cleaned = _SERIES_ALL.sub(" ", raw)
    cleaned = _normalize_franchise_seed(cleaned)
    seed, _ = _clean_title_bits(cleaned)
    if len(seed) >= 2:
        return seed
    return None


_CHAT_TITLE_STRIP = re.compile(
    r"^(?:"
    r"what(?:'s|\s+is|\s+are)\s+(?:the\s+)?|"
    r"(?:tell\s+me\s+)?(?:about|the\s+plot\s+of|plot\s+of|synopsis\s+of|summary\s+of)\s+|"
    r"(?:plot|synopsis|summary)\s+(?:of|for)\s+|"
    r"who\s+(?:directed|stars|starred|wrote|plays|played)\s+|"
    r"when\s+(?:did|was|came|released)\s+|"
    r"(?:what|which)\s+year\s+(?:is|was|did)\s+|"
    r"vertel\s+(?:me\s+)?(?:meer\s+)?over\s+"
    r")",
    re.I,
)
_CHAT_TITLE_TAIL = re.compile(
    r"\s+(?:about|over|released|come\s+out|uitgekomen)\s*\??\s*$",
    re.I,
)


def _title_hint_from_chat(text: str, parsed: MediaQuery | None) -> tuple[str, int | None, str]:
    if parsed and parsed.title and looks_like_concrete_title(parsed.title):
        year = parsed.year
        title = parsed.title.strip()
        if year is None:
            title, year = _clean_title_bits(title)
        return title, year, (parsed.media_type or "")
    raw = (text or "").strip()
    cleaned = _CHAT_TITLE_STRIP.sub("", raw)
    cleaned = _CHAT_TITLE_TAIL.sub("", cleaned)
    cleaned = cleaned.strip(" ?!.")
    title, year = _clean_title_bits(cleaned)
    if looks_like_concrete_title(title):
        return title, year, ""
    return "", year, ""


def _title_from_parsed(raw: str, parsed: MediaQuery | None) -> tuple[str, int | None, str]:
    if parsed and parsed.title:
        year = parsed.year
        title = parsed.title.strip()
        if year is None:
            title, year = _clean_title_bits(title)
        return title, year, (parsed.media_type or "")
    title, year = _clean_title_bits(raw)
    return title, year, ""


def _enrich(
    kind: MediaAskKind,
    text: str,
    *,
    parsed: MediaQuery | None,
    confidence: float,
    source: str,
    needs_llm: bool,
    jev: JevVerdict | None = None,
    note: str = "",
) -> MediaIntent:
    raw = (text or "").strip()
    media_type = (parsed.media_type or "") if parsed else ""

    if kind == "edition":
        edition = extract_edition(raw)
        if edition is not None and edition.present:
            clean, year = _clean_title_bits(edition.clean_title)
            if parsed and parsed.year is not None:
                year = parsed.year
            return MediaIntent(
                kind="edition",
                search_title=clean,
                year=year,
                media_type=media_type,
                edition_key=edition.key,
                edition_label=edition.label,
                confidence=confidence,
                source=source,
                needs_llm=False,
                raw_text=raw,
                note=note or "edition_aware",
                jev=jev,
            )
        # Jev said edition but we could not strip — fall back to exact search title.
        title, year, media_type = _title_from_parsed(raw, parsed)
        return MediaIntent(
            kind="edition",
            search_title=title,
            year=year,
            media_type=media_type,
            confidence=confidence,
            source=source,
            needs_llm=False,
            raw_text=raw,
            note=note or "edition_unstripped",
            jev=jev,
        )

    if kind == "series_all":
        seed = _series_seed(raw) or _title_from_parsed(raw, parsed)[0]
        year = parsed.year if parsed else None
        return MediaIntent(
            kind="series_all",
            search_title=seed,
            year=year,
            media_type=media_type or "movie",
            confidence=confidence,
            source=source,
            needs_llm=False,
            raw_text=raw,
            note=note or "franchise_all",
            jev=jev,
        )

    if kind in {"exact_title", "known_franchise"}:
        title, year, media_type = _title_from_parsed(raw, parsed)
        # Strip accidental edition words if present but Jev chose exact/franchise.
        edition = extract_edition(title)
        if edition is not None and edition.present:
            title = edition.clean_title
        franchise_note = note
        if kind == "known_franchise" or title.casefold() in _KNOWN_FRANCHISE_SEEDS:
            kind = "known_franchise"
            franchise_note = franchise_note or "franchise_seed"
        return MediaIntent(
            kind=kind,
            search_title=title,
            year=year,
            media_type=media_type,
            confidence=confidence,
            source=source,
            needs_llm=False,
            raw_text=raw,
            note=franchise_note,
            jev=jev,
        )

    if kind == "chat_about":
        title, year, media_type = _title_hint_from_chat(raw, parsed)
        return MediaIntent(
            kind="chat_about",
            search_title=title,
            year=year,
            media_type=media_type,
            confidence=confidence,
            source=source,
            needs_llm=True,
            raw_text=raw,
            note=note or "info_only",
            jev=jev,
        )

    if kind == "describe":
        return MediaIntent(
            kind="describe",
            search_title="",
            year=parsed.year if parsed else None,
            media_type=media_type,
            confidence=confidence,
            source=source,
            needs_llm=True,
            raw_text=raw,
            note=note or "descriptive_riddle",
            jev=jev,
        )

    return MediaIntent(
        kind="other",
        raw_text=raw,
        confidence=confidence,
        source=source,
        needs_llm=False,
        note=note or "not_media",
        jev=jev,
    )


def _local_classify(text: str, *, parsed: MediaQuery | None) -> MediaIntent:
    """Deterministic fail-open classifier when Jev is unavailable."""
    raw = (text or "").strip()
    if not raw:
        return MediaIntent(kind="other", raw_text=raw, confidence=0.0, source="local")

    if looks_like_confirm_yes(raw) or looks_like_confirm_no(raw):
        return MediaIntent(
            kind="other",
            raw_text=raw,
            confidence=0.2,
            source="local",
            note="confirm_token",
        )

    if _LIST_ASK.match(raw):
        return MediaIntent(
            kind="other",
            raw_text=raw,
            confidence=0.95,
            source="local",
            note="list_ask",
        )

    if _CHAT_ABOUT.search(raw) and not _GRAB_INTENT.search(raw):
        return _enrich("chat_about", raw, parsed=parsed, confidence=0.9, source="local", needs_llm=True)

    series_seed = _series_seed(raw)
    if series_seed:
        return _enrich(
            "series_all",
            raw,
            parsed=parsed,
            confidence=0.92,
            source="local",
            needs_llm=False,
            note="franchise_all",
        )

    edition = extract_edition(raw)
    if edition is not None and edition.present:
        clean, _ = _clean_title_bits(edition.clean_title)
        if looks_like_concrete_title(clean):
            return _enrich(
                "edition",
                raw,
                parsed=parsed,
                confidence=0.9,
                source="local",
                needs_llm=False,
            )

    probe = raw
    if parsed and parsed.title and parsed.reason == "title":
        probe = parsed.raw_text or parsed.title

    if looks_like_concrete_title(probe) or (parsed is not None and parsed.tmdb_id is not None):
        title, _, _ = _title_from_parsed(probe if not parsed else raw, parsed)
        kind: MediaAskKind = (
            "known_franchise"
            if title.casefold() in _KNOWN_FRANCHISE_SEEDS
            else "exact_title"
        )
        return _enrich(kind, raw, parsed=parsed, confidence=0.88, source="local", needs_llm=False)

    return _enrich(
        "describe",
        raw,
        parsed=parsed,
        confidence=0.75,
        source="local",
        needs_llm=True,
    )


def classify_media_ask_sync(text: str, *, parsed: MediaQuery | None = None) -> MediaIntent:
    """Deterministic local classification (unit tests / fail-open path)."""
    return _local_classify(text, parsed=parsed)


async def classify_media_ask(
    text: str,
    *,
    parsed: MediaQuery | None = None,
) -> MediaIntent:
    """Jev-first media intent. OpenAI is not called here — only routed."""
    local = _local_classify(text, parsed=parsed)

    # Bare confirm tokens / list asks never become download routes via Jev.
    if local.note in {"confirm_token", "list_ask"}:
        return local

    if not settings.jev_enabled:
        return local

    try:
        verdict = await evaluate_telegram_media(text)
        log_shadow_outcome(
            verdict,
            channel="telegram_media_router",
            tools=[],
            outcome=f"local:{local.kind}",
        )
        if not verdict.ok or verdict.answers is None:
            return MediaIntent(
                kind=local.kind,
                search_title=local.search_title,
                year=local.year,
                media_type=local.media_type,
                edition_key=local.edition_key,
                edition_label=local.edition_label,
                confidence=local.confidence,
                source="local_failopen",
                needs_llm=local.needs_llm,
                raw_text=local.raw_text,
                note=local.note or verdict.reason,
                jev=verdict,
            )

        picked = media_ask_choice(verdict.answers)
        if picked is None:
            # Low confidence → prefer LLM when local already wants it, else local.
            needs = needs_llm_resolve(verdict.answers, media_choice=None) or local.needs_llm
            return MediaIntent(
                kind=local.kind if not needs else (
                    "describe" if local.kind != "chat_about" else local.kind
                ),
                search_title=local.search_title,
                year=local.year,
                media_type=local.media_type,
                edition_key=local.edition_key,
                edition_label=local.edition_label,
                confidence=local.confidence,
                source="local_failopen",
                needs_llm=needs,
                raw_text=local.raw_text,
                note="jev_low_confidence",
                jev=verdict,
            )

        choice, conf = picked
        if choice not in MEDIA_ASK_KINDS:
            return local

        kind = _JEV_TO_KIND.get(choice, "other")
        needs = needs_llm_resolve(verdict.answers, media_choice=choice)
        # media_ask is first-class product routing whenever Jev is enabled + ok.
        return _enrich(
            kind,
            text,
            parsed=parsed,
            confidence=conf,
            source="jev",
            needs_llm=needs,
            jev=verdict,
            note=choice,
        )
    except Exception:  # noqa: BLE001 — fail open
        log.warning("jev media router failed open", exc_info=True)
        return MediaIntent(
            kind=local.kind,
            search_title=local.search_title,
            year=local.year,
            media_type=local.media_type,
            edition_key=local.edition_key,
            edition_label=local.edition_label,
            confidence=local.confidence,
            source="local_failopen",
            needs_llm=local.needs_llm,
            raw_text=local.raw_text,
            note=local.note or "jev_exception",
        )


__all__ = ["classify_media_ask", "classify_media_ask_sync"]
