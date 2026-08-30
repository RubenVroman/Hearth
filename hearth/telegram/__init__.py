"""Telegram drop-group inbox — movies/series/TV requests via Bot API long-polling.

Feature is off unless TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS are configured.
Inbound traffic is server-side only (no public webhook / Funnel).
"""

from __future__ import annotations

from hearth.telegram.service import TelegramInboxService, telegram_inbox

__all__ = ["TelegramInboxService", "telegram_inbox"]
