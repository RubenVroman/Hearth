"""Telegram conversational turns share the house agent loop.

Exact titles stay on the instant Overseerr path. Open chat, multi-turn
follow-ups, and mixed house+media turns go through AgentLoop, and Jev still
gates the tool. Nothing here queues a download from prose.
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
from hearth.telegram.store import TelegramStore

CHAT_ID = -100555
USER_ID = 55

DUNE = {
    "mediaType": "movie",
    "id": 438631,
    "title": "Dune",
    "releaseDate": "2021-10-22",
}
HARRY_POTTER = [
    {
        "mediaType": "movie",
        "id": 671,
        "title": "Harry Potter and the Philosopher's Stone",
        "releaseDate": "2001-11-16",
    },
    {
        "mediaType": "movie",
        "id": 672,
        "title": "Harry Potter and the Chamber of Secrets",
        "releaseDate": "2002-11-15",
    },
    {
        "mediaType": "movie",
        "id": 673,
        "title": "Harry Potter and the Prisoner of Azkaban",
        "releaseDate": "2004-05-31",
    },
]


def _message(text: str, *, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "text": text,
    }


class FakeOverseerr:
    live = True

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = list(results or [])
        self.search_calls: list[tuple[str, int]] = []
        self.request_calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append((query, page))
        return {"ok": True, "mode": "live", "results": list(self.results)}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        for row in self.results:
            if int(row.get("id") or 0) == int(media_id):
                return {"ok": True, "media": dict(row)}
        return {"ok": False}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(dict(kwargs))
        return {"ok": True, "requestStatus": 2, "mediaStatus": 3, "requestId": 7}


class FakeSystemOne:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def system_one(
        self,
        *,
        state: Any,
        questions: dict | None = None,
        model: str | None = None,
    ):
        self.calls.append({"state": state, "questions": questions, "model": model})
        return parse_answers(self.payload)


def _gate_payload(*, tool_allow: float = 0.9, tool_lane: str = "weather") -> dict[str, Any]:
    return {
        "model": "jev-test",
        "answers": {
            "domain": {
                "type": "choice",
                "choice": "chat",
                "confidence": 0.9,
                "probabilities": {"chat": 0.9},
            },
            "tool_lane": {
                "type": "choice",
                "choice": tool_lane,
                "confidence": 0.93,
                "probabilities": {tool_lane: 0.93},
            },
            "tool_allow": {"type": "noul", "noul": tool_allow},
            "needs_llm": {"type": "noul", "noul": 0.05},
            "is_cancel": {"type": "noul", "noul": 0.02},
            "is_confirm": {"type": "noul", "noul": 0.05},
            "wants_queue": {"type": "noul", "noul": 0.05},
            "risk": {
                "type": "score",
                "score": 0.0,
                "confidence": 0.9,
                "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"},
            },
        },
    }


@pytest.fixture
def bot_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:convo-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "openai_api_key", "")
    stores: list[TelegramStore] = []

    def make(overseerr: FakeOverseerr | None = None) -> tuple[TelegramMediaBot, FakeOverseerr]:
        fake = overseerr or FakeOverseerr()
        store = TelegramStore(tmp_path / f"convo-{len(stores)}.db")
        stores.append(store)
        return TelegramMediaBot(store, overseerr_client=fake), fake

    yield make
    for store in stores:
        store.close()


@pytest.fixture(autouse=True)
def _reset_jev():
    reset_client()
    yield
    reset_client()


def _queries(fake: FakeOverseerr) -> list[str]:
    return [query for query, _page in fake.search_calls]


@pytest.mark.asyncio
async def test_weather_uses_the_shared_agent_and_skips_catalog(
    bot_factory,
) -> None:
    bot, fake = bot_factory()
    calls: list[dict[str, Any]] = []
    real = bot.agent.run

    async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        return await real(*args, **kwargs)

    bot.agent.run = wrapped  # type: ignore[method-assign]

    reply = await bot.handle_message(_message("what's the weather"))

    assert reply is not None
    assert "14" in reply.text
    assert calls and calls[0]["kwargs"]["channel"] == "telegram"
    assert calls[0]["kwargs"]["inherit_turn"] is True
    assert fake.search_calls == []
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_follow_up_reads_the_stored_thread(bot_factory) -> None:
    bot, fake = bot_factory()
    seen: list[dict[str, Any]] = []
    real = bot.agent.run

    async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append({"text": args[0] if args else "", "kwargs": kwargs})
        return await real(*args, **kwargs)

    bot.agent.run = wrapped  # type: ignore[method-assign]

    first = await bot.handle_message(_message("what's the weather"))
    assert first is not None and "14" in first.text

    follow = await bot.handle_message(_message("and tomorrow?", message_id=2))

    assert follow is not None
    assert "Still with you" in follow.text
    assert "14" in follow.text
    assert len(seen) == 2
    note = str(seen[1]["kwargs"].get("context_note") or "")
    history = seen[1]["kwargs"].get("history") or []
    blob = note + " ".join(str(item.get("content") or "") for item in history)
    assert "14" in blob
    assert fake.search_calls == []


@pytest.mark.asyncio
async def test_mixed_house_follow_up_keeps_the_media_thread(bot_factory) -> None:
    bot, fake = bot_factory(FakeOverseerr(HARRY_POTTER))

    listed = await bot.handle_message(_message("Harry Potter"))
    assert listed is not None
    assert "Philosopher" in listed.text

    lights = await bot.handle_message(_message("also dim the lights", message_id=2))
    assert lights is not None
    assert lights.text
    assert all("dim" not in query.casefold() for query in _queries(fake))
    assert fake.request_calls == []

    picked = await bot.handle_message(_message("the second one", message_id=3))
    assert picked is not None
    assert "Chamber" in picked.text
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_dim_then_yes_still_queues_the_armed_title(bot_factory) -> None:
    bot, fake = bot_factory(FakeOverseerr([DUNE]))

    listed = await bot.handle_message(_message("Dune"))
    assert listed is not None
    await bot.handle_message(_message("also dim the lights", message_id=2))
    confirmed = await bot.handle_message(_message("yes", message_id=3))

    assert confirmed is not None
    assert len(fake.request_calls) == 1
    assert fake.request_calls[0]["media_id"] == 438631


@pytest.mark.asyncio
async def test_edition_correction_uses_the_remembered_title(bot_factory) -> None:
    bot, fake = bot_factory(FakeOverseerr([DUNE]))

    assert await bot.handle_message(_message("Dune")) is not None
    fake.search_calls.clear()

    reply = await bot.handle_message(_message("no the extended cut", message_id=2))

    assert reply is not None
    assert "extended" in reply.text.casefold()
    assert _queries(fake)
    assert all("extended" not in query.casefold() for query in _queries(fake))
    assert any(query.casefold() == "dune" for query in _queries(fake))
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_jev_gates_the_conversational_tool_once(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "jev_tool_gate", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-not-a-secret")
    fake_jev = FakeSystemOne(_gate_payload(tool_allow=0.04, tool_lane="lights"))
    set_client(fake_jev)
    bot, fake = bot_factory()

    reply = await bot.handle_message(_message("dim the lights"))

    assert reply is not None
    assert "rather answer" in reply.text.casefold()
    assert len(fake_jev.calls) == 1
    questions = fake_jev.calls[0]["questions"] or {}
    assert "tool_lane" in questions
    assert "tool_allow" in questions
    assert fake.search_calls == []
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_jev_fail_open_without_a_key_still_answers(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    fake_jev = FakeSystemOne(_gate_payload())
    set_client(fake_jev)
    bot, fake = bot_factory()

    reply = await bot.handle_message(_message("what's the weather"))

    assert reply is not None
    assert "14" in reply.text
    assert fake_jev.calls == []
    assert fake.search_calls == []


@pytest.mark.asyncio
async def test_exact_title_does_not_enter_the_agent(bot_factory) -> None:
    bot, fake = bot_factory(FakeOverseerr([DUNE]))

    async def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("exact title must stay on the catalog path")

    bot.agent.run = boom  # type: ignore[method-assign]

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert "Dune" in reply.text
    assert _queries(fake) == ["Dune"]
    assert fake.request_calls == []
    with speaker_scope(CHAT_ID, USER_ID):
        assert bot.thread.load(CHAT_ID).turns == ()


@pytest.mark.asyncio
async def test_download_prose_does_not_queue_or_call_the_agent(bot_factory) -> None:
    bot, fake = bot_factory(FakeOverseerr([DUNE]))

    async def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("queue tools stay on the Get button")

    bot.agent.run = boom  # type: ignore[method-assign]

    reply = await bot.handle_message(_message("download Dune"))

    assert reply is not None
    assert "Dune" in reply.text
    assert fake.request_calls == []


class _ConfirmAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.calls.clear()

    async def run(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"text": text, **kwargs})
        if kwargs.get("confirm"):
            return {
                "reply": "Stopped.",
                "mode": "confirm",
                "tools": [{"name": "docker_stop", "ok": True}],
            }
        return {
            "reply": "I'll stop that container. Confirm to stop it.",
            "mode": "local",
            "tools": [
                {
                    "name": "docker_stop",
                    "ok": True,
                    "needs_confirm": True,
                    "data": {},
                }
            ],
        }


@pytest.mark.asyncio
async def test_house_confirm_yes_uses_the_shared_loop(bot_factory) -> None:
    bot, fake = bot_factory()
    agent = _ConfirmAgent()
    bot.agent = agent  # type: ignore[assignment]

    preview = await bot.handle_message(_message("what's the weather"))
    assert preview is not None
    assert "Confirm" in preview.text
    assert agent.calls[0]["confirm"] is False

    # No media card is on screen, so yes confirms the house preview.
    done = await bot.handle_message(_message("yes", message_id=2))

    assert done is not None
    assert done.text == "Stopped."
    assert agent.calls[1]["confirm"] is True
    assert fake.request_calls == []
