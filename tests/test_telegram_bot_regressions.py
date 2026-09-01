from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hearth.config import settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.models import MediaQuery
from hearth.telegram.store import TelegramStore


CHAT_ID = -100909
USER_ID = 91


def _message(text: str, *, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "text": text,
    }


class DetailWithoutMediaType:
    live = True

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        assert (media_id, media_type) == (550, "movie")
        # Overseerr/Seerr detail routes already specify movie or TV in the URL,
        # and their response shape does not consistently repeat mediaType.
        return {
            "ok": True,
            "mediaType": "movie",
            "mediaId": 550,
            "mediaStatus": 1,
            "media": {
                "title": "Fight Club",
                "year": "1999",
                "tmdbId": 550,
                "mediaStatus": 1,
            },
        }


class StatusSixSearch:
    live = True

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        assert (query, page) == ("Oldboy", 1)
        return {
            "ok": True,
            "mode": "live",
            "results": [
                {
                    "mediaType": "movie",
                    "id": 670,
                    "title": "Oldboy",
                    "releaseDate": "2003-11-21",
                    "mediaInfo": {"status": 6},
                }
            ],
        }


class LocalizedSearch:
    live = True

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        assert (query, page) == ("Money Heist", 1)
        return {
            "ok": True,
            "results": [
                {
                    "mediaType": "tv",
                    "id": 999,
                    "name": "Money Hungry",
                },
                {
                    "mediaType": "tv",
                    "id": 71446,
                    "name": "La casa de papel",
                    "originalName": "Money Heist",
                },
            ],
        }


@pytest.fixture
def configured_bot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    stores: list[TelegramStore] = []

    def make(overseerr: Any) -> TelegramMediaBot:
        store = TelegramStore(tmp_path / f"telegram-regression-{len(stores)}.db")
        stores.append(store)
        return TelegramMediaBot(store, overseerr_client=overseerr)

    yield make
    for store in stores:
        store.close()


@pytest.mark.asyncio
async def test_typed_tmdb_detail_without_repeated_media_type_still_gets_exact_button(
    configured_bot,
) -> None:
    bot = configured_bot(DetailWithoutMediaType())

    reply = await bot.handle_message(
        _message("https://www.themoviedb.org/movie/550-fight-club")
    )

    assert reply is not None
    assert "Fight Club (1999)" in reply.text
    assert reply.reply_markup is not None
    data = reply.reply_markup["inline_keyboard"][0][0]["callback_data"]
    decoded = bot._callback_codec().decode(data, CHAT_ID)
    assert (decoded.media_type, decoded.tmdb_id) == ("movie", 550)


@pytest.mark.asyncio
async def test_ambiguous_status_six_is_neutral_and_remains_requestable(
    configured_bot,
) -> None:
    bot = configured_bot(StatusSixSearch())

    reply = await bot.handle_message(_message("Oldboy"))

    assert reply is not None
    assert "Blocklisted or deleted" in reply.text
    assert reply.reply_markup is not None
    data = reply.reply_markup["inline_keyboard"][0][0]["callback_data"]
    decoded = bot._callback_codec().decode(data, CHAT_ID)
    assert (decoded.media_type, decoded.tmdb_id) == ("movie", 670)


def test_request_status_five_confirms_exact_request_availability() -> None:
    state, text = TelegramMediaBot._accepted_text(
        "Example",
        request_status=5,
        media_status=3,
    )

    assert state == "available"
    assert "available in Plex" in text


def test_episode_rejection_explains_overseerr_season_granularity() -> None:
    text = TelegramMediaBot._rejection_text(
        MediaQuery(action="reject", reason="episode_not_supported")
    )

    assert "whole seasons" in text
    assert "S02" in text


@pytest.mark.asyncio
async def test_original_language_title_participates_in_exact_ranking(
    configured_bot,
) -> None:
    bot = configured_bot(LocalizedSearch())

    reply = await bot.handle_message(_message("Money Heist"))

    assert reply is not None
    assert "1. La casa de papel" in reply.text
    data = reply.reply_markup["inline_keyboard"][0][0]["callback_data"]
    decoded = bot._callback_codec().decode(data, CHAT_ID)
    assert decoded.tmdb_id == 71446
