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
def inbox(monkeypatch) -> TelegramInbox:
    monkeypatch.setattr(settings, "telegram_bot_token", "1:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", "-1001")
    monkeypatch.setattr(settings, "telegram_user_ids", "")
    monkeypatch.setattr(settings, "telegram_prefer_overseerr", False)
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 20)
    monkeypatch.setattr(settings, "overseerr_api_key", "")
    pipeline.radarr_queue.clear()
    pipeline.sonarr_queue.clear()
    pipeline.overseerr_queue.clear()
    box = TelegramInbox()
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
async def test_inbox_not_found(inbox: TelegramInbox):
    result = await inbox.handle_message(_msg("ZzzNotARealFilm999", message_id=70))
    assert result.grabbed is False
    assert "Couldn't find" in result.reply


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
