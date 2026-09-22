"""The Telegram house-device lane in front of the Overseerr media router.

Two properties matter here and are asserted throughout: a device command never
reaches the catalog, and a film title never reaches the hardware.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hearth.config import settings
from hearth.jev import reset_client, set_client
from hearth.jev.schema import parse_answers
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.store import TelegramStore
from hearth.tools.ha import ha

CHAT_ID = -100123
USER_ID = 42


def _message(text: str, *, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "text": text,
    }


class FakeOverseerr:
    """Records whether the media router was reached at all."""

    live = True

    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.request_calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append((query, page))
        return {"ok": True, "mode": "live", "results": []}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        return {"ok": False, "reason": "not_found"}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(dict(kwargs))
        return {"ok": True, "requestStatus": 2, "mediaStatus": 3, "requestId": 1}


@pytest.fixture
def bot_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], tuple[TelegramMediaBot, FakeOverseerr]]:
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    stores: list[TelegramStore] = []

    def make() -> tuple[TelegramMediaBot, FakeOverseerr]:
        store = TelegramStore(tmp_path / f"telegram-house-{len(stores)}.db")
        stores.append(store)
        fake = FakeOverseerr()
        return TelegramMediaBot(store, overseerr_client=fake), fake

    yield make
    for store in stores:
        store.close()


async def test_feed_the_cats_feeds_and_never_searches_the_catalog(bot_factory) -> None:
    bot, overseerr = bot_factory()
    before = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]

    reply = await bot.handle_message(_message("feed the cats"))

    assert reply is not None
    assert "Fed the pets" in reply.text
    assert overseerr.search_calls == []
    after = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
    assert after != before


async def test_airco_and_purifier_phrases_control_the_hardware(bot_factory) -> None:
    bot, overseerr = bot_factory()

    airco = await bot.handle_message(_message("airco 21"))
    assert airco is not None and "21 degrees" in airco.text
    assert (await ha.get_state("climate.airco"))["state"]["attributes"]["temperature"] == 21

    purifier = await bot.handle_message(_message("purifier on", message_id=2))
    assert purifier is not None and "KPT air purifier is on" in purifier.text
    assert (await ha.get_state("fan.air_purifier"))["state"]["state"] == "on"

    assert overseerr.search_calls == []


async def test_dutch_phrases_work_too(bot_factory) -> None:
    bot, overseerr = bot_factory()
    reply = await bot.handle_message(_message("geef de katten eten"))
    assert reply is not None
    assert "Fed the pets" in reply.text
    assert overseerr.search_calls == []


async def test_slash_commands_reach_the_device_lane(bot_factory) -> None:
    bot, overseerr = bot_factory()

    devices = await bot.handle_message(_message("/devices"))
    assert devices is not None
    assert "Airco is off." in devices.text

    airco = await bot.handle_message(_message("/airco 20", message_id=2))
    assert airco is not None and "20 degrees" in airco.text
    assert overseerr.search_calls == []


async def test_a_film_title_still_goes_to_the_catalog(bot_factory) -> None:
    bot, overseerr = bot_factory()
    before = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]

    await bot.handle_message(_message("Cats"))

    assert overseerr.search_calls, "an ordinary title must still reach Overseerr"
    after = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
    assert after == before


async def test_the_cooldown_answer_is_spoken_not_swallowed(bot_factory) -> None:
    bot, _ = bot_factory()
    first = await bot.handle_message(_message("feed the cats"))
    assert first is not None and "Fed the pets" in first.text

    second = await bot.handle_message(_message("feed the cats", message_id=2))
    assert second is not None
    assert "feed them anyway" in second.text

    forced = await bot.handle_message(_message("feed the cats anyway", message_id=3))
    assert forced is not None and "Fed the pets" in forced.text


async def test_an_unpaired_device_reply_includes_the_pairing_hint(
    bot_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = bot_factory()
    monkeypatch.setattr(settings, "ha_airco_entities", "climate.nope")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {"ok": True, "mode": "mock", "states": []}

    monkeypatch.setattr(ha, "list_states", _states)
    reply = await bot.handle_message(_message("airco 21"))
    assert reply is not None
    assert "HA_AIRCO_ENTITIES" in reply.text
    assert "Tuya Local" in reply.text


async def test_jev_can_refuse_a_telegram_device_command(
    bot_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforce mode: a high-confidence cancel stops the feeder from firing."""
    bot, _ = bot_factory()
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")

    class CancellingSystemOne:
        def __init__(self) -> None:
            self.calls = 0

        async def system_one(self, *, state, questions=None, model=None):
            self.calls += 1
            return parse_answers(
                {
                    "model": "jev-1.13.0",
                    "answers": {
                        "device_ask": {
                            "type": "choice",
                            "choice": "feed_pets",
                            "confidence": 0.9,
                        },
                        "is_cancel": {"type": "noul", "noul": 0.97},
                    },
                }
            )

    fake = CancellingSystemOne()
    set_client(fake)
    try:
        before = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
        reply = await bot.handle_message(_message("no, don't feed the cats"))
        assert reply is not None
        assert "won't touch that device" in reply.text
        assert fake.calls == 1
        after = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
        assert after == before
    finally:
        reset_client()


async def test_help_text_mentions_the_house_devices(bot_factory) -> None:
    bot, _ = bot_factory()
    reply = await bot.handle_message(_message("/help"))
    assert reply is not None
    assert "feed the cats" in reply.text
    assert "/purifier" in reply.text
