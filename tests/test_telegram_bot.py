from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hearth.config import settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.callbacks import CallbackCodec, ExpiredCallback, InvalidCallback
from hearth.telegram.models import BotReply
from hearth.telegram.parse import parse_message_text
from hearth.telegram.service import TelegramBotService
from hearth.telegram.store import TelegramStore
from hearth.tools.arr import OverseerrError


CHAT_ID = -100123
USER_ID = 42


def _message(
    text: str,
    *,
    message_id: int = 1,
    chat_id: int = CHAT_ID,
    user_id: int = USER_ID,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": False},
        "text": text,
    }


def _callback(
    data: str,
    *,
    callback_id: str = "callback-1",
    message_id: int = 900,
    chat_id: int = CHAT_ID,
    user_id: int = USER_ID,
) -> dict[str, Any]:
    return {
        "id": callback_id,
        "data": data,
        "from": {"id": user_id, "is_bot": False},
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "supergroup"},
        },
    }


def _first_button(reply: BotReply) -> str:
    assert reply.reply_markup is not None
    keyboard = reply.reply_markup["inline_keyboard"]
    assert keyboard
    return str(keyboard[0][0]["callback_data"])


class FakeOverseerr:
    live = True

    def __init__(
        self,
        *,
        results: list[dict[str, Any]] | None = None,
        request_result: dict[str, Any] | BaseException | None = None,
    ) -> None:
        self.results = list(results or [])
        self.request_result = request_result or {
            "ok": True,
            "requestStatus": 2,
            "mediaStatus": 3,
            "requestId": 77,
        }
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[tuple[int, str]] = []
        self.request_calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append((query, page))
        return {"ok": True, "mode": "live", "results": list(self.results)}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        self.detail_calls.append((media_id, media_type))
        for row in self.results:
            if row.get("mediaType") != media_type:
                continue
            try:
                row_id = int(row.get("id") or row.get("mediaId") or row.get("tmdbId"))
            except (TypeError, ValueError):
                continue
            if row_id == media_id:
                return {"ok": True, "media": dict(row)}
        return {"ok": False, "reason": "not_found"}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(dict(kwargs))
        if isinstance(self.request_result, BaseException):
            raise self.request_result
        return dict(self.request_result)


class RecordingProgress:
    def __init__(self) -> None:
        self.active: list[Any] = []
        self.track_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def reset(self) -> None:
        self.active.clear()
        self.track_calls.clear()

    def track(self, *args: Any, **kwargs: Any) -> None:
        self.track_calls.append((args, kwargs))
        return None


@pytest.fixture
def bot_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]]:
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    stores: list[TelegramStore] = []

    def make(
        overseerr: FakeOverseerr,
        *,
        progress: RecordingProgress | None = None,
    ) -> tuple[TelegramMediaBot, TelegramStore, RecordingProgress]:
        store = TelegramStore(tmp_path / f"telegram-{len(stores)}.db")
        tracker = progress or RecordingProgress()
        stores.append(store)
        return (
            TelegramMediaBot(store, overseerr_client=overseerr, progress=tracker),
            store,
            tracker,
        )

    yield make
    for store in stores:
        store.close()


@pytest.mark.parametrize(
    ("text", "action", "title", "year", "season", "media_type"),
    [
        ("/help", "help", "", None, None, None),
        ("/status", "status", "", None, None, None),
        ("/search Dune (2021)", "search", "Dune", 2021, None, None),
        ("download Severance S02", "search", "Severance", None, 2, "tv"),
        ("film: Blade Runner (1982)", "search", "Blade Runner", 1982, None, "movie"),
        ("zoek Dark seizoen 3", "search", "Dark", None, 3, "tv"),
        ("Get Out", "search", "Get Out", None, None, None),
        ("Search Party", "search", "Search Party", None, None, None),
        ("get me Arrival", "search", "Arrival", None, None, None),
        ("search for The Matrix", "search", "The Matrix", None, None, None),
    ],
)
def test_parser_commands_titles_years_and_seasons(
    text: str,
    action: str,
    title: str,
    year: int | None,
    season: int | None,
    media_type: str | None,
) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == action
    assert parsed.title == title
    assert parsed.year == year
    assert parsed.season == season
    assert parsed.media_type == media_type


@pytest.mark.parametrize(
    ("text", "media_type", "tmdb_id", "title", "season"),
    [
        (
            "https://www.themoviedb.org/movie/438631-dune",
            "movie",
            438631,
            "Dune",
            None,
        ),
        (
            "request https://www.themoviedb.org/tv/95396-severance S02",
            "tv",
            95396,
            "Severance",
            2,
        ),
        (
            "https://themoviedb.org/en-US/tv/1399-game-of-thrones",
            "tv",
            1399,
            "Game Of Thrones",
            None,
        ),
    ],
)
def test_parser_extracts_typed_tmdb_urls_without_fetching(
    text: str,
    media_type: str,
    tmdb_id: int,
    title: str,
    season: int | None,
) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "search"
    assert parsed.media_type == media_type
    assert parsed.tmdb_id == tmdb_id
    assert parsed.title == title
    assert parsed.season == season
    assert parsed.catalog_host in {"themoviedb.org", "www.themoviedb.org"}


@pytest.mark.parametrize(
    ("text", "kwargs", "action", "reason"),
    [
        ("magnet:?xt=urn:btih:abc", {}, "reject", "torrent_download"),
        ("movie.torrent", {}, "reject", "torrent_download"),
        ("tmdb:603", {}, "reject", "tmdb_type_required"),
        ("movie tmdb:603 S02", {}, "reject", "movie_has_season"),
        ("https://example.test/watch/603", {}, "ignore", "unsupported_url"),
        ("Dune", {"has_media": True, "media_kind": "video"}, "reject", "media_attachment:video"),
        ("hello", {}, "ignore", "chatter"),
    ],
)
def test_parser_rejects_unsafe_or_non_request_inputs(
    text: str,
    kwargs: dict[str, Any],
    action: str,
    reason: str,
) -> None:
    parsed = parse_message_text(text, **kwargs)

    assert parsed.action == action
    assert parsed.reason == reason


def test_callback_is_compact_chat_bound_tamper_evident_and_supports_specials() -> None:
    codec = CallbackCodec("test signing secret", ttl_seconds=60)
    encoded = codec.encode("tv", 95396, CHAT_ID, season=0, now=100.0)

    assert len(encoded.encode("utf-8")) <= 64
    decoded = codec.decode(encoded, CHAT_ID, now=120.0)
    assert decoded.media_type == "tv"
    assert decoded.tmdb_id == 95396
    assert decoded.season == 0

    replacement = "A" if encoded[-1] != "A" else "B"
    with pytest.raises(InvalidCallback):
        codec.decode(encoded[:-1] + replacement, CHAT_ID, now=120.0)
    with pytest.raises(InvalidCallback):
        codec.decode(encoded, CHAT_ID + 1, now=120.0)
    with pytest.raises(ExpiredCallback):
        codec.decode(encoded, CHAT_ID, now=180.0)


@pytest.mark.asyncio
async def test_search_filters_people_deduplicates_ranks_and_signs_exact_results(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
) -> None:
    fake = FakeOverseerr(
        results=[
            {"mediaType": "person", "id": 77, "name": "Denis Villeneuve"},
            {
                "mediaType": "movie",
                "id": 841,
                "title": "Dune",
                "releaseDate": "1984-12-14",
                "voteAverage": 7.0,
            },
            {
                "mediaType": "movie",
                "id": 438631,
                "title": "Dune",
                "releaseDate": "2021-09-15",
                "voteAverage": 8.0,
            },
            {
                "mediaType": "movie",
                "id": 438631,
                "title": "Dune duplicate",
                "releaseDate": "2021-09-15",
            },
            {
                "mediaType": "tv",
                "id": 123,
                "name": "Dune: Prophecy",
                "firstAirDate": "2024-11-17",
            },
        ]
    )
    bot, _, _ = bot_factory(fake)

    reply = await bot.handle_message(_message("movie: Dune (2021)"))

    assert reply is not None
    assert fake.search_calls == [("Dune", 1)]
    assert "1. Dune (2021)" in reply.text
    assert "2. Dune (1984)" in reply.text
    assert "Denis Villeneuve" not in reply.text
    assert "duplicate" not in reply.text
    assert "Prophecy" not in reply.text
    keyboard = reply.reply_markup["inline_keyboard"] if reply.reply_markup else []
    assert len(keyboard) == 2
    callback_data = keyboard[0][0]["callback_data"]
    decoded = bot._callback_codec().decode(callback_data, CHAT_ID)
    assert (decoded.media_type, decoded.tmdb_id) == ("movie", 438631)


@pytest.mark.asyncio
async def test_spoken_the_movie_suffix_is_stripped_before_overseerr_search(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
) -> None:
    """NAS: encoded \"Talk to me, the movie\" is 200/empty; search \"Talk to me\" instead."""
    fake = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 1009811,
                "title": "Talk to Me",
                "releaseDate": "2023-07-28",
            },
            {
                "mediaType": "movie",
                "id": 550,
                "title": "Talk Radio",
                "releaseDate": "1988-12-21",
            },
        ]
    )
    bot, _, _ = bot_factory(fake)

    reply = await bot.handle_message(_message("Talk to me, the movie"))

    assert reply is not None
    assert fake.search_calls == [("Talk to me", 1)]
    assert "1. Talk to Me" in reply.text
    assert reply.reply_markup is not None
    assert reply.reply_markup["inline_keyboard"][0][0]["text"].startswith("Get 1")
    decoded = bot._callback_codec().decode(_first_button(reply), CHAT_ID)
    assert (decoded.media_type, decoded.tmdb_id) == ("movie", 1009811)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "exact_title", "exact_id", "distractors"),
    [
        (
            "Talk to Me",
            "Talk to Me",
            1009811,
            [
                {
                    "mediaType": "movie",
                    "id": 550,
                    "title": "Talk Radio",
                    "releaseDate": "1988-12-21",
                },
                {
                    "mediaType": "movie",
                    "id": 13,
                    "title": "Can We Talk?",
                    "releaseDate": "2010-01-01",
                },
                {
                    "mediaType": "person",
                    "id": 99,
                    "name": "Talk Host",
                },
            ],
        ),
        (
            "Late Night with the Devil",
            "Late Night with the Devil",
            1020006,
            [
                {
                    "mediaType": "movie",
                    "id": 4248,
                    "title": "Late Night",
                    "releaseDate": "2019-06-07",
                },
                {
                    "mediaType": "tv",
                    "id": 1408,
                    "name": "Late Night with Conan O'Brien",
                    "firstAirDate": "1993-09-13",
                },
                {
                    "mediaType": "movie",
                    "id": 77,
                    "title": "The Devil Wears Prada",
                    "releaseDate": "2006-06-30",
                },
            ],
        ),
    ],
)
async def test_horror_title_search_payloads_still_rank_exact_hit_first(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
    query: str,
    exact_title: str,
    exact_id: int,
    distractors: list[dict[str, Any]],
) -> None:
    fake = FakeOverseerr(
        results=[
            *distractors,
            {
                "mediaType": "movie",
                "id": exact_id,
                "title": exact_title,
                "releaseDate": "2023-07-28" if exact_id == 1009811 else "2024-03-22",
            },
        ]
    )
    bot, _, _ = bot_factory(fake)

    reply = await bot.handle_message(_message(query))

    assert reply is not None
    assert fake.search_calls == [(query, 1)]
    ranked = [line for line in reply.text.splitlines() if line[:1].isdigit()]
    assert ranked and ranked[0].startswith(f"1. {exact_title}")
    callback_data = _first_button(reply)
    decoded = bot._callback_codec().decode(callback_data, CHAT_ID)
    assert (decoded.media_type, decoded.tmdb_id) == ("movie", exact_id)
    assert reply.reply_markup is not None
    assert reply.reply_markup["inline_keyboard"][0][0]["text"].startswith("Get 1")
    assert "Wikipedia" not in reply.text


async def _button_for(
    bot: TelegramMediaBot,
    text: str,
    *,
    message_id: int = 1,
) -> str:
    reply = await bot.handle_message(_message(text, message_id=message_id))
    assert reply is not None
    return _first_button(reply)


@pytest.mark.asyncio
async def test_callback_posts_exact_tv_id_type_and_season_then_tracks_approved_request(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
) -> None:
    fake = FakeOverseerr(
        results=[
            {
                "mediaType": "tv",
                "id": 95396,
                "name": "Severance",
                "firstAirDate": "2022-02-18",
            }
        ],
        request_result={
            "ok": True,
            "requestStatus": 2,
            "mediaStatus": 3,
            "requestId": 321,
        },
    )
    bot, _, progress = bot_factory(fake)
    data = await _button_for(bot, "Severance S02")

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert "sent it to the media stack" in reply.text
    assert fake.request_calls == [
        {
            "query": "Severance",
            "media_id": 95396,
            "media_type": "tv",
            "seasons": [2],
        }
    ]
    assert len(progress.track_calls) == 1
    args, kwargs = progress.track_calls[0]
    assert args[:3] == (CHAT_ID, "Severance", "sonarr")
    assert kwargs["tmdb_id"] == 95396
    assert kwargs["request_id"] == 321


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_status", "expected", "should_track"),
    [
        (1, "waiting for Overseerr approval", False),
        (2, "sent it to the media stack", True),
    ],
)
async def test_only_approved_requests_start_download_tracking(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
    request_status: int,
    expected: str,
    should_track: bool,
) -> None:
    fake = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 438631,
                "title": "Dune",
                "releaseDate": "2021-09-15",
            }
        ],
        request_result={
            "ok": True,
            "requestStatus": request_status,
            "mediaStatus": 2 if request_status == 1 else 3,
            "requestId": 55,
        },
    )
    bot, _, progress = bot_factory(fake)
    data = await _button_for(bot, "Dune")

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert expected in reply.text
    assert bool(progress.track_calls) is should_track


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"ok": False, "reason": "no_seasons", "status_code": 202},
            "no requestable seasons",
        ),
        (
            {"ok": False, "reason": "forbidden", "status_code": 403},
            "Check the API key",
        ),
        (
            {
                "ok": False,
                "reason": "already_requested",
                "already": True,
                "status_code": 409,
            },
            "already requested",
        ),
    ],
)
async def test_callback_reports_overseerr_202_403_and_409_without_tracking(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
    result: dict[str, Any],
    expected: str,
) -> None:
    fake = FakeOverseerr(
        results=[{"mediaType": "tv", "id": 1399, "name": "Game of Thrones"}],
        request_result=result,
    )
    bot, _, progress = bot_factory(fake)
    data = await _button_for(bot, "Game of Thrones S01")

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert expected in reply.text
    assert len(fake.request_calls) == 1
    assert progress.track_calls == []
    assert "sent it to the media stack" not in reply.text


@pytest.mark.asyncio
async def test_upstream_request_uncertainty_never_becomes_fake_success(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
) -> None:
    fake = FakeOverseerr(
        results=[{"mediaType": "movie", "id": 603, "title": "The Matrix"}],
        request_result=OverseerrError("connection lost", operation="request"),
    )
    bot, store, progress = bot_factory(fake)
    data = await _button_for(bot, "The Matrix")

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None
    assert "outcome" in reply.text
    assert "uncertain" in reply.text
    assert "Check Overseerr" in reply.text
    assert progress.track_calls == []
    digest = hashlib.sha256(f"{CHAT_ID}:900:{data}".encode()).hexdigest()[:32]
    state = store.callback_state(digest)
    assert state is not None
    assert state["state"] == "uncertain"


@pytest.mark.asyncio
async def test_callback_replay_is_idempotent_even_with_a_new_callback_query_id(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
) -> None:
    fake = FakeOverseerr(
        results=[{"mediaType": "movie", "id": 603, "title": "The Matrix"}]
    )
    bot, _, _ = bot_factory(fake)
    data = await _button_for(bot, "The Matrix")

    first = await bot.handle_callback(_callback(data, callback_id="tap-1"))
    replay = await bot.handle_callback(_callback(data, callback_id="tap-2"))

    assert first is not None and "sent it to the media stack" in first.text
    assert replay is not None and "already handled" in replay.text
    assert len(fake.request_calls) == 1


class RecordingClient:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.bot_user_id = 999
        self.bot_username = "hearth_test_bot"

    async def answer_callback_query(self, callback_query_id: str) -> dict[str, Any]:
        self.events.append(f"ack:{callback_query_id}")
        return {"ok": True}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.events.append(f"edit:{chat_id}:{message_id}")
        return {"ok": True}

    async def send_message(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"ok": True}

    async def aclose(self) -> None:
        return None


class SlowCallbackBot:
    def __init__(self, events: list[str], gate: asyncio.Event) -> None:
        self.events = events
        self.gate = gate
        self.started = asyncio.Event()
        self.progress = RecordingProgress()

    async def handle_callback(self, callback: dict[str, Any]) -> BotReply:
        self.events.append("callback:start")
        self.started.set()
        await self.gate.wait()
        self.events.append("callback:end")
        return BotReply("done", edit_message_id=900)


@pytest.mark.asyncio
async def test_service_acknowledges_callback_before_slow_backend_work(tmp_path: Path) -> None:
    events: list[str] = []
    gate = asyncio.Event()
    client = RecordingClient(events)
    bot = SlowCallbackBot(events, gate)
    store = TelegramStore(tmp_path / "service-ack.db")
    service = TelegramBotService(client=client, store=store, bot=bot)  # type: ignore[arg-type]
    service._semaphore = asyncio.Semaphore(1)
    update = {"update_id": 10, "callback_query": _callback("signed-data")}

    async def process_like_poll_loop() -> bool:
        await service._ack_callbacks([update])
        return await service._process_batch([update])

    task = asyncio.create_task(process_like_poll_loop())
    await asyncio.wait_for(bot.started.wait(), timeout=1)
    assert events[:2] == ["ack:callback-1", "callback:start"]
    assert store.get_offset() is None
    gate.set()
    assert await asyncio.wait_for(task, timeout=1) is True
    assert store.get_offset() == 11
    store.close()


class ConcurrentBot:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.two_started = asyncio.Event()
        self.events: list[tuple[str, int, int]] = []
        self.active = 0
        self.max_active = 0
        self.progress = RecordingProgress()

    async def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        message_id = int(message["message_id"])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.events.append(("start", chat_id, message_id))
        if self.active >= 2:
            self.two_started.set()
        await self.release.wait()
        await asyncio.sleep(0)
        self.events.append(("end", chat_id, message_id))
        self.active -= 1
        return None


@pytest.mark.asyncio
async def test_service_preserves_same_chat_order_bounds_cross_chat_work_and_offsets_last(
    tmp_path: Path,
) -> None:
    store = TelegramStore(tmp_path / "service-order.db")
    release = asyncio.Event()
    bot = ConcurrentBot(release)
    service = TelegramBotService(
        client=RecordingClient(),
        store=store,
        bot=bot,  # type: ignore[arg-type]
    )
    service._semaphore = asyncio.Semaphore(2)
    updates = [
        {"update_id": 20, "message": _message("one", message_id=1, chat_id=100)},
        {"update_id": 21, "message": _message("two", message_id=2, chat_id=100)},
        {"update_id": 22, "message": _message("other", message_id=3, chat_id=200)},
        {"update_id": 23, "message": _message("third", message_id=4, chat_id=300)},
    ]

    task = asyncio.create_task(service._process_batch(updates))
    await asyncio.wait_for(bot.two_started.wait(), timeout=1)
    assert bot.max_active == 2
    assert store.get_offset() is None
    release.set()
    assert await asyncio.wait_for(task, timeout=1) is True

    first_end = bot.events.index(("end", 100, 1))
    second_start = bot.events.index(("start", 100, 2))
    assert first_end < second_start
    assert bot.max_active == 2
    assert store.get_offset() == 24
    assert service.last_update_id == 23
    store.close()


# --- plot/vibe guess + button labels (VAULT Movies & Series) -----------------


@pytest.mark.asyncio
async def test_scar_wizard_plot_guesses_then_confirms_without_literal_search(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live bug: plot English was sent as a literal Overseerr title search."""
    from hearth.telegram import bot as bot_mod
    from hearth.telegram.intent import CatalogGuess

    fake = FakeOverseerr(
        results=[
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
        ]
    )
    bot, _, _ = bot_factory(fake)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-used")

    async def _fake_guess(text: str) -> CatalogGuess:
        assert "wizard" in text.lower() or "scar" in text.lower()
        return CatalogGuess(
            search_title="Harry Potter",
            year=2001,
            media_kind="movie",
            confidence=0.95,
        )

    monkeypatch.setattr(bot_mod, "guess_catalog_title", _fake_guess)

    plot = "That movie with the wizard with a scar on his face"
    reply = await bot.handle_message(_message(plot))

    assert reply is not None
    assert fake.search_calls == [("Harry Potter", 1)]
    assert plot not in {q for q, _ in fake.search_calls}
    assert "No Overseerr matches" not in reply.text
    assert "Wikipedia" not in reply.text
    assert "Harry Potter" in reply.text
    assert reply.reply_markup is not None
    assert fake.request_calls == []  # never auto-queue
    assert "Wikipedia" not in reply.text
    # Multi-hit guesses require Get (no sticky yes) — bare nah must not invent a queue.
    nah = await bot.handle_message(_message("nah", message_id=2))
    assert nah is None
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_dutch_plot_guess_path_searches_resolved_title_not_plot(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_mod
    from hearth.telegram.intent import CatalogGuess

    dutch = "Die film met de tovenaar met een litteken in zijn gezicht"
    fake = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 671,
                "title": "Harry Potter and the Philosopher's Stone",
                "releaseDate": "2001-11-16",
            }
        ]
    )
    bot, _, _ = bot_factory(fake)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    async def _fake_guess(text: str) -> CatalogGuess:
        assert "tovenaar" in text.lower() or "litteken" in text.lower()
        return CatalogGuess(search_title="Harry Potter", year=2001, media_kind="movie")

    monkeypatch.setattr(bot_mod, "guess_catalog_title", _fake_guess)

    reply = await bot.handle_message(_message(dutch))

    assert reply is not None
    assert fake.search_calls == [("Harry Potter", 1)]
    assert all("tovenaar" not in q.lower() for q, _ in fake.search_calls)
    assert "Did you mean" in reply.text
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_plot_guess_yes_queues_by_tmdb_media_id_nah_does_not(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_mod
    from hearth.telegram.intent import CatalogGuess

    fake = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 671,
                "title": "Harry Potter and the Philosopher's Stone",
                "releaseDate": "2001-11-16",
            }
        ],
        request_result={
            "ok": True,
            "requestStatus": 2,
            "mediaStatus": 3,
            "requestId": 88,
        },
    )
    bot, _, progress = bot_factory(fake)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    async def _fake_guess(text: str) -> CatalogGuess:
        return CatalogGuess(search_title="Harry Potter", year=2001, media_kind="movie")

    monkeypatch.setattr(bot_mod, "guess_catalog_title", _fake_guess)

    ask = await bot.handle_message(
        _message("That movie with the wizard with a scar on his face")
    )
    assert ask is not None
    assert fake.request_calls == []

    # Reject must clear the sticky guess without posting a request.
    nah = await bot.handle_message(_message("nah", message_id=2))
    assert nah is not None
    assert "not queueing" in nah.text.lower()
    assert fake.request_calls == []

    # Re-ask and confirm with yes → Overseerr request by mediaId.
    ask2 = await bot.handle_message(
        _message("That movie with the wizard with a scar on his face", message_id=3)
    )
    assert ask2 is not None
    yes = await bot.handle_message(_message("yes", message_id=4))
    assert yes is not None
    assert fake.request_calls == [
        {
            "query": "Harry Potter and the Philosopher's Stone",
            "media_id": 671,
            "media_type": "movie",
            "seasons": None,
        }
    ]
    assert progress.track_calls


@pytest.mark.asyncio
async def test_talk_to_me_buttons_include_year_and_kind_and_keep_distinct_rows(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
) -> None:
    fake = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 1009811,
                "title": "Talk to Me",
                "releaseDate": "2023-07-28",
            },
            {
                "mediaType": "tv",
                "id": 1405,
                "name": "Talk to Me",
                "firstAirDate": "2007-01-01",
            },
            {
                "mediaType": "movie",
                "id": 33339,
                "title": "Talk to Me",
                "releaseDate": "1984-01-01",
            },
            {
                "mediaType": "movie",
                "id": 14442,
                "title": "Talk to Me",
                "releaseDate": "2007-07-13",
            },
            {
                "mediaType": "movie",
                "id": 55555,
                "title": "Talk to Me",
                "releaseDate": "1996-01-01",
            },
            # True duplicate of 2023 movie — must collapse.
            {
                "mediaType": "movie",
                "id": 1009811,
                "title": "Talk to Me",
                "releaseDate": "2023-07-28",
            },
        ]
    )
    bot, _, _ = bot_factory(fake)

    reply = await bot.handle_message(_message("Talk to Me"))

    assert reply is not None
    assert fake.search_calls == [("Talk to Me", 1)]
    keyboard = reply.reply_markup["inline_keyboard"] if reply.reply_markup else []
    labels = [row[0]["text"] for row in keyboard]
    assert len(labels) == 5
    assert labels[0] == "Get 1 · Talk to Me (2023) movie"
    assert any("(2007) TV" in label for label in labels)
    assert any("(1984) movie" in label for label in labels)
    assert any("(1996) movie" in label for label in labels)
    # Every button must carry year + kind — never bare "Get N · Talk to Me".
    for label in labels:
        assert "(" in label and (" movie" in label or " TV" in label)


@pytest.mark.asyncio
async def test_land_exact_title_does_not_list_la_la_land(
    bot_factory: Callable[..., tuple[TelegramMediaBot, TelegramStore, RecordingProgress]],
) -> None:
    fake = FakeOverseerr(
        results=[
            {
                "mediaType": "movie",
                "id": 313369,
                "title": "La La Land",
                "releaseDate": "2016-12-09",
            },
            {
                "mediaType": "movie",
                "id": 688271,
                "title": "Land",
                "releaseDate": "2021-02-12",
            },
            {
                "mediaType": "movie",
                "id": 9470,
                "title": "Cop Land",
                "releaseDate": "1997-08-15",
            },
        ]
    )
    bot, _, _ = bot_factory(fake)

    reply = await bot.handle_message(_message("Land"))

    assert reply is not None
    assert "La La Land" not in reply.text
    assert "Cop Land" not in reply.text
    assert "Land (2021)" in reply.text
    keyboard = reply.reply_markup["inline_keyboard"] if reply.reply_markup else []
    assert len(keyboard) == 1
    assert "Land (2021) movie" in keyboard[0][0]["text"]


def test_looks_like_concrete_title_keeps_named_titles_and_rejects_plots() -> None:
    from hearth.telegram.intent import looks_like_concrete_title

    assert looks_like_concrete_title("Talk to Me")
    assert looks_like_concrete_title("Land")
    assert looks_like_concrete_title("Dune (2021)")
    assert looks_like_concrete_title("Late Night with the Devil")
    assert not looks_like_concrete_title(
        "That movie with the wizard with a scar on his face"
    )
    assert not looks_like_concrete_title(
        "Die film met de tovenaar met een litteken in zijn gezicht"
    )
    assert not looks_like_concrete_title("Land with robin wright")
