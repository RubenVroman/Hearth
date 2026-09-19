"""Confirm helpers + catalog guess re-exports for the Telegram media bot.

Descriptive title resolution lives in ``hearth.telegram.media`` (Jev-first
router + optional gpt-4o). This module keeps stable imports for confirm yes/nah
detection and ``looks_like_concrete_title``.
"""

from __future__ import annotations

from hearth.telegram.heuristics import (
    looks_like_concrete_title,
    looks_like_confirm_no,
    looks_like_confirm_yes,
)
from hearth.telegram.media.catalog import (
    TELEGRAM_INTENT_MODEL,
    CatalogGuess,
    guess_catalog_title,
    telegram_intent_model,
)

__all__ = [
    "TELEGRAM_INTENT_MODEL",
    "CatalogGuess",
    "guess_catalog_title",
    "looks_like_concrete_title",
    "looks_like_confirm_no",
    "looks_like_confirm_yes",
    "telegram_intent_model",
]
