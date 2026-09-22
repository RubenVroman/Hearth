"""Jev-first classifier for Telegram media asks.

Happy path: every media-ish turn hits TypeSafe System One (parallel Choice /
Noul / Score) via ``evaluate_telegram_media``. OpenAI is only used when Jev
says ``descriptive_riddle`` / ``needs_llm`` (or confidence is too low).

Jev decides *which lane*; the deterministic extractors in this package decide
*what the lane gets* (franchise seed, edition, person, mood coordinates, plan
items). That split keeps the fast path free of prose and keeps the fail-open
path — missing key, disabled Jev, API errors — just as capable.
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
from hearth.telegram.media.compound import split_compound_ask
from hearth.telegram.media.editions import extract_edition
from hearth.telegram.media.followups import detect_follow_up
from hearth.telegram.media.moods import (
    detect_mood,
    house_pick_spec,
    looks_like_riddle,
    looks_like_vague_ask,
)
from hearth.telegram.media.people import detect_person_ask
from hearth.telegram.media.phrases import (
    clean_title_bits,
    extract_exclusion,
    is_known_franchise,
    series_seed,
)
from hearth.telegram.media.similar import detect_similar_ask
from hearth.telegram.media.types import MediaAskKind, MediaIntent
from hearth.telegram.models import MediaQuery

log = logging.getLogger("hearth.telegram")

_CHAT_ABOUT = re.compile(
    r"(?:"
    r"what(?:'s|\s+is|\s+are)\s+(?:that|it|this|the\s+\w[\w' -]{0,40})\s+about\b|"
    r"what(?:'s|\s+is)\s+.+\s+about\b|"
    r"(?:tell\s+me\s+about|^\s*about)\b|"
    r"(?:the\s+plot\s+of|plot\s+of|synopsis\s+of|summary\s+of)\b|"
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
    r"show\s+(?:me\s+)?(?:the\s+)?(?:list|queue)"
    r")\s*[.!?]*\s*$",
    re.I,
)

_JEV_TO_KIND: dict[str, MediaAskKind] = {
    "exact_title": "exact_title",
    "known_franchise": "known_franchise",
    "series_all": "series_all",
    "edition_aware": "edition",
    "person_filmography": "person",
    "mood_vibe": "mood",
    "similar_to": "similar",
    "batch_multi": "batch",
    "follow_up": "follow_up",
    "descriptive_riddle": "describe",
    "chat_about_title": "chat_about",
    "not_media": "other",
}

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
    """Pull the title out of a question, stripping the question itself first."""
    for candidate in (text, (parsed.title if parsed else "") or ""):
        raw = (candidate or "").strip()
        if not raw:
            continue
        cleaned = _CHAT_TITLE_STRIP.sub("", raw)
        cleaned = _CHAT_TITLE_TAIL.sub("", cleaned)
        cleaned = cleaned.strip(" ?!.")
        title, year = clean_title_bits(cleaned)
        if title and looks_like_concrete_title(title):
            if parsed and parsed.year is not None:
                year = parsed.year
            return title, year, (parsed.media_type or "") if parsed else ""
    if parsed and parsed.title and looks_like_concrete_title(parsed.title):
        title, year = clean_title_bits(parsed.title.strip())
        return title, parsed.year if parsed.year is not None else year, parsed.media_type or ""
    return "", parsed.year if parsed else None, ""


def _title_from_parsed(raw: str, parsed: MediaQuery | None) -> tuple[str, int | None, str]:
    if parsed and parsed.title:
        year = parsed.year
        title = parsed.title.strip()
        if year is None:
            title, year = clean_title_bits(title)
        return title, year, (parsed.media_type or "")
    title, year = clean_title_bits(raw)
    return title, year, ""


def _unsubstantiated_kind(raw: str, parsed: MediaQuery | None) -> MediaAskKind:
    """Where a lane goes when its extractor found nothing to work with.

    A concrete title in hand is worth one instant Overseerr search; only a
    genuinely title-less ask is worth waiting on gpt.
    """
    for candidate in ((parsed.title if parsed else "") or "", raw):
        if candidate and looks_like_concrete_title(candidate):
            return "exact_title"
    return "describe"


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
    """Fill the lane payload for ``kind`` deterministically.

    When the chosen lane cannot be substantiated (Jev says "mood" but there is
    no vibe language, say), the ask degrades to a lane that can still answer
    rather than running a literal search that is bound to miss.
    """
    raw = (text or "").strip()
    media_type = (parsed.media_type or "") if parsed else ""
    base = dict(confidence=confidence, source=source, raw_text=raw, jev=jev)

    if kind == "batch":
        parts = split_compound_ask(raw)
        if parts:
            return MediaIntent(
                kind="batch",
                parts=parts,
                search_title=parts[0].title,
                media_type=media_type,
                needs_llm=False,
                note=note or "compound_plan",
                **base,
            )
        kind = "exact_title"

    if kind == "follow_up":
        follow_up = detect_follow_up(raw)
        if follow_up is not None:
            return MediaIntent(
                kind="follow_up",
                follow_up=follow_up.kind,
                ordinal=follow_up.ordinal,
                media_type=media_type,
                needs_llm=False,
                note=note or f"follow_up:{follow_up.kind}",
                **base,
            )
        kind = "exact_title"

    if kind == "person":
        person = detect_person_ask(raw)
        if person is not None:
            return MediaIntent(
                kind="person",
                person_name=person.name,
                person_role=person.role,
                search_title=person.name,
                media_type=media_type,
                needs_llm=False,
                note=note or "person_credits",
                **base,
            )
        kind = _unsubstantiated_kind(raw, parsed)
        note = "person_without_name"

    if kind == "similar":
        like = detect_similar_ask(raw)
        if like is not None:
            anchor, year = clean_title_bits(like.anchor)
            return MediaIntent(
                kind="similar",
                search_title=anchor,
                year=year,
                media_type=media_type,
                needs_llm=False,
                note=note or ("similar_context" if like.uses_context else "similar_anchor"),
                **base,
            )
        kind = _unsubstantiated_kind(raw, parsed)
        note = "similar_without_anchor"

    if kind == "mood":
        spec = detect_mood(raw)
        if spec is not None:
            return MediaIntent(
                kind="mood",
                mood=spec,
                media_type=spec.media_type,
                needs_llm=False,
                note=note or f"mood:{spec.key}",
                **base,
            )
        if looks_like_vague_ask(raw):
            kind = "house_pick"
        else:
            kind = _unsubstantiated_kind(raw, parsed)
            note = "mood_without_vibe"

    if kind == "house_pick":
        hint = "tv" if media_type == "tv" else "movie"
        return MediaIntent(
            kind="house_pick",
            mood=house_pick_spec(media_type=hint),
            media_type=hint,
            needs_llm=False,
            note=note or "house_pick",
            **base,
        )

    if kind == "edition":
        edition = extract_edition(raw)
        if edition is not None and edition.present:
            clean, year = clean_title_bits(edition.clean_title)
            if parsed and parsed.year is not None:
                year = parsed.year
            return MediaIntent(
                kind="edition",
                search_title=clean,
                year=year,
                media_type=media_type,
                edition_key=edition.key,
                edition_label=edition.label,
                needs_llm=False,
                note=note or "edition_aware",
                **base,
            )
        # Jev said edition but we could not strip — fall back to exact search title.
        title, year, media_type = _title_from_parsed(raw, parsed)
        return MediaIntent(
            kind="edition",
            search_title=title,
            year=year,
            media_type=media_type,
            needs_llm=False,
            note=note or "edition_unstripped",
            **base,
        )

    if kind == "series_all":
        body, drop_last, drop_first = extract_exclusion(raw)
        seed = series_seed(body) or _title_from_parsed(body, parsed)[0]
        edition_key = ""
        edition_label = ""
        edition = extract_edition(seed)
        if edition is not None and edition.present:
            edition_key = edition.key
            edition_label = edition.label
            seed = edition.clean_title
        return MediaIntent(
            kind="series_all",
            search_title=seed,
            year=parsed.year if parsed else None,
            media_type=media_type or "movie",
            edition_key=edition_key,
            edition_label=edition_label,
            drop_last=drop_last,
            drop_first=drop_first,
            needs_llm=False,
            note=note or "franchise_all",
            **base,
        )

    if kind in {"exact_title", "known_franchise"}:
        title, year, media_type = _title_from_parsed(raw, parsed)
        # Strip accidental edition words if present but Jev chose exact/franchise.
        edition = extract_edition(title)
        if edition is not None and edition.present:
            title = edition.clean_title
        franchise_note = note
        if kind == "known_franchise" or is_known_franchise(title):
            kind = "known_franchise"
            franchise_note = franchise_note or "franchise_seed"
        return MediaIntent(
            kind=kind,
            search_title=title,
            year=year,
            media_type=media_type,
            needs_llm=False,
            note=franchise_note,
            **base,
        )

    if kind == "chat_about":
        title, year, media_type = _title_hint_from_chat(raw, parsed)
        return MediaIntent(
            kind="chat_about",
            search_title=title,
            year=year,
            media_type=media_type,
            needs_llm=True,
            note=note or "info_only",
            **base,
        )

    if kind == "describe":
        return MediaIntent(
            kind="describe",
            search_title="",
            year=parsed.year if parsed else None,
            media_type=media_type,
            needs_llm=True,
            note=note or "descriptive_riddle",
            **base,
        )

    return MediaIntent(
        kind="other",
        needs_llm=False,
        note=note or "not_media",
        **base,
    )


def _local_kind(raw: str, *, parsed: MediaQuery | None) -> MediaAskKind:
    """Deterministic lane choice, in the order a butler would reason."""
    if _CHAT_ABOUT.search(raw) and not _GRAB_INTENT.search(raw):
        return "chat_about"
    if detect_follow_up(raw) is not None:
        return "follow_up"
    if split_compound_ask(raw):
        return "batch"

    body, _, _ = extract_exclusion(raw)
    if series_seed(body):
        return "series_all"
    if detect_similar_ask(raw) is not None:
        return "similar"
    if detect_person_ask(raw) is not None:
        return "person"
    if detect_mood(raw) is not None:
        return "mood"
    if looks_like_vague_ask(raw):
        return "house_pick"

    edition = extract_edition(raw)
    if edition is not None and edition.present:
        clean, _ = clean_title_bits(edition.clean_title)
        if looks_like_concrete_title(clean):
            return "edition"

    # "the one where the guy loses his memory" is short enough to pass as a
    # title but is really a riddle — send it to the guess lane.
    if looks_like_riddle(raw):
        return "describe"

    probe = raw
    if parsed and parsed.title and parsed.reason == "title":
        probe = parsed.raw_text or parsed.title
    if looks_like_concrete_title(probe) or (parsed is not None and parsed.tmdb_id is not None):
        title, _, _ = _title_from_parsed(probe if not parsed else raw, parsed)
        return "known_franchise" if is_known_franchise(title) else "exact_title"
    return "describe"


_LOCAL_CONFIDENCE: dict[str, float] = {
    "chat_about": 0.90,
    "follow_up": 0.86,
    "batch": 0.90,
    "series_all": 0.92,
    "similar": 0.88,
    "person": 0.88,
    "mood": 0.86,
    "house_pick": 0.80,
    "edition": 0.90,
    "known_franchise": 0.88,
    "exact_title": 0.88,
    "describe": 0.75,
}


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

    kind = _local_kind(raw, parsed=parsed)
    return _enrich(
        kind,
        raw,
        parsed=parsed,
        confidence=_LOCAL_CONFIDENCE.get(kind, 0.8),
        source="local",
        needs_llm=kind in {"describe", "chat_about"},
    )


def _carry_local(local: MediaIntent, *, source: str, note: str, needs_llm: bool | None = None,
                 jev: JevVerdict | None = None) -> MediaIntent:
    """Reuse the local verdict when Jev cannot be trusted this turn."""
    return MediaIntent(
        kind=local.kind,
        search_title=local.search_title,
        year=local.year,
        media_type=local.media_type,
        edition_key=local.edition_key,
        edition_label=local.edition_label,
        confidence=local.confidence,
        source=source,
        needs_llm=local.needs_llm if needs_llm is None else needs_llm,
        raw_text=local.raw_text,
        note=note or local.note,
        jev=jev,
        person_name=local.person_name,
        person_role=local.person_role,
        mood=local.mood,
        parts=local.parts,
        follow_up=local.follow_up,
        ordinal=local.ordinal,
        drop_last=local.drop_last,
        drop_first=local.drop_first,
    )


def classify_media_ask_sync(text: str, *, parsed: MediaQuery | None = None) -> MediaIntent:
    """Deterministic local classification (unit tests / fail-open path)."""
    return _local_classify(text, parsed=parsed)


async def classify_media_ask(
    text: str,
    *,
    parsed: MediaQuery | None = None,
    recent: list[str] | None = None,
) -> MediaIntent:
    """Jev-first media intent. OpenAI is not called here — only routed."""
    local = _local_classify(text, parsed=parsed)

    # Bare confirm tokens / list asks never become download routes via Jev.
    if local.note in {"confirm_token", "list_ask"}:
        return local

    if not settings.jev_enabled:
        return local

    try:
        verdict = await evaluate_telegram_media(text, recent=recent)
        log_shadow_outcome(
            verdict,
            channel="telegram_media_router",
            tools=[],
            outcome=f"local:{local.kind}",
        )
        if not verdict.ok or verdict.answers is None:
            return _carry_local(
                local,
                source="local_failopen",
                note=local.note or verdict.reason,
                jev=verdict,
            )

        picked = media_ask_choice(verdict.answers)
        if picked is None:
            # Low confidence → prefer LLM when local already wants it, else local.
            needs = needs_llm_resolve(verdict.answers, media_choice=None) or local.needs_llm
            carried = _carry_local(
                local,
                source="local_failopen",
                note="jev_low_confidence",
                needs_llm=needs,
                jev=verdict,
            )
            if needs and local.kind in {"exact_title", "known_franchise", "other"}:
                # Jev is unsure and says prose is needed: let gpt name the title.
                return _enrich(
                    "describe",
                    text,
                    parsed=parsed,
                    confidence=local.confidence,
                    source="local_failopen",
                    needs_llm=True,
                    jev=verdict,
                    note="jev_low_confidence",
                )
            return carried

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
        return _carry_local(
            local,
            source="local_failopen",
            note=local.note or "jev_exception",
        )


__all__ = ["classify_media_ask", "classify_media_ask_sync"]
