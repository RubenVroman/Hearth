"""Product-grade Telegram media request intelligence.

**Jev-first:** every media-ish turn hits TypeSafe System One (Choice/Noul/Score)
to classify intent. OpenAI is only used for descriptive riddles / title Q&A /
low-confidence fail-open. Candidate cards still go through the existing Get /
yes confirm pipeline — never invent a queue from chat alone.
"""

from __future__ import annotations

from hearth.telegram.media.catalog import (
    CatalogGuess,
    answer_catalog_question,
    guess_catalog_title,
    guess_catalog_titles,
    telegram_intent_model,
)
from hearth.telegram.media.classify import classify_media_ask, classify_media_ask_sync
from hearth.telegram.media.editions import EditionPreference, extract_edition
from hearth.telegram.media.types import MediaAskKind, MediaIntent

__all__ = [
    "CatalogGuess",
    "EditionPreference",
    "MediaAskKind",
    "MediaIntent",
    "answer_catalog_question",
    "classify_media_ask",
    "classify_media_ask_sync",
    "extract_edition",
    "guess_catalog_title",
    "guess_catalog_titles",
    "telegram_intent_model",
]
