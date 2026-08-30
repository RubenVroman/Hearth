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
    monkeypatch.setattr(settings, "telegram_prefer_overseerr", False)
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 20)
    monkeypatch.setattr(settings, "overseerr_api_key", "")
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
    assert "Radarr" in result.reply
    assert pipeline.radarr_queue


@pytest.mark.asyncio
async def test_inbox_queues_tmdb_link(inbox: TelegramInbox):
    result = await inbox.handle_message(
        _msg("https://www.themoviedb.org/movie/974950-the-brutalist", message_id=12)
    )
    assert result.grabbed is True
    assert "Brutalist" in result.reply


@pytest.mark.asyncio
async def test_inbox_queues_show(inbox: TelegramInbox):
    result = await inbox.handle_message(_msg("Slow Horses season 2", message_id=13))
    assert result.grabbed is True
    assert "Sonarr" in result.reply
    assert pipeline.sonarr_queue


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
            "service": "radarr",
            "results": [
                {"title": "Heat", "year": 1995, "tmdbId": 1},
                {"title": "Heat", "year": 1986, "tmdbId": 2},
                {"title": "Heat", "year": 2023, "tmdbId": 3},
            ],
        }

    monkeypatch.setattr("hearth.telegram.inbox.radarr.search", multi)
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
    pipeline.radarr_queue.clear()
    denied = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=61, user_id=99))
    assert denied.grabbed is False
    assert denied.reply == ""


@pytest.mark.asyncio
async def test_inbox_not_found(inbox: TelegramInbox, monkeypatch):
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
    assert len(pipeline.radarr_queue) >= 3
    assert all(
        "Harry Potter" in str(row.get("title") or "") for row in pipeline.radarr_queue
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
    assert len(pipeline.radarr_queue) == 1


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
    assert len(pipeline.radarr_queue) >= 3


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


def _imitation_and_davinci_radarr(monkeypatch) -> list[str]:
    """Radarr stub: Imitation Game 1–2 + Da Vinci Code single hit."""
    from hearth.telegram import inbox as inbox_mod

    imitation = [
        {"title": "The Imitation Game", "year": 2014, "tmdbId": 205596},
        {"title": "The Imitation Game", "year": 1980, "tmdbId": 999001},
    ]
    davinci = [{"title": "The Da Vinci Code", "year": 2006, "tmdbId": 591}]
    queries: list[str] = []
    original = inbox_mod.radarr.search

    async def _capture(query: str, *args, **kwargs):
        queries.append(str(query))
        q = (query or "").lower()
        if "imitation" in q:
            return {"mode": "mock", "service": "radarr", "results": list(imitation)}
        if "da vinci" in q or "davinci" in q:
            return {"mode": "mock", "service": "radarr", "results": list(davinci)}
        if q.startswith("tmdb:"):
            try:
                want = int(q.split(":", 1)[1])
            except ValueError:
                want = None
            rows = [r for r in imitation + davinci if r.get("tmdbId") == want]
            if rows:
                return {"mode": "mock", "service": "radarr", "results": rows}
        return await original(query, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.radarr, "search", _capture)
    return queries


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


def _wrap_radarr_search(monkeypatch) -> list[str]:
    """Capture Radarr search queries so tests can forbid raw Dutch plot strings."""
    from hearth.telegram import inbox as inbox_mod

    queries: list[str] = []
    original = inbox_mod.radarr.search

    async def _capture(query: str, *args, **kwargs):
        queries.append(str(query))
        return await original(query, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.radarr, "search", _capture)
    return queries


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
    rejected = [str(t).lower() for t in (payload.get("rejected_titles") or [])]
    assert any("imitation" in t for t in rejected)
    # Must not force pick-from Imitation Game candidates on the reject turn.
    cands = payload.get("candidates") or []
    assert not any("imitation" in str(c.get("title") or "").lower() for c in cands)
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
    rejected = [str(t).lower() for t in (payload.get("rejected_titles") or [])]
    assert any("imitation" in t for t in rejected)
    assert not (payload.get("candidates") or [])
    assert "Which one — reply 1" not in second.reply
    assert any("da vinci" in q.lower() for q in queries)
    subject, _ = inbox.memory.subject(-1001)
    assert "Da Vinci" in subject


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
    before_queue = len(pipeline.radarr_queue)
    nee = await inbox.handle_message(_msg("nee", message_id=321))
    assert nee.grabbed is False
    assert len(pipeline.radarr_queue) == before_queue
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
        {"title": "The Lord of the Rings: The Fellowship of the Ring", "year": 2001, "tmdbId": 120},
        {"title": "The Lord of the Rings: The Two Towers", "year": 2002, "tmdbId": 121},
        {"title": "The Lord of the Rings: The Return of the King", "year": 2003, "tmdbId": 122},
    ]

    from hearth.telegram import inbox as inbox_mod

    queries: list[str] = []
    original = inbox_mod.radarr.search

    async def _capture(query: str, *args, **kwargs):
        queries.append(str(query))
        q = (query or "").lower()
        if "lord" in q or ("ring" in q and "harry" not in q):
            return {"mode": "mock", "service": "radarr", "results": list(lotr_rows)}
        if q.startswith("tmdb:"):
            try:
                want = int(q.split(":", 1)[1])
            except ValueError:
                want = None
            if want is not None:
                hit = [row for row in lotr_rows if row.get("tmdbId") == want]
                if hit:
                    return {"mode": "mock", "service": "radarr", "results": hit}
        return await original(query, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.radarr, "search", _capture)

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
    assert len(pipeline.radarr_queue) == 0
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
    assert len(pipeline.radarr_queue) == 0
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
    assert len(pipeline.radarr_queue) >= 3


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
async def test_progress_failure_still_notifies(monkeypatch):
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

    monkeypatch.setattr("hearth.telegram.progress.radarr.queue", fake_queue)

    await tracker.poll_once(capture)
    assert len(sent) == 1
    assert "downloading" in sent[0]

    state["status"] = "failed"
    await tracker.poll_once(capture)
    assert len(sent) == 2
    assert "failed" in sent[1].lower()
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
