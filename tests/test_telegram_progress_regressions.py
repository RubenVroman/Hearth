from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hearth.telegram.progress import ProgressTracker


class FakeOverseerr:
    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details

    async def media_details(self, _media_id: int, _media_type: str) -> dict[str, Any]:
        return self.details


class FakeArr:
    live = True

    def __init__(
        self,
        queue_payload: dict[str, Any],
        *,
        retry_result: dict[str, Any] | None = None,
    ) -> None:
        self.queue_payload = queue_payload
        self.retry_result = retry_result or {
            "ok": False,
            "mode": "live",
            "reason": "exhausted",
        }
        self.retry_calls = 0

    async def queue(self, _title: str) -> dict[str, Any]:
        return self.queue_payload

    async def retry_download(self, _title: str, **_kwargs: Any) -> dict[str, Any]:
        self.retry_calls += 1
        return self.retry_result


def tracker_for(
    arr: FakeArr,
    *,
    details: dict[str, Any] | None = None,
    title: str = "Fight Club",
    tmdb_id: int | None = 550,
    request_id: int | None = 91,
    clock: Callable[[], float] | None = None,
) -> ProgressTracker:
    tracker = ProgressTracker(
        overseerr_client=FakeOverseerr(details or {"mediaInfo": {"status": 3}}),
        radarr_client=arr,
        sonarr_client=FakeArr({"mode": "live", "downloads": []}),
        clock=clock or (lambda: 100.0),
    )
    tracker.track(
        -1001,
        title,
        "radarr",
        tmdb_id=tmdb_id,
        media_type="movie",
        request_id=request_id,
        request_status=2,
    )
    return tracker


def capture() -> tuple[list[str], Callable[[int, str], Awaitable[None]]]:
    messages: list[str] = []

    async def send(_chat_id: int, text: str) -> None:
        messages.append(text)

    return messages, send


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "mode": "mock",
            "downloads": [
                {"tmdbId": 550, "title": "Fight Club", "status": "failed"}
            ],
        },
        {
            "mode": "live",
            "error": "Radarr unavailable",
            "downloads": [
                {"tmdbId": 550, "title": "Fight Club", "status": "failed"}
            ],
        },
        {
            "downloads": [
                {"tmdbId": 550, "title": "Fight Club", "status": "failed"}
            ],
        },
    ],
)
async def test_unverified_queue_payload_never_announces_or_retries(
    payload: dict[str, Any],
) -> None:
    arr = FakeArr(payload)
    tracker = tracker_for(arr)
    messages, send = capture()

    await tracker.poll_once(send)

    assert messages == []
    assert arr.retry_calls == 0
    assert len(tracker.active) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "tmdb_id", "rows"),
    [
        (
            "It",
            346364,
            [{"tmdbId": 999, "title": "Hit Man", "status": "failed"}],
        ),
        (
            "Fight Club",
            None,
            [
                {"title": "Fight Club", "status": "failed", "id": 1},
                {"title": "Fight Club", "status": "failed", "id": 2},
            ],
        ),
    ],
)
async def test_substring_or_ambiguous_title_never_selects_a_queue_row(
    title: str,
    tmdb_id: int | None,
    rows: list[dict[str, Any]],
) -> None:
    arr = FakeArr({"mode": "live", "downloads": rows})
    tracker = tracker_for(arr, title=title, tmdb_id=tmdb_id)
    messages, send = capture()

    await tracker.poll_once(send)

    assert messages == []
    assert arr.retry_calls == 0


@pytest.mark.asyncio
async def test_stable_tmdb_id_selects_the_right_row_not_the_first_row() -> None:
    arr = FakeArr(
        {
            "mode": "live",
            "downloads": [
                {
                    "tmdbId": 999,
                    "title": "Fight Club",
                    "status": "failed",
                },
                {
                    "tmdbId": 550,
                    "title": "release-name-does-not-matter",
                    "status": "downloading",
                    "percent": 25,
                },
            ],
        }
    )
    tracker = tracker_for(arr)
    messages, send = capture()

    await tracker.poll_once(send)

    assert messages == ["Fight Club is downloading, ~25%."]
    assert arr.retry_calls == 0


def test_tracking_identity_prefers_tmdb_id_and_media_type_over_title() -> None:
    tracker = ProgressTracker()
    first = tracker.track(1, "Fargo", "radarr", tmdb_id=1, media_type="movie")
    tracker.track(1, "Fargo", "radarr", tmdb_id=2, media_type="movie")
    tracker.track(1, "Fargo", "sonarr", tmdb_id=1, media_type="tv")
    duplicate = tracker.track(
        1,
        "Fargo remastered",
        "radarr",
        tmdb_id=1,
        media_type="movie",
    )

    assert len(tracker.active) == 3
    assert duplicate is first

    no_id = ProgressTracker()
    one = no_id.track(1, "Unknown", "radarr", media_type="movie")
    two = no_id.track(1, "Unknown", "radarr", media_type="movie")
    assert one is two
    assert len(no_id.active) == 1


def test_same_show_with_distinct_request_ids_tracks_seasons_independently() -> None:
    tracker = ProgressTracker()

    tracker.track(
        1,
        "Severance",
        "sonarr",
        tmdb_id=95396,
        media_type="tv",
        request_id=101,
        request_key="season-1",
    )
    tracker.track(
        1,
        "Severance",
        "sonarr",
        tmdb_id=95396,
        media_type="tv",
        request_id=102,
        request_key="season-2",
    )

    assert len(tracker.active) == 2
    assert {item.request_key for item in tracker.active} == {"season-1", "season-2"}


@pytest.mark.asyncio
async def test_explicit_request_id_never_falls_back_to_another_request() -> None:
    details = {
        "mediaInfo": {"status": 3},
        "requestStatus": 3,
        "requests": [{"id": 80, "status": 3}],
    }
    arr = FakeArr({"mode": "live", "downloads": []})
    tracker = tracker_for(arr, details=details, request_id=91)
    messages, send = capture()

    await tracker.poll_once(send)

    assert messages == []
    assert len(tracker.active) == 1
    assert tracker.active[0].request_status == 2


@pytest.mark.asyncio
async def test_numeric_request_status_four_is_terminal_failure() -> None:
    details = {
        "mediaInfo": {"status": 3},
        "requests": [{"id": 91, "status": 4}],
    }
    tracker = tracker_for(
        FakeArr({"mode": "live", "downloads": []}), details=details
    )
    messages, send = capture()

    await tracker.poll_once(send)

    assert messages == ["Fight Club failed (request failed in Overseerr)."]
    assert tracker.active == []
    assert tracker.completed[0].terminal_state == "failed"


@pytest.mark.asyncio
async def test_exact_completed_request_finishes_partial_tv_media() -> None:
    details = {
        "mediaInfo": {"status": 4},
        "requests": [{"id": 91, "status": 5}],
    }
    tracker = ProgressTracker(
        overseerr_client=FakeOverseerr(details),
        radarr_client=FakeArr({"mode": "live", "downloads": []}),
        sonarr_client=FakeArr({"mode": "live", "downloads": []}),
    )
    tracker.track(
        -1001,
        "Severance",
        "sonarr",
        season=2,
        tmdb_id=95396,
        media_type="tv",
        request_id=91,
        request_key="season-2",
        request_status=2,
    )
    messages, send = capture()

    await tracker.poll_once(send)

    assert messages == ["Severance season 2 is done — in Plex."]
    assert tracker.completed[0].terminal_state == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["healthy", "not_found"])
async def test_retry_lookup_race_is_nonterminal(reason: str) -> None:
    arr = FakeArr(
        {
            "mode": "live",
            "downloads": [
                {"tmdbId": 550, "title": "Fight Club", "status": "failed", "id": 9}
            ],
        },
        retry_result={"ok": False, "mode": "live", "reason": reason},
    )
    tracker = tracker_for(arr)
    messages, send = capture()

    await tracker.poll_once(send)
    await tracker.poll_once(send)

    assert messages == []
    assert arr.retry_calls == 1
    assert len(tracker.active) == 1


@pytest.mark.asyncio
async def test_mock_retry_result_is_never_reported_as_success() -> None:
    arr = FakeArr(
        {
            "mode": "live",
            "downloads": [
                {"tmdbId": 550, "title": "Fight Club", "status": "failed", "id": 9}
            ],
        },
        retry_result={"ok": True, "mode": "mock", "reason": "retried"},
    )
    tracker = tracker_for(arr)
    messages, send = capture()

    await tracker.poll_once(send)

    assert messages == []
    assert arr.retry_calls == 1
    assert len(tracker.active) == 1
    assert tracker.active[0].retried_queue_keys


@pytest.mark.asyncio
async def test_retry_key_is_marked_before_the_mutating_await_finishes() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingArr(FakeArr):
        async def retry_download(self, _title: str, **_kwargs: Any) -> dict[str, Any]:
            self.retry_calls += 1
            entered.set()
            await release.wait()
            return {"ok": False, "mode": "live", "reason": "healthy"}

    arr = BlockingArr(
        {
            "mode": "live",
            "downloads": [
                {"tmdbId": 550, "title": "Fight Club", "status": "failed", "id": 9}
            ],
        }
    )
    tracker = tracker_for(arr)
    _messages, send = capture()

    task = asyncio.create_task(tracker.poll_once(send))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert tracker.active[0].retried_queue_keys == ["id:9"]
    release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_failed_terminal_send_retries_notice_without_repeating_mutation() -> None:
    arr = FakeArr(
        {
            "mode": "live",
            "downloads": [
                {"tmdbId": 550, "title": "Fight Club", "status": "failed", "id": 9}
            ],
        },
        retry_result={
            "ok": False,
            "mode": "live",
            "reason": "exhausted",
            "max_attempts": 3,
        },
    )
    tracker = tracker_for(arr)

    async def failed_send(_chat_id: int, _text: str) -> dict[str, Any]:
        return {"ok": False}

    await tracker.poll_once(failed_send)
    assert arr.retry_calls == 1
    assert len(tracker.active) == 1
    assert tracker.active[0].pending_terminal_text

    messages, send = capture()
    await tracker.poll_once(send)
    assert arr.retry_calls == 1
    assert messages == [
        "Fight Club failed — ran out of alternate sources after 3 tries."
    ]
    assert tracker.completed[0].terminal_state == "failed"


@pytest.mark.asyncio
async def test_terminal_state_waits_for_successful_send_then_can_be_pruned() -> None:
    tracker = tracker_for(
        FakeArr({"mode": "live", "downloads": []}),
        details={"mediaInfo": {"status": 5}},
    )

    async def failed_send(_chat_id: int, _text: str) -> dict[str, Any]:
        return {"ok": False, "error": "Telegram unavailable"}

    await tracker.poll_once(failed_send)
    assert len(tracker.active) == 1
    assert tracker.completed == []
    assert tracker.active[0].terminal_state == ""

    messages, send = capture()
    await tracker.poll_once(send)
    assert messages == ["Fight Club is done — in Plex."]
    assert tracker.completed[0].terminal_state == "available"

    removed = tracker.prune_completed()
    assert removed[0].terminal_state == "available"
    assert tracker.active == []
    assert tracker.completed == []


@pytest.mark.asyncio
async def test_expired_item_is_preserved_with_explicit_terminal_state() -> None:
    now = [100.0]
    tracker = tracker_for(
        FakeArr({"mode": "live", "downloads": []}),
        clock=lambda: now[0],
    )
    now[0] = 102.0
    messages, send = capture()

    await tracker.poll_once(send, max_age_s=1)

    assert messages == [
        "Fight Club is still unresolved after seven days; I stopped tracking it."
    ]
    assert tracker.active == []
    assert tracker.completed[0].terminal_state == "expired"
