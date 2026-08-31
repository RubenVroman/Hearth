"""Telegram drop-group inbox — parser, safeguards, grab wiring."""

from __future__ import annotations

import pytest

from hearth.config import settings
from hearth.fixtures import pipeline
from hearth.telegram.inbox import TelegramInbox
from hearth.telegram.parse import (
    CATALOG_HOSTS,
    parse_message,
    parse_message_text,
)
from hearth.telegram.safeguards import Deduper, RateLimiter, chat_allowed, user_allowed
from hearth.telegram.service import TelegramInboxService


def _msg(
    text: str,
    *,
    chat_id: int = -1001,
    message_id: int = 1,
    user_id: int = 42,
    is_bot: bool = False,
    extra: dict | None = None,
) -> dict:
    body = {
        "message_id": message_id,
        "text": text,
        "chat": {"id": chat_id, "type": "group"},
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "Ruben"},
    }
    if extra:
        body.update(extra)
    return body


# --- parser -----------------------------------------------------------------


def test_parser_catalog_links():
    imdb = parse_message_text("https://www.imdb.com/title/tt2798920/")
    assert imdb.kind == "request"
    assert imdb.imdb_id == "tt2798920"

    tmdb = parse_message_text("https://www.themoviedb.org/movie/300668-annihilation")
    assert tmdb.kind == "request"
    assert tmdb.tmdb_id == 300668
    assert tmdb.media_kind == "movie"

    tv = parse_message_text("https://www.themoviedb.org/tv/95396-severance")
    assert tv.kind == "request"
    assert tv.tmdb_id == 95396
    assert tv.media_kind == "tv"

    trakt = parse_message_text("https://trakt.tv/movies/dune-part-two-2024")
    assert trakt.kind == "request"
    assert trakt.media_kind == "movie"
    assert "Dune" in trakt.title
    assert trakt.year == 2024


def test_parser_plain_ids_and_titles():
    assert parse_message_text("tt2798920").imdb_id == "tt2798920"
    assert parse_message_text("tmdb:300668").tmdb_id == 300668
    assert parse_message_text("tvdb:371980").tvdb_id == 371980

    titled = parse_message_text("Annihilation (2018)")
    assert titled.kind == "request"
    assert titled.title == "Annihilation"
    assert titled.year == 2018

    show = parse_message_text("Severance S02E03")
    assert show.media_kind == "tv"
    assert show.title == "Severance"
    assert show.season == 2
    assert show.episode == 3

    season = parse_message_text("Severance season 2")
    assert season.media_kind == "tv"
    assert season.season == 2

    quality = parse_message_text("Annihilation (2018) 1080p")
    assert quality.quality == "1080P"
    assert quality.title == "Annihilation"


def test_parser_ignores_chatter_and_status():
    for text in ("ok", "thanks", "hi", "👍", "yeah"):
        assert parse_message_text(text).kind == "ignore"
    assert parse_message_text("Queued Annihilation (2018) via Radarr.").kind == "ignore"
    assert parse_message_text("Annihilation is downloading, ~42%.").kind == "ignore"
    sticker = parse_message_text("", has_media_file=True, media_kind_hint="sticker")
    assert sticker.kind == "ignore"
    voice = parse_message_text("", has_media_file=True, media_kind_hint="voice")
    assert voice.kind == "ignore"


def test_parser_rejects_magnets_torrents_and_raw_files():
    magnet = parse_message_text("magnet:?xt=urn:btih:abcdef")
    assert magnet.kind == "reject_download"
    torrent = parse_message_text("please get film.torrent")
    assert torrent.kind == "reject_download"
    doc = parse_message_text("", has_media_file=True, media_kind_hint="document")
    assert doc.kind == "reject_download"


def test_parser_ignores_non_catalog_urls():
    assert "example.com" not in {h.replace("www.", "") for h in CATALOG_HOSTS}
    junk = parse_message_text("https://example.com/not-a-movie")
    assert junk.kind == "ignore"
    short = parse_message_text("https://bit.ly/abc123")
    assert short.kind == "ignore"


def test_parser_disambiguation_pick_and_loop_prevention():
    pick = parse_message_text("2")
    assert pick.kind == "disambiguation_pick"
    assert pick.pick_index == 2

    view, parsed = parse_message(_msg("Annihilation", user_id=99), bot_user_id=99)
    assert view is not None
    assert parsed.kind == "ignore"
    assert parsed.reason == "own_bot"


def test_parser_rejects_huge_or_binary_bodies():
    huge = parse_message_text("A" * 5000, max_length=200)
    assert huge.kind == "ignore"
    binary = parse_message_text("title\x00secret")
    assert binary.kind == "ignore"


# --- safeguards -------------------------------------------------------------


def test_allowlist_chat_and_user():
    assert chat_allowed(-1001, [-1001, -1002])
    assert not chat_allowed(-999, [-1001])
    assert not chat_allowed(-1001, [])

    assert user_allowed(1, [], bot_user_id=9)
    assert not user_allowed(9, [], bot_user_id=9)
    assert user_allowed(1, [1, 2], bot_user_id=9)
    assert not user_allowed(3, [1, 2], bot_user_id=9)


def test_dedup_message_and_title_window():
    deduper = Deduper(window_s=60)
    assert deduper.seen_message(-1, 10) is False
    assert deduper.seen_message(-1, 10) is True
    assert deduper.seen_title(-1, "title:annihilation:2018:movie") is False
    assert deduper.seen_title(-1, "title:annihilation:2018:movie") is True
    assert deduper.seen_title(-1, "title:other:2020:movie") is False


def test_rate_limiter():
    rate = RateLimiter(max_calls=2, window_s=60)
    assert rate.allow()
    assert rate.allow()
    assert not rate.allow()


# --- service / env off ------------------------------------------------------


def test_telegram_off_without_env(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_ids", "")
    assert settings.telegram_configured is False
    service = TelegramInboxService()
    assert service.enabled is False
    snap = service.status_snapshot()
    assert snap["configured"] is False
    assert snap["running"] is False


def test_telegram_configured_requires_token_and_chat(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:ABC")
    monkeypatch.setattr(settings, "telegram_chat_ids", "")
    assert settings.telegram_configured is False
    monkeypatch.setattr(settings, "telegram_chat_ids", "-1001, -1002")
    assert settings.telegram_configured is True
    assert settings.telegram_chat_id_list == [-1001, -1002]


@pytest.mark.asyncio
async def test_poll_start_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_ids", "")
    service = TelegramInboxService()
    await service.start()
    assert service.running is False
    await service.stop()


# --- inbox grab path (reuses *arr fixtures) ---------------------------------


@pytest.fixture
def inbox(monkeypatch, tmp_path) -> TelegramInbox:
    monkeypatch.setattr(settings, "telegram_bot_token", "1:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", "-1001")
    monkeypatch.setattr(settings, "telegram_user_ids", "")
    monkeypatch.setattr(settings, "telegram_prefer_overseerr", True)
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 20)
    monkeypatch.setattr(settings, "overseerr_api_key", "")  # mock Overseerr path
    pipeline.radarr_queue.clear()
    pipeline.sonarr_queue.clear()
    pipeline.overseerr_queue.clear()
    from hearth.telegram.memory import ChatMemory

    box = TelegramInbox(memory=ChatMemory(tmp_path / "telegram-chat-memory.json"))
    box.bot_user_id = 7
    box.rate.max_calls = 20
    return box


@pytest.mark.asyncio
async def test_inbox_ignores_outside_allowlist(inbox: TelegramInbox):
    result = await inbox.handle_message(_msg("Annihilation (2018)", chat_id=-9999))
    assert result.handled is False
    assert result.reply == ""


@pytest.mark.asyncio
async def test_inbox_queues_movie_title(inbox: TelegramInbox):
    result = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=11))
    assert result.grabbed is True
    assert "Queued The Brutalist" in result.reply
    assert "Overseerr" in result.reply
    assert pipeline.overseerr_queue


@pytest.mark.asyncio
async def test_inbox_queues_tmdb_link(inbox: TelegramInbox):
    result = await inbox.handle_message(
        _msg("https://www.themoviedb.org/movie/974950-the-brutalist", message_id=12)
    )
    assert result.grabbed is True
    assert "Brutalist" in result.reply


@pytest.mark.asyncio
async def test_inbox_queues_show(inbox: TelegramInbox, monkeypatch):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Slow Horses",
            "media_kind": "tv",
            "year": 2022,
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(_msg("Slow Horses season 2", message_id=13))
    assert result.grabbed is True
    assert "Overseerr" in result.reply
    assert pipeline.overseerr_queue
    assert any(
        (row.get("mediaType") or "") == "tv" for row in pipeline.overseerr_queue
    )


@pytest.mark.asyncio
async def test_inbox_dedup_same_message_and_title(inbox: TelegramInbox):
    first = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=20))
    assert first.grabbed is True
    again = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=20))
    assert again.grabbed is False
    assert again.reply == ""
    other = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=21))
    assert other.grabbed is False
    assert other.reply == ""  # title dedup window


@pytest.mark.asyncio
async def test_inbox_rejects_magnet_with_house_reply(inbox: TelegramInbox):
    result = await inbox.handle_message(_msg("magnet:?xt=urn:btih:deadbeef", message_id=22))
    assert result.grabbed is False
    assert "*arr" in result.reply or "Overseerr" in result.reply


@pytest.mark.asyncio
async def test_inbox_ignores_bot_outbound_loop(inbox: TelegramInbox):
    inbox.remember_outbound(-1001, 50)
    # Even if text looks like a title, outbound ids are ignored — use bot sender.
    result = await inbox.handle_message(
        _msg("Queued Annihilation (2018) via Radarr.", message_id=50, user_id=7, is_bot=True)
    )
    assert result.grabbed is False
    assert result.reply == ""


@pytest.mark.asyncio
async def test_inbox_ambiguous_then_pick(inbox: TelegramInbox, monkeypatch):
    # Force multiple fuzzy hits without an exact title match.
    async def multi(_query: str):
        return {
            "mode": "mock",
            "service": "overseerr",
            "results": [
                {"title": "Heat", "year": 1995, "tmdbId": 1, "mediaId": 1, "mediaType": "movie"},
                {"title": "Heat", "year": 1986, "tmdbId": 2, "mediaId": 2, "mediaType": "movie"},
                {"title": "Heat", "year": 2023, "tmdbId": 3, "mediaId": 3, "mediaType": "movie"},
            ],
        }

    monkeypatch.setattr("hearth.telegram.inbox.overseerr.search", multi)
    monkeypatch.setattr("hearth.telegram.catalog.overseerr.search", multi)
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Heat",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )

    ask = await inbox.handle_message(_msg("Heat", message_id=30))
    assert ask.grabbed is False
    assert "Which one" in ask.reply
    assert "1." in ask.reply

    pick = await inbox.handle_message(_msg("2", message_id=31))
    assert pick.grabbed is True
    assert "1986" in pick.reply or "Heat" in pick.reply


@pytest.mark.asyncio
async def test_inbox_already_in_download_queue(inbox: TelegramInbox):
    # Annihilation is in MOCK_RADARR_DOWNLOADS — say so instead of double-queueing.
    result = await inbox.handle_message(_msg("Annihilation (2018)", message_id=39))
    assert result.grabbed is False
    assert "already" in result.reply.lower()


@pytest.mark.asyncio
async def test_inbox_already_queued_says_so(inbox: TelegramInbox):
    await inbox.handle_message(_msg("The Endless (2017)", message_id=40))
    # New message id but clear title dedup so we exercise already-queued path.
    inbox.deduper.reset()
    again = await inbox.handle_message(_msg("The Endless (2017)", message_id=41))
    assert again.grabbed is False
    assert "already" in again.reply.lower()


@pytest.mark.asyncio
async def test_inbox_user_allowlist(inbox: TelegramInbox, monkeypatch):
    monkeypatch.setattr(settings, "telegram_user_ids", "42")
    ok = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=60, user_id=42))
    assert ok.grabbed is True
    inbox.deduper.reset()
    pipeline.overseerr_queue.clear()
    denied = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=61, user_id=99))
    assert denied.grabbed is False
    assert denied.reply == ""


@pytest.mark.asyncio
async def test_inbox_not_found(inbox: TelegramInbox, monkeypatch):
    """Model-named title with empty catalog → confirm, not 'send a link'."""
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "ZzzNotARealFilm999",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    result = await inbox.handle_message(_msg("ZzzNotARealFilm999", message_id=70))
    assert result.grabbed is False
    assert "Couldn't find" not in result.reply
    assert "IMDb" not in result.reply
    assert "Did you mean" in result.reply
    assert "ZzzNotARealFilm999" in result.reply


@pytest.mark.asyncio
async def test_inbox_instant_title_year_still_not_found(inbox: TelegramInbox):
    """Explicit Title (YYYY) instant path may still 404 when catalog misses."""
    result = await inbox.handle_message(
        _msg("ZzzNotARealFilm999 (2099)", message_id=71)
    )
    assert result.grabbed is False
    assert "Couldn't find" in result.reply


# --- intent / follow-ups ----------------------------------------------------


def test_instant_pick_all_of_them_and_de_eerste():
    from hearth.telegram.intent import instant_pick_decision

    candidates = [
        {"title": "Harry Potter and the Sorcerer's Stone", "year": 2001, "tmdbId": 671},
        {"title": "Harry Potter and the Chamber of Secrets", "year": 2002, "tmdbId": 672},
        {"title": "Harry Potter and the Prisoner of Azkaban", "year": 2004, "tmdbId": 673},
    ]
    all_of = instant_pick_decision("all of them", candidates)
    assert all_of is not None
    assert all_of.action == "pick_many"
    assert all_of.indices == [1, 2, 3]
    assert all_of.source == "instant"

    first = instant_pick_decision("de eerste", candidates)
    assert first is not None
    assert first.action == "pick"
    assert first.indices == [1]

    # Without a live list these are not instant.
    assert instant_pick_decision("all of them", None) is None
    assert instant_pick_decision("de eerste", []) is None
    # "the new one" is conversational — model path, not instant.
    assert instant_pick_decision("the new one", candidates) is None


def test_telegram_intent_model_prefers_gpt4o_over_mini(monkeypatch):
    from hearth.config import settings as _settings
    from hearth.telegram.intent import TELEGRAM_INTENT_MODEL, telegram_intent_model

    monkeypatch.setattr(_settings, "openai_model", "gpt-4o-mini")
    assert telegram_intent_model() == TELEGRAM_INTENT_MODEL
    assert telegram_intent_model() == "gpt-4o"
    monkeypatch.setattr(_settings, "openai_model", "gpt-4o")
    assert telegram_intent_model() == "gpt-4o"
    monkeypatch.setattr(_settings, "openai_model", "gpt-4.1")
    assert telegram_intent_model() == "gpt-4.1"


def test_explicit_title_year_instant_helper():
    from hearth.telegram.intent import is_explicit_title_year

    assert is_explicit_title_year("Annihilation (2018)")
    assert is_explicit_title_year("Annihilation (2018) 1080p")
    assert not is_explicit_title_year("Annihilation")
    assert not is_explicit_title_year(
        "Die film waar iemand een puzzel oplost door een spiegel"
    )


@pytest.mark.asyncio
async def test_inbox_harry_potter_then_all_of_them(inbox: TelegramInbox, monkeypatch):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    ask = await inbox.handle_message(_msg("Harry Potter", message_id=100))
    assert ask.grabbed is False
    assert "Which one" in ask.reply
    assert "all of them" in ask.reply.lower()
    assert inbox.pending.get(-1001) is not None
    assert len(inbox.pending[-1001].options) >= 3

    all_of = await inbox.handle_message(_msg("all of them", message_id=101))
    assert all_of.grabbed is True
    assert "Queued" in all_of.reply
    assert len(pipeline.overseerr_queue) >= 3
    assert all(
        "Harry Potter" in str(row.get("title") or "") for row in pipeline.overseerr_queue
    )


@pytest.mark.asyncio
async def test_inbox_followup_de_eerste_instant(inbox: TelegramInbox, monkeypatch):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    await inbox.handle_message(_msg("Harry Potter", message_id=110))
    pick = await inbox.handle_message(_msg("de eerste", message_id=111))
    assert pick.grabbed is True
    assert "2001" in pick.reply or "Sorcerer" in pick.reply or "Harry Potter" in pick.reply
    assert len(pipeline.overseerr_queue) == 1


@pytest.mark.asyncio
async def test_inbox_whole_series_via_model(inbox: TelegramInbox, monkeypatch):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "select_all": True,
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(
        _msg("the whole Harry Potter series", message_id=120)
    )
    assert result.grabbed is True
    assert len(pipeline.overseerr_queue) >= 3


@pytest.mark.asyncio
async def test_inbox_ambiguous_yes_clarifies(inbox: TelegramInbox, monkeypatch):
    _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "Harry Potter",
                "media_kind": "movie",
                "confidence": 0.95,
            },
            {
                "action": "clarify",
                "clarify_question": "Which Harry Potter — reply 1–3, or say all of them?",
                "confidence": 0.7,
            },
        ],
    )
    await inbox.handle_message(_msg("Harry Potter", message_id=130))
    yes = await inbox.handle_message(_msg("yes", message_id=131))
    assert yes.grabbed is False
    assert "Which" in yes.reply or "all of them" in yes.reply.lower()


@pytest.mark.asyncio
async def test_inbox_numeric_pick_still_works_with_franchise(
    inbox: TelegramInbox, monkeypatch
):
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    ask = await inbox.handle_message(_msg("Harry Potter", message_id=140))
    assert "1." in ask.reply
    before = len(calls)
    pick = await inbox.handle_message(_msg("2", message_id=141))
    assert pick.grabbed is True
    assert "Chamber" in pick.reply or "2002" in pick.reply or "Harry Potter" in pick.reply
    assert len(calls) == before  # bare "2" is instant — no second OpenAI hop


# --- conversation / plot-to-title (always AI) ---------------------------------


def test_looks_like_descriptive_ask_compat_shim():
    """Deprecated helper may remain, but is not used as a gate."""
    from hearth.telegram.intent import looks_like_descriptive_ask

    assert looks_like_descriptive_ask(
        "a movie about a boy with glasses who is a wizard"
    )
    assert looks_like_descriptive_ask(
        "Die film waar iemand een puzzel oplost door een spiegel"
    )


def _patch_openai_intent(monkeypatch, payload: dict | list[dict]):
    """Stub AsyncOpenAI chat.completions.create with a fixed JSON payload (or queue)."""
    import json as _json

    from hearth.config import settings as _settings

    monkeypatch.setattr(_settings, "openai_api_key", "sk-test-not-a-real-key")
    # House default stays mini; Telegram intent hop must still use gpt-4o.
    monkeypatch.setattr(_settings, "openai_model", "gpt-4o-mini")

    queue = list(payload) if isinstance(payload, list) else [payload]
    calls: list[dict] = []

    class _Completions:
        @staticmethod
        async def create(**kwargs):
            calls.append(kwargs)
            body = queue.pop(0) if queue else {"action": "clarify", "clarify_question": "Which one?"}

            class _Msg:
                content = _json.dumps(body)

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, *args, **kwargs):
            self.chat = _Chat()

    monkeypatch.setattr("openai.AsyncOpenAI", _Client)
    return calls


def _imitation_and_davinci_overseerr(monkeypatch) -> list[str]:
    """Overseerr stub: Imitation Game 1–2 + Da Vinci Code single hit."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    imitation = [
        {
            "title": "The Imitation Game",
            "year": 2014,
            "tmdbId": 205596,
            "mediaId": 205596,
            "mediaType": "movie",
        },
        {
            "title": "The Imitation Game",
            "year": 1980,
            "tmdbId": 999001,
            "mediaId": 999001,
            "mediaType": "movie",
        },
    ]
    davinci = [
        {
            "title": "The Da Vinci Code",
            "year": 2006,
            "tmdbId": 591,
            "mediaId": 591,
            "mediaType": "movie",
        }
    ]
    queries: list[str] = []
    original = inbox_mod.overseerr.search

    async def _capture(query: str, *args, **kwargs):
        queries.append(str(query))
        q = (query or "").lower()
        if "imitation" in q:
            return {"mode": "mock", "service": "overseerr", "results": list(imitation)}
        if "da vinci" in q or "davinci" in q:
            return {"mode": "mock", "service": "overseerr", "results": list(davinci)}
        if q.isdigit() or q.startswith("tmdb:"):
            try:
                want = int(q.split(":")[-1])
            except ValueError:
                want = None
            rows = [r for r in imitation + davinci if r.get("tmdbId") == want]
            if rows:
                return {"mode": "mock", "service": "overseerr", "results": rows}
        return await original(query, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.overseerr, "search", _capture)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _capture)
    return queries


# Back-compat alias for older test names in this file.
_imitation_and_davinci_radarr = _imitation_and_davinci_overseerr


def _wrap_overseerr_search(monkeypatch) -> list[str]:
    """Capture Overseerr search queries so tests can forbid raw Dutch plot strings."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    queries: list[str] = []
    original = inbox_mod.overseerr.search

    async def _capture(query: str, *args, **kwargs):
        queries.append(str(query))
        return await original(query, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.overseerr, "search", _capture)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _capture)
    return queries


_wrap_radarr_search = _wrap_overseerr_search


@pytest.mark.asyncio
async def test_interpret_descriptive_resolves_harry_potter(monkeypatch):
    from hearth.telegram.intent import interpret_intent

    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.93,
        },
    )
    decision = await interpret_intent(
        "a movie about a boy with glasses who is a wizard"
    )
    assert decision.action == "search"
    assert decision.search_title == "Harry Potter"
    assert decision.media_kind == "movie"
    assert decision.source == "openai"
    assert calls
    assert calls[0]["model"] == "gpt-4o"
    user_content = calls[0]["messages"][1]["content"]
    assert "sk-test" not in user_content
    assert "sk-test" not in calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_interpret_bare_title_still_calls_model(monkeypatch):
    """Bare title without year is conversational — always AI."""
    from hearth.telegram.intent import interpret_intent

    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Annihilation",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    decision = await interpret_intent("Annihilation")
    assert decision.action == "search"
    assert decision.search_title == "Annihilation"
    assert calls
    assert calls[0]["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_inbox_descriptive_wizard_resolves_to_harry_potter(
    inbox: TelegramInbox, monkeypatch
):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(
        _msg("a movie about a boy with glasses who is a wizard", message_id=200)
    )
    assert result.grabbed is False
    assert "Which one" in result.reply
    assert "Harry Potter" in result.reply or "Sorcerer" in result.reply
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert "Harry Potter" in pending.query or all(
        "Harry Potter" in str(row.get("title") or "") for row in pending.options[:3]
    )
    assert "sk-test" not in result.reply
    assert "OPENAI" not in result.reply


@pytest.mark.asyncio
async def test_inbox_dutch_mirror_puzzle_always_calls_openai(
    inbox: TelegramInbox, monkeypatch
):
    """Always-AI: Dutch plot must hit the model even without English descriptive regex."""
    dutch = (
        "Die film waar iemand een puzzel oplost door een spiegel voor rare tekens "
        "te houden en dan ineens kan lezen, super slim"
    )
    queries = _imitation_and_davinci_radarr(monkeypatch)
    openai_calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Imitation Game",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    result = await inbox.handle_message(_msg(dutch, message_id=300))
    assert openai_calls, "Dutch plot ask must call OpenAI"
    assert openai_calls[0]["model"] == "gpt-4o"
    user_payload = openai_calls[0]["messages"][1]["content"]
    assert "spiegel" in user_payload
    assert "Which one" in result.reply
    assert "Imitation Game" in result.reply
    assert inbox.pending.get(-1001) is not None
    for q in queries:
        assert "spiegel" not in q.lower()
        assert "puzzel" not in q.lower()


@pytest.mark.asyncio
async def test_inbox_reject_imitation_game_leonardo_clue_searches_davinci(
    inbox: TelegramInbox, monkeypatch
):
    """Wrong first guess must not trap the user in a 1–2 loop."""
    import json as _json

    dutch = (
        "Die film waar iemand een puzzel oplost door een spiegel voor rare tekens "
        "te houden en dan ineens kan lezen, super slim"
    )
    reject = (
        "Nee, niet die. Ik denk dat het van die kunstenaar leonardo dicaprio was"
    )
    queries = _imitation_and_davinci_radarr(monkeypatch)
    openai_calls = _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "The Imitation Game",
                "media_kind": "movie",
                "confidence": 0.9,
            },
            {
                "action": "search",
                "search_title": "The Da Vinci Code",
                "media_kind": "movie",
                "confidence": 0.95,
            },
        ],
    )
    first = await inbox.handle_message(_msg(dutch, message_id=301))
    assert "Imitation Game" in first.reply
    assert "1–2" in first.reply or "1-2" in first.reply or "Reply 1" in first.reply
    assert inbox.pending.get(-1001) is not None

    second = await inbox.handle_message(_msg(reject, message_id=302))
    assert len(openai_calls) >= 2
    payload = _json.loads(openai_calls[1]["messages"][1]["content"])
    # Live pending stays on screen for the model hop (not pivot-cleared first).
    assert payload.get("candidates_are_live_pending") is True
    cands = payload.get("candidates") or []
    assert any("imitation" in str(c.get("title") or "").lower() for c in cands)
    history = payload.get("recent_history") or []
    assert history, "reject turn must include chat history"
    assert "1–2" not in second.reply and "Reply 1" not in second.reply
    assert "Imitation Game" not in second.reply or "Da Vinci" in second.reply
    assert any("da vinci" in q.lower() for q in queries)
    assert not any(
        "imitation" in q.lower() for q in queries[1:]
    ) or any("da vinci" in q.lower() for q in queries)
    subject, _ = inbox.memory.subject(-1001)
    assert "Da Vinci" in subject
    assert "Imitation" not in subject
    # After the model chose a new search, Imitation Game is rejected.
    rejected_mem = [t.lower() for t in inbox.memory.rejected(-1001)]
    assert any("imitation" in t for t in rejected_mem)
    assert inbox.pending.get(-1001) is None or "Da Vinci" in (
        inbox.pending[-1001].query if inbox.pending.get(-1001) else ""
    )


@pytest.mark.asyncio
async def test_inbox_reject_imitation_game_tim_honks_searches_davinci(
    inbox: TelegramInbox, monkeypatch
):
    import json as _json

    dutch = (
        "Die film waar iemand een puzzel oplost door een spiegel voor rare tekens "
        "te houden en dan ineens kan lezen, super slim"
    )
    reject = "Niet the imitation game. Degene die ik zoek was met tim honks"
    queries = _imitation_and_davinci_radarr(monkeypatch)
    openai_calls = _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "The Imitation Game",
                "media_kind": "movie",
                "confidence": 0.9,
            },
            {
                "action": "search",
                "search_title": "The Da Vinci Code",
                "media_kind": "movie",
                "confidence": 0.95,
            },
        ],
    )
    await inbox.handle_message(_msg(dutch, message_id=310))
    second = await inbox.handle_message(_msg(reject, message_id=311))
    assert len(openai_calls) >= 2
    payload = _json.loads(openai_calls[1]["messages"][1]["content"])
    assert payload.get("candidates_are_live_pending") is True
    assert any(
        "imitation" in str(c.get("title") or "").lower()
        for c in (payload.get("candidates") or [])
    )
    assert "Which one — reply 1" not in second.reply
    assert any("da vinci" in q.lower() for q in queries)
    subject, _ = inbox.memory.subject(-1001)
    assert "Da Vinci" in subject
    rejected_mem = [t.lower() for t in inbox.memory.rejected(-1001)]
    assert any("imitation" in t for t in rejected_mem)


@pytest.mark.asyncio
async def test_inbox_bare_nee_after_disambiguation_clarifies_no_grab(
    inbox: TelegramInbox, monkeypatch
):
    dutch = (
        "Die film waar iemand een puzzel oplost door een spiegel voor rare tekens "
        "te houden en dan ineens kan lezen, super slim"
    )
    _imitation_and_davinci_radarr(monkeypatch)
    openai_calls = _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "The Imitation Game",
                "media_kind": "movie",
                "confidence": 0.9,
            },
            {
                "action": "clarify",
                "clarify_question": "Ok — which film did you mean then?",
                "confidence": 0.8,
            },
        ],
    )
    await inbox.handle_message(_msg(dutch, message_id=320))
    before_queue = len(pipeline.overseerr_queue)
    nee = await inbox.handle_message(_msg("nee", message_id=321))
    assert nee.grabbed is False
    assert len(pipeline.overseerr_queue) == before_queue
    assert "Ok" in nee.reply or "Which" in nee.reply or "?" in nee.reply
    # Must not stay stuck on the Imitation Game 1–2 prompt.
    assert "Imitation Game" not in nee.reply
    assert "1–2" not in nee.reply and "Reply 1" not in nee.reply
    assert len(openai_calls) >= 2
    assert inbox.pending.get(-1001) is None


@pytest.mark.asyncio
async def test_inbox_instant_annihilation_year_skips_openai(
    inbox: TelegramInbox, monkeypatch
):
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "search", "search_title": "WRONG", "confidence": 0.99},
    )
    result = await inbox.handle_message(_msg("Annihilation (2018)", message_id=330))
    assert calls == []
    assert "already" in result.reply.lower() or "Annihilation" in result.reply


@pytest.mark.asyncio
async def test_inbox_instant_imdb_url_skips_openai(inbox: TelegramInbox, monkeypatch):
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "search", "search_title": "WRONG", "confidence": 0.99},
    )
    # Fixture knows Annihilation via tmdb; plain imdb may not-found — still no AI.
    result = await inbox.handle_message(
        _msg("https://www.imdb.com/title/tt2798920/", message_id=331)
    )
    assert calls == []
    assert result.handled is True


@pytest.mark.asyncio
async def test_inbox_dutch_wizard_resolves_not_literal_title(
    inbox: TelegramInbox, monkeypatch
):
    dutch = "Ben je nu wat slimmer? Ik zoek die film met die bebrilde tovenaar."
    queries = _wrap_radarr_search(monkeypatch)
    openai_calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(_msg(dutch, message_id=240))
    assert openai_calls, "Dutch plot ask must call OpenAI"
    assert openai_calls[0]["model"] == "gpt-4o"
    user_payload = openai_calls[0]["messages"][1]["content"]
    assert "bebrilde tovenaar" in user_payload
    assert "Harry Potter" in result.reply or "Sorcerer" in result.reply or "Which one" in result.reply
    for q in queries:
        assert "bebrilde" not in q.lower()
        assert "tovenaar" not in q.lower()
        assert "Ben je nu" not in q
    assert any("Harry Potter" in q for q in queries) or "Harry Potter" in (
        inbox.pending.get(-1001).query if inbox.pending.get(-1001) else ""
    )


@pytest.mark.asyncio
async def test_inbox_dutch_ring_resolves_not_entire_sentence(
    inbox: TelegramInbox, monkeypatch
):
    dutch = (
        "Ok, nog een poging. Die film met dat mannetje met harige voeten "
        "die een ring kapot moet maken."
    )
    queries = _wrap_radarr_search(monkeypatch)
    openai_calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Lord of the Rings",
            "media_kind": "movie",
            "confidence": 0.94,
        },
    )
    result = await inbox.handle_message(_msg(dutch, message_id=241))
    assert openai_calls
    user_payload = openai_calls[0]["messages"][1]["content"]
    assert "harige voeten" in user_payload
    assert "Ok, nog een poging" in user_payload
    for q in queries:
        assert "harige" not in q.lower()
        assert "mannetje" not in q.lower()
        assert "poging" not in q.lower()
        assert dutch not in q
    assert result.grabbed is False or "Lord" in result.reply or "Ring" in result.reply
    assert "harige voeten" not in result.reply.lower()
    assert "nog een poging" not in result.reply.lower()


@pytest.mark.asyncio
async def test_inbox_chat_memory_dutch_followup_de_eerste(
    inbox: TelegramInbox, monkeypatch
):
    """After HP resolution, 'de eerste' is instant while the numbered list is showing."""
    queries = _wrap_radarr_search(monkeypatch)
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    ask = await inbox.handle_message(
        _msg("Ik zoek die film met die bebrilde tovenaar.", message_id=250)
    )
    assert "Which one" in ask.reply or "Harry Potter" in ask.reply
    assert inbox.pending.get(-1001) is not None
    assert inbox.memory.has_history(-1001)

    before = list(queries)
    before_calls = len(calls)
    pick = await inbox.handle_message(_msg("de eerste", message_id=251))
    assert pick.grabbed is True
    assert "2001" in pick.reply or "Sorcerer" in pick.reply or "Harry Potter" in pick.reply
    assert len(calls) == before_calls  # instant — no second model hop
    new_queries = queries[len(before) :]
    for q in new_queries:
        assert q.strip().lower() != "de eerste"
        assert "de eerste" not in q.lower()


@pytest.mark.asyncio
async def test_inbox_chat_memory_lotr_all_of_them_not_potter(
    inbox: TelegramInbox, monkeypatch
):
    """Later LOTR subject wins — 'all of them' must target LOTR, not leftover Potter."""
    lotr_rows = [
        {
            "title": "The Lord of the Rings: The Fellowship of the Ring",
            "year": 2001,
            "tmdbId": 120,
            "mediaId": 120,
            "mediaType": "movie",
        },
        {
            "title": "The Lord of the Rings: The Two Towers",
            "year": 2002,
            "tmdbId": 121,
            "mediaId": 121,
            "mediaType": "movie",
        },
        {
            "title": "The Lord of the Rings: The Return of the King",
            "year": 2003,
            "tmdbId": 122,
            "mediaId": 122,
            "mediaType": "movie",
        },
    ]

    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    queries: list[str] = []
    original = inbox_mod.overseerr.search

    async def _capture(query: str, *args, **kwargs):
        queries.append(str(query))
        q = (query or "").lower()
        if "lord" in q or ("ring" in q and "harry" not in q):
            return {"mode": "mock", "service": "overseerr", "results": list(lotr_rows)}
        if q.isdigit() or q.startswith("tmdb:"):
            try:
                want = int(q.split(":")[-1])
            except ValueError:
                want = None
            if want is not None:
                hit = [row for row in lotr_rows if row.get("tmdbId") == want]
                if hit:
                    return {"mode": "mock", "service": "overseerr", "results": hit}
        return await original(query, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.overseerr, "search", _capture)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _capture)

    _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "Harry Potter",
                "media_kind": "movie",
                "confidence": 0.95,
            },
            {
                "action": "search",
                "search_title": "Lord of the Rings",
                "media_kind": "movie",
                "confidence": 0.95,
            },
        ],
    )

    await inbox.handle_message(
        _msg("Ik zoek die film met die bebrilde tovenaar.", message_id=260)
    )
    assert inbox.memory.subject(-1001)[0]
    assert "Harry" in inbox.memory.subject(-1001)[0]

    lotr_ask = await inbox.handle_message(
        _msg(
            "Ok, nog een poging. Die film met dat mannetje met harige voeten "
            "die een ring kapot moet maken.",
            message_id=261,
        )
    )
    assert "Which one" in lotr_ask.reply or "Lord" in lotr_ask.reply or "Ring" in lotr_ask.reply
    subject, _ = inbox.memory.subject(-1001)
    assert "Lord" in subject or "Ring" in subject
    assert "Harry" not in subject
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert all("Lord" in str(row.get("title") or "") for row in pending.options[:3])

    all_of = await inbox.handle_message(_msg("all of them", message_id=262))
    assert all_of.grabbed is True
    assert "Lord of the Rings" in all_of.reply
    assert "Harry Potter" not in all_of.reply
    joined = " ".join(all_of.titles) if all_of.titles else all_of.reply
    assert "Lord of the Rings" in joined
    assert "Harry Potter" not in joined
    for q in queries:
        assert q.strip().lower() != "all of them"


@pytest.mark.asyncio
async def test_inbox_annihilation_passthrough_with_empty_history(
    inbox: TelegramInbox, monkeypatch
):
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "search", "search_title": "WRONG", "confidence": 0.99},
    )
    assert not inbox.memory.has_history(-1001)
    result = await inbox.handle_message(_msg("Annihilation (2018)", message_id=270))
    assert calls == []
    assert "already" in result.reply.lower() or "Annihilation" in result.reply


@pytest.mark.asyncio
async def test_inbox_literal_title_still_passthrough(inbox: TelegramInbox, monkeypatch):
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "search", "search_title": "WRONG", "confidence": 0.99},
    )
    result = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=210))
    assert result.grabbed is True
    assert "Brutalist" in result.reply
    assert calls == []  # year + concrete title → instant, no intent hop


@pytest.mark.asyncio
async def test_inbox_catalog_link_still_passthrough(inbox: TelegramInbox, monkeypatch):
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "search", "search_title": "WRONG", "confidence": 0.99},
    )
    result = await inbox.handle_message(
        _msg("https://www.themoviedb.org/movie/974950-the-brutalist", message_id=211)
    )
    assert result.grabbed is True
    assert "Brutalist" in result.reply
    assert calls == []
    assert "sk-test" not in result.reply


@pytest.mark.asyncio
async def test_inbox_descriptive_unsure_clarifies(inbox: TelegramInbox, monkeypatch):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": "Did you mean a specific title?",
            "confidence": 0.4,
        },
    )
    result = await inbox.handle_message(
        _msg("a movie about someone who does something vague", message_id=220)
    )
    assert result.grabbed is False
    assert "Did you mean" in result.reply or "Which" in result.reply
    assert len(pipeline.overseerr_queue) == 0
    assert "sk-test" not in result.reply


@pytest.mark.asyncio
async def test_inbox_descriptive_low_confidence_search_clarifies(
    inbox: TelegramInbox, monkeypatch
):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Maybe This",
            "confidence": 0.3,
        },
    )
    result = await inbox.handle_message(
        _msg("a film about a kid who finds a magic school", message_id=221)
    )
    assert result.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    assert "title" in result.reply.lower() or "Which" in result.reply


@pytest.mark.asyncio
async def test_inbox_followups_still_work_with_descriptive_layer(
    inbox: TelegramInbox, monkeypatch
):
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    ask = await inbox.handle_message(_msg("Harry Potter", message_id=230))
    assert "Which one" in ask.reply
    all_of = await inbox.handle_message(_msg("all of them", message_id=231))
    assert all_of.grabbed is True
    assert len(pipeline.overseerr_queue) >= 3


def test_status_endpoint_includes_telegram(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_ids", "")
    status = client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert "telegram" in body
    assert body["telegram"]["configured"] is False


def test_local_webhook_rejected_when_disabled(client):
    resp = client.post("/telegram/webhook", json={"update_id": 1})
    assert resp.status_code == 404


# --- quiet download progress -------------------------------------------------


def _queue_payload(title: str, *, percent: float | None, status: str = "downloading") -> dict:
    row: dict = {"title": title, "status": status, "percent": percent}
    return {"downloads": [row], "count": 1, "empty": False}


@pytest.mark.asyncio
async def test_progress_one_start_ping_at_threshold_no_later_spam(monkeypatch):
    from hearth.telegram.progress import ProgressTracker, START_THRESHOLD

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[tuple[int, str]] = []

    async def capture(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    state = {"percent": 2.0, "status": "downloading"}

    async def fake_queue(_title: str):
        return _queue_payload(
            "Annihilation", percent=state["percent"], status=state["status"]
        )

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    # Below threshold — no Telegram ping yet.
    await tracker.poll_once(capture)
    assert sent == []
    assert tracker.active[0].announce_started is False

    # Cross ~5% — exactly one start ping.
    state["percent"] = START_THRESHOLD
    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "Annihilation is downloading" in sent[0][1]
    assert "~5%" in sent[0][1]
    assert tracker.active[0].announce_started is True

    # Later percent ticks must not spam.
    for pct in (10.0, 25.0, 50.0, 75.0, 90.0):
        state["percent"] = pct
        await tracker.poll_once(capture)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_progress_no_duplicate_start_for_same_item(monkeypatch):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    # Retries / duplicate track calls must not create a second active item.
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    tracker.track(-1001, "annihilation", "radarr", 2018)
    assert len(tracker.active) == 1

    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    async def fake_queue(_title: str):
        return _queue_payload("Annihilation", percent=12.0)

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    await tracker.poll_once(capture)
    await tracker.poll_once(capture)
    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "downloading" in sent[0]


@pytest.mark.asyncio
async def test_progress_failure_auto_retries_alternate_source(monkeypatch):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    state = {"percent": 8.0, "status": "downloading"}

    async def fake_queue(_title: str):
        return _queue_payload(
            "Annihilation", percent=state["percent"], status=state["status"]
        )

    async def fake_retry(title: str = "", *, force: bool = False, reason: str = ""):
        assert title == "Annihilation"
        assert force is False
        assert reason.startswith("auto:")
        return {
            "ok": True,
            "reason": "retried",
            "title": "Annihilation",
            "indexer": "AltIndexer",
            "attempt": 1,
            "max_attempts": 3,
            "speak": "Annihilation stalled — trying another source via AltIndexer (attempt 1/3).",
        }

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)
    monkeypatch.setattr("hearth.telegram.progress.radarr.retry_download", fake_retry)

    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "downloading" in sent[0]

    state["status"] = "failed"
    await tracker.poll_once(capture)
    assert len(sent) == 2
    assert "another source" in sent[1].lower()
    assert tracker.active  # still watching the new grab
    assert tracker.active[0].announce_retrying is True


@pytest.mark.asyncio
async def test_progress_failure_exhausted_notifies(monkeypatch):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    async def fake_queue(_title: str):
        return _queue_payload("Annihilation", percent=8.0, status="failed")

    async def fake_retry(title: str = "", *, force: bool = False, reason: str = ""):
        return {
            "ok": False,
            "reason": "exhausted",
            "title": "Annihilation",
            "attempt": 3,
            "max_attempts": 3,
            "speak": "Annihilation failed — ran out of alternate sources after 3 tries.",
        }

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)
    monkeypatch.setattr("hearth.telegram.progress.radarr.retry_download", fake_retry)

    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "ran out of alternate sources" in sent[0].lower()
    assert tracker.active == []


@pytest.mark.asyncio
async def test_progress_done_after_start_still_notifies(monkeypatch):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    state: dict = {
        "downloads": [{"title": "Annihilation", "status": "downloading", "percent": 6.0}],
    }

    async def fake_queue(_title: str):
        downloads = state["downloads"]
        return {"downloads": downloads, "count": len(downloads), "empty": not downloads}

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    await tracker.poll_once(capture)
    assert len(sent) == 1

    state["downloads"] = []
    await tracker.poll_once(capture)
    assert len(sent) == 2
    assert "done" in sent[1].lower()
    assert tracker.active == []


@pytest.mark.asyncio
async def test_progress_failure_before_start_threshold_still_notifies(monkeypatch):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    async def fake_queue(_title: str):
        return _queue_payload("Annihilation", percent=2.0, status="failed")

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "failed" in sent[0].lower()
    assert tracker.active == []


def test_format_downloading_never_says_near_100():
    from hearth.telegram.progress import format_done, format_downloading

    assert format_downloading("Annihilation", None) == "Annihilation is downloading."
    assert format_downloading("Annihilation", 12.0) == "Annihilation is downloading, ~12%."
    assert "~100%" not in format_downloading("Annihilation", 100.0)
    assert "~99%" not in format_downloading("Annihilation", 99.0)
    assert format_downloading("Annihilation", 100.0) == "Annihilation is downloading."
    assert format_downloading("Annihilation", 95.0) == "Annihilation is downloading."
    assert format_done("Annihilation") == "Annihilation is done — in Plex."


@pytest.mark.asyncio
async def test_progress_bogus_100_does_not_start_ping(monkeypatch):
    """First poll at ~100% while still downloading must not announce downloading ~100%."""
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    state = {"percent": 100.0, "status": "downloading"}

    async def fake_queue(_title: str):
        return _queue_payload(
            "Annihilation", percent=state["percent"], status=state["status"]
        )

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    await tracker.poll_once(capture)
    assert sent == []
    assert tracker.active[0].announce_started is False

    # Missing percent — also no invented start ping.
    state["percent"] = None
    await tracker.poll_once(capture)
    assert sent == []

    # Real mid-start percent — one quiet start ping.
    state["percent"] = 8.0
    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "Annihilation is downloading, ~8%" in sent[0]
    assert "~100%" not in sent[0]


@pytest.mark.asyncio
async def test_progress_first_sighting_completed_says_done_not_downloading(
    monkeypatch,
):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    async def fake_queue(_title: str):
        return _queue_payload("Annihilation", percent=100.0, status="completed")

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "done" in sent[0].lower()
    assert "downloading" not in sent[0].lower()


@pytest.mark.asyncio
async def test_progress_idle_zero_progress_triggers_auto_retry(monkeypatch):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    async def fake_queue(_title: str):
        return _queue_payload("Annihilation", percent=0.0, status="downloading")

    calls = {"n": 0}

    async def fake_retry(title: str = "", *, force: bool = False, reason: str = ""):
        calls["n"] += 1
        assert force is False
        assert "stalled" in reason
        return {
            "ok": True,
            "reason": "retried",
            "title": "Annihilation",
            "indexer": "AltIndexer",
            "attempt": 1,
            "max_attempts": 3,
            "speak": "Annihilation stalled — trying another source via AltIndexer (attempt 1/3).",
        }

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)
    monkeypatch.setattr("hearth.telegram.progress.radarr.retry_download", fake_retry)

    # First poll records progress baseline — not idle yet.
    await tracker.poll_once(capture, stall_idle_s=30)
    assert calls["n"] == 0

    # Force the idle clock past the threshold without percent moving.
    item = tracker.active[0]
    item.last_progress_at -= 60
    await tracker.poll_once(capture, stall_idle_s=30)
    assert calls["n"] == 1
    assert any("another source" in msg.lower() for msg in sent)


@pytest.mark.asyncio
async def test_inbox_retry_intent_retries_tracked_title(inbox: TelegramInbox, monkeypatch):
    from hearth.telegram.intent import IntentDecision

    inbox.progress.track(-1001, "Annihilation", "radarr", 2018)
    inbox.memory.set_subject(-1001, "Annihilation", media_kind="movie")

    async def fake_intent(*_args, **_kwargs):
        return IntentDecision(
            action="retry",
            search_title="Annihilation",
            media_kind="movie",
            confidence=0.9,
            source="test",
        )

    monkeypatch.setattr("hearth.telegram.inbox.interpret_intent", fake_intent)

    # Seed a failed queue row so force retry has something to blocklist.
    pipeline.radarr_downloads = [
        {
            "id": 1,
            "movieId": 101,
            "title": "Annihilation",
            "status": "failed",
            "trackedDownloadState": "failed",
            "indexer": "MockIndexer",
            "downloadId": "mock-anni-1",
            "movie": {"id": 101, "title": "Annihilation", "year": 2018, "tmdbId": 300668},
        }
    ]

    result = await inbox.handle_message(
        _msg("this download didn't work, try another source", message_id=9001)
    )
    assert result.handled is True
    assert "another source" in result.reply.lower()
    assert pipeline.radarr_blocklist
    assert any(row.get("indexer") == "AltIndexer" for row in pipeline.radarr_downloads or [])


@pytest.mark.asyncio
async def test_progress_importing_stays_quiet_until_completed(monkeypatch):
    from hearth.telegram.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.track(-1001, "Annihilation", "radarr", 2018)
    sent: list[str] = []

    async def capture(_chat_id: int, text: str) -> None:
        sent.append(text)

    state = {"percent": 99.0, "status": "importing"}

    async def fake_queue(_title: str):
        return _queue_payload(
            "Annihilation", percent=state["percent"], status=state["status"]
        )

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    await tracker.poll_once(capture)
    assert sent == []

    state["status"] = "completed"
    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "done" in sent[0].lower()


@pytest.mark.asyncio
async def test_manual_status_path_still_reports_current_progress(client):
    """Voice/chat status asks use radarr_queue — unchanged by quiet Telegram pings."""
    resp = client.post(
        "/api/chat",
        json={"message": "Hey, can you check the progress of Annihilation—the download progress?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"][0]["name"] == "radarr_queue"
    assert "Annihilation" in body["reply"]
    assert "75" in body["reply"] or "%" in body["reply"]


# --- catalog resolve / years / chatter / dedup (Cape Fear + Sloss) ------------


def test_search_query_never_returns_raw_imdb_id():
    parsed = parse_message_text("https://www.imdb.com/title/tt34675596/")
    assert parsed.imdb_id == "tt34675596"
    assert parsed.search_query() == ""
    assert "tt34675596" not in parsed.display_label()


@pytest.mark.asyncio
async def test_inbox_imdb_cape_fear_resolves_tv_not_tt_string(
    inbox: TelegramInbox, monkeypatch
):
    """IMDb URL must Overseerr-resolve to Cape Fear TV 2026 — never Radarr search tt…."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    async def _fake_find(external_id: str, *, external_source: str = "imdb_id"):
        assert "tt34675596" in external_id.lower()
        assert external_source == "imdb_id"
        return {
            "movie_results": [],
            "tv_results": [
                {
                    "id": 241001,
                    "name": "Cape Fear",
                    "first_air_date": "2026-01-01",
                    "mediaType": "tv",
                }
            ],
            "source": "mock_tmdb",
        }

    monkeypatch.setattr(catalog_mod, "tmdb_find", _fake_find)

    radarr_queries: list[str] = []

    async def _radarr_forbid(query: str, *args, **kwargs):
        radarr_queries.append(str(query))
        raise AssertionError(f"inbox must not Radarr-search: {query!r}")

    monkeypatch.setattr(inbox_mod.radarr, "search", _radarr_forbid)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    result = await inbox.handle_message(
        _msg("https://www.imdb.com/title/tt34675596/", message_id=400)
    )
    assert result.grabbed is True
    assert "Cape Fear" in result.reply
    assert "2026" in result.reply
    assert "tt34675596" not in result.reply
    assert radarr_queries == []
    assert overseerr_requests
    assert overseerr_requests[0]["media_type"] == "tv"
    assert overseerr_requests[0]["media_id"] == 241001
    assert pipeline.overseerr_queue
    assert "Overseerr" in result.reply


@pytest.mark.asyncio
async def test_inbox_chatter_ignored_empty_reply(inbox: TelegramInbox, monkeypatch):
    """Meta talk / emoji must not ask which movie — empty reply (ignore)."""
    # Seed history so the old force=True path would have fired.
    inbox.memory.record_user(-1001, "https://www.imdb.com/title/tt34675596/")
    inbox.memory.record_bot(-1001, "Couldn't find a match for 'tt34675596'.")
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "clarify", "clarify_question": "Which movie or series did you mean?"},
    )
    for mid, text in (
        (401, "🙈"),
        (402, "Jaartallen kloppen niet altijd volgens mij..."),
        (403, "Ga ik fixen 😄"),
    ):
        result = await inbox.handle_message(_msg(text, message_id=mid))
        assert result.handled is True
        assert result.reply == "", f"expected ignore for {text!r}, got {result.reply!r}"
        assert result.grabbed is False
    # Chatter short-circuits before the model.
    assert calls == []


@pytest.mark.asyncio
async def test_inbox_annihilation_year_goes_through_tmdb_resolve(
    inbox: TelegramInbox, monkeypatch
):
    from hearth.telegram import catalog as catalog_mod

    resolve_calls: list[dict] = []
    original = catalog_mod.resolve_title

    async def _wrap(title: str, *, year=None, media_kind: str = ""):
        resolve_calls.append({"title": title, "year": year, "media_kind": media_kind})
        return await original(title, year=year, media_kind=media_kind)

    monkeypatch.setattr(catalog_mod, "resolve_title", _wrap)
    result = await inbox.handle_message(_msg("Annihilation (2018)", message_id=410))
    assert resolve_calls, "Title (YYYY) must TMDB/catalog-resolve the year"
    assert resolve_calls[0]["title"] == "Annihilation"
    assert resolve_calls[0]["year"] == 2018
    assert "Annihilation" in result.reply
    assert "2018" in result.reply or "already" in result.reply.lower()


@pytest.mark.asyncio
async def test_inbox_daniel_sloss_near_exact_queues_without_dup_12(
    inbox: TelegramInbox, monkeypatch
):
    """Near-exact special title → gpt-4o + catalog candidates → one grab."""
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Daniel Sloss: Can't",
            "media_kind": "movie",
            "year": 2025,
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(_msg("Daniel sloss can't", message_id=420))
    assert result.grabbed is True
    assert "Daniel Sloss" in result.reply
    assert "2025" in result.reply
    assert "Which one" not in result.reply
    assert "1." not in result.reply
    assert "2." not in result.reply
    assert calls, "bare title must call gpt-4o with catalog candidates"
    payload = calls[0]["messages"][1]["content"]
    assert "candidates" in payload
    assert "Daniel Sloss" in payload or "Sloss" in payload

    # Colon form with Overseerr duplicate rows must also collapse + grab.
    inbox.deduper.reset()
    pipeline.overseerr_queue.clear()
    again = await inbox.handle_message(_msg("Daniel sloss: Can't", message_id=421))
    assert again.grabbed is True or "already" in again.reply.lower()
    assert "1. Daniel Sloss: Can't (2025)" not in again.reply
    assert again.reply.count("2025") <= 2  # queued line only, not a 1–2 list


@pytest.mark.asyncio
async def test_dedupe_choices_collapses_identical_title_year():
    box = TelegramInbox()
    rows = [
        {"title": "Daniel Sloss: Can't", "year": 2025, "tmdbId": 1520001, "mediaType": "movie"},
        {"title": "Daniel Sloss: Can't", "year": 2025, "tmdbId": 1520002, "mediaType": "movie"},
    ]
    deduped = box._dedupe_choices(rows)
    assert len(deduped) == 1
    assert deduped[0]["year"] == 2025


@pytest.mark.asyncio
async def test_disambiguation_options_include_years(inbox: TelegramInbox, monkeypatch):
    _imitation_and_davinci_radarr(monkeypatch)
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Imitation Game",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    dutch = (
        "Die film waar iemand een puzzel oplost door een spiegel voor rare tekens "
        "te houden en dan ineens kan lezen, super slim"
    )
    result = await inbox.handle_message(_msg(dutch, message_id=430))
    assert "2014" in result.reply
    assert "1980" in result.reply or "Which one" in result.reply


# --- Overseerr-only TV fallback (Reggie Dinkins / Friends) --------------------


def test_catalog_search_title_strips_actor_clause():
    from hearth.telegram.catalog import catalog_search_title

    assert (
        catalog_search_title(
            "The fall and rise of reggie dinkins with daniel radcliff"
        )
        == "The fall and rise of reggie dinkins"
    )
    assert catalog_search_title("Gone with the Wind") == "Gone with the Wind"
    assert (
        catalog_search_title("The Fall and Rise of Reggie Dinkins")
        == "The Fall and Rise of Reggie Dinkins"
    )


@pytest.mark.asyncio
async def test_inbox_reggie_dinkins_with_actor_queues_overseerr_tv(
    inbox: TelegramInbox, monkeypatch
):
    """Known TV title+year must not format_not_found when movie ask has no hit."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    radarr_queries: list[str] = []

    async def _radarr_forbid(query: str, *args, **kwargs):
        radarr_queries.append(str(query))
        raise AssertionError(f"inbox must not Radarr-search title: {query!r}")

    monkeypatch.setattr(inbox_mod.radarr, "search", _radarr_forbid)

    overseerr_searches: list[str] = []
    original_search = inbox_mod.overseerr.search

    async def _search_capture(query: str, *args, **kwargs):
        overseerr_searches.append(str(query))
        return await original_search(query, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search_capture)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search_capture)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    # gpt-4o returns clean title + year; actor clause stripped from Overseerr query.
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Fall and Rise of Reggie Dinkins",
            "year": 2026,
            "media_kind": "movie",  # implied movie must still find the TV hit
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(
        _msg(
            "The fall and rise of reggie dinkins with daniel radcliff",
            message_id=500,
        )
    )
    assert result.grabbed is True, result.reply
    assert "Couldn't find" not in result.reply
    assert "Reggie Dinkins" in result.reply
    assert "2026" in result.reply
    assert "Overseerr" in result.reply
    assert radarr_queries == []
    assert overseerr_requests
    assert overseerr_requests[0]["media_type"] == "tv"
    assert overseerr_requests[0]["media_id"] == 291334
    assert any(
        (row.get("mediaType") or "") == "tv"
        and (row.get("id") == 291334 or row.get("tmdbId") == 291334)
        for row in pipeline.overseerr_queue
    )
    # Search string must not include the actor misspelling.
    assert overseerr_searches
    assert all("radcliff" not in q.lower() for q in overseerr_searches)
    assert all("daniel" not in q.lower() for q in overseerr_searches)


@pytest.mark.asyncio
async def test_inbox_reggie_dinkins_without_actor_still_tv(
    inbox: TelegramInbox, monkeypatch
):
    from hearth.telegram import inbox as inbox_mod

    async def _radarr_forbid(query: str, *args, **kwargs):
        raise AssertionError(f"inbox must not Radarr-search: {query!r}")

    monkeypatch.setattr(inbox_mod.radarr, "search", _radarr_forbid)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    # Bare TV title — gpt-4o with catalog candidates (no catalog-first skip).
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Fall and Rise of Reggie Dinkins",
            "year": 2026,
            "media_kind": "tv",
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(
        _msg("The Fall and Rise of Reggie Dinkins", message_id=501)
    )
    assert result.grabbed is True, result.reply
    assert "Couldn't find" not in result.reply
    assert "Overseerr" in result.reply
    assert overseerr_requests
    assert overseerr_requests[0]["media_type"] == "tv"
    assert overseerr_requests[0]["media_id"] == 291334
    assert calls, "bare title must call gpt-4o"


@pytest.mark.asyncio
async def test_inbox_friends_s1_still_queues_overseerr_tv(
    inbox: TelegramInbox, monkeypatch
):
    from hearth.telegram import inbox as inbox_mod

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Friends",
            "media_kind": "tv",
            "year": 1994,
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(_msg("Friends s1", message_id=502))
    assert result.grabbed is True, result.reply
    assert "Friends" in result.reply
    assert "Overseerr" in result.reply
    assert overseerr_requests
    assert overseerr_requests[0]["media_type"] == "tv"
    assert overseerr_requests[0]["media_id"] == 1668


# --- Christophers / invent-title / subject-leak (live Telegram 2026-08-30) ----


def test_search_title_grounded_blocks_invented_guest():
    from hearth.telegram.intent import search_title_grounded

    candidates = [
        {"title": "The Christophers", "year": 2025, "tmdbId": 1280010, "mediaType": "movie"}
    ]
    assert search_title_grounded(
        "The Christophers",
        user_message="The christophers with ian mckellan",
        candidates=candidates,
    )
    assert not search_title_grounded(
        "The Christopher Guest Movies",
        user_message="The christophers with ian mckellan",
        candidates=candidates,
    )
    assert not search_title_grounded(
        "The Da Vinci Code (2006)",
        user_message="The christophers with ian mckellan",
        candidates=candidates,
    )
    # Plot ask with empty candidates may invent.
    assert search_title_grounded(
        "Harry Potter",
        user_message="a movie about a boy with glasses who is a wizard",
        candidates=[],
    )


def test_clarify_wants_numbered_list_detects_empty_prompt():
    from hearth.telegram.intent import clarify_wants_numbered_list

    assert clarify_wants_numbered_list(
        "Which one — reply 1–3, 'all of them', or a clearer title?"
    )
    assert not clarify_wants_numbered_list("Which movie did you mean?")


@pytest.mark.asyncio
async def test_inbox_christophers_bare_queues_or_names_list(
    inbox: TelegramInbox, monkeypatch
):
    """Bare 'The christophers' → queue or a 1–N list that NAMES titles."""
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": (
                "Which one — reply 1–3, 'all of them', or a clearer title?"
            ),
            "confidence": 0.7,
        },
    )
    result = await inbox.handle_message(_msg("The christophers", message_id=600))
    assert calls, "bare title must call gpt-4o"
    payload = calls[0]["messages"][1]["content"]
    assert "Christophers" in payload or "christophers" in payload.lower()
    assert "candidates" in payload
    if result.grabbed:
        assert "Christophers" in result.reply
        assert "Couldn't find" not in result.reply
        assert "Guest" not in result.reply
        assert "Da Vinci" not in result.reply
    else:
        assert "1." in result.reply
        assert "Christophers" in result.reply
        assert "reply 1" in result.reply.lower() or "Reply 1" in result.reply


@pytest.mark.asyncio
async def test_inbox_christophers_with_mckellen_queues_never_guest(
    inbox: TelegramInbox, monkeypatch
):
    """Actor clue + invent Guest → still queue The Christophers, never Guest not-found."""
    from hearth.telegram import inbox as inbox_mod

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Christopher Guest Movies",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    result = await inbox.handle_message(
        _msg("The christophers with ian mckellan", message_id=601)
    )
    assert calls
    payload = calls[0]["messages"][1]["content"]
    assert "mckellan" in payload.lower() or "ian" in payload.lower()
    assert "Christophers" in payload or "christophers" in payload.lower()
    assert result.grabbed is True, result.reply
    assert "Christophers" in result.reply
    assert "2025" in result.reply
    assert "Guest" not in result.reply
    assert "Couldn't find" not in result.reply
    assert overseerr_requests
    assert overseerr_requests[0]["media_type"] == "movie"
    assert overseerr_requests[0]["media_id"] == 1280010


@pytest.mark.asyncio
async def test_inbox_christophers_clears_davinci_subject_leak(
    inbox: TelegramInbox, monkeypatch
):
    """Leftover Da Vinci subject must not become the Overseerr query."""
    inbox.memory.set_subject(-1001, "The Da Vinci Code", media_kind="movie")
    inbox.memory.record_user(-1001, "nee niet die, leonardo")
    inbox.memory.record_bot(
        -1001,
        "Queued The Da Vinci Code (2006) via Overseerr.",
        search_title="The Da Vinci Code",
        media_kind="movie",
    )
    subj, _ = inbox.memory.subject(-1001)
    assert "Da Vinci" in subj

    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Da Vinci Code (2006)",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(
        _msg("The christophers with ian mckellan", message_id=602)
    )
    assert calls
    # Model payload must not keep Da Vinci as the active subject.
    payload = calls[0]["messages"][1]["content"]
    import json as _json

    body = _json.loads(payload)
    assert body.get("subject_title") in {"", None} or "Christophers" in str(
        body.get("subject_title")
    )
    assert "Da Vinci" not in (body.get("subject_title") or "")
    assert result.grabbed is True, result.reply
    assert "Christophers" in result.reply
    assert "Da Vinci" not in result.reply
    assert "Couldn't find" not in result.reply


@pytest.mark.asyncio
async def test_inbox_clarify_with_hits_always_lists_titles(
    inbox: TelegramInbox, monkeypatch
):
    """Clarify 1–N without options is impossible when catalog hits exist."""
    rows = [
        {"title": "Heat", "year": 1995, "tmdbId": 1, "mediaId": 1, "mediaType": "movie"},
        {"title": "Heat", "year": 1986, "tmdbId": 2, "mediaId": 2, "mediaType": "movie"},
        {"title": "Heat", "year": 2023, "tmdbId": 3, "mediaId": 3, "mediaType": "movie"},
    ]

    async def multi(_query: str):
        return {"mode": "mock", "service": "overseerr", "results": list(rows)}

    monkeypatch.setattr("hearth.telegram.inbox.overseerr.search", multi)
    monkeypatch.setattr("hearth.telegram.catalog.overseerr.search", multi)
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": (
                "Which one — reply 1–3, 'all of them', or a clearer title?"
            ),
            "confidence": 0.6,
        },
    )
    result = await inbox.handle_message(_msg("Heat", message_id=610))
    assert result.grabbed is False
    assert "1." in result.reply
    assert "Heat (1995)" in result.reply or "1995" in result.reply
    assert "1986" in result.reply or "2023" in result.reply
    # Must not be the list-less prompt alone.
    assert result.reply.strip() != (
        "Which one — reply 1–3, 'all of them', or a clearer title?"
    )


# --- list-less 1–1 / plot guess-confirm (live Telegram 2026-08-31) ------------


def test_parse_model_json_never_defaults_to_reply_1_1():
    from hearth.telegram.intent import _parse_model_json

    parsed = _parse_model_json(
        '{"action":"clarify","confidence":0.7}',
        candidate_count=1,
    )
    assert parsed is not None
    assert "1–1" not in (parsed.clarify_question or "")
    assert "1-1" not in (parsed.clarify_question or "")
    assert "reply 1" not in (parsed.clarify_question or "").lower()

    # Live VAULT shape: model echoed the template with empty offered / count=0.
    sanitized = _parse_model_json(
        '{"action":"clarify","clarify_question":"Which one — reply 1–1, '
        "'all of them', or a clearer title?\","
        '"confidence":0.7}',
        candidate_count=0,
    )
    assert sanitized is not None
    assert sanitized.action in {"clarify", "search"}
    assert "1–1" not in (sanitized.clarify_question or "")
    assert "reply 1" not in (sanitized.clarify_question or "").lower()

    # Clarify + guess → promote to search so inbox asks yes/no.
    guessed = _parse_model_json(
        '{"action":"clarify","clarify_question":"Which one — reply 1–1, '
        "'all of them', or a clearer title?\","
        '"search_title":"Alien","year":1979,"media_kind":"movie","confidence":0.7}',
        candidate_count=0,
    )
    assert guessed is not None
    assert guessed.action == "search"
    assert guessed.search_title == "Alien"
    assert guessed.year == 1979
    assert "1–1" not in (guessed.clarify_question or "")

    # Phantom single candidate + list-less clarify → guess that title.
    phantom = _parse_model_json(
        '{"action":"clarify","clarify_question":"Which one — reply 1–1, '
        "'all of them', or a clearer title?\","
        '"confidence":0.7}',
        candidate_count=1,
        candidates=[{"title": "Alien", "year": 1979, "mediaType": "movie"}],
    )
    assert phantom is not None
    assert phantom.action == "search"
    assert phantom.search_title == "Alien"
    assert phantom.year == 1979


def test_parse_model_json_ignore_media_ask_not_silent():
    from hearth.telegram.intent import _parse_model_json

    ignored = _parse_model_json(
        '{"action":"ignore","confidence":0.9}',
        candidate_count=0,
        user_message="The coolest sci-fi you can fins",
    )
    assert ignored is not None
    assert ignored.action != "ignore"
    assert ignored.action in {"clarify", "search"}

    with_guess = _parse_model_json(
        '{"action":"ignore","search_title":"Dune","year":2021,'
        '"media_kind":"movie","confidence":0.9}',
        candidate_count=0,
        user_message="The coolest sci-fi you can fins",
    )
    assert with_guess is not None
    assert with_guess.action == "search"
    assert with_guess.search_title == "Dune"

    chatter = _parse_model_json(
        '{"action":"ignore","confidence":1.0}',
        candidate_count=0,
        user_message="lol",
    )
    assert chatter is not None
    assert chatter.action == "ignore"


def test_looks_like_concrete_title_plot_vs_named():
    from hearth.telegram.intent import looks_like_concrete_title, looks_like_media_ask

    assert not looks_like_concrete_title("Old horror movie on a spaceship")
    assert not looks_like_concrete_title("The coolest sci-fi you can fins")
    assert looks_like_concrete_title("Harry potter 2")
    assert looks_like_concrete_title(
        "The fall and rise of reggie dinkins with daniel radcliff"
    )
    assert looks_like_media_ask("The coolest sci-fi you can fins")
    assert looks_like_media_ask("Old horror movie on a spaceship")
    assert not looks_like_media_ask("lol")
    assert not looks_like_media_ask("thanks")


@pytest.mark.asyncio
async def test_inbox_after_harry_potter_spaceship_horror_asks_alien_not_1_1(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: leftover HP offered must not become list-less reply 1–1."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    alien = {
        "title": "Alien",
        "year": 1979,
        "tmdbId": 348,
        "mediaId": 348,
        "mediaType": "movie",
    }
    chamber = {
        "title": "Harry Potter and the Chamber of Secrets",
        "year": 2002,
        "tmdbId": 672,
        "mediaId": 672,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "alien" in q:
            return {"mode": "mock", "service": "overseerr", "results": [alien]}
        if "harry" in q or "chamber" in q or "potter" in q:
            return {"mode": "mock", "service": "overseerr", "results": [chamber]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    calls = _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "Harry Potter and the Chamber of Secrets",
                "year": 2002,
                "media_kind": "movie",
                "confidence": 0.95,
            },
            {
                "action": "clarify",
                "clarify_question": (
                    "Which one — reply 1–1, 'all of them', or a clearer title?"
                ),
                "confidence": 0.6,
            },
            {
                "action": "search",
                "search_title": "Alien",
                "year": 1979,
                "media_kind": "movie",
                "confidence": 0.92,
            },
        ],
    )

    hp = await inbox.handle_message(_msg("Harry potter 2", message_id=700))
    assert hp.grabbed is True, hp.reply
    assert "Chamber" in hp.reply or "2002" in hp.reply
    assert inbox.memory.offered(-1001) == []
    assert inbox.pending.get(-1001) is None
    before_queue = len(pipeline.overseerr_queue)

    # Simulate the live failure mode: model returns list-less 1–1 clarify while
    # leftover offered would have been 1 HP row (we clear offered — still safe).
    inbox.memory.set_subject(
        -1001,
        "Harry Potter and the Chamber of Secrets",
        media_kind="movie",
        offered=[chamber],
    )
    assert len(inbox.memory.offered(-1001)) == 1

    plot = await inbox.handle_message(
        _msg("Old horror movie on a spaceship", message_id=701)
    )
    assert len(calls) >= 2
    import json as _json

    payload = _json.loads(calls[1]["messages"][1]["content"])
    # Must not feed leftover Harry Potter as candidates.
    cands = payload.get("candidates") or []
    assert not any("Harry" in str(c.get("title") or "") for c in cands)
    assert "1–1" not in plot.reply
    assert "1-1" not in plot.reply
    assert "reply 1–1" not in plot.reply.lower()
    assert "reply 1-1" not in plot.reply.lower()
    assert "Harry Potter" not in plot.reply
    assert plot.grabbed is False
    assert len(pipeline.overseerr_queue) == before_queue
    # Useful clarify (no Alien yet — model returned clarify). Then retry with search.
    assert "?" in plot.reply or "mean" in plot.reply.lower() or "Which" in plot.reply

    # Second plot turn with a real search_title → Alien confirm, still no queue.
    ask = await inbox.handle_message(
        _msg("Old horror movie on a spaceship", message_id=702)
    )
    assert ask.grabbed is False
    assert "Alien" in ask.reply
    assert "1979" in ask.reply
    assert "1–1" not in ask.reply
    assert "Harry Potter" not in ask.reply
    assert len(pipeline.overseerr_queue) == before_queue
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert len(pending.options) == 1
    assert "Alien" in str(pending.options[0].get("title") or "")

    yes = await inbox.handle_message(_msg("yes", message_id=703))
    assert yes.grabbed is True, yes.reply
    assert "Alien" in yes.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 348


@pytest.mark.asyncio
async def test_inbox_unique_catalog_hit_clarify_1_n_grabs_or_names(
    inbox: TelegramInbox, monkeypatch
):
    """Unique catalog hit + model clarify 1–N → grab that hit, never list-less 1–1."""
    rows = [
        {
            "title": "The Christophers",
            "year": 2025,
            "tmdbId": 1280010,
            "mediaId": 1280010,
            "mediaType": "movie",
        }
    ]

    async def single(_query: str, *args, **kwargs):
        return {"mode": "mock", "service": "overseerr", "results": list(rows)}

    monkeypatch.setattr("hearth.telegram.inbox.overseerr.search", single)
    monkeypatch.setattr("hearth.telegram.catalog.overseerr.search", single)
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": (
                "Which one — reply 1–1, 'all of them', or a clearer title?"
            ),
            "confidence": 0.7,
        },
    )
    result = await inbox.handle_message(_msg("The christophers", message_id=710))
    assert "1–1" not in result.reply
    assert "reply 1–1" not in result.reply.lower()
    if result.grabbed:
        assert "Christophers" in result.reply
    else:
        assert "Christophers" in result.reply
        assert "1." in result.reply or "Did you mean" in result.reply or "?" in result.reply


@pytest.mark.asyncio
async def test_inbox_plot_guess_yes_queues_no_means_next_clue(
    inbox: TelegramInbox, monkeypatch
):
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    alien = {
        "title": "Alien",
        "year": 1979,
        "tmdbId": 348,
        "mediaId": 348,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "alien" in q:
            return {"mode": "mock", "service": "overseerr", "results": [alien]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "Alien",
                "year": 1979,
                "media_kind": "movie",
                "confidence": 0.9,
            },
            {
                "action": "clarify",
                "clarify_question": "Ok — any other clue (year, actor)?",
                "confidence": 0.8,
            },
        ],
    )
    ask = await inbox.handle_message(
        _msg("Old horror movie on a spaceship", message_id=720)
    )
    assert ask.grabbed is False
    assert "Alien" in ask.reply
    assert "Did you mean" in ask.reply or "?" in ask.reply
    before = len(pipeline.overseerr_queue)

    no = await inbox.handle_message(_msg("no not that", message_id=721))
    assert no.grabbed is False
    assert len(pipeline.overseerr_queue) == before
    assert "1–1" not in no.reply
    assert "Alien" not in no.reply or "clue" in no.reply.lower() or "?" in no.reply


@pytest.mark.asyncio
async def test_inbox_live_vault_listless_1_1_with_empty_offered_guesses_alien(
    inbox: TelegramInbox, monkeypatch
):
    """VAULT evidence: offered empty, bot still said reply 1–1 — ban + guess-ask."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    alien = {
        "title": "Alien",
        "year": 1979,
        "tmdbId": 348,
        "mediaId": 348,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "alien" in q:
            return {"mode": "mock", "service": "overseerr", "results": [alien]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    # Exact live clarify string; empty candidates / empty offered.
    assert inbox.memory.offered(-1001) == []
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": (
                "Which one — reply 1–1, 'all of them', or a clearer title?"
            ),
            "search_title": "Alien",
            "year": 1979,
            "media_kind": "movie",
            "confidence": 0.7,
        },
    )
    result = await inbox.handle_message(
        _msg("Old horror movie on a spaceship", message_id=730)
    )
    assert result.grabbed is False
    assert result.reply
    assert "1–1" not in result.reply
    assert "reply 1–1" not in result.reply.lower()
    assert "Alien" in result.reply
    assert "Did you mean" in result.reply or "?" in result.reply
    assert inbox.memory.offered(-1001)  # confirm stores the guess
    assert "Alien" in str(inbox.memory.offered(-1001)[0].get("title") or "")


@pytest.mark.asyncio
async def test_inbox_coolest_sci_fi_typo_not_silent_guess_and_ask(
    inbox: TelegramInbox, monkeypatch
):
    """Live: 'The coolest sci-fi you can fins' stored no bot reply — must guess-ask."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    dune = {
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "mediaId": 438631,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "dune" in q:
            return {"mode": "mock", "service": "overseerr", "results": [dune]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    # Model wrongly ignores a plot/typo ask (live silence path).
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "ignore",
            "search_title": "Dune",
            "year": 2021,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    result = await inbox.handle_message(
        _msg("The coolest sci-fi you can fins", message_id=731)
    )
    assert result.reply, "plot/typo ask must not be silent"
    assert result.grabbed is False
    assert "1–1" not in result.reply
    assert "Dune" in result.reply
    assert "Did you mean" in result.reply or "?" in result.reply

    # Pure ignore with no guess still must reply (useful question), not silence.
    inbox.reset()
    _patch_openai_intent(
        monkeypatch,
        {"action": "ignore", "confidence": 0.95},
    )
    again = await inbox.handle_message(
        _msg("The coolest sci-fi you can fins", message_id=732)
    )
    assert again.reply
    assert again.grabbed is False
    assert "1–1" not in again.reply


# --- elongated yes + recommend follow-up (live Telegram 2026-08-31) ----------


def test_looks_like_confirm_yes_accepts_elongated_and_thumbs():
    from hearth.telegram.intent import looks_like_confirm_yes, instant_pick_decision

    for text in (
        "yes",
        "Yes!",
        "yess",
        "Yesss",
        "yessss",
        "yeess",
        "yeahhh",
        "yup",
        "yep",
        "ja",
        "jaaa",
        "jawel",
        "klopt",
        "juist",
        "that's it",
        "that's the one",
        "doe maar",
        "go ahead",
        "👍",
    ):
        assert looks_like_confirm_yes(text), text

    for text in ("no", "no not that", "another one", "Event Horizon", "y"):
        assert not looks_like_confirm_yes(text), text

    row = [{"title": "Event Horizon", "year": 1997, "tmdbId": 8413}]
    for text in ("Yesss", "yess", "yes!", "jaaa", "👍"):
        decision = instant_pick_decision(text, row)
        assert decision is not None
        assert decision.action == "pick"
        assert decision.indices == [1]


def test_looks_like_confirm_no_and_list_ask():
    from hearth.telegram.intent import (
        looks_like_confirm_no,
        looks_like_confirm_yes,
        looks_like_list_ask,
        looks_like_media_ask,
        looks_like_recommend_ask,
        instant_pick_decision,
    )

    for text in (
        "Nah",
        "nah",
        "No",
        "no",
        "Nope",
        "nope",
        "nee",
        "Nee",
        "niet",
        "niet die",
        "not that",
        "no not that",
        "anders",
        "no thanks",
    ):
        assert looks_like_confirm_no(text), text
        assert not looks_like_confirm_yes(text), text

    for text in ("yes", "Yep", "another one", "The Matrix", "Niet the imitation game"):
        assert not looks_like_confirm_no(text), text

    row = [{"title": "The Elephant Man", "year": 1980, "tmdbId": 10934}]
    for text in ("Nah", "No", "nope", "nee"):
        assert instant_pick_decision(text, row) is None

    for text in (
        "Name a few more",
        "name a few",
        "a few more",
        "give me options",
        "give me a few options",
        "een paar meer",
    ):
        assert looks_like_list_ask(text), text
        assert looks_like_recommend_ask(text), text

    assert not looks_like_list_ask("Yep")
    assert not looks_like_list_ask("A cool scifi movie")
    # Seed look-alike keeps Land reuse — not the multi-guess list path.
    assert not looks_like_list_ask("Name a few that look like Land")
    assert not looks_like_recommend_ask("Name a few that look like Land")
    assert looks_like_media_ask("Name a few that look like Land")


def test_looks_like_recommend_ask_and_not_title():
    from hearth.telegram.intent import (
        looks_like_concrete_title,
        looks_like_media_ask,
        looks_like_recommend_ask,
    )

    for text in (
        "Do you know another one?",
        "Another horror in space",
        "I don't know, find one",
        "surprise me",
        "more like that",
        "another one",
        "find one",
    ):
        assert looks_like_recommend_ask(text), text
        assert looks_like_media_ask(text), text
        assert not looks_like_concrete_title(text), text

    assert not looks_like_recommend_ask("Another Earth")
    assert looks_like_concrete_title("Another Earth")


def test_parse_model_json_recommend_never_send_title_fallback():
    from hearth.telegram.intent import _parse_model_json

    for msg in (
        "Do you know another one?",
        "Another horror in space",
        "I don't know, find one",
    ):
        empty = _parse_model_json(
            '{"action":"clarify","confidence":0.7}',
            candidate_count=0,
            user_message=msg,
        )
        assert empty is not None
        assert "send the title if you know it" not in (empty.clarify_question or "").lower()

        ignored = _parse_model_json(
            '{"action":"ignore","confidence":0.9}',
            candidate_count=0,
            user_message=msg,
        )
        assert ignored is not None
        assert ignored.action in {"clarify", "search"}
        assert "send the title if you know it" not in (ignored.clarify_question or "").lower()

        guessed = _parse_model_json(
            '{"action":"clarify","search_title":"Life","year":2017,'
            '"media_kind":"movie","confidence":0.7}',
            candidate_count=0,
            user_message=msg,
            rejected_titles=["Alien", "Event Horizon"],
        )
        assert guessed is not None
        assert guessed.action == "search"
        assert guessed.search_title == "Life"
        assert guessed.year == 2017


@pytest.mark.asyncio
async def test_inbox_yesss_confirms_pending_event_horizon_not_die_ruckkehr(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: Yesss missed confirm → pivoted → queued Die Rückkehr."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    event_horizon = {
        "title": "Event Horizon",
        "year": 1997,
        "tmdbId": 8413,
        "mediaId": 8413,
        "mediaType": "movie",
    }
    die_ruckkehr = {
        "title": "Die Rückkehr",
        "year": 2026,
        "tmdbId": 999001,
        "mediaId": 999001,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "event" in q or "horizon" in q or "8413" in q:
            return {"mode": "mock", "service": "overseerr", "results": [event_horizon]}
        # Tempting wrong hit if fuzzy search runs on "Yesss" / model pivot.
        return {"mode": "mock", "service": "overseerr", "results": [die_ruckkehr]}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    # If confirm misses, the model hop would search "Yesss" and grab Die Rückkehr.
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Die Rückkehr",
            "year": 2026,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )

    option = {
        "title": "Event Horizon",
        "year": 1997,
        "mediaType": "movie",
        "tmdbId": 8413,
        "mediaId": 8413,
    }
    for i, text in enumerate(("Yesss", "yess", "yes!", "jaaa")):
        pipeline.overseerr_queue.clear()
        overseerr_requests.clear()
        inbox.pending[-1001] = PendingDisambiguation(
            chat_id=-1001,
            options=[dict(option)],
            media_kind="movie",
            query="Event Horizon",
            created_message_id=800 + i,
            last_bot_reply="Did you mean Event Horizon (1997)?",
        )
        inbox.memory.set_subject(
            -1001, "Event Horizon", media_kind="movie", offered=[dict(option)]
        )
        before_calls = len(calls)
        yes = await inbox.handle_message(_msg(text, message_id=801 + i))
        assert yes.grabbed is True, (text, yes.reply)
        assert "Event Horizon" in yes.reply
        assert "1997" in yes.reply
        assert "Die Rückkehr" not in yes.reply
        assert "Alien" not in yes.reply
        assert overseerr_requests
        assert overseerr_requests[-1]["media_id"] == 8413
        assert len(calls) == before_calls  # instant — no model hop
        assert pipeline.overseerr_queue


@pytest.mark.asyncio
async def test_inbox_plain_yes_still_confirms_guess(inbox: TelegramInbox, monkeypatch):
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    event_horizon = {
        "title": "Event Horizon",
        "year": 1997,
        "tmdbId": 8413,
        "mediaId": 8413,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "event" in q or "horizon" in q or "8413" in q:
            return {"mode": "mock", "service": "overseerr", "results": [event_horizon]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Event Horizon",
            "year": 1997,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    ask = await inbox.handle_message(_msg("horror on a spaceship", message_id=810))
    assert "Event Horizon" in ask.reply
    yes = await inbox.handle_message(_msg("yes", message_id=811))
    assert yes.grabbed is True
    assert "Event Horizon" in yes.reply
    assert overseerr_requests[-1]["media_id"] == 8413


@pytest.mark.asyncio
async def test_inbox_no_not_that_still_rejects_guess(inbox: TelegramInbox, monkeypatch):
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    event_horizon = {
        "title": "Event Horizon",
        "year": 1997,
        "tmdbId": 8413,
        "mediaId": 8413,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "event" in q or "horizon" in q:
            return {"mode": "mock", "service": "overseerr", "results": [event_horizon]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "search",
                "search_title": "Event Horizon",
                "year": 1997,
                "media_kind": "movie",
                "confidence": 0.9,
            },
            {
                "action": "clarify",
                "clarify_question": "Ok — any other clue (year, actor)?",
                "confidence": 0.8,
            },
        ],
    )
    ask = await inbox.handle_message(_msg("old horror on a spaceship", message_id=820))
    assert "Event Horizon" in ask.reply
    before = len(pipeline.overseerr_queue)
    no = await inbox.handle_message(_msg("no not that", message_id=821))
    assert no.grabbed is False
    assert len(pipeline.overseerr_queue) == before
    assert "Queued" not in no.reply


@pytest.mark.asyncio
async def test_inbox_another_horror_followups_guess_not_send_title(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: after wrong queue, 'another one' / find-one got empty-title fallback."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    event_horizon = {
        "title": "Event Horizon",
        "year": 1997,
        "tmdbId": 8413,
        "mediaId": 8413,
        "mediaType": "movie",
    }
    alien = {
        "title": "Alien",
        "year": 1979,
        "tmdbId": 348,
        "mediaId": 348,
        "mediaType": "movie",
    }
    life = {
        "title": "Life",
        "year": 2017,
        "tmdbId": 395992,
        "mediaId": 395992,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "life" in q or "395992" in q:
            return {"mode": "mock", "service": "overseerr", "results": [life]}
        if "event" in q or "horizon" in q or "8413" in q:
            return {"mode": "mock", "service": "overseerr", "results": [event_horizon]}
        if "alien" in q:
            return {"mode": "mock", "service": "overseerr", "results": [alien]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    # Seed: Alien was queued earlier; Event Horizon confirm then Yesss.
    inbox.memory.record_user(-1001, "Yes!")
    inbox.memory.record_bot(
        -1001,
        "Queued Alien (1979) via Overseerr.",
        search_title="Alien",
        media_kind="movie",
        offered=[],
    )
    inbox.memory.remember_rejected(-1001, ["Alien"])
    assert "Alien" in inbox.memory.rejected(-1001)

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Event Horizon",
            "year": 1997,
            "media_kind": "movie",
            "confidence": 0.92,
        },
    )
    ask = await inbox.handle_message(
        _msg("Ok, another old horror movie on a spaceship", message_id=830)
    )
    assert "Event Horizon" in ask.reply
    yes = await inbox.handle_message(_msg("Yesss", message_id=831))
    assert yes.grabbed is True
    assert "Event Horizon" in yes.reply
    rejected = {t.lower() for t in inbox.memory.rejected(-1001)}
    assert "alien" in rejected
    assert "event horizon" in rejected

    followups = (
        "Do you know another one?",
        "Another horror in space",
        "I don't know, find one",
    )
    for i, text in enumerate(followups):
        calls = _patch_openai_intent(
            monkeypatch,
            {
                "action": "search",
                "search_title": "Life",
                "year": 2017,
                "media_kind": "movie",
                "confidence": 0.88,
            },
        )
        result = await inbox.handle_message(_msg(text, message_id=840 + i))
        assert result.grabbed is False, (text, result.reply)
        assert "Did you mean" in result.reply or "?" in result.reply
        assert "Life" in result.reply
        assert "2017" in result.reply
        assert "Send the title if you know it" not in result.reply
        assert "Alien" not in result.reply
        assert "Event Horizon" not in result.reply
        # Rejected list must reach the model so it does not re-offer prior titles.
        assert calls
        import json as _json

        payload = _json.loads(calls[0]["messages"][1]["content"])
        rejected_payload = [
            str(t).lower() for t in (payload.get("rejected_titles") or [])
        ]
        assert any("alien" in t for t in rejected_payload)
        assert any("event horizon" in t for t in rejected_payload)
        # Clear pending so next follow-up is not an instant yes on Life.
        inbox.pending.pop(-1001, None)
        inbox.memory.clear_offered(-1001)


# --- Live Telegram 2026-08-31: Sure. Bring it / Download pandorum / find another ---


@pytest.mark.asyncio
async def test_inbox_sure_bring_it_confirms_pending_pandorum_via_model(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: 'Sure. Bring it' missed confirm → empty-title fallback.

    Instant path stays tiny (bare yes only). Model-shaped pick of the live
    1-item pending must queue that tmdbId — never clear pending first.
    """
    import json as _json

    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    pandorum = {
        "title": "Pandorum",
        "year": 2009,
        "tmdbId": 19899,
        "mediaId": 19899,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "pandorum" in q or "19899" in q:
            return {"mode": "mock", "service": "overseerr", "results": [pandorum]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    # Model sees live pending + last_bot_reply and confirms the on-screen guess.
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "pick", "indices": [1], "confidence": 0.95},
    )

    option = dict(pandorum)
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="Pandorum",
        created_message_id=900,
        last_bot_reply="Did you mean Pandorum (2009)?",
    )
    inbox.memory.set_subject(
        -1001, "Pandorum", media_kind="movie", offered=[option]
    )
    inbox.memory.record_user(-1001, "We already have that. Find another")
    inbox.memory.record_bot(
        -1001,
        "Did you mean Pandorum (2009)?",
        search_title="Pandorum",
        media_kind="movie",
        offered=[option],
    )

    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(_msg("Sure. Bring it", message_id=901))
    assert result.grabbed is True, result.reply
    assert "Pandorum" in result.reply
    assert "2009" in result.reply
    assert "Send the title if you know it" not in result.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 19899
    assert calls  # went through gpt-4o (not instant bare-yes)
    payload = _json.loads(calls[0]["messages"][1]["content"])
    assert payload["candidates"]
    assert "Pandorum" in str(payload["candidates"][0].get("title") or "")
    assert "Did you mean Pandorum" in (payload.get("last_bot_reply") or "")
    assert "Sure. Bring it" in (payload.get("user_message") or "")


@pytest.mark.asyncio
async def test_inbox_download_pandorum_queues_live_pending_via_model(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: 'Download pandorum' after pending wiped → empty-title fallback.

    With live pending still on screen, model search/pick must queue Pandorum —
    never the empty-title template.
    """
    import json as _json

    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    pandorum = {
        "title": "Pandorum",
        "year": 2009,
        "tmdbId": 19899,
        "mediaId": 19899,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "pandorum" in q or "19899" in q:
            return {"mode": "mock", "service": "overseerr", "results": [pandorum]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    # Model names the pending title — inbox reconciles search→pick on that row.
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Pandorum",
            "year": 2009,
            "media_kind": "movie",
            "confidence": 0.92,
        },
    )

    option = dict(pandorum)
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="Pandorum",
        created_message_id=910,
        last_bot_reply="Did you mean Pandorum (2009)?",
    )
    inbox.memory.set_subject(
        -1001, "Pandorum", media_kind="movie", offered=[option]
    )

    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(_msg("Download pandorum", message_id=911))
    assert result.grabbed is True, result.reply
    assert "Pandorum" in result.reply
    assert "Send the title if you know it" not in result.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 19899
    assert calls
    payload = _json.loads(calls[0]["messages"][1]["content"])
    assert payload["candidates"]
    assert "Pandorum" in str(payload["candidates"][0].get("title") or "")


@pytest.mark.asyncio
async def test_inbox_we_already_have_that_find_another_new_guess(
    inbox: TelegramInbox, monkeypatch
):
    """After Alien guess, reject+find-another → new different guess-ask."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    alien = {
        "title": "Alien",
        "year": 1979,
        "tmdbId": 348,
        "mediaId": 348,
        "mediaType": "movie",
    }
    pandorum = {
        "title": "Pandorum",
        "year": 2009,
        "tmdbId": 19899,
        "mediaId": 19899,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "pandorum" in q or "19899" in q:
            return {"mode": "mock", "service": "overseerr", "results": [pandorum]}
        if "alien" in q or "348" in q:
            return {"mode": "mock", "service": "overseerr", "results": [alien]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Pandorum",
            "year": 2009,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )

    option = dict(alien)
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="Alien",
        created_message_id=920,
        last_bot_reply="Did you mean Alien (1979)?",
    )
    inbox.memory.set_subject(-1001, "Alien", media_kind="movie", offered=[option])
    inbox.memory.record_user(-1001, "Find one")
    inbox.memory.record_bot(
        -1001,
        "Did you mean Alien (1979)?",
        search_title="Alien",
        media_kind="movie",
        offered=[option],
    )

    result = await inbox.handle_message(
        _msg("We already have that. Find another", message_id=921)
    )
    assert result.grabbed is False, result.reply
    assert "Did you mean" in result.reply or "?" in result.reply
    assert "Pandorum" in result.reply
    assert "2009" in result.reply
    assert "Alien" not in result.reply
    assert "Send the title if you know it" not in result.reply
    assert calls
    # Pending was live for the model hop (not pivot-cleared first).
    import json as _json

    payload = _json.loads(calls[0]["messages"][1]["content"])
    assert payload["candidates"]
    assert "Alien" in str(payload["candidates"][0].get("title") or "")
    assert "Did you mean Alien" in (payload.get("last_bot_reply") or "")
    # After model chose a new search, Alien is rejected and Pandorum is pending.
    rejected = {t.lower() for t in inbox.memory.rejected(-1001)}
    assert "alien" in rejected
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert "Pandorum" in str(pending.options[0].get("title") or "")


def test_looks_like_confirm_yes_still_rejects_sure_bring_it():
    """Instant yes stays tiny — 'Sure. Bring it' must go through the model."""
    from hearth.telegram.intent import looks_like_confirm_yes, instant_pick_decision

    assert looks_like_confirm_yes("sure")
    assert looks_like_confirm_yes("Sure.")
    assert not looks_like_confirm_yes("Sure. Bring it")
    assert not looks_like_confirm_yes("Download pandorum")
    # No instant pick for multi-word confirm even with pending on screen.
    pending = [{"title": "Pandorum", "year": 2009, "tmdbId": 19899}]
    assert instant_pick_decision("Sure. Bring it", pending) is None
    assert instant_pick_decision("sure", pending) is not None
    assert instant_pick_decision("sure", pending).action == "pick"


def test_parse_model_json_never_empty_title_when_candidates_live():
    """Clarify with candidates/context must not emit the empty-title template."""
    from hearth.telegram.intent import _parse_model_json

    candidates = [
        {"title": "Pandorum", "year": 2009, "tmdbId": 19899, "mediaType": "movie"}
    ]
    parsed = _parse_model_json(
        '{"action":"clarify","clarify_question":'
        '"Which movie or series did you mean? Send the title if you know it.",'
        '"confidence":0.5}',
        candidate_count=1,
        user_message="Sure. Bring it",
        candidates=candidates,
        has_context=True,
    )
    assert parsed is not None
    # Single candidate + empty-title clarify → promote to search that row.
    assert parsed.action in {"search", "clarify"}
    if parsed.action == "clarify":
        assert "send the title if you know it" not in (
            parsed.clarify_question or ""
        ).lower()
    else:
        assert parsed.search_title == "Pandorum"


# --- gpt-4o whole-message (actor / plot / year) — live Movies & Series bugs ---


def test_catalog_seed_matches_rejects_substring_titles():
    from hearth.telegram.catalog import catalog_seed_matches_title

    assert catalog_seed_matches_title("Land", "Land")
    assert not catalog_seed_matches_title("Land", "La La Land")
    assert not catalog_seed_matches_title("Land", "Cop Land")
    assert not catalog_seed_matches_title("Wild", "The Wild Robot")
    assert not catalog_seed_matches_title("Wild", "Wild Awakening")
    assert catalog_seed_matches_title("Wild", "Wild")
    assert catalog_seed_matches_title(
        "Harry Potter", "Harry Potter and the Chamber of Secrets"
    )


def test_looks_like_concrete_title_rejects_plot_shell():
    from hearth.telegram.intent import looks_like_concrete_title, search_title_grounded

    nose = "That movie with the guy with that weird nose"
    assert not looks_like_concrete_title(nose)
    assert search_title_grounded("Cyrano", user_message=nose, candidates=None)


def test_parse_model_json_plot_clue_not_vibe_template():
    from hearth.telegram.intent import CONTEXT_CLUE_CLARIFY, SOFT_CONTEXT_CLARIFY, _parse_model_json

    nose = "That movie with the guy with that weird nose"
    parsed = _parse_model_json(
        '{"action":"clarify","clarify_question":'
        '"Want another in that vibe? Any year, actor, or other clue?",'
        '"confidence":0.5}',
        candidate_count=0,
        user_message=nose,
        has_context=True,
    )
    assert parsed is not None
    assert parsed.action == "clarify"
    assert "want another in that vibe" not in (parsed.clarify_question or "").lower()
    assert "send the title if you know it" not in (parsed.clarify_question or "").lower()
    assert parsed.clarify_question == CONTEXT_CLUE_CLARIFY
    assert parsed.clarify_question != SOFT_CONTEXT_CLARIFY


@pytest.mark.asyncio
async def test_inbox_land_with_robin_wright_not_la_la_land(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: actor clue ignored; La La Land / Cop Land listed for Land."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    land = {
        "title": "Land",
        "year": 2021,
        "tmdbId": 688271,
        "mediaId": 688271,
        "mediaType": "movie",
    }
    la_la = {
        "title": "La La Land",
        "year": 2016,
        "tmdbId": 313369,
        "mediaId": 313369,
        "mediaType": "movie",
    }
    cop = {
        "title": "Cop Land",
        "year": 1997,
        "tmdbId": 9470,
        "mediaId": 9470,
        "mediaType": "movie",
    }
    soul = {
        "title": "Soul Land",
        "year": 2018,
        "tmdbId": 90001,
        "mediaId": 90001,
        "mediaType": "tv",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        # Substring noise Overseerr would return for "Land".
        if q.strip() == "land":
            return {
                "mode": "mock",
                "service": "overseerr",
                "results": [soul, la_la, land, cop],
            }
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Land",
            "year": 2021,
            "media_kind": "movie",
            "people": ["Robin Wright"],
            "confidence": 0.95,
        },
    )

    # Sticky wrong menu from a prior fuzzy first-token path (live failure shape).
    from hearth.telegram.inbox import PendingDisambiguation

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[soul, la_la],
        media_kind="movie",
        query="Land",
        created_message_id=8000,
        last_bot_reply=(
            "Which one for 'Land'? Reply 1-2:\n"
            "1. Soul Land (2018)\n2. La La Land (2016)"
        ),
    )
    inbox.memory.record_bot(
        -1001,
        "Which one for 'Land'? Reply 1-2:\n1. Soul Land (2018)\n2. La La Land (2016)",
        search_title="Land",
        media_kind="movie",
        offered=[soul, la_la],
    )

    # Exact Land still wins a bare-title resolve (no La La Land / Cop Land).
    from hearth.telegram.catalog import resolve_title

    hits = await resolve_title("Land")
    assert [h.title for h in hits] == ["Land"]
    assert hits[0].year == 2021

    result = await inbox.handle_message(
        _msg("Land with robin wright", message_id=8002)
    )
    assert result.grabbed is True or "Did you mean" in result.reply or "Queued" in result.reply
    assert "La La Land" not in result.reply
    assert "Cop Land" not in result.reply
    assert "Soul Land" not in result.reply
    assert "Land" in result.reply
    assert "2021" in result.reply
    assert calls
    import json as _json

    payload = _json.loads(calls[-1]["messages"][1]["content"])
    assert "robin" in payload["user_message"].lower()
    # Sticky list may be visible to the model; it must still search Land+actor.
    assert payload.get("user_message")


@pytest.mark.asyncio
async def test_inbox_weird_nose_plot_guess_confirm_not_vibe(
    inbox: TelegramInbox, monkeypatch
):
    """After clarify-ask, plot/appearance clue → named guess, not vibe template."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "cyrano" in q:
            return {
                "mode": "mock",
                "service": "overseerr",
                "results": [
                    {
                        "title": "Cyrano",
                        "year": 2021,
                        "tmdbId": 644479,
                        "mediaId": 644479,
                        "mediaType": "movie",
                    }
                ],
            }
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    # Prior bot ask (empty title clarify) so history/context exists.
    inbox.memory.record_bot(
        -1001,
        "Which movie or series did you mean? Send the title if you know it.",
    )

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Cyrano",
            "year": 2021,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    result = await inbox.handle_message(
        _msg("That movie with the guy with that weird nose", message_id=8100)
    )
    assert result.grabbed is False
    assert "Did you mean" in result.reply
    assert "Cyrano" in result.reply
    assert "send the title if you know it" not in result.reply.lower()
    assert "want another in that vibe" not in result.reply.lower()
    assert "1–1" not in result.reply and "1-1" not in result.reply


@pytest.mark.asyncio
async def test_inbox_numbered_menu_always_lists_titles(
    inbox: TelegramInbox, monkeypatch
):
    """Never send a numbered prompt without the actual Title (year) rows."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.progress import format_ambiguous

    heat_a = {"title": "Heat", "year": 1995, "tmdbId": 949, "mediaType": "movie"}
    heat_b = {"title": "Heat", "year": 1986, "tmdbId": 10784, "mediaType": "movie"}

    async def _search(query: str, *args, **kwargs):
        if "heat" in (query or "").lower():
            return {"mode": "mock", "service": "overseerr", "results": [heat_a, heat_b]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Heat",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(_msg("Heat", message_id=8200))
    assert "1." in result.reply
    assert "Heat" in result.reply
    assert "1995" in result.reply or "1986" in result.reply
    # 1-item pending is guess-confirm, never list-less 1–1.
    one = format_ambiguous("Alien", [{"title": "Alien", "year": 1979}])
    assert "Did you mean" in one
    assert "1–1" not in one and "1-1" not in one


@pytest.mark.asyncio
async def test_inbox_wild_reese_witherspoon_not_wild_robot(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: first-token Wild → Wild Robot; actor ignored."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    wild = {
        "title": "Wild",
        "year": 2014,
        "tmdbId": 228150,
        "mediaId": 228150,
        "mediaType": "movie",
    }
    awakening = {
        "title": "Wild Awakening",
        "year": 2016,
        "tmdbId": 400001,
        "mediaId": 400001,
        "mediaType": "movie",
    }
    robot = {
        "title": "The Wild Robot",
        "year": 2024,
        "tmdbId": 1184918,
        "mediaId": 1184918,
        "mediaType": "movie",
    }

    searches: list[str] = []

    async def _search(query: str, *args, **kwargs):
        searches.append(str(query))
        q = (query or "").lower().strip()
        if q == "wild":
            return {
                "mode": "mock",
                "service": "overseerr",
                "results": [awakening, robot, wild],
            }
        if "reese" in q or "witherspoon" in q:
            # Full-blob search must not be what pins the menu.
            return {
                "mode": "mock",
                "service": "overseerr",
                "results": [awakening, robot],
            }
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Wild",
            "year": 2014,
            "media_kind": "movie",
            "people": ["Reese Witherspoon"],
            "confidence": 0.96,
        },
    )
    result = await inbox.handle_message(
        _msg("Wild reese witherspoon", message_id=8300)
    )
    assert "Wild Robot" not in result.reply
    assert "Wild Awakening" not in result.reply
    assert "Wild" in result.reply
    assert "2014" in result.reply
    assert result.grabbed is True or "Did you mean" in result.reply


@pytest.mark.asyncio
async def test_inbox_wild_reese_witherspoon_2014(
    inbox: TelegramInbox, monkeypatch
):
    """Year + actor must resolve to Wild (2014), not Wild Robot."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    wild = {
        "title": "Wild",
        "year": 2014,
        "tmdbId": 228150,
        "mediaId": 228150,
        "mediaType": "movie",
    }
    robot = {
        "title": "The Wild Robot",
        "year": 2024,
        "tmdbId": 1184918,
        "mediaId": 1184918,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower().strip()
        if q == "wild":
            return {"mode": "mock", "service": "overseerr", "results": [robot, wild]}
        return {"mode": "mock", "service": "overseerr", "results": [robot]}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Wild",
            "year": 2014,
            "media_kind": "movie",
            "people": ["Reese Witherspoon"],
            "confidence": 0.97,
        },
    )
    result = await inbox.handle_message(
        _msg("Wild reese witherspoon 2014", message_id=8301)
    )
    assert "Wild Robot" not in result.reply
    assert "2014" in result.reply
    assert "Wild" in result.reply
    assert result.grabbed is True or "Did you mean" in result.reply


@pytest.mark.asyncio
async def test_inbox_eat_pray_love_no_404_after_model_resolve(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: model named Eat Pray Love (2010) then catalog 404'd."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    eat = {
        "title": "Eat Pray Love",
        "year": 2010,
        "tmdbId": 38050,
        "mediaId": 38050,
        "mediaType": "movie",
    }

    async def _search_miss(query: str, *args, **kwargs):
        # Simulate Overseerr miss even for the canonical title.
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search_miss)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search_miss)

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Eat Pray Love",
            "year": 2010,
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    result = await inbox.handle_message(_msg("Eat pray love", message_id=8400))
    assert "Couldn't find" not in result.reply
    assert "IMDb" not in result.reply and "TMDB" not in result.reply.upper().replace(
        "TMDBID", ""
    )
    assert "Eat Pray Love" in result.reply or "Eat pray love" in result.reply
    assert "2010" in result.reply
    assert "Did you mean" in result.reply or result.grabbed is True

    # When catalog does have it, unique grab (or confirm) — still no 404.
    async def _search_hit(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "eat" in q and "pray" in q:
            return {"mode": "mock", "service": "overseerr", "results": [eat]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search_hit)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search_hit)
    inbox.deduper.reset()
    inbox.pending.clear()
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Eat Pray Love",
            "year": 2010,
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    hit = await inbox.handle_message(_msg("Eat pray love", message_id=8401))
    assert "Couldn't find" not in hit.reply
    assert "Eat Pray Love" in hit.reply or hit.grabbed


# --- Squid / anaphoric follow-up (live Telegram 2026-08-31) -------------------


@pytest.mark.asyncio
async def test_inbox_squid_and_the_whale_thread_no_send_link(
    inbox: TelegramInbox, monkeypatch
):
    """Live Movies & Series bug: Squid miss → send-link; find-that → any year?

    Replay the exact thread. Catalog empty on first ask (model already named the
    title) must confirm, not 'send a link'. Anaphoric 'Find a title that matches
    that' must reuse the prior title — never 'Any year, actor, or other clue?'.
    """
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.intent import CONTEXT_CLUE_CLARIFY

    squid = {
        "title": "The Squid and the Whale",
        "year": 2005,
        "tmdbId": 116,
        "mediaId": 116,
        "mediaType": "movie",
    }

    async def _search_empty(query: str, *args, **kwargs):
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search_empty)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search_empty)

    # 1) Bare title — model names it, catalog empty → confirm (not send-link).
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Squid and the Whale",
            "year": 2005,
            "media_kind": "movie",
            "confidence": 0.92,
        },
    )
    first = await inbox.handle_message(
        _msg("The squid and the whale", message_id=8500)
    )
    assert "Couldn't find" not in first.reply
    assert "IMDb" not in first.reply
    assert "send" not in first.reply.lower() or "Did you mean" in first.reply
    assert "Squid" in first.reply
    assert "2005" in first.reply
    assert "Did you mean" in first.reply or first.grabbed

    # 2) Anaphoric follow-up — even if the model wrongly clarifies, reuse prior.
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": CONTEXT_CLUE_CLARIFY,
            "confidence": 0.4,
        },
    )
    # Clear live pending so this matches the live 404 shape (no on-screen guess).
    inbox.pending.clear()
    inbox.memory.clear_offered(-1001)

    follow = await inbox.handle_message(
        _msg("Find a title that matches that", message_id=8501)
    )
    assert follow.reply != CONTEXT_CLUE_CLARIFY
    assert "Any year, actor, or other clue" not in follow.reply
    assert "send the title if you know it" not in follow.reply.lower()
    assert "Squid" in follow.reply or follow.grabbed

    # 3) User repeats the title — still no send-link template.
    async def _search_hit(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "squid" in q:
            return {"mode": "mock", "service": "overseerr", "results": [squid]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search_hit)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search_hit)
    inbox.pending.clear()
    inbox.deduper.reset()
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Squid and the Whale",
            "year": 2005,
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    again = await inbox.handle_message(
        _msg("The squid and the whale", message_id=8502)
    )
    assert "Couldn't find" not in again.reply
    assert "IMDb" not in again.reply
    assert "Squid" in again.reply or again.grabbed
    assert again.grabbed is True or "Did you mean" in again.reply


@pytest.mark.asyncio
async def test_inbox_find_title_that_matches_that_reuses_prior(
    inbox: TelegramInbox, monkeypatch
):
    """After a titled miss, 'find a title that matches that' searches the prior."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.intent import CONTEXT_CLUE_CLARIFY

    squid = {
        "title": "The Squid and the Whale",
        "year": 2005,
        "tmdbId": 116,
        "mediaId": 116,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "squid" in q:
            return {"mode": "mock", "service": "overseerr", "results": [squid]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    # Seed history as if the prior ask already named Squid (bot missed).
    inbox.memory.record_user(-1001, "The squid and the whale")
    inbox.memory.record_bot(
        -1001,
        "Couldn't find a match for 'The Squid and the Whale'. Send an IMDb/TMDB link?",
        search_title="The Squid and the Whale",
        media_kind="movie",
    )
    inbox.memory.set_subject(-1001, "The Squid and the Whale", media_kind="movie")

    # Model wrongly asks for clues — inbox must still reuse Squid.
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": CONTEXT_CLUE_CLARIFY,
            "confidence": 0.35,
        },
    )
    result = await inbox.handle_message(
        _msg("Find a title that matches that", message_id=8510)
    )
    assert calls, "gpt-4o must still see the follow-up"
    payload = __import__("json").loads(calls[0]["messages"][1]["content"])
    assert payload["user_message"]
    assert payload.get("recent_history") or payload.get("subject_title")
    assert "Any year, actor, or other clue" not in result.reply
    assert result.reply != CONTEXT_CLUE_CLARIFY
    assert "Squid" in result.reply or result.grabbed is True
    assert result.grabbed is True or "Did you mean" in result.reply


@pytest.mark.asyncio
async def test_inbox_made_up_exact_title_catalog_hit(
    inbox: TelegramInbox, monkeypatch
):
    """Real-sounding exact title present in catalog → queue or confirm, no 404."""
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Cedar Hollow Signal",
            "year": 2019,
            "media_kind": "movie",
            "confidence": 0.96,
        },
    )
    result = await inbox.handle_message(
        _msg("Cedar Hollow Signal", message_id=8520)
    )
    assert "Couldn't find" not in result.reply
    assert "IMDb" not in result.reply
    assert "Cedar Hollow Signal" in result.reply or result.grabbed
    assert result.grabbed is True or "Did you mean" in result.reply


def test_looks_like_concrete_title_rejects_anaphoric_find_match():
    from hearth.telegram.intent import looks_like_concrete_title

    assert not looks_like_concrete_title("Find a title that matches that")
    assert not looks_like_concrete_title("zoek die film die ik net noemde")
    assert looks_like_concrete_title("The squid and the whale")
    assert looks_like_concrete_title("Cedar Hollow Signal")


# --- Land / La La Land confirm thread (live Telegram 2026-08-31 23:30–23:32) ---


@pytest.mark.asyncio
async def test_inbox_land_thread_yes_duh_queues_land_not_la_la_land(
    inbox: TelegramInbox, monkeypatch
):
    """Live Movies & Series bug: Did you mean Land (2021)? → Yes... duh → La La Land.

    Replay the critical confirm. Pending must stay Land (2021). Yes / Yes... duh
    must queue THAT title+year via catalog_seed_matches — never La La Land even
    when Overseerr substring-noise returns it for 'Land'.
    """
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation
    from hearth.telegram.intent import CONTEXT_CLUE_CLARIFY

    land = {
        "title": "Land",
        "year": 2021,
        "tmdbId": 688271,
        "mediaId": 688271,
        "mediaType": "movie",
    }
    la_la = {
        "title": "La La Land",
        "year": 2016,
        "tmdbId": 313369,
        "mediaId": 313369,
        "mediaType": "movie",
    }
    cop = {
        "title": "Cop Land",
        "year": 1997,
        "tmdbId": 9470,
        "mediaId": 9470,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower().strip()
        if q == "land" or "688271" in q:
            # Overseerr substring noise — exact helper must keep only Land.
            return {
                "mode": "mock",
                "service": "overseerr",
                "results": [la_la, land, cop],
            }
        if "la la" in q:
            return {"mode": "mock", "service": "overseerr", "results": [la_la]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    # Seed: bot already asked Did you mean Land (2021)? (plot path succeeded).
    option = {
        "title": "Land",
        "year": 2021,
        "mediaType": "movie",
        # No tmdbId — forces title+year resolve path that used to pick La La Land.
    }
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="Land",
        created_message_id=8600,
        last_bot_reply="Did you mean Land (2021)?",
    )
    inbox.memory.set_subject(
        -1001, "Land", media_kind="movie", offered=[option]
    )
    inbox.memory.record_user(
        -1001, "Edee, in the aftermath of an unfathomable event"
    )
    inbox.memory.record_bot(
        -1001,
        "Did you mean Land (2021)?",
        search_title="Land",
        media_kind="movie",
        offered=[option],
    )

    pipeline.overseerr_queue.clear()

    # Model confirms the on-screen guess (full thread + pending — no phrase list).
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "pick", "indices": [1], "confidence": 0.95},
    )
    yes = await inbox.handle_message(_msg("Yes... duh", message_id=8601))
    assert calls, "gpt-4o must see Yes... duh with full thread"
    payload = __import__("json").loads(calls[0]["messages"][1]["content"])
    assert payload["user_message"] == "Yes... duh"
    assert payload.get("candidates_are_live_pending") is True or payload.get(
        "candidates"
    )
    assert yes.grabbed is True, yes.reply
    assert "Land" in yes.reply
    assert "2021" in yes.reply
    assert "La La Land" not in yes.reply
    assert CONTEXT_CLUE_CLARIFY not in yes.reply
    assert "Any year, actor, or other clue" not in yes.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 688271

    # Bare "yes" with the same pending shape (reset) also queues Land.
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[dict(option)],
        media_kind="movie",
        query="Land",
        created_message_id=8602,
        last_bot_reply="Did you mean Land (2021)?",
    )
    inbox.memory.set_subject(
        -1001, "Land", media_kind="movie", offered=[dict(option)]
    )
    pipeline.overseerr_queue.clear()
    overseerr_requests.clear()
    bare = await inbox.handle_message(_msg("yes", message_id=8603))
    assert bare.grabbed is True, bare.reply
    assert "Land" in bare.reply
    assert "2021" in bare.reply
    assert "La La Land" not in bare.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 688271


@pytest.mark.asyncio
async def test_inbox_land_thread_bare_land_and_lookalike_not_clue_template(
    inbox: TelegramInbox, monkeypatch
):
    """Bare 'Land' and 'Name a few that look like Land' must not clue-fish.

    gpt-4o sees the full thread; catalog exact/prefix keeps Land ≠ La La Land.
    Canned 'Any year, actor, or other clue?' / send-link are never the reply.
    """
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.intent import CONTEXT_CLUE_CLARIFY

    land = {
        "title": "Land",
        "year": 2021,
        "tmdbId": 688271,
        "mediaId": 688271,
        "mediaType": "movie",
    }
    la_la = {
        "title": "La La Land",
        "year": 2016,
        "tmdbId": 313369,
        "mediaId": 313369,
        "mediaType": "movie",
    }
    cop = {
        "title": "Cop Land",
        "year": 1997,
        "tmdbId": 9470,
        "mediaId": 9470,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower().strip()
        if q == "land" or "688271" in q:
            return {
                "mode": "mock",
                "service": "overseerr",
                "results": [la_la, land, cop],
            }
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    # Model wrongly clue-fishes on bare Land — inbox/parser must still resolve.
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": CONTEXT_CLUE_CLARIFY,
            "confidence": 0.4,
        },
    )
    first = await inbox.handle_message(_msg("Land", message_id=8700))
    assert calls, "gpt-4o must see bare Land"
    payload = __import__("json").loads(calls[0]["messages"][1]["content"])
    assert payload["user_message"] == "Land"
    assert first.reply != CONTEXT_CLUE_CLARIFY
    assert "Any year, actor, or other clue" not in first.reply
    assert "Send an IMDb" not in first.reply
    assert "send the title if you know it" not in first.reply.lower()
    assert "La La Land" not in first.reply
    assert "Cop Land" not in first.reply
    assert "Land" in first.reply
    assert first.grabbed is True or "Did you mean" in first.reply or "Queued" in first.reply

    # Follow-up look-alike ask — still no clue template; model + history for Land.
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "clarify",
            "clarify_question": CONTEXT_CLUE_CLARIFY,
            "confidence": 0.35,
        },
    )
    # Clear live pending so this matches a canned-template-only bot turn.
    inbox.pending.clear()
    look = await inbox.handle_message(
        _msg("Name a few that look like Land", message_id=8701)
    )
    assert look.reply != CONTEXT_CLUE_CLARIFY
    assert "Any year, actor, or other clue" not in look.reply
    assert "Send an IMDb" not in look.reply
    assert "La La Land" not in look.reply or "Land (2021)" in look.reply
    assert "Land" in look.reply or look.grabbed


@pytest.mark.asyncio
async def test_inbox_yes_duh_model_search_la_la_still_queues_pending_land(
    inbox: TelegramInbox, monkeypatch
):
    """If the model invents La La Land on confirm, pending Land (2021) still wins."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    land = {
        "title": "Land",
        "year": 2021,
        "tmdbId": 688271,
        "mediaId": 688271,
        "mediaType": "movie",
    }
    la_la = {
        "title": "La La Land",
        "year": 2016,
        "tmdbId": 313369,
        "mediaId": 313369,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower().strip()
        if "la la" in q:
            return {"mode": "mock", "service": "overseerr", "results": [la_la]}
        if q == "land" or "688271" in q:
            return {
                "mode": "mock",
                "service": "overseerr",
                "results": [la_la, land],
            }
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    option = dict(land)
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="Land",
        created_message_id=8800,
        last_bot_reply="Did you mean Land (2021)?",
    )
    inbox.memory.set_subject(
        -1001, "Land", media_kind="movie", offered=[option]
    )
    inbox.memory.record_bot(
        -1001,
        "Did you mean Land (2021)?",
        search_title="Land",
        media_kind="movie",
        offered=[option],
    )

    pipeline.overseerr_queue.clear()
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "La La Land",
            "year": 2016,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    result = await inbox.handle_message(_msg("Yes... duh", message_id=8801))
    assert result.grabbed is True, result.reply
    assert "Land" in result.reply
    assert "2021" in result.reply
    assert "La La Land" not in result.reply


# --- Movies & Series reject + list ask (live Telegram 2026-09-01 00:19) -------


@pytest.mark.asyncio
async def test_inbox_screenshot_nah_yep_few_more_no_thread(
    inbox: TelegramInbox, monkeypatch
):
    """Replay Ruben's 00:19 thread: Nah/No must not queue; few more is a list.

    1. Pending Elephant Man + Nah → never queue; pending cleared.
    2. Cool scifi → Blade Runner confirm; Yep → queue Blade Runner.
    3. Name a few more → numbered 2–4 list (not Did you mean The Matrix?).
    4. No on that list / Matrix pending → never queue The Matrix.
    """
    import re as _re

    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    elephant = {
        "title": "The Elephant Man",
        "year": 1980,
        "tmdbId": 10934,
        "mediaId": 10934,
        "mediaType": "movie",
    }
    blade = {
        "title": "Blade Runner",
        "year": 1982,
        "tmdbId": 78,
        "mediaId": 78,
        "mediaType": "movie",
    }
    matrix = {
        "title": "The Matrix",
        "year": 1999,
        "tmdbId": 603,
        "mediaId": 603,
        "mediaType": "movie",
    }
    arrival = {
        "title": "Arrival",
        "year": 2016,
        "tmdbId": 329865,
        "mediaId": 329865,
        "mediaType": "movie",
    }
    interstellar = {
        "title": "Interstellar",
        "year": 2014,
        "tmdbId": 157336,
        "mediaId": 157336,
        "mediaType": "movie",
    }
    dune = {
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "mediaId": 438631,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        results = []
        if "elephant" in q:
            results.append(elephant)
        if "blade" in q:
            results.append(blade)
        if "matrix" in q:
            results.append(matrix)
        if "arrival" in q:
            results.append(arrival)
        if "interstellar" in q:
            results.append(interstellar)
        if "dune" in q and "blade" not in q:
            results.append(dune)
        return {"mode": "mock", "service": "overseerr", "results": results}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    # --- 1) Nah must NOT queue The Elephant Man (even if model invents pick) ---
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[dict(elephant)],
        media_kind="movie",
        query="The Elephant Man",
        created_message_id=9000,
        last_bot_reply="Did you mean The Elephant Man (1980)?",
    )
    inbox.memory.set_subject(
        -1001, "The Elephant Man", media_kind="movie", offered=[elephant]
    )
    inbox.memory.record_bot(
        -1001,
        "Did you mean The Elephant Man (1980)?",
        search_title="The Elephant Man",
        media_kind="movie",
        offered=[elephant],
    )

    pipeline.overseerr_queue.clear()
    overseerr_requests.clear()
    # Worst case: model invents pick / ungrounded search on reject.
    _patch_openai_intent(
        monkeypatch,
        [
            {
                "action": "pick",
                "indices": [1],
                "confidence": 0.95,
            },
            {
                "action": "search",
                "search_title": "Blade Runner",
                "year": 1982,
                "media_kind": "movie",
                "confidence": 0.92,
            },
            # Yep is an instant confirm — no model hop / no payload here.
            {
                "action": "search",
                "search_title": "The Matrix",
                "search_titles": [
                    "The Matrix",
                    "Arrival",
                    "Interstellar",
                    "Dune",
                ],
                "media_kind": "movie",
                "confidence": 0.9,
            },
            {
                "action": "pick",
                "indices": [1],
                "confidence": 0.95,
            },
        ],
    )

    nah = await inbox.handle_message(_msg("Nah", message_id=9001))
    assert nah.grabbed is False, nah.reply
    assert "Queued" not in (nah.reply or "")
    assert overseerr_requests == []
    rejected = {t.lower() for t in inbox.memory.rejected(-1001)}
    assert any("elephant" in t for t in rejected)

    # --- 2) Cool scifi → Blade Runner confirm; Yep queues it ---
    ask = await inbox.handle_message(_msg("A cool scifi movie", message_id=9002))
    assert ask.grabbed is False
    assert "Blade Runner" in ask.reply
    assert "Did you mean" in ask.reply
    assert inbox.pending.get(-1001) is not None
    assert len(inbox.pending[-1001].options) == 1

    yep = await inbox.handle_message(_msg("Yep", message_id=9003))
    assert yep.grabbed is True, yep.reply
    assert "Blade Runner" in yep.reply
    assert "Queued" in yep.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 78

    # --- 3) Name a few more → short list, not single Matrix Did-you-mean ---
    few = await inbox.handle_message(_msg("Name a few more", message_id=9004))
    assert few.grabbed is False, few.reply
    assert "Did you mean The Matrix" not in few.reply
    assert "Queued" not in few.reply
    assert _re.search(r"1\.\s*.+\n2\.\s*", few.reply or ""), few.reply
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert len(pending.options) >= 2
    assert any(
        t in (few.reply or "")
        for t in ("Matrix", "Arrival", "Interstellar", "Dune")
    )

    # --- 4) No must NOT queue The Matrix (or first list row) ---
    before_reqs = len(overseerr_requests)
    no = await inbox.handle_message(_msg("No", message_id=9005))
    assert no.grabbed is False, no.reply
    assert "Queued" not in (no.reply or "")
    assert len(overseerr_requests) == before_reqs
    assert not any(r.get("media_id") == 603 for r in overseerr_requests)


@pytest.mark.asyncio
async def test_inbox_nah_clears_pending_even_when_model_searches_same_title(
    inbox: TelegramInbox, monkeypatch
):
    """Model naming the rejected pending title must still not queue it."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    elephant = {
        "title": "The Elephant Man",
        "year": 1980,
        "tmdbId": 10934,
        "mediaId": 10934,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "elephant" in q:
            return {"mode": "mock", "service": "overseerr", "results": [elephant]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[dict(elephant)],
        media_kind="movie",
        query="The Elephant Man",
        created_message_id=9100,
        last_bot_reply="Did you mean The Elephant Man (1980)?",
    )

    pipeline.overseerr_queue.clear()
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Elephant Man",
            "year": 1980,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    result = await inbox.handle_message(_msg("Nah", message_id=9101))
    assert result.grabbed is False
    assert "Queued" not in (result.reply or "")
    assert len(pipeline.overseerr_queue) == 0


@pytest.mark.asyncio
async def test_inbox_no_on_matrix_confirm_does_not_queue(
    inbox: TelegramInbox, monkeypatch
):
    """Single Did-you-mean The Matrix? + No → never queue."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    matrix = {
        "title": "The Matrix",
        "year": 1999,
        "tmdbId": 603,
        "mediaId": 603,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "matrix" in q:
            return {"mode": "mock", "service": "overseerr", "results": [matrix]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[dict(matrix)],
        media_kind="movie",
        query="The Matrix",
        created_message_id=9200,
        last_bot_reply="Did you mean The Matrix?",
    )
    inbox.memory.record_bot(
        -1001,
        "Did you mean The Matrix?",
        search_title="The Matrix",
        media_kind="movie",
        offered=[matrix],
    )

    pipeline.overseerr_queue.clear()
    # Model invents pick (the live bug shape).
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "pick",
            "indices": [1],
            "confidence": 0.99,
        },
    )
    result = await inbox.handle_message(_msg("No", message_id=9201))
    assert result.grabbed is False, result.reply
    assert "Queued" not in (result.reply or "")
    assert len(pipeline.overseerr_queue) == 0
