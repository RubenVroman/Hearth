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

async def _get_pending_via_callback(inbox: TelegramInbox, *, message_id: int = 1, chat_id: int = -1001, user_id: int = 42):
    """Tap Get on the live pending offer (HITL queue)."""
    pending = inbox.pending.get(chat_id)
    assert pending is not None and pending.options, "expected a live offer"
    row = pending.options[0]
    tmdb = row.get("tmdbId") or row.get("mediaId")
    assert tmdb not in (None, "")
    kind = str(row.get("mediaType") or pending.media_kind or "movie")
    if kind not in {"movie", "tv"}:
        kind = "movie"
    return await inbox.handle_callback(
        {
            "id": f"cb-{message_id}",
            "data": f"q:{kind}:{int(tmdb)}",
            "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        }
    )



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
    """Title (YYYY) offers Get — queue only after button tap."""
    offer = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=11))
    assert offer.grabbed is False
    assert "Brutalist" in (offer.reply or "")
    pending = inbox.pending.get(-1001)
    assert pending is not None
    tmdb = pending.options[0].get("tmdbId") or pending.options[0].get("mediaId")
    result = await inbox.handle_callback(
        {
            "id": "cb1",
            "data": f"q:movie:{tmdb}",
            "from": {"id": 42},
            "message": {"message_id": 11, "chat": {"id": -1001}},
        }
    )
    assert result.grabbed is True
    assert "Queued The Brutalist" in result.reply
    assert "Overseerr" in result.reply
    assert pipeline.overseerr_queue


@pytest.mark.asyncio
async def test_inbox_queues_tmdb_link(inbox: TelegramInbox):
    offer = await inbox.handle_message(
        _msg("https://www.themoviedb.org/movie/974950-the-brutalist", message_id=12)
    )
    assert offer.grabbed is False
    assert "Brutalist" in (offer.reply or "")
    pending = inbox.pending.get(-1001)
    assert pending is not None
    tmdb = pending.options[0].get("tmdbId") or pending.options[0].get("mediaId")
    result = await inbox.handle_callback(
        {
            "id": "cb2",
            "data": f"q:movie:{tmdb}",
            "from": {"id": 42},
            "message": {"message_id": 12, "chat": {"id": -1001}},
        }
    )
    assert result.grabbed is True
    assert "Brutalist" in result.reply


@pytest.mark.asyncio
async def test_inbox_queues_show(inbox: TelegramInbox, monkeypatch):
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_title",
                            "arguments": {
                                "title": "Slow Horses",
                                "media_type": "tv",
                                "year": 2022,
                            },
                        }
                    }
                ]
            }
        ],
    )
    offer = await inbox.handle_message(_msg("Slow Horses season 2", message_id=13))
    assert offer.grabbed is False
    pending = inbox.pending.get(-1001)
    assert pending is not None
    row = pending.options[0]
    tmdb = row.get("tmdbId") or row.get("mediaId")
    kind = row.get("mediaType") or "tv"
    result = await inbox.handle_callback(
        {
            "id": "cb3",
            "data": f"q:{kind}:{tmdb}",
            "from": {"id": 42},
            "message": {"message_id": 13, "chat": {"id": -1001}},
        }
    )
    assert result.grabbed is True
    assert "Overseerr" in result.reply
    assert pipeline.overseerr_queue
    assert any(
        (r.get("mediaType") or "") == "tv" for r in pipeline.overseerr_queue
    )


@pytest.mark.asyncio
async def test_inbox_dedup_same_message_and_title(inbox: TelegramInbox):
    first = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=20))
    assert first.handled is True
    assert first.grabbed is False
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
    """Ambiguous list → Get button queues one id (not bare '2')."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    heat = [
        {"title": "Heat", "year": 1995, "tmdbId": 949, "mediaId": 949, "mediaType": "movie"},
        {"title": "Heat", "year": 1986, "tmdbId": 10001, "mediaId": 10001, "mediaType": "movie"},
    ]

    async def _search(query: str, *args, **kwargs):
        return {"mode": "mock", "service": "overseerr", "results": list(heat)}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Heat",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    ask = await inbox.handle_message(_msg("Heat", message_id=30))
    assert ask.grabbed is False
    assert inbox.pending.get(-1001) is not None
    assert len(inbox.pending[-1001].options) >= 2
    # Bare 2 does not queue.
    no = await inbox.handle_message(_msg("2", message_id=31))
    assert no.grabbed is False
    # Get 2 via callback on second option.
    row = inbox.pending[-1001].options[1]
    tmdb = row.get("tmdbId") or row.get("mediaId")
    pick = await inbox.handle_callback(
        {
            "id": "cb-heat",
            "data": f"q:movie:{tmdb}",
            "from": {"id": 42},
            "message": {"message_id": 30, "chat": {"id": -1001}},
        }
    )
    assert pick.grabbed is True
    assert "Heat" in pick.reply


@pytest.mark.asyncio
async def test_inbox_already_in_download_queue(inbox: TelegramInbox):
    # Annihilation is in MOCK_RADARR_DOWNLOADS — say so instead of double-queueing.
    offer = await inbox.handle_message(_msg("Annihilation (2018)", message_id=39))
    assert offer.grabbed is False
    result = await _get_pending_via_callback(inbox, message_id=39)
    assert result.grabbed is False
    assert "already" in result.reply.lower()


@pytest.mark.asyncio
async def test_inbox_already_queued_says_so(inbox: TelegramInbox):
    offer = await inbox.handle_message(_msg("The Endless (2017)", message_id=40))
    assert offer.grabbed is False
    first = await _get_pending_via_callback(inbox, message_id=40)
    assert first.grabbed is True
    # New message id but clear title dedup so we exercise already-queued path.
    inbox.deduper.reset()
    again_offer = await inbox.handle_message(_msg("The Endless (2017)", message_id=41))
    assert again_offer.grabbed is False
    again = await _get_pending_via_callback(inbox, message_id=41)
    assert again.grabbed is False
    assert "already" in again.reply.lower()


@pytest.mark.asyncio
async def test_inbox_user_allowlist(inbox: TelegramInbox, monkeypatch):
    monkeypatch.setattr(settings, "telegram_user_ids", "42")
    offer = await inbox.handle_message(_msg("The Brutalist (2024)", message_id=60, user_id=42))
    assert offer.grabbed is False
    ok = await _get_pending_via_callback(inbox, message_id=60, user_id=42)
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
async def test_inbox_instant_title_year_still_offers_get(inbox: TelegramInbox):
    """Explicit Title (YYYY) miss still offers Get/request — no encyclopedia 404 lecture."""
    result = await inbox.handle_message(
        _msg("ZzzNotARealFilm999 (2099)", message_id=71)
    )
    assert result.grabbed is False
    assert "Couldn't find" not in (result.reply or "")
    assert "Did you mean" in (result.reply or "") or "Get" in (result.reply or "")
    assert "ZzzNotARealFilm999" in (result.reply or "")
    # Upcoming year may note it's not out yet, but must still offer.
    assert "wikipedia" not in (result.reply or "").lower()
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert any(
        "ZzzNotARealFilm999" in str(o.get("title") or "") for o in pending.options
    )


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
    """Free-text 'all of them' must NOT queue (second brain killed)."""
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "search_titles": [
                "Harry Potter and the Sorcerer's Stone",
                "Harry Potter and the Chamber of Secrets",
                "Harry Potter and the Prisoner of Azkaban",
            ],
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    ask = await inbox.handle_message(_msg("Harry Potter", message_id=100))
    assert ask.grabbed is False
    assert inbox.pending.get(-1001) is not None
    assert "all of them" not in (ask.reply or "").lower() or "Get" in str(ask.reply_markup)
    pipeline.overseerr_queue.clear()
    all_of = await inbox.handle_message(_msg("all of them", message_id=101))
    assert all_of.grabbed is False
    assert "Queued" not in (all_of.reply or "")
    assert len(pipeline.overseerr_queue) == 0


@pytest.mark.asyncio
async def test_inbox_followup_de_eerste_instant(inbox: TelegramInbox, monkeypatch):
    """'de eerste' must NOT queue — buttons only."""
    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "search_titles": [
                "Harry Potter and the Sorcerer's Stone",
                "Harry Potter and the Chamber of Secrets",
            ],
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    await inbox.handle_message(_msg("Harry Potter", message_id=110))
    pick = await inbox.handle_message(_msg("de eerste", message_id=111))
    assert pick.grabbed is False
    assert "Queued" not in (pick.reply or "")


@pytest.mark.asyncio
async def test_inbox_whole_series_via_model(inbox: TelegramInbox, monkeypatch):
    """Whole-series select_all no longer bulk-queues — offers Get buttons."""
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
    assert result.grabbed is False
    assert "Queued 3" not in (result.reply or "")
    assert "Queued 4" not in (result.reply or "")


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
    """Bare '2' must NOT queue — remind user to tap Get."""
    calls = _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "Harry Potter",
            "search_titles": [
                "Harry Potter and the Sorcerer's Stone",
                "Harry Potter and the Chamber of Secrets",
                "Harry Potter and the Prisoner of Azkaban",
            ],
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    ask = await inbox.handle_message(_msg("Harry Potter", message_id=140))
    assert ask.grabbed is False
    assert inbox.pending.get(-1001) is not None
    pick = await inbox.handle_message(_msg("2", message_id=141))
    assert pick.grabbed is False
    assert "Queued" not in (pick.reply or "")
    assert "Get" in (pick.reply or "") or pick.reply_markup


def test_looks_like_descriptive_ask_compat_shim():
    """Deprecated helper may remain, but is not used as a gate."""
    from hearth.telegram.intent import looks_like_descriptive_ask

    assert looks_like_descriptive_ask(
        "a movie about a boy with glasses who is a wizard"
    )
    assert looks_like_descriptive_ask(
        "Die film waar iemand een puzzel oplost door een spiegel"
    )


def _openai_user_texts(calls: list[dict]) -> list[str]:
    """Collect user-role message contents from Chat Completions call kwargs."""
    out: list[str] = []
    for call in calls or []:
        for msg in call.get("messages") or []:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                out.append(msg["content"])
    return out


def _openai_has_tools(calls: list[dict]) -> bool:
    return any(bool(c.get("tools")) for c in (calls or []))


def _openai_session_context(calls: list[dict]) -> str:
    for call in calls or []:
        for msg in call.get("messages") or []:
            if msg.get("role") == "system" and "Session context" in str(
                msg.get("content") or ""
            ):
                return str(msg.get("content") or "")
    return ""


def _patch_openai_intent(monkeypatch, payload: dict | list[dict]):
    """Stub AsyncOpenAI for Telegram: legacy intent JSON → tool-call turns.

    Inbox conversation uses Chat Completions native tools. Tests may still
    pass old ``{action, search_title, …}`` payloads; this adapter converts
    them into ``search_catalog`` / ``suggest_titles`` / ``queue_request``
    calls, then synthesizes a final assistant reply from tool results.
    """
    import json as _json
    import re as _re
    from types import SimpleNamespace

    from hearth.config import settings as _settings
    from hearth.telegram.intent import looks_like_confirm_yes, looks_like_list_ask

    monkeypatch.setattr(_settings, "openai_api_key", "sk-test-not-a-real-key")
    # House default stays mini; Telegram agent must still use gpt-4o.
    monkeypatch.setattr(_settings, "openai_model", "gpt-4o-mini")

    queue: list[dict] = list(payload) if isinstance(payload, list) else [payload]
    calls: list[dict] = []
    _tc_seq = {"n": 0}
    _select_all = {"flag": False}

    def _next_id() -> str:
        _tc_seq["n"] += 1
        return f"call_test_{_tc_seq['n']}"

    def _user_text(messages: list) -> str:
        for msg in reversed(messages or []):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"]
        return ""

    def _pending_options(messages: list) -> list[dict]:
        for msg in messages or []:
            if msg.get("role") != "system":
                continue
            content = str(msg.get("content") or "")
            if "Session context" not in content:
                continue
            try:
                raw = content.split("\n", 1)[1]
                data = _json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            pending = data.get("pending") or {}
            opts = pending.get("options") or data.get("offered") or []
            if opts:
                return list(opts)
        return []

    def _has_tool_result(messages: list) -> bool:
        return any(m.get("role") == "tool" for m in (messages or []))

    def _tool_results(messages: list) -> list[dict]:
        out = []
        for msg in messages or []:
            if msg.get("role") != "tool":
                continue
            try:
                out.append(_json.loads(msg.get("content") or "{}"))
            except Exception:  # noqa: BLE001
                out.append({})
        return out

    def _intent_to_tool_calls(body: dict, messages: list) -> list[dict]:
        action = str(body.get("action") or "clarify")
        user = _user_text(messages)
        pending = _pending_options(messages)

        # Live single Did-you-mean: confirm / pick / download-of-pending →
        # queue that row (Land ≠ La La Land). List asks and rejects never bind.
        from hearth.telegram.agent import should_refuse_queue as _refuse
        from hearth.telegram.parse import normalize_title as _norm

        if pending and len(pending) == 1 and not _refuse(user):
            row = pending[0]
            pending_title = str(row.get("title") or "").strip()
            search_title = str(body.get("search_title") or "").strip()
            bind = False
            if looks_like_confirm_yes(user) or action in {"pick", "pick_many"}:
                bind = True
            elif action == "search" and pending_title and looks_like_confirm_yes(user):
                bind = True
            elif (
                action == "search"
                and pending_title
                and _norm(pending_title)
                and _norm(pending_title) in _norm(user)
                and any(
                    v in user.lower()
                    for v in ("download", "queue", "get", "bring", "add", "grab")
                )
            ):
                bind = True
            if bind and pending_title:
                args = {
                    "title": pending_title,
                    "media_type": row.get("mediaType") or "movie",
                }
                if row.get("year") not in (None, ""):
                    args["year"] = row["year"]
                tid = row.get("tmdbId") or row.get("mediaId")
                if tid not in (None, ""):
                    args["tmdb_id"] = tid
                return [
                    {
                        "id": _next_id(),
                        "type": "function",
                        "function": {
                            "name": "queue_request",
                            "arguments": _json.dumps(args),
                        },
                    }
                ]

        if action in {"pick", "pick_many"} or (
            looks_like_confirm_yes(user) and pending and len(pending) == 1
        ):
            row = pending[0] if pending else {}
            title = str(row.get("title") or body.get("search_title") or "").strip()
            if not title:
                return []
            args = {"title": title}
            if row.get("year") not in (None, ""):
                args["year"] = row["year"]
            elif body.get("year") not in (None, ""):
                args["year"] = body["year"]
            tid = row.get("tmdbId") or row.get("mediaId")
            if tid not in (None, ""):
                args["tmdb_id"] = tid
            mt = row.get("mediaType") or body.get("media_kind") or "movie"
            args["media_type"] = mt
            return [
                {
                    "id": _next_id(),
                    "type": "function",
                    "function": {
                        "name": "queue_request",
                        "arguments": _json.dumps(args),
                    },
                }
            ]

        if action == "retry":
            args = {"title": str(body.get("search_title") or "")}
            if body.get("media_kind"):
                args["media_type"] = body["media_kind"]
            return [
                {
                    "id": _next_id(),
                    "type": "function",
                    "function": {
                        "name": "retry_download",
                        "arguments": _json.dumps(args),
                    },
                }
            ]

        if action == "search":
            if body.get("select_all"):
                _select_all["flag"] = True
            titles = [
                str(t).strip()
                for t in (body.get("search_titles") or [])
                if str(t).strip()
            ]
            primary = str(body.get("search_title") or "").strip()
            people = [
                str(p).strip()
                for p in (body.get("people") or [])
                if str(p).strip()
            ]
            listish = (
                looks_like_list_ask(user)
                or len(titles) >= 2
                or bool(_re.search(r"\ba few\b", user, _re.I))
            )
            from hearth.telegram.agent import (
                extract_person_name as _person_name,
                looks_like_person_ask as _person_ask,
            )

            # Only clear filmography asks — actor clues on a titled search stay
            # on search_title / suggest_titles.
            if _person_ask(user):
                person_name = (people[0] if people else "") or _person_name(user)
                args = {
                    "name": person_name or primary or user,
                    "limit": 4,
                }
                if body.get("media_kind"):
                    args["media_type"] = body["media_kind"]
                elif _re.search(r"(?i)\b(?:movies?|films?)\b", user):
                    args["media_type"] = "movie"
                return [
                    {
                        "id": _next_id(),
                        "type": "function",
                        "function": {
                            "name": "search_person",
                            "arguments": _json.dumps(args),
                        },
                    }
                ]
            if listish and not body.get("select_all"):
                from hearth.telegram.buttons import genre_hint_from_text

                if primary and primary not in titles:
                    titles.insert(0, primary)
                hint_inc, hint_exc = genre_hint_from_text(primary or user)
                if hint_inc and len(titles) < 2:
                    args = {
                        "genre_ids": hint_inc,
                        "exclude_genre_ids": hint_exc,
                        "limit": 4,
                        "query": primary or user,
                    }
                    if body.get("media_kind"):
                        args["media_type"] = body["media_kind"]
                    return [
                        {
                            "id": _next_id(),
                            "type": "function",
                            "function": {
                                "name": "discover_by_genre",
                                "arguments": _json.dumps(args),
                            },
                        }
                    ]
                if len(titles) < 2:
                    # Non-genre list ask without names — still suggest_titles
                    # with empty pack so the tool can error or discover.
                    titles = list(titles)
                args = {"titles": titles[:4], "limit": min(4, max(2, len(titles[:4]) or 2))}
                if body.get("media_kind"):
                    args["media_type"] = body["media_kind"]
                if primary or user:
                    args["query"] = primary or user
                return [
                    {
                        "id": _next_id(),
                        "type": "function",
                        "function": {
                            "name": "suggest_titles",
                            "arguments": _json.dumps(args),
                        },
                    }
                ]
            if primary:
                args = {"title": primary}
                if body.get("year") not in (None, ""):
                    args["year"] = body["year"]
                if body.get("media_kind"):
                    args["media_type"] = body["media_kind"]
                return [
                    {
                        "id": _next_id(),
                        "type": "function",
                        "function": {
                            "name": "search_title",
                            "arguments": _json.dumps(args),
                        },
                    }
                ]

        return []

    def _followup_tool_calls(messages: list) -> list[dict] | None:
        """After search/discover, do NOT auto-queue (HITL: Get button or yes)."""
        results = _tool_results(messages)
        if not results:
            return None
        for msg in messages or []:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                if fn.get("name") == "queue_request":
                    return None
        last = results[-1]
        if last.get("refused") or last.get("grabbed"):
            return None
        # Whole-series select_all used to bulk-queue — now just leave the offer.
        if _select_all["flag"]:
            _select_all["flag"] = False
        return None

    def _final_from_tools(messages: list) -> str:
        results = _tool_results(messages)
        for payload in reversed(results):
            if payload.get("refused"):
                continue
            reply = str(payload.get("reply") or "").strip()
            if reply and payload.get("grabbed"):
                return reply
            if payload.get("grabbed") and payload.get("title"):
                title = payload["title"]
                year = payload.get("year")
                label = f"{title} ({year})" if year else title
                return f"Queued {label} via Overseerr."
        for payload in reversed(results):
            if payload.get("refused"):
                continue
            reply = str(payload.get("reply") or "").strip()
            if reply:
                return reply
        if any(p.get("refused") for p in results):
            return (
                "Got it — not that one. Want a few other options, "
                "or name a title?"
            )
        return "Which movie or series did you mean?"

    def _as_tool_call_objs(raw_calls: list[dict]) -> list:
        out = []
        for tc in raw_calls:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = _json.dumps(args or {})
            out.append(
                SimpleNamespace(
                    id=tc.get("id") or _next_id(),
                    type="function",
                    function=SimpleNamespace(
                        name=fn.get("name") or "",
                        arguments=args,
                    ),
                )
            )
        return out

    def _message_from_body(body: dict, messages: list, *, tools: bool) -> SimpleNamespace:
        # Explicit tool-call script from newer tests.
        if body.get("tool_calls"):
            return SimpleNamespace(
                content=body.get("content") or "",
                tool_calls=_as_tool_call_objs(body["tool_calls"]),
            )
        if body.get("content") is not None and "action" not in body and tools:
            return SimpleNamespace(content=str(body.get("content") or ""), tool_calls=None)

        if tools and _has_tool_result(messages):
            follow = _followup_tool_calls(messages)
            if follow:
                return SimpleNamespace(
                    content="", tool_calls=_as_tool_call_objs(follow)
                )
            return SimpleNamespace(
                content=_final_from_tools(messages),
                tool_calls=None,
            )

        if tools:
            user = _user_text(messages)
            pending_opts = _pending_options(messages)
            # Confirm of a live single Did-you-mean → queue_request without
            # burning the next scripted intent (Yep after Blade Runner).
            if looks_like_confirm_yes(user) and pending_opts:
                if len(pending_opts) == 1:
                    body = {"action": "pick", "indices": [1], "confidence": 0.95}
                else:
                    body = {
                        "action": "clarify",
                        "clarify_question": (
                            "Which one — reply with a number, or say all of them?"
                        ),
                        "confidence": 0.7,
                    }
            elif queue:
                body = queue.pop(0)
            else:
                body = {"action": "clarify", "clarify_question": "Which movie or series did you mean?"}
            if body.get("tool_calls"):
                return SimpleNamespace(
                    content=body.get("content") or "",
                    tool_calls=_as_tool_call_objs(body["tool_calls"]),
                )
            if body.get("content") is not None and "action" not in body:
                return SimpleNamespace(
                    content=str(body.get("content") or ""), tool_calls=None
                )
            tool_calls = _intent_to_tool_calls(body, messages)
            if tool_calls:
                return SimpleNamespace(
                    content="", tool_calls=_as_tool_call_objs(tool_calls)
                )
            # clarify / ignore → prose only (sanitize list-less 1–1 / banned copy)
            if body.get("action") == "ignore":
                # Media asks must not go silent — fall through to a useful ask.
                from hearth.telegram.intent import looks_like_media_ask as _media

                if _media(user):
                    primary = str(body.get("search_title") or "").strip()
                    if primary:
                        return SimpleNamespace(
                            content="",
                            tool_calls=_as_tool_call_objs(
                                [
                                    {
                                        "id": _next_id(),
                                        "type": "function",
                                        "function": {
                                            "name": "search_catalog",
                                            "arguments": _json.dumps(
                                                {"title": primary}
                                            ),
                                        },
                                    }
                                ]
                            ),
                        )
                    return SimpleNamespace(
                        content="Which movie or series did you mean?",
                        tool_calls=None,
                    )
                return SimpleNamespace(content="", tool_calls=None)
            # Prefer search_catalog when clarify still named a title.
            primary = str(body.get("search_title") or "").strip()
            if body.get("action") == "clarify" and primary:
                return SimpleNamespace(
                    content="",
                    tool_calls=_as_tool_call_objs(
                        [
                            {
                                "id": _next_id(),
                                "type": "function",
                                "function": {
                                    "name": "search_catalog",
                                    "arguments": _json.dumps({"title": primary}),
                                },
                            }
                        ]
                    ),
                )
            clarify = str(body.get("clarify_question") or "").strip()
            lowered = clarify.lower()
            if (
                not clarify
                or "1–1" in clarify
                or "1-1" in clarify
                or "reply 1–1" in lowered
                or "reply 1-1" in lowered
                or "send the title" in lowered
                or "any year, actor" in lowered
            ):
                clarify = "Which movie or series did you mean?"
            return SimpleNamespace(content=clarify, tool_calls=None)

        # Legacy interpret_intent JSON path (unit tests still call it).
        return SimpleNamespace(content=_json.dumps(body), tool_calls=None)

    class _Completions:
        @staticmethod
        async def create(**kwargs):
            calls.append(kwargs)
            messages = kwargs.get("messages") or []
            tools = bool(kwargs.get("tools"))
            body: dict = {}
            if not tools:
                body = queue.pop(0) if queue else {
                    "action": "clarify",
                    "clarify_question": "Which one?",
                }
            msg = _message_from_body(body, messages, tools=tools)

            class _Choice:
                message = msg

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


def _patch_openai_tools(monkeypatch, script: list[dict]):
    """Stub OpenAI with an explicit tool-call / content script (preferred)."""
    return _patch_openai_intent(monkeypatch, script)

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
    joined = " ".join(_openai_user_texts(calls) + [str(calls[0]["messages"][0].get("content") or "")])
    assert "sk-test" not in joined


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
    assert any("spiegel" in t.lower() for t in _openai_user_texts(openai_calls))
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
    assert ('1.' in (first.reply or '') and '2.' in (first.reply or '')) or bool(first.reply_markup)
    assert inbox.pending.get(-1001) is not None

    second = await inbox.handle_message(_msg(reject, message_id=302))
    assert len(openai_calls) >= 2
    # Tool-calling agent: reject turn sees history + pending via session context.
    assert any("leonardo" in t.lower() or "nee" in t.lower() for t in _openai_user_texts(openai_calls))
    ctx = _openai_session_context(openai_calls).lower()
    assert "imitation" in ctx or "pending" in ctx
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
    assert any("honks" in t.lower() or "nee" in t.lower() for t in _openai_user_texts(openai_calls))
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
    assert any("bebrilde tovenaar" in t.lower() for t in _openai_user_texts(openai_calls))
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
    assert any("harige voeten" in t.lower() for t in _openai_user_texts(openai_calls))
    assert any("ok, nog een poging" in t.lower() for t in _openai_user_texts(openai_calls))
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
    assert pick.grabbed is False
    assert "2001" in pick.reply or "Sorcerer" in pick.reply or "Harry Potter" in pick.reply
    assert len(calls) >= before_calls  # free-text de eerste is agent turn, not instant queue
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
    assert all_of.grabbed is False
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
    assert result.grabbed is False  # HITL offer first
    result = await _get_pending_via_callback(inbox)
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
    assert result.grabbed is False  # HITL offer first
    result = await _get_pending_via_callback(inbox)
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
    assert (
        "title" in result.reply.lower()
        or "Which" in result.reply
        or "Did you mean" in result.reply
    )


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
    assert all_of.grabbed is False
    assert len(pipeline.overseerr_queue) == 0  # free-text all of them never bulk-queues


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
    inbox.progress.track(-1001, "Annihilation", "radarr", 2018)
    inbox.memory.set_subject(-1001, "Annihilation", media_kind="movie")

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "retry",
            "search_title": "Annihilation",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )

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
async def test_inbox_retry_library_nofile_offers_release_get_buttons(
    inbox: TelegramInbox, monkeypatch
):
    """Known library title, not in queue, no file → Get buttons, no auto-grab."""
    from hearth.telegram.buttons import parse_release_callback

    inbox.memory.set_subject(-1001, "The Brutalist", media_kind="movie")
    pipeline.radarr_downloads = []

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "retry",
            "search_title": "The Brutalist",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )

    result = await inbox.handle_message(
        _msg(
            "There is a version, but it's too big and it won't play well, "
            "so either retry the download and download another one.",
            message_id=9101,
        )
    )
    assert result.handled is True
    assert result.grabbed is False
    assert "pick" in result.reply.lower() or "release" in result.reply.lower()
    assert result.reply_markup is not None
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert pending.offer_kind == "release"
    assert pending.options
    assert all(opt.get("releaseToken") for opt in pending.options)
    # No download enqueued yet.
    assert pipeline.radarr_downloads == []

    # Vague yes on a multi-offer must NOT grab.
    yes = await inbox.handle_message(_msg("yes", message_id=9102))
    assert yes.grabbed is False
    assert "get button" in yes.reply.lower() or "tap" in yes.reply.lower()
    assert pipeline.radarr_downloads == []

    # Callback Get on the preferred (first) release does grab.
    token = str(pending.options[0]["releaseToken"])
    cb = {
        "id": "cb-rel-1",
        "from": {"id": 42},
        "message": {"message_id": 9101, "chat": {"id": -1001}},
        "data": f"r:movie:{token}",
    }
    assert parse_release_callback(cb["data"]) == ("movie", token)
    grabbed = await inbox.handle_callback(cb)
    assert grabbed.grabbed is True
    assert "grabbing" in grabbed.reply.lower() or "different release" in grabbed.reply.lower()
    assert pipeline.radarr_downloads
    assert inbox.pending.get(-1001) is None


@pytest.mark.asyncio
async def test_inbox_release_single_yes_confirms_without_auto_from_all(
    inbox: TelegramInbox, monkeypatch
):
    """Single pending release: explicit yes grabs; 'all of them' never does."""
    from hearth.telegram.agent import should_refuse_queue
    from hearth.telegram.parse import MessageView
    from hearth.tools.arr import radarr

    pipeline.radarr_downloads = []
    listed = await radarr.list_alternate_releases("The Brutalist")
    # Offer only the preferred row so yes is allowed.
    only = [listed["preferred"]]
    inbox.memory.set_subject(-1001, "The Brutalist", media_kind="movie")
    view = MessageView(
        chat_id=-1001,
        message_id=1,
        user_id=42,
        text="",
        is_bot=False,
    )
    offered = inbox._offer_release_rows(
        view,
        "The Brutalist",
        only,
        speak=listed["speak"],
        reason="needs_pick",
    )
    assert offered.mode == "confirm"
    assert inbox.pending[-1001].offer_kind == "release"

    assert should_refuse_queue("all of them") is True

    yes = await inbox.handle_message(_msg("yes", message_id=9202))
    assert yes.grabbed is True
    assert pipeline.radarr_downloads


@pytest.mark.asyncio
async def test_inbox_keep_both_offers_get_buttons_without_deleting(
    inbox: TelegramInbox, monkeypatch
):
    """Already-has-file + don't-delete → Get buttons; confirm keeps library file."""
    from hearth.telegram.buttons import parse_release_callback

    inbox.memory.set_subject(-1001, "Event Horizon", media_kind="movie")
    pipeline.radarr_downloads = []
    pipeline.radarr_client_downloads.clear()

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "retry",
            "search_title": "Event Horizon",
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )

    result = await inbox.handle_message(
        _msg(
            "Can you download Event Horizon? It's already there, but find another "
            "download. Don't delete the old one.",
            message_id=9301,
        )
    )
    assert result.handled is True
    assert result.grabbed is False
    assert "already" in result.reply.lower() or "keep" in result.reply.lower() or "pick" in result.reply.lower()
    assert result.reply_markup is not None
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert pending.offer_kind == "release"
    assert pending.keep_existing is True
    assert pending.options
    assert pipeline.radarr_downloads == []
    assert pipeline.radarr_client_downloads == []

    # Vague multi-offer yes must NOT grab.
    yes = await inbox.handle_message(_msg("yes", message_id=9302))
    assert yes.grabbed is False
    assert "get button" in yes.reply.lower() or "tap" in yes.reply.lower()
    assert pipeline.radarr_downloads == []
    assert pipeline.list_radarr_library("Event Horizon")[0].get("hasFile") is True

    token = str(pending.options[0]["releaseToken"])
    cb = {
        "id": "cb-keep-1",
        "from": {"id": 42},
        "message": {"message_id": 9301, "chat": {"id": -1001}},
        "data": f"r:movie:{token}",
    }
    assert parse_release_callback(cb["data"]) == ("movie", token)
    grabbed = await inbox.handle_callback(cb)
    assert grabbed.grabbed is True
    assert "keeping" in grabbed.reply.lower() or "extra" in grabbed.reply.lower()
    assert pipeline.radarr_downloads == []
    assert pipeline.radarr_client_downloads
    lib = pipeline.list_radarr_library("Event Horizon")
    assert lib and lib[0].get("hasFile") is True
    assert lib[0].get("movieFile", {}).get("id") == 905
    assert 905 not in pipeline._deleted_movie_files
    assert inbox.pending.get(-1001) is None


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
    if not result.grabbed:
        result = await _get_pending_via_callback(inbox)
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
    if not result.grabbed:
        result = await _get_pending_via_callback(inbox)
    assert result.grabbed is True
    assert "Daniel Sloss" in result.reply
    assert "2025" in result.reply
    assert "Which one" not in result.reply
    assert "1." not in result.reply
    assert "2." not in result.reply
    assert calls, "bare title must call gpt-4o with catalog candidates"
    assert _openai_has_tools(calls)
    assert any("daniel sloss" in t.lower() for t in _openai_user_texts(calls))

    # Colon form with Overseerr duplicate rows must also collapse + grab.
    inbox.deduper.reset()
    pipeline.overseerr_queue.clear()
    again = await inbox.handle_message(_msg("Daniel sloss: Can't", message_id=421))
    if not again.grabbed:
        again = await _get_pending_via_callback(inbox)
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
    assert catalog_search_title("Miss you love you 2026 film") == "Miss you love you"
    assert catalog_search_title("Miss You, Love You (2026)") == "Miss You, Love You"
    # Year-in-title stays when there is no film/movie disambiguator.
    assert catalog_search_title("Blade Runner 2049") == "Blade Runner 2049"


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
    if not result.grabbed:
        result = await _get_pending_via_callback(inbox)
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
    if not result.grabbed:
        result = await _get_pending_via_callback(inbox)
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
    if not result.grabbed:
        result = await _get_pending_via_callback(inbox)
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
    assert any("christophers" in t.lower() for t in _openai_user_texts(calls))
    assert _openai_has_tools(calls)
    if result.grabbed:
        assert "Christophers" in result.reply
        assert "Couldn't find" not in result.reply
        assert "Guest" not in result.reply
        assert "Da Vinci" not in result.reply
    else:
        # Clarify-with-hits must surface the catalog rows somehow.
        assert "Christophers" in result.reply or "Did you mean" in result.reply


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
    assert any("mckellan" in t.lower() for t in _openai_user_texts(calls))
    assert any("christophers" in t.lower() for t in _openai_user_texts(calls))
    if not result.grabbed:
        result = await _get_pending_via_callback(inbox)
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
    # Model must not keep Da Vinci as the active subject.
    users = _openai_user_texts(calls)
    ctx = _openai_session_context(calls)
    assert any("christophers" in t.lower() for t in users)
    assert "Da Vinci" not in ctx or "Christophers" in " ".join(users)
    if not result.grabbed:
        result = await _get_pending_via_callback(inbox)
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
    if not hp.grabbed:
        hp = await _get_pending_via_callback(inbox)
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

    # Tool-calling agent: user text + session context (not JSON intent blob).
    users = _openai_user_texts(calls)
    assert users
    ctx = _openai_session_context(calls)
    assert "Harry" not in ctx or "Alien" in (plot.reply or "")
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
    if not yes.grabbed:
        yes = await _get_pending_via_callback(inbox)
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
        inbox.deduper.reset()
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
        # Confirm queues via Python HITL short-circuit (pending tmdb_id / resolve).
        assert pipeline.overseerr_queue
        del before_calls


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

        ctx = _openai_session_context(calls).lower()
        assert "alien" in ctx or "event horizon" in ctx
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
    assert any("Sure. Bring it" in t for t in _openai_user_texts(calls))
    assert "Pandorum" in _openai_session_context(calls)
    assert _openai_has_tools(calls)


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
    # Download-of-pending short-circuits in Python onto the live Get row.
    del calls


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

    assert _openai_has_tools(calls)
    assert "Alien" in _openai_session_context(calls) or "Alien" in (result.reply or "")
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
    # Tool-calling agent: user text is a chat message, not a JSON intent blob.
    user_bits = [
        str(m.get("content") or "")
        for c in calls
        for m in (c.get("messages") or [])
        if m.get("role") == "user"
    ]
    assert any("robin" in bit.lower() for bit in user_bits)
    assert any(c.get("tools") for c in calls)

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
    assert any("find a title" in t.lower() for t in _openai_user_texts(calls))
    assert _openai_has_tools(calls)
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

    # Yes / Yes... duh short-circuits in Python onto the pending Land row —
    # never waits on a model hop that might invent La La Land.
    calls = _patch_openai_intent(
        monkeypatch,
        {"action": "pick", "indices": [1], "confidence": 0.95},
    )
    yes = await inbox.handle_message(_msg("Yes... duh", message_id=8601))
    assert yes.grabbed is True, yes.reply
    assert "Land" in yes.reply
    assert "2021" in yes.reply
    assert "La La Land" not in yes.reply
    assert CONTEXT_CLUE_CLARIFY not in yes.reply
    assert "Any year, actor, or other clue" not in yes.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 688271
    # Model may be skipped when pending confirm is bound in Python.
    del calls

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
    assert any(t.strip() == "Land" for t in _openai_user_texts(calls))
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
async def test_inbox_plot_man_from_earth_yes_queues_via_overseerr(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: plot → Did you mean The Man from Earth? → Yes must queue.

    Overseerr UI finds the title; Hearth must search+request by mediaId — never
    invent 'isn't available in the catalog'.
    """
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    man = {
        "title": "The Man from Earth",
        "year": 2007,
        "tmdbId": 17401,
        "mediaId": 17401,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "man from earth" in q or "17401" in q:
            return {"mode": "mock", "service": "overseerr", "results": [man]}
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
            "search_title": "The Man from Earth",
            "year": 2007,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    ask = await inbox.handle_message(
        _msg(
            "Movie about the guy that doesnt age and is 14000 years old…",
            message_id=9100,
        )
    )
    assert ask.grabbed is False
    assert "Man from Earth" in ask.reply
    assert "Did you mean" in ask.reply or "?" in ask.reply
    assert "isn't available" not in (ask.reply or "").lower()
    assert "couldn't find" not in (ask.reply or "").lower()
    pending = inbox.pending.get(-1001)
    assert pending is not None and len(pending.options) == 1
    assert pending.options[0].get("tmdbId") in (17401, "17401")
    assert ask.reply_markup is not None  # Get attached

    pipeline.overseerr_queue.clear()
    overseerr_requests.clear()
    yes = await inbox.handle_message(_msg("Yes", message_id=9101))
    assert yes.grabbed is True, yes.reply
    assert "Man from Earth" in yes.reply
    assert "catalog" not in (yes.reply or "").lower()
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 17401
    assert overseerr_requests[-1]["media_type"] == "movie"


@pytest.mark.asyncio
async def test_inbox_rescued_by_ruby_yes_queues_via_overseerr(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: 'Rescued by ruby' → Did you mean → Yes must queue via Overseerr."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    ruby = {
        "title": "Rescued by Ruby",
        "year": 2022,
        "tmdbId": 799876,
        "mediaId": 799876,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "rescued by ruby" in q or "799876" in q:
            return {"mode": "mock", "service": "overseerr", "results": [ruby]}
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
            "search_title": "Rescued by Ruby",
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    ask = await inbox.handle_message(_msg("Rescued by ruby", message_id=9200))
    assert ask.grabbed is False
    assert "Rescued by Ruby" in ask.reply
    assert "Did you mean" in ask.reply or "Get" in (ask.reply or "")
    assert "couldn't find" not in (ask.reply or "").lower()
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert pending.options[0].get("tmdbId") in (799876, "799876")

    pipeline.overseerr_queue.clear()
    overseerr_requests.clear()
    yes = await inbox.handle_message(_msg("Yes", message_id=9201))
    assert yes.grabbed is True, yes.reply
    assert "Rescued by Ruby" in yes.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 799876


@pytest.mark.asyncio
async def test_inbox_yes_without_pending_tmdb_still_queues_resolved_title(
    inbox: TelegramInbox, monkeypatch
):
    """Did-you-mean without tmdb_id: Yes re-searches Overseerr and queues mediaId."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    man = {
        "title": "The Man from Earth",
        "year": 2007,
        "tmdbId": 17401,
        "mediaId": 17401,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "man from earth" in q or "17401" in q:
            return {"mode": "mock", "service": "overseerr", "results": [man]}
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

    option = {
        "title": "The Man from Earth",
        "year": 2007,
        "mediaType": "movie",
    }
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="The Man from Earth",
        created_message_id=9300,
        last_bot_reply="Did you mean The Man from Earth (2007)?",
    )
    inbox.memory.record_bot(
        -1001,
        "Did you mean The Man from Earth (2007)?",
        search_title="The Man from Earth",
        media_kind="movie",
        offered=[option],
    )
    pipeline.overseerr_queue.clear()
    yes = await inbox.handle_message(_msg("Yes", message_id=9301))
    assert yes.grabbed is True, yes.reply
    assert "Man from Earth" in yes.reply
    assert "catalog" not in (yes.reply or "").lower()
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 17401


@pytest.mark.asyncio
async def test_inbox_yes_recovers_did_you_mean_from_history(
    inbox: TelegramInbox, monkeypatch
):
    """Pending lost but last bot was Did-you-mean Title — Yes still queues."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    ruby = {
        "title": "Rescued by Ruby",
        "year": 2022,
        "tmdbId": 799876,
        "mediaId": 799876,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "rescued by ruby" in q or "799876" in q:
            return {"mode": "mock", "service": "overseerr", "results": [ruby]}
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

    inbox.memory.record_user(-1001, "Rescued by ruby")
    inbox.memory.record_bot(
        -1001,
        "Did you mean Rescued by Ruby (2022)?",
        search_title="Rescued by Ruby",
        media_kind="movie",
        offered=[
            {
                "title": "Rescued by Ruby",
                "year": 2022,
                "tmdbId": 799876,
                "mediaType": "movie",
            }
        ],
    )
    assert -1001 not in inbox.pending
    pipeline.overseerr_queue.clear()
    yes = await inbox.handle_message(_msg("Yes", message_id=9401))
    assert yes.grabbed is True, yes.reply
    assert "Rescued by Ruby" in yes.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 799876


def test_title_seed_matches_multiword_and_short_land():
    """Exact-token Land≠La La Land; multi-word confirmed titles still match."""
    from hearth.tools.arr import title_seed_matches

    assert title_seed_matches("The Man from Earth", "The Man from Earth")
    assert title_seed_matches("Rescued by ruby", "Rescued by Ruby")
    assert title_seed_matches("Rescued by Ruby", "Rescued by Ruby")
    assert title_seed_matches("Land", "Land")
    assert not title_seed_matches("Land", "La La Land")


def test_resolve_offer_short_seed_rejects_la_la_land():
    from hearth.telegram.offer import offer_row_matches_seed, short_seed_matches

    assert short_seed_matches("Land", "Land")
    assert not short_seed_matches("Land", "La La Land")
    assert not offer_row_matches_seed("Land", "La La Land")
    assert offer_row_matches_seed("Late Night with the Devil", "Late Night with the Devil")
    assert offer_row_matches_seed("Rescued by ruby", "Rescued by Ruby")
    assert offer_row_matches_seed("The Man from Earth", "The Man from Earth")


@pytest.mark.asyncio
async def test_inbox_late_night_with_the_devil_yes_queues_media_id(
    inbox: TelegramInbox, monkeypatch
):
    """Live 2026-09-01: Did you mean Late Night with the Devil? → Yes must POST mediaId.

    Never reply format_not_found / Couldn't find a match when Overseerr search
    returns that title with an id — even if pending tmdb_id is missing.
    """
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    late = {
        "id": 1020006,
        "title": "Late Night with the Devil",
        "year": 2023,
        "tmdbId": 1020006,
        "mediaId": 1020006,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "late night" in q or "1020006" in q:
            return {"mode": "mock", "service": "overseerr", "results": [late]}
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

    # Pending lost its tmdb_id (history recovery / id-less Did-you-mean).
    option = {
        "title": "Late Night with the Devil",
        "year": 2023,
        "mediaType": "movie",
    }
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="Late Night with the Devil",
        created_message_id=9500,
        last_bot_reply="Did you mean Late Night with the Devil?",
    )
    inbox.memory.record_bot(
        -1001,
        "Did you mean Late Night with the Devil?",
        search_title="Late Night with the Devil",
        media_kind="movie",
        offered=[option],
    )
    pipeline.overseerr_queue.clear()
    yes = await inbox.handle_message(_msg("Yes", message_id=9501))
    assert yes.grabbed is True, yes.reply
    assert "couldn't find a match" not in (yes.reply or "").lower()
    assert "Couldn't find a match" not in (yes.reply or "")
    assert "Late Night" in yes.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 1020006
    assert overseerr_requests[-1]["media_type"] == "movie"


@pytest.mark.asyncio
async def test_inbox_late_night_retype_title_never_format_not_found(
    inbox: TelegramInbox, monkeypatch
):
    """Retyping the offered title after Yes must not loop format_not_found."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    late = {
        "id": 1020006,
        "title": "Late Night with the Devil",
        "year": 2023,
        "tmdbId": 1020006,
        "mediaId": 1020006,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "late night" in q or "1020006" in q:
            return {"mode": "mock", "service": "overseerr", "results": [late]}
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
            "search_title": "Late Night with the Devil",
            "year": 2023,
            "media_kind": "movie",
            "confidence": 0.95,
        },
    )
    ask = await inbox.handle_message(
        _msg("Late Night with the Devil", message_id=9510)
    )
    assert "couldn't find a match" not in (ask.reply or "").lower()
    assert ask.grabbed is False
    assert "Late Night" in (ask.reply or "")
    pending = inbox.pending.get(-1001)
    assert pending is not None and len(pending.options) == 1
    assert pending.options[0].get("tmdbId") in (1020006, "1020006")

    pipeline.overseerr_queue.clear()
    overseerr_requests.clear()
    yes = await inbox.handle_message(_msg("Yes", message_id=9511))
    assert yes.grabbed is True, yes.reply
    assert "couldn't find a match" not in (yes.reply or "").lower()
    assert overseerr_requests[-1]["media_id"] == 1020006


@pytest.mark.asyncio
async def test_inbox_yes_fuzzy_recovers_when_pending_id_missing(
    inbox: TelegramInbox, monkeypatch
):
    """History recovery + search fallback: fuzzy-match offered string → mediaId."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    man = {
        "id": 17401,
        "title": "The Man from Earth",
        "year": 2007,
        "tmdbId": 17401,
        "mediaId": 17401,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "man from earth" in q or "17401" in q:
            return {"mode": "mock", "service": "overseerr", "results": [man]}
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

    # No live pending — recover Did-you-mean from history (no tmdb on offered).
    inbox.memory.record_bot(
        -1001,
        "Did you mean The Man from Earth (2007)?",
        search_title="The Man from Earth",
        media_kind="movie",
        offered=[{"title": "The Man from Earth", "year": 2007, "mediaType": "movie"}],
    )
    assert -1001 not in inbox.pending
    pipeline.overseerr_queue.clear()
    yes = await inbox.handle_message(_msg("Yes", message_id=9520))
    assert yes.grabbed is True, yes.reply
    assert "couldn't find a match" not in (yes.reply or "").lower()
    assert overseerr_requests[-1]["media_id"] == 17401
    assert overseerr_requests[-1]["media_type"] == "movie"


@pytest.mark.asyncio
async def test_inbox_plot_man_from_earth_confirm_yes_never_not_found(
    inbox: TelegramInbox, monkeypatch
):
    """Plot confirm for The Man from Earth → Yes never format_not_found."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    man = {
        "title": "The Man from Earth",
        "year": 2007,
        "tmdbId": 17401,
        "mediaId": 17401,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "man from earth" in q:
            return {"mode": "mock", "service": "overseerr", "results": [man]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    _patch_openai_intent(
        monkeypatch,
        {
            "action": "search",
            "search_title": "The Man from Earth",
            "year": 2007,
            "media_kind": "movie",
            "confidence": 0.9,
        },
    )
    ask = await inbox.handle_message(
        _msg(
            "that movie where a professor says he's been alive for 14000 years",
            message_id=9530,
        )
    )
    assert "couldn't find" not in (ask.reply or "").lower()
    yes = await inbox.handle_message(_msg("Yes", message_id=9531))
    assert yes.grabbed is True, yes.reply
    assert "couldn't find a match" not in (yes.reply or "").lower()


@pytest.mark.asyncio
async def test_inbox_tv_yes_requests_seasons_all(inbox: TelegramInbox, monkeypatch):
    """TV confirm → Overseerr request(mediaId, mediaType=tv); seasons all on wire."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation
    from hearth.tools import arr as arr_mod

    show = {
        "id": 95396,
        "title": "Severance",
        "year": 2022,
        "tmdbId": 95396,
        "mediaId": 95396,
        "mediaType": "tv",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "severance" in q or "95396" in q:
            return {"mode": "mock", "service": "overseerr", "results": [show]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    posted: list[dict] = []

    class _FakeResp:
        status_code = 201

        def raise_for_status(self):
            return None

    class _FakeHttp:
        async def post(self, path, json=None):
            posted.append({"path": path, "json": dict(json or {})})
            return _FakeResp()

    async def _http():
        return _FakeHttp()

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        # Exercise the live POST body builder for seasons: "all".
        monkeypatch.setattr(arr_mod.settings, "overseerr_url", "http://overseerr.test")
        monkeypatch.setattr(arr_mod.settings, "overseerr_api_key", "test-key")
        monkeypatch.setattr(arr_mod.settings, "mock_if_unconfigured", False)
        monkeypatch.setattr(
            type(arr_mod.overseerr), "live", property(lambda self: True)
        )
        monkeypatch.setattr(arr_mod.overseerr, "_http", _http)
        return await arr_mod.overseerr._post_request(
            {"title": query or "Severance", "mediaType": media_type or "tv"},
            media_id=int(media_id),
            media_type=str(media_type or "tv"),
            query=query or "Severance",
        )

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[
            {
                "title": "Severance",
                "year": 2022,
                "tmdbId": 95396,
                "mediaId": 95396,
                "mediaType": "tv",
            }
        ],
        media_kind="tv",
        query="Severance",
        created_message_id=9540,
        last_bot_reply="Did you mean Severance (2022)?",
    )
    pipeline.overseerr_queue.clear()

    async def _not_queued(title: str, service: str = "") -> bool:
        return False

    monkeypatch.setattr(inbox, "_already_queued", _not_queued)
    yes = await inbox.handle_message(_msg("Yes", message_id=9541))
    assert yes.grabbed is True, yes.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 95396
    assert overseerr_requests[-1]["media_type"] == "tv"
    assert posted, "expected live Overseerr POST"
    body = posted[-1]["json"]
    assert body.get("mediaType") == "tv"
    assert body.get("mediaId") == 95396
    assert body.get("seasons") == "all"


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


# --- Movies & Series tool-call agent (live Telegram ~01:00 2026-09-01) --------


def test_telegram_tool_schemas_include_required_tools():
    from hearth.telegram.agent import TELEGRAM_CHAT_TOOLS, should_refuse_queue
    from hearth.telegram.buttons import GENRE_FANTASY, GENRE_SCI_FI, genre_hint_from_text

    names = {t["function"]["name"] for t in TELEGRAM_CHAT_TOOLS}
    assert "search_title" in names
    assert "discover_by_genre" in names
    assert "web_search" in names
    assert "queue_request" in names
    assert "library_status" in names
    assert "download_progress" in names
    assert "retry_download" in names
    assert should_refuse_queue("No")
    assert should_refuse_queue("Nah")
    assert should_refuse_queue("Name a few more")
    assert should_refuse_queue("a few cool space sci-fi movies")
    assert should_refuse_queue("I was asking for a few")
    assert should_refuse_queue("all of them")
    assert should_refuse_queue("3")
    assert should_refuse_queue("those are all scifi")
    assert should_refuse_queue("Fantasy.. those are all scifi")
    assert should_refuse_queue("Why did you do that")
    assert not should_refuse_queue("Yep")
    assert not should_refuse_queue("Blade Runner")
    inc, exc = genre_hint_from_text("cool fantasy movies")
    assert GENRE_FANTASY in inc
    assert GENRE_SCI_FI in exc


@pytest.mark.asyncio
async def test_inbox_0100_no_after_matrix_does_not_queue_via_toolcall(
    inbox: TelegramInbox, monkeypatch
):
    """01:00 replay: Did you mean The Matrix? → No → must NOT queue."""
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
        created_message_id=9300,
        last_bot_reply="Did you mean The Matrix?",
    )

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)
    pipeline.overseerr_queue.clear()

    # Explicit mistaken queue_request (the live bug shape under tool-calling).
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "queue_request",
                            "arguments": {
                                "title": "The Matrix",
                                "year": 1999,
                                "tmdb_id": 603,
                                "media_type": "movie",
                            },
                        }
                    }
                ]
            }
        ],
    )
    result = await inbox.handle_message(_msg("No", message_id=9301))
    assert result.grabbed is False, result.reply
    assert "Queued" not in (result.reply or "")
    assert overseerr_requests == []
    assert len(pipeline.overseerr_queue) == 0


@pytest.mark.asyncio
async def test_inbox_0100_few_space_scifi_and_few_more_suggest_list(
    inbox: TelegramInbox, monkeypatch
):
    """01:00 replay: list/vibe asks → 2–4 titles via suggest_titles, never queue."""
    import re as _re

    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod

    catalog = {
        "matrix": {
            "title": "The Matrix",
            "year": 1999,
            "tmdbId": 603,
            "mediaId": 603,
            "mediaType": "movie",
        },
        "arrival": {
            "title": "Arrival",
            "year": 2016,
            "tmdbId": 329865,
            "mediaId": 329865,
            "mediaType": "movie",
        },
        "interstellar": {
            "title": "Interstellar",
            "year": 2014,
            "tmdbId": 157336,
            "mediaId": 157336,
            "mediaType": "movie",
        },
        "dune": {
            "title": "Dune",
            "year": 2021,
            "tmdbId": 438631,
            "mediaId": 438631,
            "mediaType": "movie",
        },
        "blade": {
            "title": "Blade Runner",
            "year": 1982,
            "tmdbId": 78,
            "mediaId": 78,
            "mediaType": "movie",
        },
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        results = []
        for key, row in catalog.items():
            if key in q or row["title"].lower() in q:
                results.append(row)
        return {"mode": "mock", "service": "overseerr", "results": results}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append({"query": query, "media_id": media_id})
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "suggest_titles",
                            "arguments": {
                                "query": "cool space sci-fi",
                                "titles": [
                                    "Interstellar",
                                    "Arrival",
                                    "Dune",
                                    "Blade Runner",
                                ],
                            },
                        }
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "suggest_titles",
                            "arguments": {
                                "query": "a few more",
                                "titles": [
                                    "The Matrix",
                                    "Arrival",
                                    "Interstellar",
                                    "Dune",
                                ],
                            },
                        }
                    }
                ]
            },
        ],
    )

    space = await inbox.handle_message(
        _msg("Give me a few cool space sci-fi movies", message_id=9401)
    )
    assert space.grabbed is False, space.reply
    assert "Queued" not in (space.reply or "")
    assert "Did you mean Interstellar" not in (space.reply or "")
    assert _re.search(r"1\.\s*.+\n2\.\s*", space.reply or ""), space.reply
    assert inbox.pending.get(-1001) is not None
    assert len(inbox.pending[-1001].options) >= 2

    few = await inbox.handle_message(_msg("Name a few more", message_id=9402))
    assert few.grabbed is False, few.reply
    assert "Queued" not in (few.reply or "")
    assert "Did you mean The Matrix" not in (few.reply or "")
    assert _re.search(r"1\.\s*.+\n2\.\s*", few.reply or ""), few.reply
    assert overseerr_requests == []


@pytest.mark.asyncio
async def test_inbox_0100_asking_for_a_few_does_not_queue_interstellar(
    inbox: TelegramInbox, monkeypatch
):
    """01:00 replay: after Interstellar confirm, 'I was asking for a few' ≠ queue."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    interstellar = {
        "title": "Interstellar",
        "year": 2014,
        "tmdbId": 157336,
        "mediaId": 157336,
        "mediaType": "movie",
    }
    others = [
        {
            "title": "Arrival",
            "year": 2016,
            "tmdbId": 329865,
            "mediaId": 329865,
            "mediaType": "movie",
        },
        {
            "title": "Dune",
            "year": 2021,
            "tmdbId": 438631,
            "mediaId": 438631,
            "mediaType": "movie",
        },
        {
            "title": "Blade Runner",
            "year": 1982,
            "tmdbId": 78,
            "mediaId": 78,
            "mediaType": "movie",
        },
    ]

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        results = []
        for row in [interstellar, *others]:
            if row["title"].lower() in q or any(
                tok in q for tok in row["title"].lower().split()
            ):
                results.append(row)
        return {"mode": "mock", "service": "overseerr", "results": results}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[dict(interstellar)],
        media_kind="movie",
        query="Interstellar",
        created_message_id=9500,
        last_bot_reply="Did you mean Interstellar (2014)?",
    )
    inbox.memory.record_bot(
        -1001,
        "Did you mean Interstellar (2014)?",
        search_title="Interstellar",
        media_kind="movie",
        offered=[interstellar],
    )

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append({"query": query, "media_id": media_id})
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)
    pipeline.overseerr_queue.clear()

    # Mistaken queue_request + recovery suggest_titles on the next model hop
    # after the safety rail refuses.
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "queue_request",
                            "arguments": {
                                "title": "Interstellar",
                                "year": 2014,
                                "tmdb_id": 157336,
                            },
                        }
                    }
                ]
            },
        ],
    )
    result = await inbox.handle_message(
        _msg("I was asking for a few", message_id=9501)
    )
    assert result.grabbed is False, result.reply
    assert "Queued" not in (result.reply or "")
    assert overseerr_requests == []
    assert not any(r.get("media_id") == 157336 for r in overseerr_requests)


@pytest.mark.asyncio
async def test_inbox_0100_yep_queues_blade_runner_via_queue_request(
    inbox: TelegramInbox, monkeypatch
):
    """01:00 replay: Yep after Blade Runner still queues via queue_request only."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation

    blade = {
        "title": "Blade Runner",
        "year": 1982,
        "tmdbId": 78,
        "mediaId": 78,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower()
        if "blade" in q:
            return {"mode": "mock", "service": "overseerr", "results": [blade]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[dict(blade)],
        media_kind="movie",
        query="Blade Runner",
        created_message_id=9600,
        last_bot_reply="Did you mean Blade Runner (1982)?",
    )
    inbox.memory.set_subject(
        -1001, "Blade Runner", media_kind="movie", offered=[blade]
    )

    overseerr_requests: list[dict] = []
    original_req = inbox_mod.overseerr.request

    async def _req_capture(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append(
            {"query": query, "media_id": media_id, "media_type": media_type}
        )
        return await original_req(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _req_capture)
    pipeline.overseerr_queue.clear()

    calls = _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "queue_request",
                            "arguments": {
                                "title": "Blade Runner",
                                "year": 1982,
                                "tmdb_id": 78,
                                "media_type": "movie",
                            },
                        }
                    }
                ]
            }
        ],
    )
    result = await inbox.handle_message(_msg("Yep", message_id=9601))
    assert result.grabbed is True, result.reply
    assert "Blade Runner" in result.reply
    assert "Queued" in result.reply
    assert overseerr_requests
    assert overseerr_requests[-1]["media_id"] == 78
    # Yep on a single pending Get short-circuits in Python (no model hop required).
    del calls


# --- Fantasy HITL modes (live Telegram ~01:32 2026-09-01) --------------------


@pytest.mark.asyncio
async def test_inbox_0132_fantasy_discover_excludes_scifi(
    inbox: TelegramInbox, monkeypatch
):
    """Fantasy ask → TMDB genre 14 list; not Matrix/Arrival/Interstellar alone."""
    from hearth.telegram.buttons import GENRE_FANTASY, GENRE_SCI_FI

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "discover_by_genre",
                            "arguments": {
                                "genre_ids": [GENRE_FANTASY],
                                "exclude_genre_ids": [GENRE_SCI_FI],
                                "query": "cool fantasy movies",
                                "limit": 4,
                            },
                        }
                    }
                ]
            }
        ],
    )
    result = await inbox.handle_message(
        _msg("Give me a few cool fantasy movies", message_id=10101)
    )
    assert result.grabbed is False
    assert "Queued" not in (result.reply or "")
    titles = " ".join(
        str(r.get("title") or "") for r in (inbox.pending.get(-1001).options if inbox.pending.get(-1001) else [])
    ).lower()
    assert "green knight" in titles or "labyrinth" in titles or "spirited" in titles or "lord of the rings" in titles
    scifi_only = {"the matrix", "arrival", "interstellar"}
    offered = {
        str(r.get("title") or "").strip().lower()
        for r in (inbox.pending[-1001].options if inbox.pending.get(-1001) else [])
    }
    assert not offered or not offered.issubset(scifi_only)
    assert result.reply_markup or inbox.pending.get(-1001)


@pytest.mark.asyncio
async def test_inbox_0132_fantasy_correction_does_not_queue(
    inbox: TelegramInbox, monkeypatch
):
    """'Fantasy.. those are all scifi' does NOT queue; rediscovers fantasy."""
    from hearth.telegram.buttons import GENRE_FANTASY, GENRE_SCI_FI
    from hearth.telegram.inbox import PendingDisambiguation

    scifi = [
        {"title": "The Matrix", "year": 1999, "tmdbId": 603, "mediaType": "movie"},
        {"title": "Arrival", "year": 2016, "tmdbId": 329865, "mediaType": "movie"},
        {"title": "Interstellar", "year": 2014, "tmdbId": 157336, "mediaType": "movie"},
    ]
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=scifi,
        media_kind="movie",
        query="cool fantasy",
        created_message_id=10200,
        last_bot_reply="1. The Matrix\n2. Arrival\n3. Interstellar",
        mode="offer",
    )
    pipeline.overseerr_queue.clear()
    overseerr_requests: list[dict] = []
    from hearth.telegram import inbox as inbox_mod

    original = inbox_mod.overseerr.request

    async def _cap(query: str = "", media_id=None, media_type=None):
        overseerr_requests.append({"query": query, "media_id": media_id})
        return await original(query, media_id=media_id, media_type=media_type)

    monkeypatch.setattr(inbox_mod.overseerr, "request", _cap)

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "discover_by_genre",
                            "arguments": {
                                "genre_ids": [GENRE_FANTASY],
                                "exclude_genre_ids": [GENRE_SCI_FI],
                                "query": "fantasy",
                            },
                        }
                    }
                ]
            },
        ],
    )
    result = await inbox.handle_message(
        _msg("Fantasy.. those are all scifi", message_id=10201)
    )
    assert result.grabbed is False
    assert "Queued" not in (result.reply or "")
    assert overseerr_requests == []
    assert "Queued 3" not in (result.reply or "")
    # Correction stays browse / offer fantasy — never Matrix-only.
    assert inbox.pending.get(-1001) is not None or "fantasy" in (result.reply or "").lower() or "sci-fi" in (result.reply or "").lower()
    if inbox.pending.get(-1001):
        offered = {
            str(r.get("title") or "").strip().lower()
            for r in inbox.pending[-1001].options
        }
        assert offered
        assert not offered.issubset({"the matrix", "arrival", "interstellar"})


@pytest.mark.asyncio
async def test_inbox_0132_why_did_you_do_that_explains_no_reoffer(
    inbox: TelegramInbox, monkeypatch
):
    """'Why did you do that' does not queue and does not re-offer sci-fi as fantasy."""
    from hearth.telegram.inbox import PendingDisambiguation

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[
            {"title": "The Matrix", "year": 1999, "tmdbId": 603, "mediaType": "movie"},
            {"title": "Arrival", "year": 2016, "tmdbId": 329865, "mediaType": "movie"},
            {"title": "Interstellar", "year": 2014, "tmdbId": 157336, "mediaType": "movie"},
        ],
        media_kind="movie",
        query="fantasy",
        created_message_id=10300,
        last_bot_reply="1. The Matrix\n2. Arrival\n3. Interstellar",
    )
    pipeline.overseerr_queue.clear()
    # Explain mode is Python short-circuit — no OpenAI needed.
    result = await inbox.handle_message(_msg("Why did you do that", message_id=10301))
    assert result.grabbed is False
    assert "Queued" not in (result.reply or "")
    assert "sorry" in (result.reply or "").lower() or "bug" in (result.reply or "").lower()
    # Must not re-list the sci-fi set as fantasy.
    assert "the matrix" not in (result.reply or "").lower()
    assert result.mode == "explain"


@pytest.mark.asyncio
async def test_inbox_0132_callback_queues_only_that_tmdb_id(inbox: TelegramInbox):
    """Button callback q:movie:<id> queues that id only."""
    from hearth.telegram.inbox import PendingDisambiguation

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[
            {
                "title": "The Green Knight",
                "year": 2021,
                "tmdbId": 497698,
                "mediaId": 497698,
                "mediaType": "movie",
            },
            {
                "title": "Pan's Labyrinth",
                "year": 2006,
                "tmdbId": 1417,
                "mediaId": 1417,
                "mediaType": "movie",
            },
        ],
        media_kind="movie",
        query="fantasy",
        created_message_id=10400,
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_callback(
        {
            "id": "cb-green",
            "data": "q:movie:497698",
            "from": {"id": 42},
            "message": {"message_id": 10400, "chat": {"id": -1001}},
        }
    )
    assert result.grabbed is True
    assert "Green Knight" in result.reply
    assert "Queued" in result.reply
    assert "Queued 2" not in result.reply
    assert "Labyrinth" not in result.reply
    assert len(pipeline.overseerr_queue) == 1


@pytest.mark.asyncio
async def test_inbox_0132_bare_3_and_all_scifi_do_not_queue(
    inbox: TelegramInbox, monkeypatch
):
    """Bare '3' or 'all scifi' on an offer does not queue."""
    from hearth.telegram.inbox import PendingDisambiguation

    options = [
        {"title": "The Matrix", "year": 1999, "tmdbId": 603, "mediaType": "movie"},
        {"title": "Arrival", "year": 2016, "tmdbId": 329865, "mediaType": "movie"},
        {"title": "Interstellar", "year": 2014, "tmdbId": 157336, "mediaType": "movie"},
    ]
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=options,
        media_kind="movie",
        query="space",
        created_message_id=10500,
        last_bot_reply="1. The Matrix\n2. Arrival\n3. Interstellar",
    )
    pipeline.overseerr_queue.clear()

    # Bare 3 — parser disambiguation_pick path, must not queue.
    three = await inbox.handle_message(_msg("3", message_id=10501))
    assert three.grabbed is False
    assert "Queued" not in (three.reply or "")

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=options,
        media_kind="movie",
        query="space",
        created_message_id=10500,
        last_bot_reply="1. The Matrix\n2. Arrival\n3. Interstellar",
    )
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "queue_request",
                            "arguments": {
                                "tmdb_id": 603,
                                "media_type": "movie",
                                "title": "The Matrix",
                            },
                        }
                    }
                ]
            }
        ],
    )
    all_scifi = await inbox.handle_message(_msg("all scifi", message_id=10502))
    assert all_scifi.grabbed is False
    assert "Queued" not in (all_scifi.reply or "")
    assert len(pipeline.overseerr_queue) == 0


@pytest.mark.asyncio
async def test_inbox_0132_all_of_them_does_not_queue(
    inbox: TelegramInbox, monkeypatch
):
    from hearth.telegram.inbox import PendingDisambiguation

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[
            {"title": "A", "year": 2001, "tmdbId": 1, "mediaType": "movie"},
            {"title": "B", "year": 2002, "tmdbId": 2, "mediaType": "movie"},
        ],
        media_kind="movie",
        query="franchise",
        created_message_id=10600,
    )
    pipeline.overseerr_queue.clear()
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "queue_request",
                            "arguments": {"tmdb_id": 1, "media_type": "movie", "title": "A"},
                        }
                    }
                ]
            }
        ],
    )
    result = await inbox.handle_message(_msg("all of them", message_id=10601))
    assert result.grabbed is False
    assert "Queued" not in (result.reply or "")
    assert len(pipeline.overseerr_queue) == 0


# --- Fantasy released-only / others / no stale Get (live 2026-09-01 02:03) ---


@pytest.mark.asyncio
async def test_inbox_0203_fantasy_not_unreleased_2026(
    inbox: TelegramInbox, monkeypatch
):
    """Fantasy discover must not offer Odyssey/Moana/Minions 2026 vaporware."""
    from hearth.telegram.buttons import GENRE_FANTASY, GENRE_SCI_FI

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "discover_by_genre",
                            "arguments": {
                                "genre_ids": [GENRE_FANTASY],
                                "exclude_genre_ids": [GENRE_SCI_FI],
                                "query": "Fantasy",
                                "limit": 3,
                            },
                        }
                    }
                ]
            }
        ],
    )
    result = await inbox.handle_message(_msg("Fantasy", message_id=20101))
    assert result.grabbed is False
    assert result.reply_markup
    pending = inbox.pending.get(-1001)
    assert pending is not None
    titles = [str(r.get("title") or "") for r in pending.options]
    years = [r.get("year") for r in pending.options]
    joined = " | ".join(titles).lower()
    assert "odyssey" not in joined
    assert "moana" not in joined
    assert "minions" not in joined
    assert all(y is None or int(y) <= 2026 for y in years)
    # Real released fantasy — not all-2026.
    assert any(
        "green knight" in t.lower()
        or "labyrinth" in t.lower()
        or "spirited" in t.lower()
        or "lord of the rings" in t.lower()
        for t in titles
    )
    assert any(y is not None and int(y) < 2026 for y in years)


@pytest.mark.asyncio
async def test_inbox_0204_none_of_these_and_others_new_pack(
    inbox: TelegramInbox, monkeypatch
):
    """None-of-these + 'others' yields different titles; no duplicate Get row."""
    import json

    from hearth.telegram.buttons import GENRE_FANTASY, GENRE_SCI_FI

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "discover_by_genre",
                            "arguments": {
                                "genre_ids": [GENRE_FANTASY],
                                "exclude_genre_ids": [GENRE_SCI_FI],
                                "query": "Fantasy",
                                "limit": 3,
                            },
                        }
                    }
                ]
            }
        ],
    )
    first = await inbox.handle_message(_msg("Fantasy", message_id=20201))
    assert first.reply_markup
    first_ids = {
        int(r.get("tmdbId") or r.get("mediaId"))
        for r in inbox.pending[-1001].options
    }
    first_markup = json.dumps(first.reply_markup, sort_keys=True)

    none = await inbox.handle_callback(
        {
            "id": "cb-none",
            "data": "q:none",
            "from": {"id": 42},
            "message": {"message_id": 20201, "chat": {"id": -1001}},
        }
    )
    assert none.grabbed is False
    # Auto next-pack or a clear exhausted message — never the same Get ids alone.
    if none.reply_markup:
        second_ids = {
            int(btn["callback_data"].split(":")[-1])
            for row in none.reply_markup.get("inline_keyboard") or []
            for btn in row
            if str(btn.get("callback_data") or "").startswith("q:movie:")
            or str(btn.get("callback_data") or "").startswith("q:tv:")
        }
        assert second_ids
        assert second_ids.isdisjoint(first_ids)
        assert json.dumps(none.reply_markup, sort_keys=True) != first_markup
    else:
        assert "different genre" in (none.reply or "").lower() or "out of" in (
            none.reply or ""
        ).lower() or "what should i look for" in (none.reply or "").lower()

    # Text follow-up "you've just mentioned these, give me some others".
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "discover_by_genre",
                            "arguments": {
                                "genre_ids": [GENRE_FANTASY],
                                "exclude_genre_ids": [GENRE_SCI_FI],
                                "query": "fantasy others",
                                "limit": 3,
                            },
                        }
                    }
                ]
            }
        ],
    )
    # Seed a stale pending that matches the first pack (bug reproduction).
    from hearth.telegram.inbox import PendingDisambiguation

    stale = [
        {
            "title": "The Odyssey",
            "year": 2026,
            "tmdbId": 1110001,
            "mediaType": "movie",
        },
        {
            "title": "Moana",
            "year": 2026,
            "tmdbId": 1110002,
            "mediaType": "movie",
        },
        {
            "title": "Minions & Monsters",
            "year": 2026,
            "tmdbId": 1110003,
            "mediaType": "movie",
        },
    ]
    inbox.memory.remember_shown(-1001, list(stale))
    inbox.memory.set_discover_cursor(
        -1001,
        genre_ids=[GENRE_FANTASY],
        exclude_genre_ids=[GENRE_SCI_FI],
        page=1,
        media_type="movie",
    )
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=stale,
        media_kind="movie",
        query="Fantasy",
        created_message_id=20210,
        last_bot_reply="1. The Odyssey (2026)\n2. Moana (2026)\n3. Minions & Monsters (2026)",
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "Get 1", "callback_data": "q:movie:1110001"},
                    {"text": "Get 2", "callback_data": "q:movie:1110002"},
                    {"text": "Get 3", "callback_data": "q:movie:1110003"},
                ],
                [{"text": "None of these", "callback_data": "q:none"}],
            ]
        },
    )
    others = await inbox.handle_message(
        _msg(
            "You've just mentioned these, give me some others",
            message_id=20211,
        )
    )
    assert others.grabbed is False
    assert "Queued" not in (others.reply or "")
    # Must not re-attach the same three Get buttons for the 2026 vapor set.
    markup = others.reply_markup
    if markup:
        get_ids = {
            int(btn["callback_data"].split(":")[-1])
            for row in markup.get("inline_keyboard") or []
            for btn in row
            if str(btn.get("callback_data") or "").startswith("q:")
            and btn["callback_data"] != "q:none"
        }
        assert get_ids.isdisjoint({1110001, 1110002, 1110003})
        body = (others.reply or "").lower()
        assert not (
            "trouble" in body and {"1110001", "1110002", "1110003"}.issubset(
                {str(i) for i in get_ids}
            )
        )
    else:
        assert markup is None


@pytest.mark.asyncio
async def test_inbox_0204_exhausted_reply_has_no_get_buttons(
    inbox: TelegramInbox, monkeypatch
):
    """If the model admits it can't refresh, do not re-attach prior Get buttons."""
    from hearth.telegram.inbox import PendingDisambiguation

    options = [
        {"title": "The Odyssey", "year": 2026, "tmdbId": 1110001, "mediaType": "movie"},
        {"title": "Moana", "year": 2026, "tmdbId": 1110002, "mediaType": "movie"},
        {
            "title": "Minions & Monsters",
            "year": 2026,
            "tmdbId": 1110003,
            "mediaType": "movie",
        },
    ]
    markup = {
        "inline_keyboard": [
            [
                {"text": "Get 1", "callback_data": "q:movie:1110001"},
                {"text": "Get 2", "callback_data": "q:movie:1110002"},
                {"text": "Get 3", "callback_data": "q:movie:1110003"},
            ],
            [{"text": "None of these", "callback_data": "q:none"}],
        ]
    }
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=options,
        media_kind="movie",
        query="Fantasy",
        created_message_id=20300,
        last_bot_reply="1. The Odyssey (2026)\n2. Moana (2026)\n3. Minions",
        reply_markup=markup,
    )
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "content": (
                    "It seems I'm having trouble finding new fantasy options for you "
                    "right now. The same titles keep appearing. Could you specify a "
                    "different genre or type of movie you might be interested in?"
                )
            }
        ],
    )
    result = await inbox.handle_message(
        _msg("You've just mentioned these, give me some others", message_id=20301)
    )
    assert result.grabbed is False
    assert result.reply_markup is None
    assert "trouble" in (result.reply or "").lower() or "same titles" in (
        result.reply or ""
    ).lower()


@pytest.mark.asyncio
async def test_inbox_0204_web_search_fallback_when_discover_exhausted(
    inbox: TelegramInbox, monkeypatch
):
    """When discover has nothing left, fall back to web_search + search_title."""
    from hearth.telegram.buttons import GENRE_FANTASY, GENRE_SCI_FI

    # Ban every released fantasy fixture id so discover is empty.
    exhaust_ids = [
        497698,
        1417,
        120,
        129,
        4935,
        118,
        2493,
        1110001,
        1110002,
        1110003,
    ]
    inbox.memory.remember_shown(
        -1001,
        [{"tmdbId": tid} for tid in exhaust_ids],
    )
    inbox.memory.set_discover_cursor(
        -1001,
        genre_ids=[GENRE_FANTASY],
        exclude_genre_ids=[GENRE_SCI_FI],
        page=1,
        media_type="movie",
    )
    web_calls: list[str] = []
    from hearth.tools import websearch as websearch_mod

    original = websearch_mod.web_search

    async def _cap(args):
        web_calls.append(str(args.get("query") or ""))
        return await original(args)

    monkeypatch.setattr(websearch_mod, "web_search", _cap)

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "discover_by_genre",
                            "arguments": {
                                "genre_ids": [GENRE_FANTASY],
                                "exclude_genre_ids": [GENRE_SCI_FI],
                                "query": "fantasy others",
                                "limit": 3,
                            },
                        }
                    }
                ]
            }
        ],
    )
    result = await inbox.handle_message(
        _msg("give me some others", message_id=20401)
    )
    assert web_calls, "expected web_search fallback when discover exhausted"
    assert result.grabbed is False
    if result.reply_markup:
        body = (result.reply or "").lower()
        assert (
            "willow" in body
            or "dark crystal" in body
            or "labyrinth" in body
            or "princess bride" in body
        )


def test_telegram_memory_window_is_24_turns_and_6h():
    from hearth.telegram import memory as mem

    assert mem.MAX_TURNS >= 24
    assert mem.IDLE_TTL_S >= 6 * 60 * 60


def test_amsterdam_today_helper():
    import re

    from hearth.telegram.inbox import TelegramInbox

    day = TelegramInbox._amsterdam_today()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)


# --- Spider-Man triple-queue + Land web_search (live 2026-09-01 ~02:26–02:28) ---


def test_format_queued_never_emits_raw_tmdb_label():
    from hearth.telegram.progress import format_queued

    assert "tmdb:" not in format_queued("tmdb:429617", 2019, "Overseerr").lower()
    assert "tmdb:" not in format_queued("tmdb:1930", None, "Overseerr").lower()
    assert "Amazing" in format_queued("The Amazing Spider-Man", 2012, "Overseerr")


def test_dedup_seen_tmdb_window():
    deduper = Deduper(window_s=60)
    assert deduper.seen_tmdb(-1001, 429617) is False
    assert deduper.seen_tmdb(-1001, 429617) is True
    assert deduper.seen_tmdb(-1001, 1930) is False


@pytest.mark.asyncio
async def test_inbox_0227_get_matrix_does_not_queue_spiderman(inbox: TelegramInbox):
    """Live bug: 90s sci-fi Get buttons on screen; stale Spider-Man callbacks must not queue."""
    from hearth.telegram.inbox import PendingDisambiguation

    matrix = {
        "title": "The Matrix",
        "year": 1999,
        "tmdbId": 603,
        "mediaId": 603,
        "mediaType": "movie",
    }
    recall = {
        "title": "Total Recall",
        "year": 1990,
        "tmdbId": 861,
        "mediaId": 861,
        "mediaType": "movie",
    }
    troopers = {
        "title": "Starship Troopers",
        "year": 1997,
        "tmdbId": 563,
        "mediaId": 563,
        "mediaType": "movie",
    }
    fifth = {
        "title": "The Fifth Element",
        "year": 1997,
        "tmdbId": 18,
        "mediaId": 18,
        "mediaType": "movie",
    }
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[matrix, recall, troopers, fifth],
        media_kind="movie",
        query="90s sci-fi",
        created_message_id=22701,
        last_bot_reply=(
            "1. The Matrix (1999)\n2. Total Recall (1990)\n"
            "3. Starship Troopers (1997)\n4. The Fifth Element (1997)"
        ),
    )
    pipeline.overseerr_queue.clear()

    # Stale Amazing Spider-Man + Far From Home callbacks while sci-fi list is live.
    stale_asm = await inbox.handle_callback(
        {
            "id": "cb-asm",
            "data": "q:movie:1930",
            "from": {"id": 42},
            "message": {"message_id": 22699, "chat": {"id": -1001}},
        }
    )
    assert stale_asm.grabbed is False
    assert "Queued" not in (stale_asm.reply or "")
    assert "Spider" not in (stale_asm.reply or "")

    stale_ffh = await inbox.handle_callback(
        {
            "id": "cb-ffh",
            "data": "q:movie:429617",
            "from": {"id": 42},
            "message": {"message_id": 22699, "chat": {"id": -1001}},
        }
    )
    assert stale_ffh.grabbed is False
    assert "tmdb:" not in (stale_ffh.reply or "").lower()
    assert len(pipeline.overseerr_queue) == 0

    # Real Get 1 on Matrix queues Matrix only.
    ok = await inbox.handle_callback(
        {
            "id": "cb-matrix",
            "data": "q:movie:603",
            "from": {"id": 42},
            "message": {"message_id": 22701, "chat": {"id": -1001}},
        }
    )
    assert ok.grabbed is True
    assert "Matrix" in ok.reply
    assert "Spider" not in ok.reply
    assert "tmdb:" not in ok.reply.lower()
    assert len(pipeline.overseerr_queue) == 1


@pytest.mark.asyncio
async def test_inbox_0227_duplicate_callback_does_not_triple_queue(
    inbox: TelegramInbox,
):
    """Duplicate Get deliveries must not queue the same tmdb id three times."""
    from hearth.telegram.inbox import PendingDisambiguation

    option = {
        "title": "The Amazing Spider-Man",
        "year": 2012,
        "tmdbId": 1930,
        "mediaId": 1930,
        "mediaType": "movie",
    }
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[option],
        media_kind="movie",
        query="Spider-Man",
        created_message_id=22710,
    )
    pipeline.overseerr_queue.clear()

    cb = {
        "id": "cb-dup-1",
        "data": "q:movie:1930",
        "from": {"id": 42},
        "message": {"message_id": 22710, "chat": {"id": -1001}},
    }
    first = await inbox.handle_callback(cb)
    assert first.grabbed is True
    assert "Amazing Spider-Man" in first.reply
    assert "tmdb:" not in first.reply.lower()

    # Same id again (Telegram redelivery / double-tap) — suppressed.
    second = await inbox.handle_callback({**cb, "id": "cb-dup-2"})
    third = await inbox.handle_callback(
        {
            "id": "cb-dup-3",
            "data": "q:movie:429617",
            "from": {"id": 42},
            "message": {"message_id": 22710, "chat": {"id": -1001}},
        }
    )
    assert second.grabbed is False
    assert third.grabbed is False
    assert "tmdb:" not in (second.reply or "").lower()
    assert "tmdb:" not in (third.reply or "").lower()
    assert len(pipeline.overseerr_queue) == 1


@pytest.mark.asyncio
async def test_inbox_0227_queue_reply_never_contains_tmdb_label(
    inbox: TelegramInbox,
):
    """Even a title-less pending row must not post 'Queued tmdb:N'."""
    from hearth.telegram.inbox import PendingDisambiguation

    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[
            {
                "title": "tmdb:429617",
                "year": 2019,
                "tmdbId": 429617,
                "mediaId": 429617,
                "mediaType": "movie",
            }
        ],
        media_kind="movie",
        query="Spider-Man",
        created_message_id=22720,
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_callback(
        {
            "id": "cb-ffh-title",
            "data": "q:movie:429617",
            "from": {"id": 42},
            "message": {"message_id": 22720, "chat": {"id": -1001}},
        }
    )
    assert result.grabbed is True
    assert "tmdb:" not in (result.reply or "").lower()
    assert "Spider-Man" in result.reply or "that title" in result.reply


@pytest.mark.asyncio
async def test_inbox_0227_land_catalog_miss_web_search_offers_land_2021(
    inbox: TelegramInbox, monkeypatch
):
    """Land miss → web_search → Land (2021) Get offer; no auto-queue; not La La Land."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.tools import websearch as websearch_mod

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

    land_lookups = {"n": 0}

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower().strip()
        if "688271" in q or q.startswith("tmdb:688271"):
            return {"mode": "mock", "service": "overseerr", "results": [land]}
        if "la la" in q:
            return {"mode": "mock", "service": "overseerr", "results": [la_la]}
        if q == "land" or (q.startswith("land") and "la la" not in q):
            land_lookups["n"] += 1
            # First Overseerr hit is a hard miss; later lookups (after web) resolve.
            if land_lookups["n"] == 1:
                return {"mode": "mock", "service": "overseerr", "results": []}
            return {"mode": "mock", "service": "overseerr", "results": [land, la_la]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    web_calls: list[str] = []
    original = websearch_mod.web_search

    async def _cap(args):
        web_calls.append(str(args.get("query") or ""))
        return await original(args)

    monkeypatch.setattr(websearch_mod, "web_search", _cap)

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_title",
                            "arguments": {"title": "Land", "media_type": "movie"},
                        }
                    }
                ]
            }
        ],
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(_msg("Land", message_id=22730))
    assert web_calls, "catalog miss must trigger web_search"
    assert result.grabbed is False
    assert "couldn't find" not in (result.reply or "").lower()
    assert "La La Land" not in (result.reply or "")
    body = (result.reply or "").lower()
    assert "land" in body
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert any(int(o.get("tmdbId") or 0) == 688271 for o in pending.options)
    assert all(int(o.get("tmdbId") or 0) != 313369 for o in pending.options)
    assert result.reply_markup is not None
    assert len(pipeline.overseerr_queue) == 0


@pytest.mark.asyncio
async def test_inbox_0227_do_a_websearch_uses_tool(
    inbox: TelegramInbox, monkeypatch
):
    """'Do a websearch' must call web_search — never 'I cannot search the web'."""
    from hearth.telegram import catalog as catalog_mod
    from hearth.telegram import inbox as inbox_mod
    from hearth.telegram.inbox import PendingDisambiguation
    from hearth.tools import websearch as websearch_mod

    land = {
        "title": "Land",
        "year": 2021,
        "tmdbId": 688271,
        "mediaId": 688271,
        "mediaType": "movie",
    }

    async def _search(query: str, *args, **kwargs):
        q = (query or "").lower().strip()
        if "la la" in q:
            return {"mode": "mock", "service": "overseerr", "results": []}
        if "land" in q or "688271" in q:
            return {"mode": "mock", "service": "overseerr", "results": [land]}
        return {"mode": "mock", "service": "overseerr", "results": []}

    monkeypatch.setattr(inbox_mod.overseerr, "search", _search)
    monkeypatch.setattr(catalog_mod.overseerr, "search", _search)

    web_calls: list[str] = []
    original = websearch_mod.web_search

    async def _cap(args):
        web_calls.append(str(args.get("query") or ""))
        return await original(args)

    monkeypatch.setattr(websearch_mod, "web_search", _cap)

    # Prior miss context: subject is Land.
    inbox.memory.set_subject(-1001, "Land", media_kind="movie")
    inbox.memory.record_user(-1001, "Land")
    inbox.memory.record_bot(
        -1001,
        "I couldn't find a movie titled 'Land' in the catalog.",
        search_title="Land",
        media_kind="movie",
    )
    inbox.pending[-1001] = PendingDisambiguation(
        chat_id=-1001,
        options=[{"title": "Land", "year": None, "mediaType": "movie"}],
        media_kind="movie",
        query="Land",
        created_message_id=22740,
        last_bot_reply="I couldn't find a movie titled 'Land' in the catalog.",
    )

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "web_search",
                            "arguments": {
                                "query": "Land 2021 Robin Wright movie",
                                "media_type": "movie",
                            },
                        }
                    }
                ]
            }
        ],
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(_msg("Do a websearch", message_id=22741))
    assert web_calls, "expected explicit web_search tool call"
    assert "cannot" not in (result.reply or "").lower()
    assert "unable" not in (result.reply or "").lower()
    assert "i can't search" not in (result.reply or "").lower()
    assert result.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    pending = inbox.pending.get(-1001)
    assert pending is not None
    titles = " ".join(str(o.get("title") or "") for o in pending.options)
    assert "Land" in titles
    assert "La La Land" not in titles

# --- Named title+year → Get, not encyclopedia (live 2026-09-01 ~10:02) ---


def test_named_title_year_helpers():
    from hearth.telegram.intent import looks_like_named_title_year
    from hearth.telegram.parse import parse_message_text, strip_title_year_media

    assert looks_like_named_title_year("Miss you love you 2026 film")
    assert looks_like_named_title_year("Miss You, Love You (2026)")
    assert looks_like_named_title_year("the 2026 film Miss you love you")
    assert not looks_like_named_title_year("show me cool fantasy")
    assert strip_title_year_media("Miss you love you 2026 film") == (
        "Miss you love you",
        2026,
    )
    parsed = parse_message_text("Miss you love you 2026 film")
    assert parsed.title == "Miss you love you"
    assert parsed.year == 2026
    assert parsed.media_kind == "movie"


def test_looks_like_encyclopedia_dump():
    from hearth.telegram.agent import looks_like_encyclopedia_dump

    dump = (
        "Miss You, Love You is a 2026 comedy starring Jim Rash and Allison Janney, "
        "set for HBO on May 29 2026 with 88% on Rotten Tomatoes. "
        "https://en.wikipedia.org/wiki/Miss_You,_Love_You "
        "https://www.rottentomatoes.com/m/x?utm_source=openai"
    )
    assert looks_like_encyclopedia_dump(dump)
    assert not looks_like_encyclopedia_dump("Did you mean Miss You, Love You (2026)?")


@pytest.mark.asyncio
async def test_inbox_1002_miss_you_love_you_2026_film_offers_get(
    inbox: TelegramInbox, monkeypatch
):
    """Named title+year must offer Get — never a Wikipedia/RT encyclopedia dump."""
    from hearth.fixtures import pipeline

    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(
        _msg("Miss you love you 2026 film", message_id=100201)
    )
    assert result.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    body = result.reply or ""
    lowered = body.lower()
    assert "wikipedia.org" not in lowered
    assert "rottentomatoes.com" not in lowered
    assert "utm_source=openai" not in lowered
    assert "jim rash" not in lowered
    assert "allison janney" not in lowered
    assert "88%" not in body
    assert "Miss You" in body or "Miss you" in body or "miss you" in lowered
    assert "2026" in body
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert any(
        int(o.get("tmdbId") or o.get("mediaId") or 0) == 1482001
        or "miss you" in str(o.get("title") or "").lower()
        for o in pending.options
    )
    assert result.reply_markup is not None
    markup = str(result.reply_markup)
    assert "q:movie:" in markup or "Get" in markup


@pytest.mark.asyncio
async def test_inbox_1002_named_title_year_agent_dump_forced_to_get(
    inbox: TelegramInbox, monkeypatch
):
    """If the model dumps wiki/RT text, Python forces search_title → Get."""
    from hearth.fixtures import pipeline

    dump = (
        "Miss You, Love You (2026) is an upcoming comedy starring Jim Rash and "
        "Allison Janney, premiering on HBO May 29 2026 (88% RT). "
        "Read more: https://en.wikipedia.org/wiki/Miss_You,_Love_You "
        "https://www.rottentomatoes.com/m/miss_you_love_you?utm_source=openai"
    )
    pipeline.overseerr_queue.clear()
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "web_search",
                            "arguments": {
                                "query": "Miss you love you 2026 film",
                                "year": 2026,
                                "media_type": "movie",
                            },
                        }
                    }
                ]
            },
            {"content": dump},
        ],
    )
    # Concrete title without bare year+film so the agent loop runs (not instant).
    result = await inbox.handle_message(
        _msg("Miss you love you film", message_id=100211)
    )
    assert result.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    body = (result.reply or "").lower()
    assert "wikipedia.org" not in body
    assert "rottentomatoes.com" not in body
    assert "utm_source=openai" not in body
    assert "jim rash" not in body
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert result.reply_markup is not None or any(
        "miss you" in str(o.get("title") or "").lower() for o in pending.options
    )


@pytest.mark.asyncio
async def test_inbox_1002_paren_title_year_offers_get(inbox: TelegramInbox):
    """Miss You, Love You (2026) instant path offers Get, no auto-queue."""
    from hearth.fixtures import pipeline

    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(
        _msg("Miss You, Love You (2026)", message_id=100220)
    )
    assert result.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    body = (result.reply or "").lower()
    assert "wikipedia.org" not in body
    assert "utm_source=openai" not in body
    assert "miss you" in body
    assert result.reply_markup is not None
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert any(int(o.get("tmdbId") or 0) == 1482001 for o in pending.options)


@pytest.mark.asyncio
async def test_inbox_1002_discover_still_skips_unreleased_vapor(
    inbox: TelegramInbox, monkeypatch
):
    """Discover/browse lists still skip random unreleased filler (not named titles)."""
    from hearth.telegram.buttons import GENRE_FANTASY, GENRE_SCI_FI

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "discover_by_genre",
                            "arguments": {
                                "genre_ids": [GENRE_FANTASY],
                                "exclude_genre_ids": [GENRE_SCI_FI],
                                "query": "Fantasy",
                                "limit": 3,
                            },
                        }
                    }
                ]
            }
        ],
    )
    result = await inbox.handle_message(_msg("show me cool fantasy", message_id=100230))
    assert result.grabbed is False
    pending = inbox.pending.get(-1001)
    assert pending is not None
    joined = " | ".join(str(r.get("title") or "") for r in pending.options).lower()
    assert "odyssey" not in joined
    assert "moana" not in joined
    assert "miss you" not in joined


# --- actor / person filmography (TMDB person credits, not Overseerr title) ---


def test_looks_like_person_ask_and_extract_name():
    from hearth.telegram.agent import (
        extract_person_name,
        looks_like_person_ask,
        looks_like_person_followup,
        resolve_person_query,
    )

    assert looks_like_person_ask("Give me a few movies with leonardo dicaprot")
    assert looks_like_person_ask("films met Leonardo DiCaprio")
    assert looks_like_person_ask("Geef me een paar films met Leonardo DiCaprio")
    assert looks_like_person_ask("Give me a few movies with tom hanks")
    assert looks_like_person_ask("Tom Hanks? Movies starring that guy?")
    assert not looks_like_person_ask("Land (2021)")
    # Plot / descriptive asks are title guesses — not person filmography.
    assert not looks_like_person_ask(
        "a movie about a boy with glasses who is a wizard"
    )
    assert not looks_like_person_ask(
        "Ik zoek die film met die bebrilde tovenaar."
    )
    assert not looks_like_person_ask(
        "Nee, niet die. Ik denk dat het van die kunstenaar leonardo dicaprio was"
    )
    assert extract_person_name("Give me a few movies with leonardo dicaprot") == (
        "leonardo dicaprot"
    )
    assert extract_person_name("films met Leonardo DiCaprio") == "Leonardo DiCaprio"
    assert extract_person_name("Tom Hanks? Movies starring that guy?") == "Tom Hanks"
    hist = [
        {"role": "user", "text": "Give me a few movies with tom hanks"},
        {
            "role": "bot",
            "text": "I couldn't find any movies with Tom Hanks in the catalog.",
        },
    ]
    assert looks_like_person_followup("He's like, sortof famous?", hist)
    assert resolve_person_query("He's like, sortof famous?", hist).lower() == "tom hanks"
    assert not looks_like_person_followup(
        "a movie about a boy with glasses who is a wizard", hist
    )


@pytest.mark.asyncio
async def test_inbox_movies_with_dicaprot_typo_yeah_lists_credits(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: leonardo dicaprot → Yeah must list DiCaprio films with Get.

    Never 'couldn't find in the catalog' / 'Overseerr catalog'. Overseerr title
    search is the wrong catalog — TMDB person credits is required.
    """
    from hearth.fixtures import pipeline
    from hearth.telegram import inbox as inbox_mod

    person_calls: list[str] = []
    credit_calls: list[int] = []
    original_person = inbox_mod.overseerr.search_person
    original_credits = inbox_mod.overseerr.person_combined_credits

    async def _cap_person(query: str, *args, **kwargs):
        person_calls.append(str(query))
        return await original_person(query)

    async def _cap_credits(person_id: int, *args, **kwargs):
        credit_calls.append(int(person_id))
        return await original_credits(person_id)

    monkeypatch.setattr(inbox_mod.overseerr, "search_person", _cap_person)
    monkeypatch.setattr(inbox_mod.overseerr, "person_combined_credits", _cap_credits)

    # Model wrongly tries Overseerr title search for the person string.
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_title",
                            "arguments": {
                                "title": "Leonardo DiCaprio",
                                "media_type": "movie",
                            },
                        }
                    }
                ]
            },
            {
                "content": (
                    "I couldn't find specific movies with Leonardo DiCaprio "
                    "in the catalog."
                )
            },
        ],
    )
    pipeline.overseerr_queue.clear()
    first = await inbox.handle_message(
        _msg("Give me a few movies with leonardo dicaprot", message_id=11001)
    )
    assert first.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    body = (first.reply or "").lower()
    assert "overseerr catalog" not in body
    assert "couldn't find" not in body or "did you mean" in body
    # Typo path: confirm the corrected person name (no Get yet).
    assert "leonardo dicaprio" in body
    assert "did you mean" in body
    assert first.reply_markup is None
    assert inbox.pending_person.get(-1001) is not None
    assert person_calls, "must call TMDB/Overseerr person search"

    # Yeah continues actor credits — not a dead catalog miss.
    _patch_openai_tools(
        monkeypatch,
        [
            {
                "content": (
                    "I couldn't find specific movies with Leonardo DiCaprio "
                    "in the catalog. I'm using the Overseerr catalog."
                )
            }
        ],
    )
    yeah = await inbox.handle_message(_msg("Yeah", message_id=11002))
    assert yeah.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    ybody = (yeah.reply or "").lower()
    assert "overseerr catalog" not in ybody
    assert "i'm using the overseerr" not in ybody
    assert "couldn't find" not in ybody
    assert credit_calls and 6193 in credit_calls
    pending = inbox.pending.get(-1001)
    assert pending is not None and len(pending.options) >= 2
    titles = {str(o.get("title") or "").lower() for o in pending.options}
    assert "titanic" in titles or "inception" in titles or "the revenant" in titles
    # Unreleased filler must not appear.
    assert "untitled dicaprio project" not in titles
    assert yeah.reply_markup is not None
    assert "get" in str(yeah.reply_markup).lower() or any(
        "Get" in (btn.get("text") or "")
        for row in (yeah.reply_markup or {}).get("inline_keyboard") or []
        for btn in row
    )


@pytest.mark.asyncio
async def test_inbox_films_met_person_offers_get_no_overseerr_catalog_claim(
    inbox: TelegramInbox, monkeypatch
):
    """Dutch 'films met …' uses person credits; never claims Overseerr catalog."""
    from hearth.fixtures import pipeline

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_person",
                            "arguments": {
                                "name": "Leonardo DiCaprio",
                                "media_type": "movie",
                                "confirmed": True,
                                "limit": 3,
                            },
                        }
                    }
                ]
            }
        ],
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(
        _msg("Geef me een paar films met Leonardo DiCaprio", message_id=11010)
    )
    assert result.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    assert "overseerr catalog" not in (result.reply or "").lower()
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert any(int(o.get("tmdbId") or 0) in {597, 27205, 11324, 68718, 106646, 281957} for o in pending.options)
    assert result.reply_markup is not None


@pytest.mark.asyncio
async def test_inbox_person_typo_confirm_from_history_on_yeah(
    inbox: TelegramInbox, monkeypatch
):
    """When the model typo-confirmed in prose (no tool state), Yeah still credits."""
    from hearth.fixtures import pipeline
    from hearth.telegram.inbox import PendingDisambiguation

    # Seed: prior person ask + bot typo confirm without pending_person.
    inbox.memory.record_user(
        -1001, "Give me a few movies with leonardo dicaprot"
    )
    inbox.memory.record_bot(-1001, "Did you mean Leonardo DiCaprio?")
    assert inbox.pending_person.get(-1001) is None

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "content": (
                    "I couldn't find specific movies with Leonardo DiCaprio "
                    "in the catalog. I'm using the Overseerr catalog…"
                )
            }
        ],
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(_msg("Yeah", message_id=11020))
    assert result.grabbed is False
    assert "overseerr" not in (result.reply or "").lower() or "queued" in (
        result.reply or ""
    ).lower()
    assert "couldn't find" not in (result.reply or "").lower()
    assert "overseerr catalog" not in (result.reply or "").lower()
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert len(pending.options) >= 2
    assert isinstance(pending, PendingDisambiguation)
    assert result.reply_markup is not None


@pytest.mark.asyncio
async def test_inbox_tom_hanks_person_credits_not_catalog_miss(
    inbox: TelegramInbox, monkeypatch
):
    """Live bug: movies with Tom Hanks must offer Get credits, never catalog miss."""
    from hearth.fixtures import pipeline

    _patch_openai_tools(
        monkeypatch,
        [
            {
                "content": (
                    "I couldn't find any movies with Tom Hanks in the catalog."
                )
            }
        ],
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(
        _msg("Give me a few movies with tom hanks", message_id=12001)
    )
    assert result.grabbed is False
    assert len(pipeline.overseerr_queue) == 0
    body = (result.reply or "").lower()
    assert "overseerr catalog" not in body
    assert "couldn't find" not in body
    assert "in the catalog" not in body
    pending = inbox.pending.get(-1001)
    assert pending is not None and len(pending.options) >= 2
    titles = {str(o.get("title") or "").lower() for o in pending.options}
    assert "forrest gump" in titles or "toy story" in titles or "saving private ryan" in titles
    assert "untitled hanks project" not in titles
    assert result.reply_markup is not None


@pytest.mark.asyncio
async def test_inbox_person_followup_after_miss_retries_search_person(
    inbox: TelegramInbox, monkeypatch
):
    """Tom Hanks? Movies starring that guy? after a miss must retry person search."""
    from hearth.fixtures import pipeline

    inbox.memory.record_user(-1001, "Give me a few movies with tom hanks")
    inbox.memory.record_bot(
        -1001, "I couldn't find any movies with Tom Hanks in the catalog."
    )
    _patch_openai_tools(
        monkeypatch,
        [{"content": "Maybe check the spelling? I'm using the Overseerr catalog."}],
    )
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(
        _msg("Tom Hanks? Movies starring that guy?", message_id=12002)
    )
    assert result.grabbed is False
    body = (result.reply or "").lower()
    assert "overseerr catalog" not in body
    assert "spelling" not in body
    assert "couldn't find" not in body
    pending = inbox.pending.get(-1001)
    assert pending is not None
    assert any(int(o.get("tmdbId") or 0) in {13, 862, 857, 497} for o in pending.options)
    assert result.reply_markup is not None


@pytest.mark.asyncio
async def test_inbox_second_actor_streep_not_hardcoded(
    inbox: TelegramInbox, monkeypatch
):
    """A second actor (not DiCaprio/Hanks aliases only) must also resolve via person search."""
    from hearth.fixtures import pipeline

    _patch_openai_tools(monkeypatch, [{"content": "nope"}])
    pipeline.overseerr_queue.clear()
    result = await inbox.handle_message(
        _msg("films met Meryl Streep", message_id=12003)
    )
    assert result.grabbed is False
    assert "overseerr catalog" not in (result.reply or "").lower()
    pending = inbox.pending.get(-1001)
    assert pending is not None
    titles = {str(o.get("title") or "").lower() for o in pending.options}
    assert "the devil wears prada" in titles or "the deer hunter" in titles
    assert result.reply_markup is not None


@pytest.mark.asyncio
async def test_overseerr_search_person_keeps_person_when_title_filter_would_drop(
    monkeypatch,
):
    """Live-shaped Overseerr multi-search: person after 8 title hits + knownFor.

    Title-only movie/tv filtering (or truncating to 8 before person filter) would
    drop the person. search_person must keep them. Also accept knownFor when
    mediaType is missing. Never pass mediaType=person as a query param.
    """
    import httpx
    from hearth.config import settings
    from hearth.tools.arr import Overseerr, _is_person_result

    # 8 movie rows first (what title-only / [:8] would keep), then person.
    movie_rows = [
        {
            "id": 1000 + i,
            "mediaType": "movie",
            "title": f"Tom Hanks Doc Part {i}",
            "releaseDate": "2010-01-01",
            "popularity": 1.0 + i,
        }
        for i in range(8)
    ]
    person_row = {
        "id": 31,
        # mediaType intentionally omitted — live edge; knownFor marks person.
        "name": "Tom Hanks",
        "popularity": 82.4,
        "profilePath": "/hanks.jpg",
        "knownFor": [
            {
                "id": 13,
                "mediaType": "movie",
                "title": "Forrest Gump",
                "releaseDate": "1994-07-06",
            }
        ],
    }
    credits_payload = {
        "id": 31,
        "cast": [
            {
                "id": 13,
                "mediaType": "movie",
                "title": "Forrest Gump",
                "releaseDate": "1994-07-06",
                "popularity": 95.0,
                "voteCount": 27000,
            },
            {
                "id": 862,
                "mediaType": "movie",
                "title": "Toy Story",
                "releaseDate": "1995-11-22",
                "popularity": 90.0,
                "voteCount": 18000,
            },
            {
                "id": 857,
                "mediaType": "movie",
                "title": "Saving Private Ryan",
                "releaseDate": "1998-07-24",
                "popularity": 78.0,
                "voteCount": 15000,
            },
        ],
        "crew": [],
    }

    calls: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("GET", "http://overseerr.test"),
                    response=httpx.Response(self.status_code),
                )

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, path, params=None, headers=None):
            calls.append({"path": path, "params": dict(params or {})})
            if path == "/api/v1/search":
                # Prove title-only filter would drop the person.
                title_only = [
                    r for r in movie_rows + [person_row] if r.get("mediaType") in {"movie", "tv"}
                ]
                assert len(title_only) == 8
                assert not any(_is_person_result(r) for r in title_only)
                assert _is_person_result(person_row)
                return FakeResponse(
                    200,
                    {
                        "page": 1,
                        "totalPages": 1,
                        "totalResults": 9,
                        "results": movie_rows + [person_row],
                    },
                )
            if path == "/api/v1/person/31/combined_credits":
                return FakeResponse(200, credits_payload)
            return FakeResponse(404, {})

        async def aclose(self):
            return None

    monkeypatch.setattr(settings, "overseerr_api_key", "test-key")
    monkeypatch.setattr(settings, "overseerr_url", "http://overseerr.test")
    monkeypatch.setattr(settings, "mock_if_unconfigured", False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = Overseerr()
    await client.aclose()
    found = await client.search_person("tom hanks")
    assert found.get("mode") == "live"
    people = found.get("results") or []
    assert people and int(people[0]["id"]) == 31
    assert people[0]["name"] == "Tom Hanks"
    assert people[0]["mediaType"] == "person"
    assert people[0].get("knownFor")

    search_calls = [c for c in calls if c["path"] == "/api/v1/search"]
    assert search_calls
    for c in search_calls:
        assert "mediaType" not in c["params"]
        assert c["params"].get("query") == "tom hanks"

    credits = await client.person_combined_credits(31)
    assert len(credits.get("cast") or []) >= 3
    await client.aclose()


@pytest.mark.asyncio
async def test_overseerr_search_person_second_actor_live_http(monkeypatch):
    """Second actor via live-shaped multi-search JSON (no production hardcoding)."""
    import httpx
    from hearth.config import settings
    from hearth.tools.arr import Overseerr

    calls: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("GET", "http://overseerr.test"),
                    response=httpx.Response(self.status_code),
                )

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, path, params=None, headers=None):
            calls.append({"path": path, "params": dict(params or {})})
            if path == "/api/v1/search":
                return FakeResponse(
                    200,
                    {
                        "page": 1,
                        "totalPages": 1,
                        "results": [
                            {
                                "id": 9001,
                                "mediaType": "movie",
                                "title": "Streep: A Documentary",
                                "releaseDate": "2012-01-01",
                                "popularity": 3.0,
                            },
                            {
                                "id": 5064,
                                "mediaType": "person",
                                "name": "Meryl Streep",
                                "popularity": 55.1,
                                "knownFor": [
                                    {
                                        "id": 152601,
                                        "mediaType": "movie",
                                        "title": "The Devil Wears Prada",
                                    }
                                ],
                            },
                        ],
                    },
                )
            if path.startswith("/api/v1/person/"):
                return FakeResponse(
                    200,
                    {
                        "id": 5064,
                        "cast": [
                            {
                                "id": 152601,
                                "mediaType": "movie",
                                "title": "The Devil Wears Prada",
                                "releaseDate": "2006-06-30",
                                "popularity": 70.0,
                                "voteCount": 12000,
                            }
                        ],
                        "crew": [],
                    },
                )
            return FakeResponse(404, {})

        async def aclose(self):
            return None

    monkeypatch.setattr(settings, "overseerr_api_key", "test-key")
    monkeypatch.setattr(settings, "overseerr_url", "http://overseerr.test")
    monkeypatch.setattr(settings, "mock_if_unconfigured", False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = Overseerr()
    await client.aclose()
    found = await client.search_person("meryl streep")
    assert any(int(p.get("id") or 0) == 5064 for p in (found.get("results") or []))
    for c in calls:
        if c["path"] == "/api/v1/search":
            assert "mediaType" not in c["params"]
    await client.aclose()
