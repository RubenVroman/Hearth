"""Product-grade Telegram media request intelligence.

**Jev-first:** every media-ish turn hits TypeSafe System One (Choice/Noul/Score)
to pick a lane — exact title, franchise, series-all, edition, person, mood,
"like X", batch, or in-thread follow-up. OpenAI is only used for descriptive
riddles / title Q&A / low-confidence fail-open.

Lane payloads (franchise seed, edition, person name, discover coordinates, plan
items) are always derived deterministically, so the fast path never waits on
prose. Candidate cards still go through the existing Get / yes confirm pipeline
— never invent a queue from chat alone.
"""

from __future__ import annotations

# Imported first: the sibling modules below reference ``media.voice`` while this
# package is still initialising.
from hearth.telegram.media import voice
from hearth.telegram.media.cards import CardRenderer, RenderedCards, blocked_status_line
from hearth.telegram.media.catalog import (
    CatalogGuess,
    answer_catalog_question,
    guess_catalog_title,
    guess_catalog_titles,
    telegram_intent_model,
)
from hearth.telegram.media.classify import classify_media_ask, classify_media_ask_sync
from hearth.telegram.media.compound import split_compound_ask
from hearth.telegram.media.editions import EditionPreference, extract_edition
from hearth.telegram.media.followups import FollowUpAsk, detect_follow_up
from hearth.telegram.media.memory import ChatContext, MediaMemory, RememberedHit
from hearth.telegram.media.moods import detect_mood, house_pick_spec, looks_like_vague_ask
from hearth.telegram.media.people import PersonAsk, detect_person_ask
from hearth.telegram.media.ranking import (
    MAX_RESULTS,
    SERIES_MAX_RESULTS,
    apply_exclusions,
    best_franchise_seed,
    in_release_order,
    rank_hits,
    to_hits,
    without_ids,
)
from hearth.telegram.media.search import CatalogSearch, CatalogUnavailable
from hearth.telegram.media.similar import SimilarAsk, detect_similar_ask
from hearth.telegram.media.types import AskPart, MediaAskKind, MediaIntent, MoodSpec

__all__ = [
    "MAX_RESULTS",
    "SERIES_MAX_RESULTS",
    "AskPart",
    "CardRenderer",
    "CatalogGuess",
    "CatalogSearch",
    "CatalogUnavailable",
    "ChatContext",
    "EditionPreference",
    "FollowUpAsk",
    "MediaAskKind",
    "MediaIntent",
    "MediaMemory",
    "MoodSpec",
    "PersonAsk",
    "RememberedHit",
    "RenderedCards",
    "SimilarAsk",
    "answer_catalog_question",
    "apply_exclusions",
    "best_franchise_seed",
    "blocked_status_line",
    "classify_media_ask",
    "classify_media_ask_sync",
    "detect_follow_up",
    "detect_mood",
    "detect_person_ask",
    "detect_similar_ask",
    "extract_edition",
    "guess_catalog_title",
    "guess_catalog_titles",
    "house_pick_spec",
    "in_release_order",
    "looks_like_vague_ask",
    "rank_hits",
    "split_compound_ask",
    "telegram_intent_model",
    "to_hits",
    "voice",
    "without_ids",
]
