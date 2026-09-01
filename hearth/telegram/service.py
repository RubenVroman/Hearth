"""Lifecycle and ordered long-poll transport for the Telegram media bot."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.client import TelegramBotClient
from hearth.telegram.models import BotReply
from hearth.telegram.progress import (
    format_done,
    format_expired,
    format_failed,
    matching_request_row,
)
from hearth.telegram.store import TelegramStore

log = logging.getLogger("hearth.telegram")

MAX_UPDATE_ATTEMPTS = 3
MAX_REQUEST_AGE_SECONDS = 7 * 24 * 60 * 60


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _request_status_for(details: dict[str, Any], request_id: int | None) -> int | None:
    """Return only this bot request's status when an id is available."""
    rows = details.get("requests")
    if not isinstance(rows, list):
        media = details.get("mediaInfo")
        rows = media.get("requests") if isinstance(media, dict) else []
    request_rows = [row for row in (rows or []) if isinstance(row, dict)]
    if request_id is None:
        # Aggregate/latest status may belong to another season request.
        return None
    for row in request_rows:
        if _integer(row.get("id")) == request_id:
            return _integer(row.get("status"))
    return None


class TelegramBotService:
    """Run one poller with per-chat order and bounded cross-chat concurrency."""

    def __init__(
        self,
        *,
        client: TelegramBotClient | None = None,
        store: TelegramStore | None = None,
        bot: TelegramMediaBot | None = None,
        store_factory: Callable[[], TelegramStore] | None = None,
    ) -> None:
        self.client = client or TelegramBotClient()
        self.store = store or (bot.store if bot is not None else None)
        self.bot = bot
        self._store_factory = store_factory or TelegramStore
        self._owns_store = store is None and bot is None
        self._task: asyncio.Task[None] | None = None
        self._progress_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._offset: int | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self.running = False
        self.last_error = ""
        self.last_update_id: int | None = None
        self._bot_ready = False

    @property
    def enabled(self) -> bool:
        return settings.telegram_configured and settings.telegram_poll

    @property
    def progress(self):
        return self.bot.progress if self.bot is not None else None

    def status_snapshot(self) -> dict[str, Any]:
        active = len(self.bot.progress.active) if self.bot is not None else 0
        pending = 0
        if self.store is not None:
            try:
                pending = len(self.store.list_active_requests(states=("pending",)))
            except Exception:  # noqa: BLE001
                pending = 0
        return {
            "configured": settings.telegram_configured,
            "poll": settings.telegram_poll,
            "running": self.running,
            "overseerr_configured": settings.overseerr_configured,
            "chat_ids": settings.telegram_chat_id_list,
            "user_allowlist": bool(settings.telegram_user_id_list),
            "bot_username": self.client.bot_username or None,
            "tracked": active,
            "pending": pending,
            "last_update_id": self.last_update_id,
            "error": self.last_error or None,
        }

    def reset(self) -> None:
        """Reset volatile state without deleting durable request history."""
        if self.bot is not None:
            self.bot.reset()
        self.last_error = ""
        self.last_update_id = None
        self._bot_ready = False
        if self._task is None:
            self.running = False

    def _ensure_components(self) -> None:
        if self.store is None:
            self.store = self._store_factory()
        if self.bot is None:
            self.bot = TelegramMediaBot(self.store)

    async def start(self) -> None:
        if not settings.telegram_configured:
            log.info("telegram bot off (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS)")
            return
        if not settings.telegram_poll:
            log.info("telegram bot polling disabled (TELEGRAM_POLL=false)")
            return
        if self._task is not None:
            return

        self._ensure_components()
        assert self.store is not None
        assert self.bot is not None
        if not self.store.acquire_poller_lock():
            self.last_error = "another Hearth process owns the Telegram poller"
            log.error(self.last_error)
            return

        try:
            self.bot.rate.max_calls = max(1, int(settings.telegram_rate_limit_per_minute))
            self._restore_progress()
            # The offset is loaded only after getMe binds it to this bot id.
            self._offset = None
            self._semaphore = asyncio.Semaphore(max(1, int(settings.telegram_concurrency)))
            self._stop.clear()
            self._task = asyncio.create_task(self._poll_loop(), name="hearth-telegram-poll")
            self._progress_task = asyncio.create_task(
                self._progress_loop(), name="hearth-telegram-progress"
            )
        except BaseException:
            tasks = [
                task for task in (self._task, self._progress_task) if task is not None
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._task = None
            self._progress_task = None
            self._bot_ready = False
            self.running = False
            self.store.release_poller_lock()
            raise
        self.running = True
        self.last_error = ""
        log.info("telegram bot polling for %s chat(s)", len(settings.telegram_chat_id_list))

    async def stop(self) -> None:
        self._stop.set()
        self.running = False
        tasks = [task for task in (self._task, self._progress_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._progress_task = None
        self._bot_ready = False
        await self.client.aclose()
        if self.store is not None:
            if self._owns_store:
                self.store.close()
                self.store = None
                self.bot = None
            else:
                self.store.release_poller_lock()

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, seconds))
        except TimeoutError:
            pass

    async def _ensure_bot(self) -> bool:
        if self._bot_ready and self.client.bot_user_id is not None:
            return True
        data = await self.client.get_me()
        if not data.get("ok"):
            self.last_error = str(data.get("error") or "Telegram getMe failed")
            log.warning("telegram getMe failed: %s", redact(self.last_error))
            return False
        assert self.bot is not None
        assert self.store is not None
        if self.client.bot_user_id is None:
            self.last_error = "Telegram getMe returned no bot id"
            return False
        changed = self.store.bind_bot(self.client.bot_user_id)
        self._offset = self.store.get_offset()
        if changed:
            log.warning("telegram bot identity changed; reset transport offset state")
        self.bot.bot_user_id = self.client.bot_user_id
        deleted = await self.client.delete_webhook(drop_pending_updates=False)
        if not deleted.get("ok"):
            self.last_error = str(deleted.get("error") or "deleteWebhook failed")
            return False
        self._bot_ready = True
        self.last_error = ""
        return True

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not await self._ensure_bot():
                    await self._wait_or_stop(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                data = await self.client.get_updates(offset=self._offset, timeout=25)
                if self._stop.is_set():
                    break
                if not data.get("ok"):
                    self.last_error = str(data.get("error") or "getUpdates failed")
                    await self._wait_or_stop(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                backoff = 1.0
                updates = [row for row in (data.get("result") or []) if isinstance(row, dict)]
                if not updates:
                    continue
                await self._ack_callbacks(updates)
                complete = await self._process_batch(updates)
                if not complete:
                    await self._wait_or_stop(backoff)
                    backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                log.exception("telegram poll loop error")
                await self._wait_or_stop(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _ack_callbacks(self, updates: list[dict[str, Any]]) -> None:
        """Stop Telegram client spinners before locks or provider work."""
        calls = []
        for update in updates:
            callback = update.get("callback_query")
            if not isinstance(callback, dict):
                continue
            callback_id = str(callback.get("id") or "")
            if callback_id:
                calls.append(self.client.answer_callback_query(callback_id))
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)

    @staticmethod
    def _conversation_key(update: dict[str, Any]) -> tuple[int, int] | tuple[str, int]:
        payload = update.get("message")
        if not isinstance(payload, dict):
            callback = update.get("callback_query")
            if isinstance(callback, dict):
                payload = callback.get("message")
        if isinstance(payload, dict):
            chat = payload.get("chat")
            if isinstance(chat, dict):
                chat_id = _integer(chat.get("id"))
                if chat_id is not None:
                    return chat_id, _integer(payload.get("message_thread_id")) or 0
        return "update", _integer(update.get("update_id")) or 0

    async def _process_batch(self, updates: list[dict[str, Any]]) -> bool:
        groups: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
        update_ids: list[int] = []
        for update in updates:
            update_id = _integer(update.get("update_id"))
            if update_id is None:
                continue
            update_ids.append(update_id)
            groups.setdefault(self._conversation_key(update), []).append(update)
        if not update_ids:
            return True
        results = await asyncio.gather(
            *(self._process_group(group) for group in groups.values()),
            return_exceptions=True,
        )
        complete = all(result is True for result in results)
        if complete:
            assert self.store is not None
            offset = max(update_ids) + 1
            self.store.set_offset(offset)
            self._offset = offset
            self.last_update_id = max(update_ids)
            self.last_error = ""
        return complete

    async def _process_group(self, updates: list[dict[str, Any]]) -> bool:
        assert self.store is not None
        assert self._semaphore is not None
        complete = True
        for update in updates:
            update_id = _integer(update.get("update_id"))
            if update_id is None:
                continue
            if self.store.is_update_processed(update_id):
                continue
            if not self.store.claim_update(update_id, lease_s=30.0):
                complete = False
                break
            attempt = self.store.update_attempt_count(update_id)
            try:
                async with self._semaphore:
                    await self._dispatch_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                error = type(exc).__name__
                if attempt >= MAX_UPDATE_ATTEMPTS:
                    self.store.finish_update(
                        update_id,
                        state="dead_letter",
                        error=error,
                    )
                    log.exception(
                        "telegram update %s dead-lettered after %s attempts",
                        update_id,
                        attempt,
                    )
                    continue
                complete = False
                self.store.finish_update(update_id, state="failed", error=error)
                log.exception(
                    "telegram update %s failed (attempt %s/%s)",
                    update_id,
                    attempt,
                    MAX_UPDATE_ATTEMPTS,
                )
                break
            else:
                self.store.finish_update(update_id, state="done")
        return complete

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        assert self.bot is not None
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            reply = await self.bot.handle_callback(callback)
            if reply is not None and reply.text:
                await self._deliver_callback(callback, reply)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        reply = await self.bot.handle_message(message)
        if reply is None or not reply.text:
            return
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = _integer(chat.get("id"))
        message_id = _integer(message.get("message_id"))
        if chat_id is None:
            return
        await self._send_status(
            chat_id,
            reply.text,
            reply_to_message_id=message_id,
            reply_markup=reply.reply_markup,
            require_success=True,
        )

    async def _deliver_callback(self, callback: dict[str, Any], reply: BotReply) -> None:
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = _integer(chat.get("id"))
        message_id = reply.edit_message_id or _integer(message.get("message_id"))
        if chat_id is None:
            return
        if message_id is not None:
            try:
                edited = await self.client.edit_message_text(
                    chat_id,
                    message_id,
                    reply.text,
                    reply_markup=reply.reply_markup or {"inline_keyboard": []},
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("telegram callback edit raised unexpectedly")
                edited = {"ok": False, "outcome_unknown": True}
            if edited.get("ok"):
                return
            if edited.get("outcome_unknown"):
                # The edit may have reached Telegram. A fallback send would
                # turn a lost response into a duplicate message.
                log.warning("telegram callback edit outcome is unknown")
                return
            if "message is not modified" in str(edited.get("error") or "").casefold():
                return
        try:
            # Callback business state is already durable and exactly-once by
            # this point. Notification failure must not replay or poison it.
            await self._send_status(
                chat_id,
                reply.text,
                reply_markup=reply.reply_markup,
                require_success=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("telegram callback fallback send raised unexpectedly")

    async def _progress_loop(self) -> None:
        interval = max(15.0, float(settings.telegram_progress_interval_seconds))
        while not self._stop.is_set():
            await self._wait_or_stop(interval)
            if self._stop.is_set():
                break
            try:
                await self._promote_pending_requests()
                if self.bot is not None:
                    await self.bot.progress.poll_once(
                        self._send_status,
                        checkpoint=self._checkpoint_progress_item,
                    )
                self._persist_progress()
                if self.store is not None:
                    self.store.prune()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("telegram progress loop error")

    def _restore_progress(self) -> None:
        assert self.store is not None
        assert self.bot is not None
        tracked: list[dict[str, Any]] = []
        for row in self.store.list_active_requests(
            states=("processing", "downloading", "retrying")
        ):
            metadata = row.get("metadata")
            state = metadata.get("tracked") if isinstance(metadata, dict) else None
            if isinstance(state, dict):
                restored = dict(state)
                restored.setdefault("request_key", str(row["request_key"]))
                tracked.append(restored)
        self.bot.progress.restore(tracked)

    async def _promote_pending_requests(self) -> None:
        if self.store is None or self.bot is None or not settings.overseerr_configured:
            return
        for row in self.store.list_active_requests(states=("pending",), limit=200):
            request_key = str(row["request_key"])
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            chat_id = _integer(metadata.get("chat_id"))
            media_type = str(row.get("media_type") or "")
            tmdb_id = _integer(row.get("tmdb_id"))
            if chat_id is None or tmdb_id is None or media_type not in {"movie", "tv"}:
                continue
            try:
                created_at = float(row.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0
            if created_at > 0 and time.time() - created_at > MAX_REQUEST_AGE_SECONDS:
                title = str(row.get("title") or f"TMDB {tmdb_id}")
                season = _integer(row.get("season"))
                notification_title = (
                    f"{title} season {season}" if season is not None else title
                )
                await self._send_status(
                    chat_id,
                    format_expired(notification_title),
                    require_success=True,
                )
                self.store.update_request(request_key, state="expired")
                continue
            try:
                details = await self.bot.overseerr.media_details(tmdb_id, media_type)
            except Exception:  # noqa: BLE001
                # Rotate a temporarily unreachable row so a large pending set
                # cannot starve newer household requests forever.
                self.store.update_request(request_key, state="pending")
                continue
            if not details.get("ok"):
                self.store.update_request(request_key, state="pending")
                continue
            media_status = _integer(details.get("mediaStatus"))
            request_id = _integer(row.get("external_request_id"))
            title = str(row.get("title") or f"TMDB {tmdb_id}")
            season = _integer(row.get("season"))
            matched_request = matching_request_row(
                details,
                request_id,
                media_type=media_type,
                season=season,
            )
            if request_id is None and matched_request is not None:
                request_id = _integer(matched_request.get("id"))
                if request_id is not None:
                    self.store.update_request(
                        request_key,
                        external_request_id=request_id,
                    )
            request_status = (
                _integer(matched_request.get("status"))
                if matched_request is not None
                else _request_status_for(details, request_id)
            )
            notification_title = (
                f"{title} season {season}" if season is not None else title
            )
            if media_status == 5 or request_status == 5:
                await self._send_status(
                    chat_id,
                    format_done(notification_title),
                    require_success=True,
                )
                self.store.update_request(request_key, state="available")
                continue
            if media_status in {6, 7}:
                await self._send_status(
                    chat_id,
                    format_failed(
                        notification_title,
                        "blocked or removed in Overseerr",
                    ),
                    require_success=True,
                )
                self.store.update_request(request_key, state="failed")
                continue
            if request_status in {3, 4}:
                reason = "declined" if request_status == 3 else "failed"
                await self._send_status(
                    chat_id,
                    format_failed(notification_title, f"Overseerr {reason}"),
                    require_success=True,
                )
                self.store.update_request(request_key, state=reason)
                continue
            if request_status != 2:
                refreshed = dict(metadata)
                refreshed.update(
                    request_status=request_status,
                    media_status=media_status,
                )
                self.store.update_request(
                    request_key,
                    state="pending",
                    metadata=refreshed,
                )
                continue
            tracked = self.bot.progress.track(
                chat_id,
                title,
                "radarr" if media_type == "movie" else "sonarr",
                _integer(metadata.get("year")),
                season=season,
                tmdb_id=tmdb_id,
                media_type=media_type,
                request_id=request_id,
                request_key=request_key,
                request_status=request_status,
            )
            metadata = dict(metadata)
            metadata.update(
                request_status=request_status,
                media_status=media_status,
                tracked=tracked.to_dict() if tracked is not None else None,
            )
            self.store.update_request(
                request_key, state="processing", metadata=metadata
            )

    async def _checkpoint_progress_item(self, item: Any) -> None:
        """Durably record a retry guard before the external *arr mutation."""
        if self.store is None or not str(getattr(item, "request_key", "")):
            raise RuntimeError("tracked request has no durable request key")
        request_key = str(item.request_key)
        row = self.store.get_request(request_key)
        if row is None:
            raise RuntimeError("tracked request is missing from the durable store")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        updated = dict(metadata)
        updated["tracked"] = item.to_dict()
        if not self.store.update_request(request_key, metadata=updated):
            raise RuntimeError("failed to checkpoint tracked request")

    def _persist_progress(self) -> None:
        if self.store is None or self.bot is None:
            return
        active = {
            item.request_key: item
            for item in self.bot.progress.active
            if item.request_key
        }
        completed = {
            item.request_key: item
            for item in self.bot.progress.completed
            if item.request_key
        }
        for row in self.store.list_active_requests(
            states=("processing", "downloading", "retrying")
        ):
            request_key = str(row["request_key"])
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            terminal = completed.get(request_key)
            if terminal is not None:
                terminal_state = terminal.terminal_state or "failed"
                updated = dict(metadata)
                updated["tracked"] = terminal.to_dict()
                self.store.update_request(
                    request_key,
                    state=terminal_state,
                    metadata=updated,
                )
                continue
            item = active.get(request_key)
            if item is None:
                # Never invent a terminal meaning for malformed legacy state.
                log.warning("telegram request %s has no restored tracker", request_key)
                continue
            metadata = dict(metadata)
            metadata["tracked"] = item.to_dict()
            state = "retrying" if item.announce_retrying else (
                "downloading" if item.announce_started else "processing"
            )
            self.store.update_request(
                request_key, state=state, metadata=metadata
            )
        self.bot.progress.prune_completed()

    async def _send_status(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        require_success: bool = False,
    ) -> dict[str, Any]:
        data = await self.client.send_message(
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )
        if not data.get("ok"):
            error = redact(str(data.get("error") or "Telegram send failed"))
            log.warning("telegram status post failed: %s", error)
            if require_success and not data.get("outcome_unknown"):
                raise RuntimeError(error)
        return data


TelegramInboxService = TelegramBotService

telegram_inbox = TelegramBotService()
