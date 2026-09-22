"""Jev gating on the two Telegram chokepoints that leave the house.

The Telegram bot reaches Overseerr and the TV directly rather than through the
tool registry, so those calls are gated in the bot. Taps and typed yeses are
already confirms, so only Jev's hard stops may block them — anything else would
mean the bot ignores a button the user just pressed.

All TypeSafe calls are mocked; no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hearth.config import settings
from hearth.jev import reset_client, set_client
from hearth.jev.schema import parse_answers
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.media.memory import speaker_scope
from hearth.telegram.models import MediaHit
from hearth.telegram.store import TelegramStore

CHAT_ID = -100777
USER_ID = 77


def _message(text: str, *, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "text": text,
    }


def _callback(data: str, *, message_id: int = 5) -> dict[str, Any]:
    return {
        "id": "cb-1",
        "data": data,
        "from": {"id": USER_ID, "is_bot": False},
        "message": {
            "message_id": message_id,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
        },
    }


class RecordingOverseerr:
    """Minimal Overseerr stand-in that records every request it is asked to make."""

    live = True

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
            "results": [
                {
                    "mediaType": "movie",
                    "id": 438631,
                    "title": "Dune",
                    "releaseDate": "2021-09-15",
                }
            ],
        }

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return {
            "ok": True,
            "mode": "live",
            "requestStatus": 2,
            "mediaStatus": 3,
            "requestId": 77,
        }


class FakeSystemOne:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def system_one(self, **kwargs: Any):
        self.calls.append(kwargs)
        return parse_answers(self.payload)


def _payload(
    *,
    media_ask: str = "exact_title",
    tool_allow: float = 0.9,
    is_cancel: float = 0.02,
    is_confirm: float = 0.05,
    risk: float = 0.0,
    risk_conf: float = 0.9,
    domain: str | None = None,
    domain_conf: float = 0.95,
) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "media_ask": {
            "type": "choice",
            "choice": media_ask,
            "confidence": 0.94,
            "probabilities": {media_ask: 0.94},
        },
        "needs_llm": {"type": "noul", "noul": 0.05},
        "multi_item": {"type": "noul", "noul": 0.05},
        "tool_allow": {"type": "noul", "noul": tool_allow},
        "wants_queue": {"type": "noul", "noul": 0.8},
        "is_confirm": {"type": "noul", "noul": is_confirm},
        "is_cancel": {"type": "noul", "noul": is_cancel},
        "risk": {
            "type": "score",
            "score": risk,
            "confidence": risk_conf,
            "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"},
        },
    }
    if domain is not None:
        answers["domain"] = {
            "type": "choice",
            "choice": domain,
            "confidence": domain_conf,
            "probabilities": {domain: domain_conf},
        }
    return {"model": "jev-1.13.0", "answers": answers}


@pytest.fixture
def bot_and_overseerr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    reset_client()
    store = TelegramStore(tmp_path / "telegram-jev-gate.db")
    provider = RecordingOverseerr()
    try:
        yield TelegramMediaBot(store, overseerr_client=provider), provider
    finally:
        store.close()
        reset_client()


def _enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "jev_tool_gate", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")


def _arm_pending(bot: TelegramMediaBot) -> None:
    # Group chats key pending guesses per speaker (#86). Arm under the same
    # scope handle_message binds, or a typed yes will miss the offer.
    with speaker_scope(CHAT_ID, USER_ID):
        bot._set_pending_guess(
            CHAT_ID,
            MediaHit(media_type="movie", tmdb_id=438631, title="Dune", year=2021),
            season=None,
        )


async def _get_button(bot: TelegramMediaBot) -> str:
    reply = await bot.handle_message(_message("Dune"))
    assert reply is not None and reply.reply_markup is not None
    return reply.reply_markup["inline_keyboard"][0][0]["callback_data"]


# --- typed yes on a pending guess ---------------------------------------------


@pytest.mark.asyncio
async def test_typed_yes_queues_when_jev_agrees(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    _enforce(monkeypatch)
    set_client(FakeSystemOne(_payload(is_confirm=0.95)))
    _arm_pending(bot)

    reply = await bot.handle_message(_message("yes"))

    assert reply is not None
    assert "Dune" in reply.text
    assert len(provider.requests) == 1
    assert provider.requests[0]["media_id"] == 438631


@pytest.mark.asyncio
async def test_hard_stop_blocks_a_typed_yes_without_queueing(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    _enforce(monkeypatch)
    # do_not_auto_run is a hard stop: it outranks even an explicit confirm.
    set_client(FakeSystemOne(_payload(is_confirm=0.95, risk=2.0, risk_conf=0.95)))
    _arm_pending(bot)

    reply = await bot.handle_message(_message("yes"))

    assert reply is not None
    assert provider.requests == []
    assert "won't run it on my own" in reply.text
    # The offer is cleared so a stale yes cannot be replayed later.
    assert bot._get_pending_guess(CHAT_ID) is None


@pytest.mark.asyncio
async def test_shadow_mode_still_queues_a_typed_yes(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    set_client(FakeSystemOne(_payload(risk=2.0, risk_conf=0.99)))
    _arm_pending(bot)

    reply = await bot.handle_message(_message("yes"))

    assert reply is not None
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_missing_api_key_still_queues_a_typed_yes(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    fake = FakeSystemOne(_payload(risk=2.0, risk_conf=0.99))
    set_client(fake)
    _arm_pending(bot)

    reply = await bot.handle_message(_message("yes"))

    assert reply is not None
    assert len(provider.requests) == 1
    assert fake.calls == []


@pytest.mark.asyncio
async def test_gate_error_still_queues_a_typed_yes(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    _enforce(monkeypatch)

    class Broken:
        async def system_one(self, **_kwargs: Any):
            raise RuntimeError("system one is down")

    set_client(Broken())
    _arm_pending(bot)

    reply = await bot.handle_message(_message("yes"))

    assert reply is not None
    assert len(provider.requests) == 1


# --- Get button tap ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tap_queues_when_jev_agrees(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    _enforce(monkeypatch)
    set_client(FakeSystemOne(_payload()))
    data = await _get_button(bot)

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_hard_stop_blocks_a_get_tap_without_queueing(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    _enforce(monkeypatch)
    set_client(FakeSystemOne(_payload(domain="refuse", domain_conf=0.96)))
    data = await _get_button(bot)

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert provider.requests == []
    assert "not going to do that" in reply.text


@pytest.mark.asyncio
async def test_a_denied_get_button_is_not_spent(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused tap must leave the button usable, not brick it forever."""
    bot, provider = bot_and_overseerr
    _enforce(monkeypatch)
    set_client(FakeSystemOne(_payload(domain="refuse", domain_conf=0.96)))
    data = await _get_button(bot)

    denied = await bot.handle_callback(_callback(data))
    assert denied is not None
    assert provider.requests == []

    # Jev changes its mind; the same button still works.
    set_client(FakeSystemOne(_payload()))
    allowed = await bot.handle_callback(_callback(data))

    assert allowed is not None
    assert "already handled" not in allowed.text
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_a_low_tool_allow_never_blocks_a_tapped_get(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tap *is* the instruction; a soft signal must not veto it."""
    bot, provider = bot_and_overseerr
    _enforce(monkeypatch)
    set_client(FakeSystemOne(_payload(tool_allow=0.01, is_cancel=0.99)))
    data = await _get_button(bot)

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_jev_off_leaves_the_queue_path_untouched(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, provider = bot_and_overseerr
    monkeypatch.setattr(settings, "jev_enabled", False)
    fake = FakeSystemOne(_payload(domain="refuse", domain_conf=0.99))
    set_client(fake)
    data = await _get_button(bot)

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert len(provider.requests) == 1
    assert fake.calls == []


# --- one call per Telegram turn ------------------------------------------------


@pytest.mark.asyncio
async def test_router_and_queue_gate_share_one_system_one_call(
    bot_and_overseerr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The media router's verdict is reused by the gate, not paid for twice."""
    bot, _provider = bot_and_overseerr
    _enforce(monkeypatch)
    fake = FakeSystemOne(_payload())
    set_client(fake)

    await bot.handle_message(_message("Dune"))

    assert len(fake.calls) == 1


# --- shared Overseerr error phrasing ------------------------------------------


def test_request_error_text_uses_the_shared_taxonomy_with_the_richer_label() -> None:
    from hearth.tools.arr import overseerr_request_error_text

    label = "Severance (2022) · Season 2"
    for result, reason in (
        ({"ok": False, "reason": "no_seasons"}, "no_seasons"),
        ({"ok": False, "reason": "forbidden"}, "forbidden"),
        ({"ok": False, "reason": "invalid_request"}, "invalid_request"),
        ({"ok": False, "reason": "already_requested", "already": True}, "already_requested"),
    ):
        bot_text = TelegramMediaBot._request_error_text(label, result)
        assert bot_text == overseerr_request_error_text(
            label,
            reason=reason,
            already=bool(result.get("already")),
        )
        # The season detail the gateway never saw survives into the reply.
        assert "Season 2" in bot_text


def test_uncertain_request_text_distinguishes_a_silent_provider() -> None:
    silent = TelegramMediaBot._uncertain_request_text("Dune (2021)", answered=False)
    answered = TelegramMediaBot._uncertain_request_text("Dune (2021)", answered=True)

    assert "Overseerr did not answer" in silent
    assert "Overseerr did not answer" not in answered
    # Both send the house to the same place to find out what really happened.
    assert "Check Overseerr" in silent
    assert "Check Overseerr" in answered
