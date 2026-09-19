"""Telegram media intelligence — Jev-first router + acceptance paths.

Mocks TypeSafe / Overseerr / OpenAI. Never hits the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from hearth.config import settings
from hearth.jev import reset_client, set_client
from hearth.jev.schema import parse_answers
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.media import classify_media_ask, classify_media_ask_sync, extract_edition
from hearth.telegram.media.catalog import CatalogGuess
from hearth.telegram.store import TelegramStore


CHAT_ID = -100321
USER_ID = 77

HARRY_POTTER_RESULTS = [
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
    {
        "mediaType": "movie",
        "id": 999,
        "title": "Something Else Entirely",
        "releaseDate": "2020-01-01",
    },
]

LOTR_RESULTS = [
    {
        "mediaType": "movie",
        "id": 120,
        "title": "The Lord of the Rings: The Fellowship of the Ring",
        "releaseDate": "2001-12-19",
    },
    {
        "mediaType": "movie",
        "id": 121,
        "title": "The Lord of the Rings: The Two Towers",
        "releaseDate": "2002-12-18",
    },
    {
        "mediaType": "movie",
        "id": 122,
        "title": "The Lord of the Rings: The Return of the King",
        "releaseDate": "2003-12-17",
    },
    {
        "mediaType": "movie",
        "id": 888,
        "title": "Lord of the Rings Extended Edition Documentary",
        "releaseDate": "2011-01-01",
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

    def __init__(self, *, results: list[dict[str, Any]] | None = None) -> None:
        self.results = list(results or [])
        self.search_calls: list[tuple[str, int]] = []
        self.request_calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append((query, page))
        return {"ok": True, "mode": "live", "results": list(self.results)}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        for row in self.results:
            if int(row.get("id") or 0) == media_id and row.get("mediaType") == media_type:
                return {"ok": True, "media": dict(row)}
        return {"ok": False}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(dict(kwargs))
        return {
            "ok": True,
            "requestStatus": 2,
            "mediaStatus": 3,
            "requestId": 1,
        }


class FakeSystemOne:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def system_one(self, *, state: Any, questions: dict | None = None, model: str | None = None):
        self.calls.append({"state": state, "questions": questions, "model": model})
        return parse_answers(self.payload)


def _media_payload(
    choice: str,
    *,
    conf: float = 0.9,
    needs_llm: float = 0.1,
    wants_queue: float = 0.8,
) -> dict[str, Any]:
    return {
        "model": "jev-test",
        "answers": {
            "media_ask": {
                "type": "choice",
                "choice": choice,
                "confidence": conf,
                "probabilities": {choice: conf},
            },
            "needs_llm": {"type": "noul", "noul": needs_llm},
            "wants_queue": {"type": "noul", "noul": wants_queue},
            "is_confirm": {"type": "noul", "noul": 0.05},
            "is_cancel": {"type": "noul", "noul": 0.05},
            "risk": {
                "type": "score",
                "score": 1.0,
                "confidence": 0.8,
                "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"},
            },
        },
    }


@pytest.fixture
def bot_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "jev_enabled", False)
    stores: list[TelegramStore] = []

    def make(overseerr: Any) -> TelegramMediaBot:
        store = TelegramStore(tmp_path / f"media-intel-{len(stores)}.db")
        stores.append(store)
        return TelegramMediaBot(store, overseerr_client=overseerr)

    yield make
    for store in stores:
        store.close()


@pytest.fixture(autouse=True)
def _reset_jev():
    reset_client()
    yield
    reset_client()


# --- local fail-open classifier -------------------------------------------------


def test_local_classifier_acceptance_examples() -> None:
    assert classify_media_ask_sync("Harry Potter").kind == "known_franchise"
    series = classify_media_ask_sync("Harry Potter, all movies")
    assert series.kind == "series_all"
    assert series.search_title == "Harry Potter"
    assert series.needs_llm is False

    edition = classify_media_ask_sync("Lord of the Rings extended edition")
    assert edition.kind == "edition"
    assert edition.search_title == "Lord of the Rings"
    assert edition.edition_key == "extended"
    assert edition.needs_llm is False

    riddle = classify_media_ask_sync(
        "that movie with the glasses that became a wizard"
    )
    assert riddle.kind == "describe"
    assert riddle.needs_llm is True

    chat = classify_media_ask_sync("what's Harry Potter about?")
    assert chat.kind == "chat_about"
    assert chat.search_title == "Harry Potter"
    assert chat.needs_llm is True


def test_extract_edition_strips_literal_extended_phrase() -> None:
    pref = extract_edition("Lord of the Rings extended edition")
    assert pref is not None
    assert pref.clean_title == "Lord of the Rings"
    assert pref.key == "extended"
    assert "extended" not in pref.clean_title.casefold() or pref.clean_title == "Lord of the Rings"


# --- Jev-first routing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_jev_first_routes_exact_title_without_llm(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_mod

    fake_ov = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 438631,
                "title": "Dune",
                "releaseDate": "2021-10-22",
            }
        ]
    )
    bot = bot_factory(fake_ov)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    monkeypatch.setattr(settings, "jev_shadow", True)
    set_client(FakeSystemOne(_media_payload("exact_title", needs_llm=0.05)))

    async def _boom(_text: str):
        raise AssertionError("LLM must not run for exact_title")

    monkeypatch.setattr(bot_mod, "guess_catalog_titles", _boom)

    reply = await bot.handle_message(_message("Dune"))
    assert reply is not None
    assert fake_ov.search_calls == [("Dune", 1)]
    assert "Dune" in reply.text
    assert reply.reply_markup is not None
    assert fake_ov.request_calls == []


@pytest.mark.asyncio
async def test_jev_first_known_franchise_harry_potter(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_mod

    fake_ov = FakeOverseerr(results=HARRY_POTTER_RESULTS)
    bot = bot_factory(fake_ov)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    set_client(FakeSystemOne(_media_payload("known_franchise", needs_llm=0.05)))

    async def _boom(_text: str):
        raise AssertionError("LLM must not run for known_franchise")

    monkeypatch.setattr(bot_mod, "guess_catalog_titles", _boom)

    reply = await bot.handle_message(_message("Harry Potter"))
    assert reply is not None
    assert fake_ov.search_calls == [("Harry Potter", 1)]
    assert "Philosopher" in reply.text or "Chamber" in reply.text
    assert "Something Else Entirely" not in reply.text
    assert "franchise" in reply.text.lower() or "Harry Potter" in reply.text
    assert reply.reply_markup is not None
    assert fake_ov.request_calls == []


@pytest.mark.asyncio
async def test_jev_series_all_multi_get_never_silent_queues(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ov = FakeOverseerr(results=HARRY_POTTER_RESULTS)
    bot = bot_factory(fake_ov)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    set_client(FakeSystemOne(_media_payload("series_all", needs_llm=0.05)))

    reply = await bot.handle_message(_message("all Harry Potters"))
    assert reply is not None
    assert fake_ov.search_calls and fake_ov.search_calls[0][0] == "Harry Potter"
    assert "whole series" in reply.text.lower() or "tap get on each" in reply.text.lower()
    keyboard = reply.reply_markup["inline_keyboard"] if reply.reply_markup else []
    assert len(keyboard) >= 2
    assert fake_ov.request_calls == []

    # Nah acknowledges out loud (never silent) but must not invent a queue.
    nah = await bot.handle_message(_message("nah", message_id=2))
    assert nah is not None
    assert "not queueing" in nah.text.lower()
    assert nah.reply_markup is None
    assert fake_ov.request_calls == []

    listed = await bot.handle_message(_message("list movies", message_id=3))
    assert listed is not None
    assert "list" in listed.text.lower()
    assert fake_ov.request_calls == []


@pytest.mark.asyncio
async def test_jev_edition_aware_does_not_literal_search_extended(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_mod

    fake_ov = FakeOverseerr(results=LOTR_RESULTS)
    bot = bot_factory(fake_ov)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    set_client(FakeSystemOne(_media_payload("edition_aware", needs_llm=0.05)))

    async def _boom(_text: str):
        raise AssertionError("LLM must not run for edition_aware")

    monkeypatch.setattr(bot_mod, "guess_catalog_titles", _boom)

    reply = await bot.handle_message(_message("Lord of the Rings extended edition"))
    assert reply is not None
    assert fake_ov.search_calls == [("Lord of the Rings", 1)]
    assert all("extended edition" not in q.casefold() for q, _ in fake_ov.search_calls)
    assert "extended" in reply.text.lower()
    assert "Fellowship" in reply.text or "Two Towers" in reply.text
    assert fake_ov.request_calls == []


@pytest.mark.asyncio
async def test_jev_descriptive_riddle_uses_llm_then_confirm(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_mod

    fake_ov = FakeOverseerr(results=HARRY_POTTER_RESULTS[:1])
    bot = bot_factory(fake_ov)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    set_client(
        FakeSystemOne(_media_payload("descriptive_riddle", needs_llm=0.95, wants_queue=0.7))
    )

    async def _fake_guess(text: str):
        assert "wizard" in text.lower() or "glasses" in text.lower()
        return [CatalogGuess(search_title="Harry Potter", year=2001, media_kind="movie")]

    monkeypatch.setattr(bot_mod, "guess_catalog_titles", _fake_guess)

    plot = "that movie with the glasses that became a wizard"
    reply = await bot.handle_message(_message(plot))
    assert reply is not None
    assert fake_ov.search_calls == [("Harry Potter", 1)]
    assert plot not in {q for q, _ in fake_ov.search_calls}
    assert fake_ov.request_calls == []

    nah = await bot.handle_message(_message("nah", message_id=2))
    assert nah is not None
    assert "not queueing" in nah.text.lower()
    assert fake_ov.request_calls == []


@pytest.mark.asyncio
async def test_jev_chat_about_answers_without_get_or_queue(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_mod

    fake_ov = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 671,
                "title": "Harry Potter and the Philosopher's Stone",
                "releaseDate": "2001-11-16",
                "overview": "A boy discovers he is a wizard.",
            }
        ]
    )
    bot = bot_factory(fake_ov)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    set_client(
        FakeSystemOne(_media_payload("chat_about_title", needs_llm=0.9, wants_queue=0.05))
    )

    async def _fake_answer(text: str, *, catalog_context: str = ""):
        assert "about" in text.lower()
        return {
            "answer": "Harry Potter follows a young wizard at Hogwarts.",
            "search_title": "Harry Potter",
            "year": 2001,
            "media_kind": "movie",
        }

    monkeypatch.setattr(bot_mod, "answer_catalog_question", _fake_answer)

    reply = await bot.handle_message(_message("what's Harry Potter about?"))
    assert reply is not None
    assert "wizard" in reply.text.lower() or "Hogwarts" in reply.text
    assert reply.reply_markup is None
    assert fake_ov.request_calls == []


@pytest.mark.asyncio
async def test_jev_fail_open_when_disabled_uses_local_path(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ov = FakeOverseerr(results=LOTR_RESULTS)
    bot = bot_factory(fake_ov)
    monkeypatch.setattr(settings, "jev_enabled", False)

    reply = await bot.handle_message(_message("Lord of the Rings extended edition"))
    assert reply is not None
    assert fake_ov.search_calls == [("Lord of the Rings", 1)]
    assert fake_ov.request_calls == []


@pytest.mark.asyncio
async def test_classify_media_ask_uses_jev_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    fake = FakeSystemOne(_media_payload("series_all", needs_llm=0.05))
    set_client(fake)

    intent = await classify_media_ask("give me every Harry Potter film")
    assert intent.source == "jev"
    assert intent.kind == "series_all"
    assert intent.search_title.lower().startswith("harry potter") or "Harry" in intent.search_title
    assert intent.needs_llm is False
    assert fake.calls
    assert "media_ask" in (fake.calls[0]["questions"] or {})
