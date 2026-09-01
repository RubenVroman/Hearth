from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from hearth.telegram.client import TelegramBotClient
from hearth.telegram.progress import ProgressTracker
from hearth.telegram.safeguards import RateLimiter
from hearth.telegram.store import TelegramStore


def _json_request(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


@pytest.mark.asyncio
async def test_client_uses_bounded_updates_and_modern_message_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(
            "123:test-token",
            api_root="https://telegram.test",
            client=http,
        )
        await client.get_updates(offset=42, timeout=25, limit=500)
        await client.send_message(
            -1001,
            "Dune",
            reply_to_message_id=7,
            reply_markup={"inline_keyboard": []},
        )
        await client.edit_message_text(-1001, 8, "Updated")

    poll, send, edit = (_json_request(request) for request in requests)
    assert poll == {
        "timeout": 25,
        "limit": 100,
        "allowed_updates": ["message", "callback_query"],
        "offset": 42,
    }
    assert send["link_preview_options"] == {"is_disabled": True}
    assert send["reply_parameters"] == {
        "message_id": 7,
        "allow_sending_without_reply": True,
    }
    assert send["reply_markup"] == {"inline_keyboard": []}
    assert "disable_web_page_preview" not in send
    assert "reply_to_message_id" not in send
    assert edit["link_preview_options"] == {"is_disabled": True}


@pytest.mark.asyncio
async def test_client_retries_one_confirmed_429_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 2},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    sleep = AsyncMock()
    monkeypatch.setattr("hearth.telegram.client.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await TelegramBotClient("123:test", client=http).send_message(1, "Dune")

    assert result["ok"] is True
    assert calls == 2
    sleep.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_client_never_retries_ambiguous_transport_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("network down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await TelegramBotClient("123:test", client=http).send_message(1, "Dune")

    assert result["ok"] is False
    assert "transport error" in result["error"]
    assert calls == 1


def test_store_offset_is_explicit_and_monotonic(tmp_path: Path) -> None:
    with TelegramStore(tmp_path / "telegram.db") as store:
        assert store.claim_update(9) is True
        store.finish_update(9)
        assert store.get_offset() is None

        store.set_offset(10)
        store.set_offset(4)
        store.set_offset(12)
        assert store.get_offset() == 12


def test_store_update_and_callback_idempotency(tmp_path: Path) -> None:
    with TelegramStore(tmp_path / "telegram.db") as store:
        assert store.claim_update(10) is True
        assert store.claim_update(10) is False
        store.finish_update(10, state="done")
        assert store.claim_update(10, lease_s=1) is False
        assert store.is_update_processed(10) is True

        kwargs = {
            "callback_query_id": "callback-1",
            "chat_id": -1001,
            "user_id": 7,
            "media_key": "movie:550",
        }
        assert store.claim_callback("action-1", **kwargs) is True
        assert store.claim_callback("action-1", **kwargs) is False
        assert store.finish_callback("action-1", state="done") is True
        assert store.claim_callback("action-1", lease_s=0, **kwargs) is False
        assert store.callback_state("action-1")["state"] == "done"


def test_poller_is_exclusive_and_replays_updates_but_not_callbacks(tmp_path: Path) -> None:
    path = tmp_path / "telegram.db"
    first = TelegramStore(path)
    second = TelegramStore(path)
    try:
        assert first.acquire_poller_lock() is True
        assert second.acquire_poller_lock() is False
        assert first.claim_update(11) is True
        assert first.claim_callback(
            "request:movie:550",
            callback_query_id="callback-11",
            chat_id=-1001,
            user_id=7,
        ) is True

        first.release_poller_lock()
        assert second.acquire_poller_lock() is True
        assert second.claim_update(11, lease_s=1) is True
        assert (
            second.claim_callback(
                "request:movie:550",
                callback_query_id="callback-redelivery",
                chat_id=-1001,
                user_id=7,
                lease_s=1,
            )
            is False
        )
        callback = second.callback_state("request:movie:550")
        assert callback is not None
        assert callback["state"] == "uncertain"
    finally:
        first.close()
        second.close()


def test_callback_metadata_expires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = 1_700_000_000.0
    monkeypatch.setattr("hearth.telegram.store.time.time", lambda: now)
    with TelegramStore(tmp_path / "telegram.db") as store:
        store.put_callback_media("movie:550", {"title": "Fight Club"}, ttl_s=10)
        assert store.get_callback_media("movie:550") == {"title": "Fight Club"}

        now += 10
        assert store.get_callback_media("movie:550") is None
        assert store.get_callback_media("movie:550") is None


def test_rate_limit_is_keyed_and_uses_a_sliding_window() -> None:
    now = [100.0]
    limiter = RateLimiter(max_calls=2, window_s=10, clock=lambda: now[0])

    assert limiter.allow((-1001, 7)) is True
    assert limiter.allow((-1001, 7)) is True
    assert limiter.allow((-1001, 7)) is False
    assert limiter.retry_after((-1001, 7)) == pytest.approx(10)
    assert limiter.allow((-1001, 8)) is True
    assert limiter.allow((-1002, 7)) is True

    now[0] += 10
    assert limiter.allow((-1001, 7)) is True


class _FakeOverseerr:
    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        assert media_id == 550
        assert media_type == "movie"
        return self.details


class _FakeArr:
    def __init__(
        self,
        downloads: list[dict[str, Any]],
        *,
        retry_result: dict[str, Any] | None = None,
    ) -> None:
        self.downloads = downloads
        self.retry_result = retry_result or {"ok": False, "reason": "exhausted"}
        self.retry_calls = 0

    async def queue(self, title: str) -> dict[str, Any]:
        return {"mode": "live", "downloads": self.downloads}

    async def retry_download(self, title: str, **kwargs: Any) -> dict[str, Any]:
        self.retry_calls += 1
        return self.retry_result


async def _capture_messages() -> tuple[
    list[tuple[int, str]], Callable[[int, str], Awaitable[None]]
]:
    messages: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        messages.append((chat_id, text))

    return messages, send


def _tracker(details: dict[str, Any], arr: _FakeArr) -> ProgressTracker:
    tracker = ProgressTracker(
        overseerr_client=_FakeOverseerr(details),
        radarr_client=arr,
        sonarr_client=_FakeArr([]),
    )
    tracker.track(
        -1001,
        "Fight Club",
        "radarr",
        tmdb_id=550,
        media_type="movie",
        request_id=91,
    )
    return tracker


@pytest.mark.asyncio
async def test_progress_only_status_five_completes() -> None:
    messages, send = await _capture_messages()
    tracker = _tracker(
        {"mediaInfo": {"status": 4}, "requests": [{"id": 91, "status": 2}]},
        _FakeArr([]),
    )

    await tracker.poll_once(send)
    assert messages == []
    assert len(tracker.active) == 1

    tracker._overseerr.details = {"mediaInfo": {"status": 5}}
    await tracker.poll_once(send)
    assert messages == [(-1001, "Fight Club is done — in Plex.")]
    assert tracker.active == []


@pytest.mark.asyncio
async def test_queue_absence_never_becomes_success() -> None:
    messages, send = await _capture_messages()
    tracker = _tracker({"mediaInfo": {"status": 3}}, _FakeArr([]))

    await tracker.poll_once(send)

    assert messages == []
    assert len(tracker.active) == 1


@pytest.mark.asyncio
async def test_real_download_progress_is_announced_once() -> None:
    messages, send = await _capture_messages()
    arr = _FakeArr(
        [
            {
                "id": 1,
                "tmdbId": 550,
                "title": "Fight Club",
                "status": "downloading",
                "percent": 37.5,
            }
        ]
    )
    tracker = _tracker({"mediaInfo": {"status": 3}}, arr)

    await tracker.poll_once(send)
    await tracker.poll_once(send)

    assert messages == [(-1001, "Fight Club is downloading, ~37.5%.")]


@pytest.mark.asyncio
async def test_failed_download_retries_once_per_queue_item() -> None:
    messages, send = await _capture_messages()
    arr = _FakeArr(
        [
            {
                "id": 9,
                "tmdbId": 550,
                "title": "Fight Club",
                "status": "failed",
                "percent": 12,
            }
        ],
        retry_result={
            "ok": True,
            "reason": "retried",
            "indexer": "Prowlarr",
            "attempt": 1,
            "max_attempts": 3,
        },
    )
    tracker = _tracker({"mediaInfo": {"status": 3}}, arr)

    await tracker.poll_once(send)
    await tracker.poll_once(send)

    assert arr.retry_calls == 1
    assert len(messages) == 1
    assert "trying another source" in messages[0][1]
    assert "attempt 1/3" in messages[0][1]
