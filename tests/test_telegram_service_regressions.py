from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hearth.config import settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.models import BotReply
from hearth.telegram.progress import ProgressTracker
from hearth.telegram.service import MAX_UPDATE_ATTEMPTS, TelegramBotService
from hearth.telegram.store import TelegramStore


def _message_update(update_id: int = 10) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 41,
            "chat": {"id": -1001},
            "from": {"id": 7, "is_bot": False},
            "text": "Dune (2021)",
        },
    }


def _callback_update(update_id: int = 20) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-20",
            "data": "signed-button",
            "from": {"id": 7, "is_bot": False},
            "message": {"message_id": 88, "chat": {"id": -1001}},
        },
    }


class SearchOverseerr:
    live = True

    def __init__(self) -> None:
        self.search_calls = 0

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls += 1
        return {
            "ok": True,
            "results": [
                {
                    "mediaType": "movie",
                    "id": 438631,
                    "title": "Dune",
                    "releaseDate": "2021-09-15",
                }
            ],
        }


class FlakyMessageClient:
    def __init__(self) -> None:
        self.send_calls = 0

    async def send_message(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.send_calls += 1
        if self.send_calls == 1:
            return {"ok": False, "error": "temporary send failure"}
        return {"ok": True, "result": {"message_id": 99}}


@pytest.mark.asyncio
async def test_message_is_reprocessed_after_reply_delivery_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "telegram_chat_ids", "-1001")
    monkeypatch.setattr(settings, "telegram_user_ids", "7")
    store = TelegramStore(tmp_path / "message-retry.db")
    overseerr = SearchOverseerr()
    bot = TelegramMediaBot(store, overseerr_client=overseerr)
    client = FlakyMessageClient()
    service = TelegramBotService(client=client, store=store, bot=bot)  # type: ignore[arg-type]
    service._semaphore = asyncio.Semaphore(1)
    update = _message_update()

    assert await service._process_batch([update]) is False
    assert store.get_offset() is None
    assert store.update_attempt_count(10) == 1

    assert await service._process_batch([update]) is True
    assert store.get_offset() == 11
    assert overseerr.search_calls == 2
    assert client.send_calls == 2
    store.close()


class TerminalCallbackBot:
    def __init__(self, store: TelegramStore) -> None:
        self.store = store
        self.calls = 0
        self.progress = SimpleNamespace(active=[])

    async def handle_callback(self, callback: dict[str, Any]) -> BotReply:
        self.calls += 1
        assert self.store.claim_callback(
            "durable-action",
            callback_query_id=str(callback["id"]),
            chat_id=-1001,
            user_id=7,
        )
        self.store.finish_callback("durable-action", state="done")
        return BotReply("Requested Dune", edit_message_id=88)


class FailedCallbackDeliveryClient:
    def __init__(self) -> None:
        self.edit_calls = 0
        self.send_calls = 0

    async def edit_message_text(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.edit_calls += 1
        return {"ok": False, "error": "bot was blocked"}

    async def send_message(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.send_calls += 1
        return {"ok": False, "error": "bot was blocked"}


@pytest.mark.asyncio
async def test_terminal_callback_delivery_failure_does_not_poison_offset(
    tmp_path: Path,
) -> None:
    store = TelegramStore(tmp_path / "callback-delivery.db")
    bot = TerminalCallbackBot(store)
    client = FailedCallbackDeliveryClient()
    service = TelegramBotService(client=client, store=store, bot=bot)  # type: ignore[arg-type]
    service._semaphore = asyncio.Semaphore(1)

    assert await service._process_batch([_callback_update()]) is True
    assert store.get_offset() == 21
    assert store.callback_state("durable-action")["state"] == "done"
    assert bot.calls == 1
    assert client.edit_calls == 1
    assert client.send_calls == 1
    store.close()


class PoisonBot:
    def __init__(self) -> None:
        self.calls = 0
        self.progress = SimpleNamespace(active=[])

    async def handle_message(self, _: dict[str, Any]) -> None:
        self.calls += 1
        raise ValueError("deterministic poison update")


@pytest.mark.asyncio
async def test_poison_update_is_dead_lettered_after_bounded_retries(tmp_path: Path) -> None:
    store = TelegramStore(tmp_path / "poison.db")
    bot = PoisonBot()
    service = TelegramBotService(
        client=SimpleNamespace(),
        store=store,
        bot=bot,  # type: ignore[arg-type]
    )
    service._semaphore = asyncio.Semaphore(1)
    update = _message_update(update_id=30)

    for attempt in range(1, MAX_UPDATE_ATTEMPTS):
        assert await service._process_batch([update]) is False
        record = store.update_record(30)
        assert record is not None
        assert record["state"] == "failed"
        assert record["attempts"] == attempt
        assert store.get_offset() is None

    assert await service._process_batch([update]) is True
    record = store.update_record(30)
    assert record is not None
    assert record["state"] == "dead_letter"
    assert record["attempts"] == MAX_UPDATE_ATTEMPTS
    assert store.is_update_processed(30)
    assert store.get_offset() == 31
    assert bot.calls == MAX_UPDATE_ATTEMPTS
    store.close()


def test_terminal_callbacks_outlive_capacity_until_button_ttl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = [1_700_000_000.0]
    monkeypatch.setattr("hearth.telegram.store.time.time", lambda: now[0])
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 120)
    store = TelegramStore(tmp_path / "callback-retention.db", max_callbacks=1)

    for action in ("first", "second"):
        assert store.claim_callback(
            action,
            callback_query_id=action,
            chat_id=-1001,
            user_id=7,
        )
        store.finish_callback(action)
        now[0] += 1

    now[0] = 1_700_000_179.0
    store.prune()
    assert store.callback_state("first") is not None
    assert store.callback_state("second") is not None

    now[0] = 1_700_000_182.0
    store.prune()
    remaining = [store.callback_state("first"), store.callback_state("second")]
    assert sum(item is not None for item in remaining) == 1
    store.close()


def test_store_migrates_legacy_retry_and_retention_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    now = time.time()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE telegram_updates (
            update_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            processed_at REAL,
            error TEXT
        );
        CREATE TABLE telegram_callback_actions (
            action_id TEXT PRIMARY KEY,
            callback_query_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            media_key TEXT,
            state TEXT NOT NULL,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO telegram_updates(update_id, state, claimed_at, error)
        VALUES (7, 'failed', ?, 'old failure')
        """,
        (now,),
    )
    connection.execute(
        """
        INSERT INTO telegram_callback_actions(
            action_id, callback_query_id, chat_id, state, created_at, updated_at
        ) VALUES ('legacy', 'callback', -1001, 'done', ?, ?)
        """,
        (now, now),
    )
    connection.commit()
    connection.close()

    store = TelegramStore(path)
    update_columns = {
        row["name"] for row in store._conn.execute("PRAGMA table_info(telegram_updates)")
    }
    callback_columns = {
        row["name"]
        for row in store._conn.execute("PRAGMA table_info(telegram_callback_actions)")
    }
    assert "attempts" in update_columns
    assert "retain_until" in callback_columns
    assert store.claim_update(7)
    assert store.update_attempt_count(7) == 1
    assert store.callback_state("legacy")["retain_until"] > now
    store.close()


class BrokenRestoreProgress:
    active: list[Any] = []

    def restore(self, _: list[dict[str, Any]]) -> None:
        raise RuntimeError("corrupt progress state")


class BrokenRestoreBot:
    def __init__(self, store: TelegramStore) -> None:
        self.store = store
        self.rate = SimpleNamespace(max_calls=0)
        self.progress = BrokenRestoreProgress()


@pytest.mark.asyncio
async def test_startup_failure_releases_exclusive_poller_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "123:test")
    monkeypatch.setattr(settings, "telegram_chat_ids", "-1001")
    path = tmp_path / "startup.db"
    store = TelegramStore(path)
    service = TelegramBotService(
        client=SimpleNamespace(),
        store=store,
        bot=BrokenRestoreBot(store),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="corrupt progress"):
        await service.start()
    assert not store.owns_poller_lock

    contender = TelegramStore(path)
    assert contender.acquire_poller_lock()
    contender.close()
    store.close()


class RequestDetailsOverseerr:
    live = True

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details

    async def media_details(self, _tmdb_id: int, _media_type: str) -> dict[str, Any]:
        return dict(self.details)


class StatusClient:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.messages: list[str] = []

    async def send_message(self, _chat_id: int, text: str, **_: Any) -> dict[str, Any]:
        self.messages.append(text)
        return {"ok": self.ok, "error": None if self.ok else "offline"}


@pytest.mark.asyncio
async def test_pending_promotion_uses_its_exact_overseerr_request_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "overseerr_api_key", "live-key")
    store = TelegramStore(tmp_path / "pending-exact-request.db")
    store.upsert_request(
        "request-key",
        media_type="tv",
        tmdb_id=95396,
        title="Severance",
        external_request_id=91,
        state="pending",
        metadata={"chat_id": -1001},
    )
    backend = RequestDetailsOverseerr(
        {
            "ok": True,
            "mediaStatus": 3,
            # A different, newer request is declined. This bot's exact request
            # is still pending and must not inherit that status.
            "requestStatus": 3,
            "requests": [
                {"id": 80, "status": 3},
                {"id": 91, "status": 1},
            ],
        }
    )
    bot = TelegramMediaBot(store, overseerr_client=backend)
    client = StatusClient()
    service = TelegramBotService(client=client, store=store, bot=bot)  # type: ignore[arg-type]

    await service._promote_pending_requests()

    assert store.get_request("request-key")["state"] == "pending"
    assert store.get_request("request-key")["metadata"]["request_status"] == 1
    assert client.messages == []
    store.close()


@pytest.mark.asyncio
async def test_pending_terminal_state_waits_for_successful_telegram_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "overseerr_api_key", "live-key")
    store = TelegramStore(tmp_path / "pending-delivery.db")
    store.upsert_request(
        "request-key",
        media_type="movie",
        tmdb_id=550,
        title="Fight Club",
        external_request_id=91,
        state="pending",
        metadata={"chat_id": -1001},
    )
    backend = RequestDetailsOverseerr(
        {"ok": True, "mediaStatus": 5, "requests": [{"id": 91, "status": 2}]}
    )
    bot = TelegramMediaBot(store, overseerr_client=backend)
    service = TelegramBotService(
        client=StatusClient(ok=False),  # type: ignore[arg-type]
        store=store,
        bot=bot,
    )

    with pytest.raises(RuntimeError, match="offline"):
        await service._promote_pending_requests()

    assert store.get_request("request-key")["state"] == "pending"
    store.close()


class RetryArr:
    live = True

    def __init__(self) -> None:
        self.retry_calls = 0

    async def queue(self, _title: str) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
            "downloads": [
                {"id": 7, "tmdbId": 550, "title": "Fight Club", "status": "failed"}
            ],
        }

    async def retry_download(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.retry_calls += 1
        return {"ok": False, "mode": "live", "reason": "healthy"}


@pytest.mark.asyncio
async def test_retry_guard_is_checkpointed_before_arr_mutation(tmp_path: Path) -> None:
    store = TelegramStore(tmp_path / "retry-checkpoint.db")
    backend = RequestDetailsOverseerr(
        {"ok": True, "mediaStatus": 3, "requests": [{"id": 91, "status": 2}]}
    )
    arr = RetryArr()
    tracker = ProgressTracker(
        overseerr_client=backend,
        radarr_client=arr,
        sonarr_client=arr,
    )
    item = tracker.track(
        -1001,
        "Fight Club",
        "radarr",
        tmdb_id=550,
        media_type="movie",
        request_id=91,
        request_key="request-key",
        request_status=2,
    )
    assert item is not None
    store.upsert_request(
        "request-key",
        media_type="movie",
        tmdb_id=550,
        title="Fight Club",
        external_request_id=91,
        state="processing",
        metadata={"chat_id": -1001, "tracked": item.to_dict()},
    )
    bot = SimpleNamespace(progress=tracker, overseerr=backend)
    service = TelegramBotService(
        client=StatusClient(),  # type: ignore[arg-type]
        store=store,
        bot=bot,  # type: ignore[arg-type]
    )

    await tracker.poll_once(
        service._send_status,
        checkpoint=service._checkpoint_progress_item,
    )

    persisted = store.get_request("request-key")["metadata"]["tracked"]
    assert persisted["retried_queue_keys"] == ["id:7"]
    assert arr.retry_calls == 1
    store.close()


def test_completed_progress_persists_exact_terminal_state(tmp_path: Path) -> None:
    store = TelegramStore(tmp_path / "terminal-persistence.db")
    tracker = ProgressTracker()
    item = tracker.track(
        -1001,
        "Fight Club",
        "radarr",
        tmdb_id=550,
        media_type="movie",
        request_id=91,
        request_key="request-key",
        request_status=2,
    )
    assert item is not None
    item.terminal_state = "available"
    item.done = True
    store.upsert_request(
        "request-key",
        media_type="movie",
        tmdb_id=550,
        title="Fight Club",
        external_request_id=91,
        state="processing",
        metadata={"chat_id": -1001, "tracked": item.to_dict()},
    )
    service = TelegramBotService(
        client=StatusClient(),  # type: ignore[arg-type]
        store=store,
        bot=SimpleNamespace(progress=tracker),  # type: ignore[arg-type]
    )

    service._persist_progress()

    assert store.get_request("request-key")["state"] == "available"
    assert tracker.completed == []
    store.close()
