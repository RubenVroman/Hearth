"""Regressions for the Telegram media surface: misroutes, silence, and honesty.

Every case here is a real message that used to get the wrong answer. The
groupings mirror the failure modes rather than the modules: a real title read
as a vibe or a two-item plan, a routed turn that ended in silence, a Play
button that could not possibly succeed, and an LLM hop spent on a seed the
deterministic extractors had already produced.

Everything is mocked — TypeSafe, Overseerr, Infuse and OpenAI are never reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hearth.config import settings
from hearth.jev import reset_client, set_client
from hearth.jev.schema import parse_answers
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.callbacks import ACTION_CODES, ACTION_TITLE, CallbackCodec
from hearth.telegram.media.cards import CardRenderer
from hearth.telegram.media.classify import classify_media_ask_sync
from hearth.telegram.media.compound import split_compound_ask
from hearth.telegram.media.memory import speaker_scope
from hearth.telegram.media.moods import detect_mood, looks_like_vague_ask, names_one_release
from hearth.telegram.media.people import detect_person_ask
from hearth.telegram.media.play import PlayOutcome
from hearth.telegram.media.ranking import apply_exclusions
from hearth.jev.tools import is_write_tool, lane_for_tool
from hearth.telegram.models import MediaHit
from hearth.telegram.store import TelegramStore

CHAT_ID = -100777
USER_ID = 5151

COWBOYS_AND_ALIENS = {
    "mediaType": "movie",
    "id": 49849,
    "title": "Cowboys & Aliens",
    "releaseDate": "2011-07-29",
}
DATE_NIGHT = {
    "mediaType": "movie",
    "id": 35056,
    "title": "Date Night",
    "releaseDate": "2010-04-09",
}
DUNE = {
    "mediaType": "movie",
    "id": 438631,
    "title": "Dune",
    "releaseDate": "2021-10-22",
    "mediaInfo": {"status": 5},
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
]
LOTR = {
    "mediaType": "movie",
    "id": 120,
    "title": "The Lord of the Rings: The Fellowship of the Ring",
    "releaseDate": "2001-12-19",
}
HOBBIT = {
    "mediaType": "movie",
    "id": 49051,
    "title": "The Hobbit: An Unexpected Journey",
    "releaseDate": "2012-11-26",
}
SCARY_PICKS = [
    {"mediaType": "movie", "id": 900, "title": "Hereditary", "releaseDate": "2018-06-07"},
    {"mediaType": "movie", "id": 901, "title": "The Witch", "releaseDate": "2015-02-19"},
]


def _message(text: str, *, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "text": text,
    }


def _callback(data: str, *, message_id: int = 1, callback_id: str = "cb-1") -> dict[str, Any]:
    return {
        "id": callback_id,
        "data": data,
        "from": {"id": USER_ID, "is_bot": False},
        "message": {"message_id": message_id, "chat": {"id": CHAT_ID, "type": "supergroup"}},
    }


class FakeOverseerr:
    live = True

    def __init__(
        self,
        *,
        results: list[dict[str, Any]] | None = None,
        discover_results: list[dict[str, Any]] | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.results = list(results or [])
        self.discover_results = list(discover_results or [])
        # TMDB resolves well-known abbreviations; the fake needs to as well or
        # it cannot exercise the alias path at all.
        self.aliases = {k.casefold(): v.casefold() for k, v in (aliases or {}).items()}
        self.search_calls: list[str] = []
        self.discover_calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append(query)
        needle = " ".join(query.casefold().split())
        needle = self.aliases.get(needle, needle)
        rows = [
            row
            for row in self.results
            if needle in str(row.get("title") or "").casefold()
        ]
        return {"ok": True, "mode": "live", "results": rows}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        for row in self.results:
            if int(row.get("id") or 0) == int(media_id):
                return {"ok": True, "media": dict(row)}
        return {"ok": False}

    async def discover(self, **kwargs: Any) -> dict[str, Any]:
        self.discover_calls.append(dict(kwargs))
        return {"ok": True, "mode": "live", "results": list(self.discover_results)}

    async def collection(self, collection_id: int, *, limit: int = 12) -> dict[str, Any]:
        return {"ok": True, "mode": "live", "results": []}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "requestStatus": 2, "mediaStatus": 3, "requestId": 1}


class FakeSystemOne:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def system_one(
        self,
        *,
        state: Any,
        questions: dict | None = None,
        model: str | None = None,
    ):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.error is not None:
            raise self.error
        return parse_answers(self.payload)


def _media_payload(choice: str, *, conf: float = 0.9, needs_llm: float = 0.05) -> dict[str, Any]:
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
            "multi_item": {"type": "noul", "noul": 0.1},
            "wants_queue": {"type": "noul", "noul": 0.8},
            "is_confirm": {"type": "noul", "noul": 0.05},
            "is_cancel": {"type": "noul", "noul": 0.05},
            "risk": {"type": "score", "score": 1.0, "confidence": 0.8},
        },
    }


class _MemStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def put_callback_media(
        self, key: str, metadata: dict[str, Any], *, ttl_s: float = 0
    ) -> None:
        self.data[key] = dict(metadata)

    def get_callback_media(self, key: str) -> dict[str, Any] | None:
        payload = self.data.get(key)
        return dict(payload) if payload else None

    def clear_callback_media(self, key: str) -> None:
        self.data.pop(key, None)


@pytest.fixture
def bot_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:psychic-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "jev_enabled", False)
    stores: list[TelegramStore] = []

    def make(overseerr: Any) -> TelegramMediaBot:
        store = TelegramStore(tmp_path / f"psychic-{len(stores)}.db")
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


def _buttons(reply: Any) -> list[dict[str, str]]:
    keyboard = reply.reply_markup["inline_keyboard"] if reply.reply_markup else []
    return [button for row in keyboard for button in row]


# --- 1) A real title is never shredded into a two-item plan --------------------


@pytest.mark.parametrize(
    "title",
    [
        "Cowboys & Aliens",
        "Fast & Furious",
        "Dungeons & Dragons",
        "Bonnie & Clyde",
        "Will & Grace",
        "Starsky & Hutch",
        "Tango & Cash",
        "Sex & the City",
        "Harold & Kumar Go to White Castle",
    ],
)
def test_an_ampersand_inside_a_title_is_not_a_separator(title: str) -> None:
    assert split_compound_ask(title) == ()


@pytest.mark.parametrize(
    "title",
    [
        "Bonnie and Clyde",
        "Pride and Prejudice",
        "Thelma and Louise",
        "Harold and Maude",
        "Romeo and Juliet",
    ],
)
def test_and_joins_one_title_when_nothing_frames_a_plan(title: str) -> None:
    assert split_compound_ask(title) == ()


def test_a_two_item_comma_needs_a_plan_signal() -> None:
    # One film, and the shape is identical to a two-item list.
    assert split_compound_ask("Crouching Tiger, Hidden Dragon") == ()
    # Three items can only be a list.
    assert [p.title for p in split_compound_ask("Inception, Interstellar, Tenet")] == [
        "Inception",
        "Interstellar",
        "Tenet",
    ]


def test_real_compound_asks_still_split() -> None:
    assert [p.title for p in split_compound_ask("grab Inception and Interstellar")] == [
        "Inception",
        "Interstellar",
    ]
    assert [p.title for p in split_compound_ask("get me Dune and Arrival")] == [
        "Dune",
        "Arrival",
    ]
    assert [p.title for p in split_compound_ask("LOTR extended + Hobbit theatrical")] == [
        "LOTR",
        "Hobbit",
    ]
    assert [p.title for p in split_compound_ask("download Heat and also Collateral")] == [
        "Heat",
        "Collateral",
    ]


async def test_an_ampersand_title_is_searched_once_as_itself(bot_factory) -> None:
    overseerr = FakeOverseerr(results=[COWBOYS_AND_ALIENS])
    bot = bot_factory(overseerr)

    reply = await bot.handle_message(_message("Cowboys & Aliens"))

    assert reply is not None
    assert overseerr.search_calls == ["Cowboys & Aliens"]
    assert "Cowboys & Aliens" in reply.text
    assert len([b for b in _buttons(reply) if b["text"].startswith("Get ")]) == 1


# --- 2) Hard title evidence beats a vibe reading -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Scary Movie (2000)",
        "Funny Games (1997)",
        "Date Night (2010)",
        "Scary Movie 3",
        '"Date Night"',
    ],
)
def test_a_named_release_is_never_a_mood(text: str) -> None:
    assert names_one_release(text) is True
    assert detect_mood(text) is None


@pytest.mark.parametrize(
    "title",
    [
        "American Horror Story",
        "Friday Night Lights",
        "Comedy Central Roast",
        "Cowboys & Aliens",
        "Crouching Tiger, Hidden Dragon",
    ],
)
def test_title_cased_genre_phrases_stay_on_the_title_path(title: str) -> None:
    assert detect_mood(title) is None
    intent = classify_media_ask_sync(title)
    assert intent.kind in {"exact_title", "known_franchise"}, f"{title} → {intent.kind}"
    assert intent.needs_llm is False


@pytest.mark.parametrize(
    "ask",
    [
        "something scary",
        "something scary under 2 hours",
        "any horror movies",
        "kids movie",
        "Friday night for us",
        "kids movie for Parel",
        "best 90s action movies",
        "anything funny tonight",
    ],
)
def test_real_vibe_asks_still_reach_discover(ask: str) -> None:
    assert detect_mood(ask) is not None, ask


def test_a_something_title_is_not_a_vague_ask() -> None:
    assert looks_like_vague_ask("Something Wild") is False
    assert looks_like_vague_ask("Something's Gotta Give") is False
    assert classify_media_ask_sync("Something Wild").kind == "exact_title"
    # Real vague asks are untouched.
    assert looks_like_vague_ask("what should we watch") is True
    assert looks_like_vague_ask("surprise me") is True


# --- 3) Ambiguity is offered, not guessed --------------------------------------


@pytest.mark.parametrize("text", ["Date Night", "Feel Good", "Sofa Sunday"])
def test_an_ambiguous_vibe_carries_the_title_it_might_have_meant(text: str) -> None:
    spec = detect_mood(text)
    assert spec is not None
    assert spec.ambiguous_title == text


def test_a_framed_vibe_ask_has_nothing_to_disambiguate() -> None:
    for ask in ("something scary", "what should we watch", "any horror movies"):
        spec = detect_mood(ask)
        if spec is not None:
            assert spec.ambiguous_title == "", ask


def test_title_action_is_a_signed_refine_code() -> None:
    assert ACTION_TITLE in ACTION_CODES


def test_the_title_chip_outranks_the_other_refine_buttons() -> None:
    store = _MemStore()
    cards = CardRenderer(CallbackCodec(secret="psychic-secret-key"), store, ttl_s=600)
    rendered = cards.render(
        CHAT_ID,
        [MediaHit(media_type="movie", tmdb_id=900, title="Hereditary", year=2018)],
        header="Scary — house shortlist:",
        similar_anchor=MediaHit(media_type="movie", tmdb_id=900, title="Hereditary"),
        offer_more=True,
        offer_dismiss=True,
        title_chip="Date Night",
    )
    refine = [b for b in _buttons(rendered.reply) if not b["text"].startswith("Get ")]
    assert refine, "expected a refine row"
    assert "Date Night" in refine[0]["text"]
    assert store.get_callback_media(refine[0]["callback_data"])["title"] == "Date Night"


async def test_tapping_the_title_chip_runs_an_exact_search(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_mood_lane", True)
    overseerr = FakeOverseerr(results=[DATE_NIGHT], discover_results=SCARY_PICKS)
    bot = bot_factory(overseerr)

    vibe = await bot.handle_message(_message("Date Night"))
    assert vibe is not None
    assert overseerr.discover_calls, "the vibe lane should still answer first"
    chip = next(b for b in _buttons(vibe) if "Date Night" in b["text"] and "🎬" in b["text"])

    corrected = await bot.handle_callback(_callback(chip["callback_data"], message_id=1))

    assert corrected is not None
    assert corrected.edit_message_id == 1
    assert "Date Night" in corrected.text
    assert overseerr.search_calls == ["Date Night"]


# --- 4) Play is useful when it can be, honest when it cannot -------------------


async def test_play_on_tv_text_plays_the_card_on_screen(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)
    played: list[dict[str, Any]] = []

    async def _fake_play(**kwargs: Any) -> PlayOutcome:
        played.append(kwargs)
        return PlayOutcome(ok=True, message="Playing", path="infuse")

    monkeypatch.setattr("hearth.telegram.bot.play_on_tv", _fake_play)

    await bot.handle_message(_message("Dune"))
    reply = await bot.handle_message(_message("play it on the TV", message_id=2))

    assert reply is not None
    assert played and played[0]["title"] == "Dune"
    assert played[0]["tmdb_id"] == 438631
    assert reply.text == "Playing", "the play path's own outcome is the honest answer"


async def test_play_on_tv_text_is_honest_when_it_is_not_on_plex(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = dict(DUNE, mediaInfo={"status": 1})
    overseerr = FakeOverseerr(results=[missing])
    bot = bot_factory(overseerr)

    async def _never(**kwargs: Any) -> PlayOutcome:  # pragma: no cover - must not run
        raise AssertionError("play must not be attempted for a title that is not on Plex")

    monkeypatch.setattr("hearth.telegram.bot.play_on_tv", _never)

    await bot.handle_message(_message("Dune"))
    reply = await bot.handle_message(_message("play it on the TV", message_id=2))

    assert reply is not None
    assert "Plex" in reply.text
    assert "Get" in reply.text


async def test_play_on_tv_text_with_nothing_on_screen_says_so(bot_factory) -> None:
    bot = bot_factory(FakeOverseerr())

    reply = await bot.handle_message(_message("play it on the TV"))

    assert reply is not None
    assert "don't have a title in this thread" in reply.text


async def test_play_button_survives_an_expired_chat_context(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signed button outlives the context; the title must come with it."""
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    monkeypatch.setattr(settings, "telegram_status_truth", True)
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)
    played: list[dict[str, Any]] = []

    async def _fake_play(**kwargs: Any) -> PlayOutcome:
        played.append(kwargs)
        return PlayOutcome(ok=True, message="Playing", path="infuse")

    monkeypatch.setattr("hearth.telegram.bot.play_on_tv", _fake_play)

    shown = await bot.handle_message(_message("Dune"))
    assert shown is not None
    play_button = next(b for b in _buttons(shown) if "Play" in b["text"])

    # The card is still on screen, but the thread context has aged out.
    with speaker_scope(CHAT_ID, USER_ID):
        bot.memory.forget(CHAT_ID)

    reply = await bot.handle_callback(_callback(play_button["callback_data"]))

    assert reply is not None
    assert played, "Play must not be dropped just because the context expired"
    assert played[0]["title"] == "Dune"
    assert "TMDB" not in reply.text


async def test_a_single_on_plex_hit_keeps_its_play_button(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest "already on Plex" line used to drop the whole keyboard."""
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    monkeypatch.setattr(settings, "telegram_status_truth", True)
    bot = bot_factory(FakeOverseerr(results=[DUNE]))

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert "Plex" in reply.text
    buttons = _buttons(reply)
    assert [b for b in buttons if b["text"].startswith("Get ")] == []
    assert any("Play" in b["text"] for b in buttons)


async def test_play_button_without_a_known_title_refuses_instead_of_guessing(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)

    async def _never(**kwargs: Any) -> PlayOutcome:  # pragma: no cover - must not run
        raise AssertionError("play must not be attempted without a real title")

    monkeypatch.setattr("hearth.telegram.bot.play_on_tv", _never)

    shown = await bot.handle_message(_message("Dune"))
    assert shown is not None
    play_button = next(b for b in _buttons(shown) if "Play" in b["text"])

    # Group threads are scoped per speaker, so reach into memory the same way
    # the handler does rather than under the bare chat key.
    with speaker_scope(CHAT_ID, USER_ID):
        bot.memory.forget(CHAT_ID)
    bot.store.clear_callback_media(play_button["callback_data"])

    reply = await bot.handle_callback(_callback(play_button["callback_data"]))

    assert reply is not None
    assert "TMDB" not in reply.text
    assert "Search it again" in reply.text


# --- 5) A routed turn is never silent ------------------------------------------


async def test_an_unexpected_lane_failure_still_answers(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_factory(FakeOverseerr(results=[DUNE]))

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("overseerr client blew up in an unexpected way")

    monkeypatch.setattr(bot.catalog, "hits", _explode)

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert reply.text.strip()
    assert "nothing was queued" in reply.text


async def test_an_unexpected_refine_failure_still_edits_the_card(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tap is unambiguously addressed to the bot, so it must answer."""
    overseerr = FakeOverseerr(results=[DATE_NIGHT], discover_results=SCARY_PICKS)
    bot = bot_factory(overseerr)

    shown = await bot.handle_message(_message("Date Night"))
    assert shown is not None
    chip = next(b for b in _buttons(shown) if "🎬" in b["text"])

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("catalog client blew up in an unexpected way")

    monkeypatch.setattr(bot.catalog, "hits", _explode)

    reply = await bot.handle_callback(_callback(chip["callback_data"]))

    assert reply is not None
    assert reply.text.strip()
    assert reply.edit_message_id == 1
    assert "nothing was queued" in reply.text


async def test_a_rate_limited_ask_is_echoed_back(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 1)
    bot = bot_factory(FakeOverseerr(results=[DUNE]))

    await bot.handle_message(_message("Dune"))
    reply = await bot.handle_message(_message("Interstellar", message_id=2))

    assert reply is not None
    assert "Interstellar" in reply.text, "a dropped ask the user must retype is the sting"


# --- 6) Jev routes; the LLM only runs when there is nothing to go on -----------


async def test_an_unsure_jev_verdict_keeps_a_deterministic_franchise_seed(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    overseerr = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(overseerr)
    # Below the media_ask floor, and "needs prose" well above its threshold.
    set_client(FakeSystemOne(_media_payload("exact_title", conf=0.2, needs_llm=0.95)))

    reply = await bot.handle_message(_message("all Harry Potters"))

    assert reply is not None
    assert overseerr.search_calls, "the franchise seed must still hit Overseerr"
    assert "Harry Potter" in reply.text
    assert "configure OpenAI" not in reply.text


# --- 7) Plans, exclusions, names and "next" ------------------------------------


async def test_a_franchise_alias_in_a_plan_is_not_reported_as_a_miss(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"LOTR" can never fuzzy-match its own full title — the guard ate the item."""
    monkeypatch.setattr(settings, "telegram_batch_lane", True)
    overseerr = FakeOverseerr(
        results=[LOTR, HOBBIT],
        aliases={"LOTR": "The Lord of the Rings"},
    )
    bot = bot_factory(overseerr)

    reply = await bot.handle_message(_message("grab LOTR and Hobbit"))

    assert reply is not None
    assert "no catalog match" not in reply.text
    assert "Fellowship" in reply.text
    assert "Hobbit" in reply.text


def test_an_explicit_plan_beats_the_franchise_prefix_veto() -> None:
    """The veto protects mid-title "and"; a grab verb says it is two requests."""
    assert [p.title for p in split_compound_ask("grab Harry Potter and Dune")] == [
        "Harry Potter",
        "Dune",
    ]
    assert [p.title for p in split_compound_ask("grab LOTR and Hobbit")] == ["LOTR", "Hobbit"]
    # The lower-case article still holds the real title together.
    assert split_compound_ask("grab Harry Potter and the Chamber of Secrets") == ()
    assert split_compound_ask("grab Beauty and the Beast") == ()
    assert split_compound_ask("grab Romeo and Juliet") == ()


def test_a_numeric_exclusion_is_read_not_searched() -> None:
    from hearth.telegram.media.phrases import extract_exclusion

    assert extract_exclusion("all Harry Potters except the last 4") == ("all Harry Potters", 4, 0)
    assert extract_exclusion("all Harry Potters except the first 2") == ("all Harry Potters", 0, 2)


def test_an_exclusion_that_swallows_everything_returns_nothing() -> None:
    hits = [
        MediaHit(media_type="movie", tmdb_id=index, title=title, year=2000 + index)
        for index, title in enumerate(["One", "Two", "Three"], start=1)
    ]
    assert apply_exclusions(hits, drop_last=3) == []
    assert apply_exclusions(hits, drop_first=3) == []
    assert apply_exclusions(hits, drop_last=2, drop_first=2) == []
    # A real exclusion is untouched.
    assert [h.title for h in apply_exclusions(hits, drop_last=1)] == ["One", "Two"]


async def test_over_excluding_a_franchise_says_so_instead_of_showing_it_all(
    bot_factory,
) -> None:
    overseerr = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(overseerr)

    # The fixture holds two entries, so skipping two leaves nothing at all.
    reply = await bot.handle_message(_message("all Harry Potters except the last two"))

    assert reply is not None
    assert "leaves nothing" in reply.text
    assert [b for b in _buttons(reply) if b["text"].startswith("Get ")] == []


@pytest.mark.parametrize(
    ("ask", "name"),
    [
        ("movies with Robert de Niro", "Robert de Niro"),
        ("movies with Robert De Niro", "Robert De Niro"),
        ("anything with Olivia de Havilland", "Olivia de Havilland"),
        ("directed by Brian De Palma", "Brian De Palma"),
        ("anything with de Niro", "de Niro"),
        ("films van Carice van Houten", "Carice van Houten"),
    ],
)
def test_surname_particles_survive_the_person_lane(ask: str, name: str) -> None:
    found = detect_person_ask(ask)
    assert found is not None, ask
    assert found.name == name


def test_descriptions_are_still_not_people() -> None:
    for text in ("the movie with the spaceship", "iets met het meisje", "iets met de film"):
        assert detect_person_ask(text) is None, text


async def test_next_after_a_queue_continues_the_pack(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nudge names the next film; "next" must not page the old search."""
    monkeypatch.setattr(settings, "telegram_watch_next", True)
    overseerr = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(overseerr)

    await bot.handle_message(_message("Harry Potter"))
    # Group threads are scoped per speaker, so arm the watch-next the same way
    # a real queue would — inside this speaker's scope.
    with speaker_scope(CHAT_ID, USER_ID):
        bot.memory.remember_watch_next(
            CHAT_ID,
            media_type="movie",
            tmdb_id=672,
            title="Harry Potter and the Chamber of Secrets",
            year=2002,
            from_title="Harry Potter and the Philosopher's Stone",
            from_tmdb_id=671,
        )

    reply = await bot.handle_message(_message("next", message_id=2))

    assert reply is not None
    assert "Chamber of Secrets" in reply.text
    # And the offer is consumed, so a second "next" pages normally again.
    with speaker_scope(CHAT_ID, USER_ID):
        assert bot.memory.load(CHAT_ID).watch_next() is None


# --- 8) Every Telegram tool call is decided by the shared Jev gate -------------


def _gate_payload(
    *,
    domain: str = "media",
    domain_conf: float = 0.9,
    tool_allow: float = 0.9,
    is_cancel: float = 0.02,
    risk: float = 0.0,
    risk_conf: float = 0.9,
    media_ask: str = "exact_title",
) -> dict[str, Any]:
    """A Telegram media verdict that also carries the tool-gate signals."""
    payload = _media_payload(media_ask)
    payload["answers"].update(
        {
            "domain": {
                "type": "choice",
                "choice": domain,
                "confidence": domain_conf,
                "probabilities": {domain: domain_conf},
            },
            "tool_allow": {"type": "noul", "noul": tool_allow},
            "is_cancel": {"type": "noul", "noul": is_cancel},
            "risk": {
                "type": "score",
                "score": risk,
                "confidence": risk_conf,
                "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"},
            },
        }
    )
    return payload


def _enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "jev_tool_gate", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")


async def test_one_telegram_turn_asks_jev_once_for_routing_and_every_tool(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing and authorization share one System One call, not one per lookup."""
    _enforce(monkeypatch)
    overseerr = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(overseerr)
    fake = FakeSystemOne(_gate_payload(media_ask="series_all"))
    set_client(fake)

    reply = await bot.handle_message(_message("all Harry Potters"))

    assert reply is not None
    assert overseerr.search_calls, "the catalog read still happened"
    assert len(fake.calls) == 1, f"one turn must cost one typed call, not {len(fake.calls)}"


async def test_a_refused_turn_stops_before_the_catalog(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard stop applies to reads too, and the user is told rather than ignored."""
    _enforce(monkeypatch)
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)
    set_client(FakeSystemOne(_gate_payload(domain="refuse", domain_conf=0.97)))

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert reply.text.strip()
    assert overseerr.search_calls == [], "a refused turn must not reach Overseerr"


async def test_a_catalog_read_fails_open_when_the_gate_is_unreachable(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A house that cannot reach its gate behaves like a house without one."""
    _enforce(monkeypatch)
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)
    set_client(FakeSystemOne(error=RuntimeError("typesafe unreachable")))

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert overseerr.search_calls == ["Dune"]
    assert "Dune" in reply.text


async def test_shadow_mode_never_blocks_a_catalog_read(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "jev_shadow", True)
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)
    set_client(FakeSystemOne(_gate_payload(domain="refuse", domain_conf=0.99)))

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert overseerr.search_calls == ["Dune"], "shadow computes the deny but never acts on it"


async def test_a_typed_play_goes_through_the_write_gate(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed instruction is not a tap, so cancel language stops it."""
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)

    async def _never(**kwargs: Any) -> PlayOutcome:  # pragma: no cover - must not run
        raise AssertionError("a denied play must never reach the TV")

    monkeypatch.setattr("hearth.telegram.bot.play_on_tv", _never)

    set_client(FakeSystemOne(_gate_payload()))
    await bot.handle_message(_message("Dune"))

    set_client(FakeSystemOne(_gate_payload(is_cancel=0.95)))
    reply = await bot.handle_message(_message("play it on the TV", message_id=2))

    assert reply is not None
    assert reply.text.strip()


async def test_an_allowed_typed_play_still_reaches_the_tv(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    overseerr = FakeOverseerr(results=[DUNE])
    bot = bot_factory(overseerr)
    played: list[dict[str, Any]] = []

    async def _fake_play(**kwargs: Any) -> PlayOutcome:
        played.append(kwargs)
        return PlayOutcome(ok=True, message="Playing", path="infuse")

    monkeypatch.setattr("hearth.telegram.bot.play_on_tv", _fake_play)
    set_client(FakeSystemOne(_gate_payload()))

    await bot.handle_message(_message("Dune"))
    await bot.handle_message(_message("play it on the TV", message_id=2))

    assert played and played[0]["title"] == "Dune"


async def test_the_status_probe_is_authorized_like_any_other_read(
    bot_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    overseerr = FakeOverseerr(results=[DUNE])
    probes: list[int] = []

    async def _probe() -> dict[str, Any]:
        probes.append(1)
        return {"ok": True}

    overseerr.provider_probe = _probe  # type: ignore[attr-defined]
    bot = bot_factory(overseerr)
    set_client(FakeSystemOne(_gate_payload(domain="refuse", domain_conf=0.97)))

    reply = await bot.handle_message(_message("/status"))

    assert reply is not None
    assert probes == [], "a refused turn must not leave the house for a health check"


def test_catalog_reads_are_not_classified_as_writes() -> None:
    """Gating a read must never be able to turn a healthy catalog into a miss."""
    assert is_write_tool("overseerr_search") is False
    assert lane_for_tool("overseerr_search") == "media_status"
    assert lane_for_tool("plex_play") == "media_playback"
    assert is_write_tool("plex_play") is True


def test_the_telegram_surface_has_no_second_gate() -> None:
    """Every Jev entry point under telegram/ is the shared one."""
    root = Path(__file__).resolve().parents[1] / "hearth" / "telegram"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "system_one(" in text or "SystemOneClient(" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"telegram must not call System One directly: {offenders}"


def test_an_unresolvable_lane_falls_back_to_search_not_to_prose() -> None:
    # Jev can say "person" on a message with no name in it; a concrete title in
    # hand is worth one instant search rather than a round trip to gpt.
    from hearth.telegram.media.classify import _enrich
    from hearth.telegram.parse import parse_message_text

    parsed = parse_message_text("Dune")
    intent = _enrich(
        "person",
        "Dune",
        parsed=parsed,
        confidence=0.9,
        source="jev",
        needs_llm=True,
    )
    assert intent.kind == "exact_title"
    assert intent.search_title == "Dune"
    assert intent.needs_llm is False
    assert intent.note == "person_without_name"


def test_a_truly_title_less_lane_failure_still_reaches_the_guess_lane() -> None:
    from hearth.telegram.media.classify import _enrich

    intent = _enrich(
        "similar",
        "more of that sort of thing you know the one",
        parsed=None,
        confidence=0.9,
        source="jev",
        needs_llm=True,
    )
    assert intent.kind == "describe"
    assert intent.needs_llm is True
