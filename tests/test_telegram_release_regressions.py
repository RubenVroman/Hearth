from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from hearth.config import settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.models import BotReply, MediaHit, MediaQuery
from hearth.telegram.parse import parse_message_text
from hearth.telegram.progress import ProgressTracker
from hearth.telegram.service import TelegramBotService, _request_status_for
from hearth.telegram.store import TelegramStore
from hearth.tools.arr import Overseerr, _indistinguishable_overseerr_hits


CHAT_ID = -1001


class LiveOverseerr:
    live = True

    def __init__(
        self,
        *,
        details: dict[str, Any] | None = None,
        request_result: dict[str, Any] | None = None,
    ) -> None:
        self.details = details or {
            "ok": True,
            "mode": "live",
            "mediaInfo": {"status": 3},
            "requests": [{"id": 91, "status": 2}],
        }
        self.request_result = request_result or {
            "ok": True,
            "mode": "live",
            "requestId": 91,
            "requestStatus": 2,
            "mediaStatus": 3,
        }
        self.request_calls: list[dict[str, Any]] = []

    async def media_details(self, *_: Any) -> dict[str, Any]:
        return dict(self.details)

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(kwargs)
        return dict(self.request_result)


class LiveArr:
    live = True
    max_retries = 3

    def __init__(
        self,
        downloads: list[dict[str, Any]],
        *,
        retry_result: dict[str, Any] | None = None,
    ) -> None:
        self.downloads = downloads
        self.retry_result = retry_result or {
            "ok": False,
            "mode": "live",
            "reason": "healthy",
        }
        self.retry_calls: list[dict[str, Any]] = []

    async def queue(self, _title: str) -> dict[str, Any]:
        return {"mode": "live", "downloads": list(self.downloads)}

    async def retry_download(self, _title: str, **kwargs: Any) -> dict[str, Any]:
        self.retry_calls.append(kwargs)
        return dict(self.retry_result)


def _callback(data: str) -> dict[str, Any]:
    return {
        "id": "callback-1",
        "from": {"id": 7, "is_bot": False},
        "data": data,
        "message": {
            "message_id": 900,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
        },
    }


def _button(
    bot: TelegramMediaBot,
    store: TelegramStore,
    *,
    media_type: str = "movie",
    tmdb_id: int = 550,
    season: int | None = None,
) -> str:
    data = bot._callback_codec().encode(  # noqa: SLF001 - signing boundary test
        media_type,  # type: ignore[arg-type]
        tmdb_id,
        CHAT_ID,
        season=season,
    )
    store.put_callback_media(
        data,
        {
            "chat_id": CHAT_ID,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "title": "Fight Club" if media_type == "movie" else "Severance",
            "year": 1999 if media_type == "movie" else 2022,
            "season": season,
        },
    )
    return data


def test_dotted_episode_notation_is_never_widened_to_a_season_request() -> None:
    for value in ("Show.S01E02", "Show-S01-E02", "Show_S01E02", "ShowS01E02"):
        parsed = parse_message_text(value)
        assert parsed.action == "reject"
        assert parsed.reason == "episode_not_supported"


def test_season_before_typed_tmdb_id_is_preserved_or_rejected_for_movie() -> None:
    show = parse_message_text("S02 tmdb:tv:95396")
    words = parse_message_text("season 2 tmdb:tv:95396")
    movie = parse_message_text("S02 tmdb:movie:603")
    assert (show.action, show.season, show.media_type) == ("search", 2, "tv")
    assert (words.action, words.season, words.media_type) == ("search", 2, "tv")
    assert (movie.action, movie.reason) == ("reject", "movie_has_season")


@pytest.mark.parametrize(
    "value",
    [
        "tmdb:tv:95396 S1000",
        "tmdb:tv:95396 season two",
        "tmdb:tv:95396 S02E",
        "https://www.themoviedb.org/tv/95396-severance S1000",
    ],
)
def test_malformed_exact_id_season_never_defaults_to_all(value: str) -> None:
    parsed = parse_message_text(value)
    assert parsed.action == "reject"
    assert parsed.reason == "invalid_season"


def test_explicit_tv_season_keeps_button_when_another_season_is_processing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "app_secret_key", "release-test-secret")
    store = TelegramStore(tmp_path / "season-button.db")
    bot = TelegramMediaBot(store, overseerr_client=LiveOverseerr())
    reply = bot._results_reply(  # noqa: SLF001 - rendering invariant
        CHAT_ID,
        MediaQuery(action="search", title="Severance", season=2, media_type="tv"),
        [
            MediaHit(
                media_type="tv",
                tmdb_id=95396,
                title="Severance",
                media_status=3,
            )
        ],
    )
    assert reply.reply_markup
    assert reply.reply_markup["inline_keyboard"][0][0]["text"].endswith("S02")
    store.close()


@pytest.mark.asyncio
async def test_overseerr_rejects_nonintegral_mutation_coordinates() -> None:
    client = Overseerr()
    movie = await client.request(media_id=550.9, media_type="movie")  # type: ignore[arg-type]
    season = await client.request(
        media_id=550,
        media_type="tv",
        seasons=[2.9],  # type: ignore[list-item]
    )
    assert movie["reason"] == "invalid_media_id"
    assert season["reason"] == "invalid_seasons"
    await client.aclose()


def test_distinct_tmdb_ids_are_never_treated_as_one_title_only_match() -> None:
    hits = [
        {"id": 1, "mediaType": "movie", "title": "Fargo", "year": 1996},
        {"id": 2, "mediaType": "movie", "title": "Fargo", "year": 1996},
    ]
    assert not _indistinguishable_overseerr_hits(hits)


@pytest.mark.asyncio
async def test_conflicting_stable_id_never_falls_back_to_same_title() -> None:
    arr = LiveArr(
        [
            {
                "queueId": 9,
                "tmdbId": 103801,
                "mediaTitle": "Fargo",
                "title": "Fargo",
                "status": "failed",
            }
        ]
    )
    tracker = ProgressTracker(
        overseerr_client=LiveOverseerr(),
        radarr_client=arr,
        sonarr_client=arr,
    )
    tracker.track(
        CHAT_ID,
        "Fargo",
        "radarr",
        tmdb_id=275,
        media_type="movie",
        request_id=91,
        request_key="fargo-1996",
        request_status=2,
    )

    async def send(*_: Any) -> dict[str, Any]:
        return {"ok": True}

    await tracker.poll_once(send)
    assert arr.retry_calls == []


@pytest.mark.asyncio
async def test_sonarr_retry_is_bound_to_series_season_and_queue_id() -> None:
    details = {
        "ok": True,
        "mode": "live",
        "mediaInfo": {
            "status": 3,
            "externalServiceId": 42,
            "tvdbId": 371980,
        },
        "requests": [{"id": 91, "status": 2}],
    }
    arr = LiveArr(
        [
            {
                "queueId": 8,
                "seriesId": 42,
                "tvdbId": 371980,
                "seasonNumber": 1,
                "mediaTitle": "Severance",
                "status": "failed",
            },
            {
                "queueId": 9,
                "seriesId": 42,
                "tvdbId": 371980,
                "seasonNumber": 2,
                "mediaTitle": "Severance",
                "status": "failed",
            },
        ]
    )
    tracker = ProgressTracker(
        overseerr_client=LiveOverseerr(details=details),
        radarr_client=arr,
        sonarr_client=arr,
    )
    tracker.track(
        CHAT_ID,
        "Severance",
        "sonarr",
        season=2,
        tmdb_id=95396,
        media_type="tv",
        request_id=91,
        request_key="severance-s2",
        request_status=2,
    )

    async def send(*_: Any) -> dict[str, Any]:
        return {"ok": True}

    await tracker.poll_once(send)
    assert arr.retry_calls == [{"force": False, "reason": "auto:failed", "queue_id": 9}]


@pytest.mark.asyncio
async def test_missing_request_id_never_inherits_latest_season_status() -> None:
    details = {
        "ok": True,
        "mode": "live",
        "mediaInfo": {"status": 4},
        "requestStatus": 5,
        "requests": [{"id": 92, "status": 5}],
    }
    arr = LiveArr([])
    tracker = ProgressTracker(
        overseerr_client=LiveOverseerr(details=details),
        radarr_client=arr,
        sonarr_client=arr,
    )
    for season in (1, 2):
        tracker.track(
            CHAT_ID,
            "Show",
            "sonarr",
            season=season,
            tmdb_id=10,
            media_type="tv",
            request_key=f"show-s{season}",
            request_status=2,
        )
    messages: list[str] = []

    async def send(_chat_id: int, text: str) -> dict[str, Any]:
        messages.append(text)
        return {"ok": True}

    await tracker.poll_once(send)
    assert len(tracker.active) == 2
    assert messages == []
    assert _request_status_for(details, None) is None


@pytest.mark.asyncio
async def test_mock_overseerr_status_never_announces_plex_completion() -> None:
    class MockOverseerr:
        live = False

        async def media_details(self, *_: Any) -> dict[str, Any]:
            return {"ok": True, "mode": "mock", "mediaStatus": 5}

    arr = LiveArr([])
    tracker = ProgressTracker(
        overseerr_client=MockOverseerr(),
        radarr_client=arr,
        sonarr_client=arr,
    )
    tracker.track(
        CHAT_ID,
        "Fight Club",
        "radarr",
        tmdb_id=550,
        media_type="movie",
        request_id=91,
        request_key="fight-club",
    )
    messages: list[str] = []

    async def send(_chat_id: int, text: str) -> dict[str, Any]:
        messages.append(text)
        return {"ok": True}

    await tracker.poll_once(send)
    assert messages == []
    assert len(tracker.active) == 1


@pytest.mark.asyncio
async def test_terminal_retry_outbox_survives_restart_before_send() -> None:
    arr = LiveArr(
        [
            {
                "queueId": 9,
                "tmdbId": 550,
                "mediaTitle": "Fight Club",
                "status": "failed",
            }
        ],
        retry_result={
            "ok": False,
            "mode": "live",
            "reason": "exhausted",
            "max_attempts": 3,
        },
    )
    tracker = ProgressTracker(
        overseerr_client=LiveOverseerr(),
        radarr_client=arr,
        sonarr_client=arr,
    )
    tracker.track(
        CHAT_ID,
        "Fight Club",
        "radarr",
        tmdb_id=550,
        media_type="movie",
        request_id=91,
        request_key="fight-club",
    )
    checkpoints: list[dict[str, Any]] = []

    async def checkpoint(item: Any) -> None:
        checkpoints.append(item.to_dict())

    async def failed_send(*_: Any) -> dict[str, Any]:
        return {"ok": False}

    await tracker.poll_once(failed_send, checkpoint=checkpoint)
    durable = checkpoints[-1]
    assert durable["pending_terminal_text"]

    restored = ProgressTracker(
        overseerr_client=LiveOverseerr(),
        radarr_client=arr,
        sonarr_client=arr,
    )
    restored.restore([durable])
    messages: list[str] = []

    async def successful_send(_chat_id: int, text: str) -> dict[str, Any]:
        messages.append(text)
        return {"ok": True}

    await restored.poll_once(successful_send, checkpoint=checkpoint)
    assert len(messages) == 1
    assert restored.completed[0].terminal_state == "failed"


def test_atomic_request_journal_rolls_back_when_action_is_not_processing(
    tmp_path: Path,
) -> None:
    store = TelegramStore(tmp_path / "atomic.db")
    with pytest.raises(RuntimeError, match="not processing"):
        store.record_request_and_finish_callback(
            "missing-action",
            "request-key",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
            external_request_id=91,
            state="pending",
        )
    assert store.get_request("request-key") is None
    store.close()


@pytest.mark.asyncio
async def test_uncertain_callback_is_reconciled_through_provider_duplicate_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "app_secret_key", "release-test-secret")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    provider = LiveOverseerr(
        request_result={
            "ok": False,
            "mode": "live",
            "reason": "already_requested",
            "already": True,
            "status_code": 409,
        }
    )
    store = TelegramStore(tmp_path / "uncertain.db")
    bot = TelegramMediaBot(store, overseerr_client=provider)
    data = _button(bot, store)
    digest = hashlib.sha256(f"{CHAT_ID}:900:{data}".encode()).hexdigest()[:32]
    assert store.claim_callback(
        digest,
        callback_query_id="old",
        chat_id=CHAT_ID,
        user_id=7,
        media_key="movie:550:all",
    )
    assert store.finish_callback(digest, state="uncertain")

    reply = await bot.handle_callback(_callback(data))
    assert reply is not None and "recovered its tracking state" in reply.text
    assert len(provider.request_calls) == 1
    assert store.callback_state(digest)["state"] == "done"
    durable = store.get_request(f"{CHAT_ID}:movie:550:all")
    assert durable is not None
    assert durable["external_request_id"] == "91"
    assert durable["state"] == "processing"
    store.close()


@pytest.mark.asyncio
async def test_uncertain_tv_callback_recovers_only_the_requested_season(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "app_secret_key", "release-test-secret")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    provider = LiveOverseerr(
        details={
            "ok": True,
            "mode": "live",
            "mediaId": 95396,
            "mediaType": "tv",
            "mediaStatus": 3,
            "requests": [
                {"id": 91, "status": 2, "seasons": [{"seasonNumber": 1}]},
                {"id": 92, "status": 2, "seasons": [{"seasonNumber": 2}]},
            ],
        },
        request_result={
            "ok": False,
            "mode": "live",
            "reason": "already_requested",
            "already": True,
            "status_code": 409,
        },
    )
    store = TelegramStore(tmp_path / "uncertain-tv.db")
    bot = TelegramMediaBot(store, overseerr_client=provider)
    data = _button(bot, store, media_type="tv", tmdb_id=95396, season=2)
    digest = hashlib.sha256(f"{CHAT_ID}:900:{data}".encode()).hexdigest()[:32]
    assert store.claim_callback(
        digest,
        callback_query_id="old",
        chat_id=CHAT_ID,
        user_id=7,
        media_key="tv:95396:2",
    )
    assert store.finish_callback(digest, state="uncertain")

    reply = await bot.handle_callback(_callback(data))

    assert reply is not None and "recovered its tracking state" in reply.text
    durable = store.get_request(f"{CHAT_ID}:tv:95396:2")
    assert durable is not None
    assert durable["external_request_id"] == "92"
    store.close()


@pytest.mark.asyncio
async def test_pending_reconciler_later_recovers_an_unambiguous_request_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "overseerr_url", "http://overseerr.test")
    monkeypatch.setattr(settings, "overseerr_api_key", "test-key")
    provider = LiveOverseerr()
    store = TelegramStore(tmp_path / "pending-id.db")
    store.upsert_request(
        "pending-request",
        media_type="movie",
        tmdb_id=550,
        title="Fight Club",
        state="pending",
        metadata={"chat_id": CHAT_ID, "year": 1999},
    )
    bot = TelegramMediaBot(store, overseerr_client=provider)
    service = TelegramBotService(store=store, bot=bot)

    await service._promote_pending_requests()  # noqa: SLF001 - recovery invariant

    durable = store.get_request("pending-request")
    assert durable is not None
    assert durable["external_request_id"] == "91"
    assert durable["state"] == "processing"
    store.close()


def test_bot_identity_change_resets_only_transport_state(tmp_path: Path) -> None:
    store = TelegramStore(tmp_path / "identity.db")
    store.set_offset(500)
    assert not store.bind_bot(1)
    store.mark_update_processed(499)
    store.upsert_request(
        "request-key",
        media_type="movie",
        tmdb_id=550,
        title="Fight Club",
        state="processing",
    )

    assert store.bind_bot(2)
    assert store.get_offset() is None
    assert store.update_record(499) is None
    assert store.get_request("request-key") is not None
    store.close()


@pytest.mark.asyncio
async def test_telegram_transport_failure_marks_write_outcome_unknown() -> None:
    from hearth.telegram.client import TelegramBotClient

    async def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        client = TelegramBotClient("123:test", api_root="https://telegram.test", client=http)
        result = await client.send_message(CHAT_ID, "hello")
    assert result["ok"] is False
    assert result["outcome_unknown"] is True


@pytest.mark.asyncio
async def test_unknown_callback_edit_never_falls_back_to_duplicate_send() -> None:
    class UnknownEditClient:
        bot_user_id = 1
        bot_username = "test"

        def __init__(self) -> None:
            self.send_calls = 0

        async def edit_message_text(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": False, "outcome_unknown": True}

        async def send_message(self, *_: Any, **__: Any) -> dict[str, Any]:
            self.send_calls += 1
            return {"ok": True}

    client = UnknownEditClient()
    service = TelegramBotService(client=client)  # type: ignore[arg-type]
    await service._deliver_callback(  # noqa: SLF001 - ambiguity regression
        _callback("signed"),
        BotReply("done", edit_message_id=900),
    )
    assert client.send_calls == 0


def test_invalid_user_allowlist_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "123:test")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", "not-a-number")
    assert settings.telegram_user_ids_valid is False
    assert settings.telegram_configured is False
