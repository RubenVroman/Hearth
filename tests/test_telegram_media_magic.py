"""Magic Telegram media lanes: person, mood, like-X, batch, follow-ups.

Everything is mocked — TypeSafe, Overseerr and OpenAI are never reached. The
invariants under test are the product promises: exact titles stay on the fast
path, intent beats literal strings, a routed turn is never silent, confirming
queues by mediaId without a second title search, and nothing queues from chat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from hearth.config import settings
from hearth.jev import reset_client, set_client
from hearth.jev.schema import parse_answers
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.media import (
    classify_media_ask,
    classify_media_ask_sync,
    detect_follow_up,
    detect_mood,
    detect_person_ask,
    detect_similar_ask,
    split_compound_ask,
)
from hearth.telegram.media.moods import FAMILY, HORROR
from hearth.telegram.store import TelegramStore

CHAT_ID = -100999
USER_ID = 4242

DUNE = {
    "mediaType": "movie",
    "id": 438631,
    "title": "Dune",
    "releaseDate": "2021-10-22",
}
DUNE_TWO = {
    "mediaType": "movie",
    "id": 693134,
    "title": "Dune: Part Two",
    "releaseDate": "2024-02-27",
}
INCEPTION = {
    "mediaType": "movie",
    "id": 27205,
    "title": "Inception",
    "releaseDate": "2010-07-16",
}
INTERSTELLAR = {
    "mediaType": "movie",
    "id": 157336,
    "title": "Interstellar",
    "releaseDate": "2014-11-05",
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


def _callback(data: str, *, message_id: int = 1, callback_id: str = "cb-1") -> dict[str, Any]:
    return {
        "id": callback_id,
        "data": data,
        "from": {"id": USER_ID, "is_bot": False},
        "message": {"message_id": message_id, "chat": {"id": CHAT_ID, "type": "supergroup"}},
    }


class FakeOverseerr:
    """Records every call so tests can assert *which* route answered."""

    live = True

    def __init__(
        self,
        *,
        results: list[dict[str, Any]] | None = None,
        people: list[dict[str, Any]] | None = None,
        credits: dict[str, Any] | None = None,
        discover_results: list[dict[str, Any]] | None = None,
        neighbours: list[dict[str, Any]] | None = None,
        collection_parts: list[dict[str, Any]] | None = None,
        collection_id: int | None = None,
        request_result: dict[str, Any] | None = None,
    ) -> None:
        self.results = list(results or [])
        self.people = list(people or [])
        self.credits = dict(credits or {"cast": [], "crew": []})
        self.discover_results = list(discover_results or [])
        self.neighbour_results = list(neighbours or [])
        self.collection_parts = list(collection_parts or [])
        self.collection_id = collection_id
        self.request_result = dict(
            request_result
            or {"ok": True, "requestStatus": 2, "mediaStatus": 3, "requestId": 7}
        )
        self.search_calls: list[tuple[str, int]] = []
        self.request_calls: list[dict[str, Any]] = []
        self.discover_calls: list[dict[str, Any]] = []
        self.person_calls: list[str] = []
        self.credit_calls: list[int] = []
        self.neighbour_calls: list[tuple[int, str]] = []
        self.collection_calls: list[int] = []
        self.details_calls: list[tuple[int, str]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append((query, page))
        needle = " ".join(query.casefold().split())
        rows = [
            row
            for row in self.results
            if needle in str(row.get("title") or row.get("name") or "").casefold()
        ]
        # A real catalog miss returns nothing; it never substitutes a neighbour.
        return {"ok": True, "mode": "live", "results": rows}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        self.details_calls.append((int(media_id), media_type))
        for row in self.results + self.collection_parts:
            if int(row.get("id") or 0) == int(media_id) and row.get("mediaType") == media_type:
                payload: dict[str, Any] = {"ok": True, "media": dict(row)}
                if self.collection_id is not None:
                    payload["collectionId"] = self.collection_id
                    payload["collectionName"] = "Test Collection"
                return payload
        return {"ok": False}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(dict(kwargs))
        return dict(self.request_result)

    async def discover(self, **kwargs: Any) -> dict[str, Any]:
        self.discover_calls.append(dict(kwargs))
        return {"ok": True, "mode": "live", "results": list(self.discover_results)}

    async def search_person(self, query: str) -> dict[str, Any]:
        self.person_calls.append(query)
        return {"ok": True, "mode": "live", "results": list(self.people)}

    async def person_combined_credits(self, person_id: int) -> dict[str, Any]:
        self.credit_calls.append(int(person_id))
        return {"ok": True, "mode": "live", "id": int(person_id), **self.credits}

    async def neighbours(
        self,
        media_id: int,
        media_type: str,
        *,
        limit: int = 8,
        include_recommendations: bool = True,
    ) -> dict[str, Any]:
        self.neighbour_calls.append((int(media_id), media_type))
        return {"ok": True, "mode": "live", "results": list(self.neighbour_results)}

    async def collection(self, collection_id: int, *, limit: int = 12) -> dict[str, Any]:
        self.collection_calls.append(int(collection_id))
        return {
            "ok": True,
            "mode": "live",
            "collectionId": int(collection_id),
            "name": "Test Collection",
            "results": list(self.collection_parts),
        }


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


def _media_payload(
    choice: str,
    *,
    conf: float = 0.9,
    needs_llm: float = 0.05,
    multi_item: float = 0.1,
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
            "multi_item": {"type": "noul", "noul": multi_item},
            "wants_queue": {"type": "noul", "noul": 0.8},
            "is_confirm": {"type": "noul", "noul": 0.05},
            "is_cancel": {"type": "noul", "noul": 0.05},
            "risk": {"type": "score", "score": 1.0, "confidence": 0.8},
        },
    }


@pytest.fixture
def bot_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:magic-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "jev_enabled", False)
    stores: list[TelegramStore] = []

    def make(overseerr: Any) -> TelegramMediaBot:
        store = TelegramStore(tmp_path / f"magic-{len(stores)}.db")
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


def _get_rows(reply: Any) -> list[dict[str, str]]:
    keyboard = reply.reply_markup["inline_keyboard"] if reply.reply_markup else []
    return [row[0] for row in keyboard if row and row[0]["text"].startswith("Get ")]


def _action_rows(reply: Any) -> list[dict[str, str]]:
    keyboard = reply.reply_markup["inline_keyboard"] if reply.reply_markup else []
    return [
        button
        for row in keyboard
        for button in row
        if not button["text"].startswith("Get ")
    ]


# --- deterministic detectors ---------------------------------------------------


def test_mood_maps_scary_under_two_hours_to_horror_and_runtime() -> None:
    spec = detect_mood("something scary under 2 hours")
    assert spec is not None
    assert HORROR in spec.genre_ids
    assert spec.runtime_lte == 120
    assert spec.media_type == "movie"
    assert "under 2h" in spec.label


def test_mood_reads_kids_and_comfort_asks() -> None:
    kids = detect_mood("kids movie")
    assert kids is not None
    assert FAMILY in kids.genre_ids
    assert HORROR in kids.exclude_genre_ids

    comfort = detect_mood("something for friday night comfort")
    assert comfort is not None
    assert comfort.genre_ids


def test_mood_never_hijacks_a_plot_riddle_or_a_real_title() -> None:
    # "wizard" is a clue about one specific film, not a genre request.
    assert detect_mood("that movie with the glasses that became a wizard") is None
    assert detect_mood("Dune") is None
    assert detect_mood("Horror Express") is None


def test_person_ask_detection_covers_cast_and_director_phrasings() -> None:
    for text, name in (
        ("anything with Florence Pugh", "Florence Pugh"),
        ("movies with Tom Hanks", "Tom Hanks"),
        ("Tom Hanks filmography", "Tom Hanks"),
        ("filmography of Greta Gerwig", "Greta Gerwig"),
    ):
        ask = detect_person_ask(text)
        assert ask is not None, text
        assert ask.name == name
        assert ask.role == "cast"

    directed = detect_person_ask("something directed by Christopher Nolan")
    assert directed is not None
    assert directed.name == "Christopher Nolan"
    assert directed.role == "directing"


def test_person_ask_ignores_titles_and_descriptions() -> None:
    assert detect_person_ask("Harry Potter") is None
    assert detect_person_ask("the movie with the spaceship") is None
    assert detect_person_ask("Dune") is None


def test_similar_ask_reads_anchor_or_falls_back_to_context() -> None:
    anchored = detect_similar_ask("something like Arrival")
    assert anchored is not None
    assert anchored.anchor == "Arrival"

    contextual = detect_similar_ask("more like that")
    assert contextual is not None
    assert contextual.anchor == ""
    assert contextual.uses_context is True

    assert detect_similar_ask("Dune") is None


def test_follow_up_detection_covers_the_whole_thread_vocabulary() -> None:
    cases = {
        "the sequel": "sequel",
        "all of them": "all_of_them",
        "more like that": "more_like_that",
        "nah the other one": "other_one",
        "yeah that one": "that_one",
        "more": "more",
    }
    for text, kind in cases.items():
        ask = detect_follow_up(text)
        assert ask is not None, text
        assert ask.kind == kind, text

    ordinal = detect_follow_up("the second one")
    assert ordinal is not None
    assert (ordinal.kind, ordinal.ordinal) == ("ordinal", 2)


def test_compound_splits_two_titles_but_never_cuts_a_franchise_title() -> None:
    both = split_compound_ask("grab Inception and Interstellar")
    assert [part.title for part in both] == ["Inception", "Interstellar"]

    # "and" lives inside these titles — splitting would guarantee a miss.
    assert split_compound_ask("Harry Potter and the Chamber of Secrets") == ()
    assert split_compound_ask("Fast and Furious") == ()
    assert split_compound_ask("The Good, the Bad and the Ugly") == ()
    assert split_compound_ask("Beauty and the Beast") == ()


def test_compound_keeps_per_item_editions_and_folds_modifiers() -> None:
    parts = split_compound_ask("LOTR extended + Hobbit theatrical")
    assert [part.title for part in parts] == ["LOTR", "Hobbit"]
    assert [part.edition_key for part in parts] == ["extended", "theatrical"]

    # "all movies" describes the item before it, so this stays one ask.
    assert split_compound_ask("Harry Potter, all movies") == ()


def test_series_all_honours_except_the_last() -> None:
    intent = classify_media_ask_sync("all Harry Potters except the last")
    assert intent.kind == "series_all"
    assert intent.search_title == "Harry Potter"
    assert intent.drop_last == 1
    assert intent.needs_llm is False


def test_local_classifier_routes_every_new_lane_without_an_llm() -> None:
    expected = {
        "Dune": "exact_title",
        "Harry Potter": "known_franchise",
        "Harry Potter, all movies": "series_all",
        "Lord of the Rings extended edition": "edition",
        "anything with Florence Pugh": "person",
        "something scary under 2 hours": "mood",
        "something like Arrival": "similar",
        "grab Inception and Interstellar": "batch",
        "the sequel": "follow_up",
        "what should we watch?": "house_pick",
    }
    for text, kind in expected.items():
        intent = classify_media_ask_sync(text)
        assert intent.kind == kind, f"{text} → {intent.kind}"
        assert intent.needs_llm is False, text


# --- Jev routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("choice", "text", "kind"),
    [
        ("person_filmography", "anything with Florence Pugh", "person"),
        ("mood_vibe", "something scary under 2 hours", "mood"),
        ("similar_to", "something like Arrival", "similar"),
        ("batch_multi", "grab Inception and Interstellar", "batch"),
        ("follow_up", "the sequel", "follow_up"),
    ],
)
@pytest.mark.asyncio
async def test_jev_choice_selects_the_new_lanes(
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    text: str,
    kind: str,
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    fake = FakeSystemOne(_media_payload(choice))
    set_client(fake)

    intent = await classify_media_ask(text)
    assert intent.source == "jev"
    assert intent.kind == kind
    assert intent.needs_llm is False
    assert "multi_item" in (fake.calls[0]["questions"] or {})


@pytest.mark.asyncio
async def test_jev_lane_without_evidence_degrades_instead_of_dumb_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jev says "mood" for a plot riddle → the guess lane, not a genre dump."""
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    set_client(FakeSystemOne(_media_payload("mood_vibe")))

    intent = await classify_media_ask("that movie with the glasses that became a wizard")
    assert intent.kind == "describe"
    assert intent.needs_llm is True


# --- lane integration ----------------------------------------------------------


@pytest.mark.asyncio
async def test_person_lane_uses_credits_never_a_literal_title_search(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(
        people=[{"id": 6193, "mediaType": "person", "name": "Florence Pugh", "popularity": 40.0}],
        credits={
            "cast": [
                {**INCEPTION, "popularity": 90.0, "voteCount": 30000},
                {**INTERSTELLAR, "popularity": 80.0, "voteCount": 28000},
            ],
            "crew": [],
        },
    )
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("anything with Florence Pugh"))

    assert reply is not None
    assert fake.person_calls == ["Florence Pugh"]
    assert fake.credit_calls == [6193]
    assert fake.search_calls == []  # never search the sentence as a title
    assert "Florence Pugh" in reply.text
    assert "Inception" in reply.text
    assert len(_get_rows(reply)) == 2
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_unknown_person_says_so_instead_of_faking_a_miss(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(people=[])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("movies with Zzyzx Nobody"))

    assert reply is not None
    assert "Zzyzx Nobody" in reply.text
    assert "spelling" in reply.text.lower()
    assert fake.search_calls == []
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_mood_lane_discovers_by_genre_and_runtime_not_by_title(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(
        discover_results=[
            {
                "mediaType": "movie",
                "id": 138843,
                "title": "The Conjuring",
                "releaseDate": "2013-07-18",
            },
            {
                "mediaType": "movie",
                "id": 381288,
                "title": "Split",
                "releaseDate": "2016-11-15",
            },
        ]
    )
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("something scary under 2 hours"))

    assert reply is not None
    assert fake.search_calls == []
    assert len(fake.discover_calls) == 1
    call = fake.discover_calls[0]
    assert HORROR in call["genre_ids"]
    assert call["with_runtime_lte"] == 120
    assert call["primary_release_date_lte"]  # released titles only
    assert "Conjuring" in reply.text
    assert len(_get_rows(reply)) == 2
    assert any("More options" in button["text"] for button in _action_rows(reply))
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_vague_ask_becomes_house_picks_rather_than_a_shrug(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(discover_results=[DUNE, INCEPTION])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("what should we watch?"))

    assert reply is not None
    assert fake.discover_calls
    assert "Dune" in reply.text
    assert _get_rows(reply)
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_similar_lane_resolves_anchor_once_then_asks_for_neighbours(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    arrival = {
        "mediaType": "movie",
        "id": 329865,
        "title": "Arrival",
        "releaseDate": "2016-11-10",
    }
    fake = FakeOverseerr(results=[arrival], neighbours=[INTERSTELLAR, DUNE])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("something like Arrival"))

    assert reply is not None
    assert fake.search_calls == [("Arrival", 1)]
    assert fake.neighbour_calls == [(329865, "movie")]
    assert "Arrival" in reply.text
    assert "Interstellar" in reply.text
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_batch_ask_becomes_one_plan_with_a_button_per_title(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[INCEPTION, INTERSTELLAR])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("grab Inception and Interstellar"))

    assert reply is not None
    assert [query for query, _ in fake.search_calls] == ["Inception", "Interstellar"]
    assert "Inception" in reply.text
    assert "Interstellar" in reply.text
    get_rows = _get_rows(reply)
    assert len(get_rows) == 2
    assert fake.request_calls == []  # a plan is still not a queue


@pytest.mark.asyncio
async def test_batch_reports_the_item_it_could_not_find(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[INCEPTION])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("grab Inception and Zzyzxynthia"))

    assert reply is not None
    assert "Inception" in reply.text
    assert "Zzyzxynthia" in reply.text
    assert "no catalog match" in reply.text
    assert len(_get_rows(reply)) == 1


@pytest.mark.asyncio
async def test_series_all_except_the_last_drops_the_newest_entry(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("all Harry Potters except the last"))

    assert reply is not None
    assert fake.search_calls and fake.search_calls[0][0] == "Harry Potter"
    assert "Philosopher" in reply.text
    assert "Chamber" in reply.text
    assert "Prisoner of Azkaban" not in reply.text  # newest entry skipped
    assert len(_get_rows(reply)) == 2
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_franchise_prefers_the_real_tmdb_collection_over_fuzzy_titles(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(
        results=HARRY_POTTER[:1],
        collection_id=1241,
        collection_parts=HARRY_POTTER,
    )
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("Harry Potter, all movies"))

    assert reply is not None
    assert fake.collection_calls == [1241]
    assert "Prisoner of Azkaban" in reply.text
    assert len(_get_rows(reply)) == 3


@pytest.mark.asyncio
async def test_already_available_answers_in_one_clear_line_with_no_get_button(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[{**DUNE, "mediaStatus": 5}])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert "Plex" in reply.text or "library" in reply.text.lower()
    assert reply.reply_markup is None
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_already_requested_says_so_instead_of_offering_a_duplicate(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[{**DUNE, "mediaStatus": 3}])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert "already" in reply.text.lower()
    assert reply.reply_markup is None
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_exact_title_miss_retries_once_before_declaring_a_miss(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    """"Dune: Part Two" must not fail just because the catalog spells it plainly."""
    fake = FakeOverseerr(results=[DUNE])
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("Screamadelica: Nonexistent Cut"))

    assert reply is not None
    assert len(fake.search_calls) == 2  # full string, then broadened head
    assert fake.search_calls[1][0] == "Screamadelica"
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_routed_media_turn_is_never_silent(
    bot_factory: Callable[..., TelegramMediaBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeOverseerr(results=[DUNE])
    bot = bot_factory(fake)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test")
    set_client(FakeSystemOne(_media_payload("not_media")))

    reply = await bot.handle_message(_message("hmm what about the thing we discussed"))

    assert reply is not None
    assert reply.text
    assert fake.request_calls == []


# --- in-thread follow-ups ------------------------------------------------------


@pytest.mark.asyncio
async def test_all_of_them_expands_the_franchise_of_the_last_card(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(fake)

    first = await bot.handle_message(_message("Harry Potter and the Chamber of Secrets"))
    assert first is not None
    fake.search_calls.clear()

    follow = await bot.handle_message(_message("all of them", message_id=2))

    assert follow is not None
    assert fake.search_calls  # franchise seed, not the literal "all of them"
    assert "all of them" not in {query.casefold() for query, _ in fake.search_calls}
    assert "Philosopher" in follow.text
    assert len(_get_rows(follow)) >= 2
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_the_sequel_resolves_to_the_next_entry_by_year(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[DUNE, DUNE_TWO])
    bot = bot_factory(fake)

    first = await bot.handle_message(_message("Dune (2021)"))
    assert first is not None

    follow = await bot.handle_message(_message("the sequel", message_id=2))

    assert follow is not None
    assert "Dune: Part Two" in follow.text
    assert len(_get_rows(follow)) == 1
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_more_like_that_uses_the_remembered_top_hit(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[INCEPTION], neighbours=[INTERSTELLAR, DUNE])
    bot = bot_factory(fake)

    assert await bot.handle_message(_message("Inception")) is not None
    follow = await bot.handle_message(_message("more like that", message_id=2))

    assert follow is not None
    assert fake.neighbour_calls == [(27205, "movie")]
    assert "Interstellar" in follow.text
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_ordinal_follow_up_arms_yes_for_that_card_only(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(fake)

    listed = await bot.handle_message(_message("Harry Potter"))
    assert listed is not None
    ordered = [line for line in listed.text.splitlines() if line.startswith("2. ")]
    assert ordered and "Chamber" in ordered[0]

    picked = await bot.handle_message(_message("the second one", message_id=2))
    assert picked is not None
    assert "Chamber" in picked.text
    assert fake.request_calls == []

    confirmed = await bot.handle_message(_message("yes", message_id=3))
    assert confirmed is not None
    assert len(fake.request_calls) == 1
    assert fake.request_calls[0]["media_id"] == 672


@pytest.mark.asyncio
async def test_nah_the_other_one_offers_the_runner_up(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=HARRY_POTTER)
    bot = bot_factory(fake)

    assert await bot.handle_message(_message("Harry Potter")) is not None
    other = await bot.handle_message(_message("nah the other one", message_id=2))

    assert other is not None
    assert "Chamber" in other.text
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_more_pages_the_mood_lane_without_repeating_titles(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(discover_results=[INCEPTION, INTERSTELLAR])
    bot = bot_factory(fake)

    assert await bot.handle_message(_message("something scary under 2 hours")) is not None
    fake.discover_results = [DUNE]

    more = await bot.handle_message(_message("more", message_id=2))

    assert more is not None
    assert len(fake.discover_calls) == 2
    second = fake.discover_calls[1]
    assert second["page"] == 2
    assert sorted(second["exclude_tmdb_ids"]) == [27205, 157336]
    assert "Dune" in more.text


@pytest.mark.asyncio
async def test_follow_up_without_context_falls_back_to_a_title_search(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    """"Next" is also a real film — with no thread to resolve, search it."""
    fake = FakeOverseerr(
        results=[{"mediaType": "movie", "id": 1738, "title": "Next", "releaseDate": "2007-04-27"}]
    )
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("next"))

    assert reply is not None
    assert fake.search_calls == [("next", 1)]
    assert "Next" in reply.text


# --- confirm / queue boundary --------------------------------------------------


@pytest.mark.asyncio
async def test_yes_queues_by_media_id_without_a_second_title_search(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[DUNE])
    bot = bot_factory(fake)

    offered = await bot.handle_message(_message("Dune"))
    assert offered is not None
    searches_before = len(fake.search_calls)

    confirmed = await bot.handle_message(_message("yes", message_id=2))

    assert confirmed is not None
    assert len(fake.search_calls) == searches_before  # no re-search on confirm
    assert len(fake.request_calls) == 1
    assert fake.request_calls[0]["media_id"] == 438631
    assert fake.request_calls[0]["media_type"] == "movie"


@pytest.mark.asyncio
async def test_get_button_queues_by_media_id_without_searching(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[INCEPTION])
    bot = bot_factory(fake)

    offered = await bot.handle_message(_message("Inception"))
    assert offered is not None
    button = _get_rows(offered)[0]
    fake.search_calls.clear()

    reply = await bot.handle_callback(_callback(button["callback_data"]))

    assert reply is not None
    assert fake.search_calls == []
    assert len(fake.request_calls) == 1
    assert fake.request_calls[0]["media_id"] == 27205


@pytest.mark.asyncio
async def test_more_like_this_button_never_queues(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[INCEPTION], neighbours=[INTERSTELLAR])
    bot = bot_factory(fake)

    offered = await bot.handle_message(_message("Inception"))
    assert offered is not None
    similar = next(
        button for button in _action_rows(offered) if "More like this" in button["text"]
    )

    reply = await bot.handle_callback(_callback(similar["callback_data"]))

    assert reply is not None
    assert fake.neighbour_calls == [(27205, "movie")]
    assert fake.request_calls == []
    assert "Interstellar" in reply.text


@pytest.mark.asyncio
async def test_nah_button_clears_the_offer_and_says_so(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[INCEPTION])
    bot = bot_factory(fake)

    offered = await bot.handle_message(_message("Inception"))
    assert offered is not None
    nah = next(button for button in _action_rows(offered) if "Nah" in button["text"])

    reply = await bot.handle_callback(_callback(nah["callback_data"]))

    assert reply is not None
    assert "not queueing" in reply.text.lower()
    assert fake.request_calls == []

    # The dismissed offer must not be revivable by a bare yes.
    assert await bot.handle_message(_message("yes", message_id=2)) is None
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_refine_buttons_are_chat_bound_and_reject_a_foreign_chat(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    fake = FakeOverseerr(results=[INCEPTION], neighbours=[INTERSTELLAR])
    bot = bot_factory(fake)

    offered = await bot.handle_message(_message("Inception"))
    assert offered is not None
    similar = next(
        button for button in _action_rows(offered) if "More like this" in button["text"]
    )
    forged = _callback(similar["callback_data"])
    forged["message"]["chat"]["id"] = CHAT_ID  # authorised chat, tampered payload
    forged["data"] = similar["callback_data"][:-4] + "AAAA"

    reply = await bot.handle_callback(forged)

    assert reply is not None
    assert "invalid or expired" in reply.text
    assert fake.neighbour_calls == []
    assert fake.request_calls == []


@pytest.mark.asyncio
async def test_backend_failure_reads_as_a_backend_error_not_a_catalog_miss(
    bot_factory: Callable[..., TelegramMediaBot],
) -> None:
    from hearth.tools.arr import OverseerrError

    class BrokenOverseerr(FakeOverseerr):
        async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
            raise OverseerrError("boom", operation="search")

    fake = BrokenOverseerr()
    bot = bot_factory(fake)

    reply = await bot.handle_message(_message("Dune"))

    assert reply is not None
    assert "not a catalog miss" in reply.text
    assert fake.request_calls == []
