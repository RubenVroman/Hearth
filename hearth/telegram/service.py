"""Telegram inbox background service — long-polling getUpdates by default."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.client import TelegramBotClient
from hearth.telegram.inbox import TelegramInbox

log = logging.getLogger("hearth.telegram")


class TelegramInboxService:
    def __init__(self) -> None:
        self.client = TelegramBotClient()
        self.inbox = TelegramInbox()
        self._task: asyncio.Task[None] | None = None
        self._progress_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._offset: int | None = None
        self.running = False

    @property
    def enabled(self) -> bool:
        return settings.telegram_configured and settings.telegram_poll

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "configured": settings.telegram_configured,
            "poll": settings.telegram_poll,
            "webhook_local": settings.telegram_webhook_local,
            "running": self.running,
            "chat_ids": settings.telegram_chat_id_list,
            "user_allowlist": bool(settings.telegram_user_id_list),
            "bot_username": self.client.bot_username or None,
            "tracked": len(self.inbox.progress.active),
        }

    async def start(self) -> None:
        if not settings.telegram_configured:
            log.info("telegram inbox off (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS)")
            return
        if not settings.telegram_poll:
            log.info("telegram long-poll disabled (TELEGRAM_POLL=false)")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self.inbox.rate.max_calls = max(1, int(settings.telegram_rate_limit_per_minute))
        self.inbox.deduper.window_s = float(settings.telegram_dedup_window_seconds)
        self._task = asyncio.create_task(self._poll_loop(), name="hearth-telegram-poll")
        self._progress_task = asyncio.create_task(
            self._progress_loop(),
            name="hearth-telegram-progress",
        )
        self.running = True
        log.info(
            "telegram inbox polling for %s chat(s)",
            len(settings.telegram_chat_id_list),
        )

    async def stop(self) -> None:
        self._stop.set()
        self.running = False
        for task in (self._task, self._progress_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        self._progress_task = None
        await self.client.aclose()

    async def _ensure_bot(self) -> bool:
        if self.client.bot_user_id is not None:
            self.inbox.bot_user_id = self.client.bot_user_id
            return True
        me = await self.client.get_me()
        if not me.get("ok"):
            log.warning("telegram getMe failed: %s", me.get("error"))
            return False
        self.inbox.bot_user_id = self.client.bot_user_id
        await self.client.delete_webhook()
        return True

    async def _poll_loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                if not await self._ensure_bot():
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    backoff = min(backoff * 1.5, 60.0)
                    continue
                backoff = 2.0
                data = await self.client.get_updates(offset=self._offset, timeout=25)
                if self._stop.is_set():
                    break
                if not data.get("ok"):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    backoff = min(backoff * 1.5, 60.0)
                    continue
                for update in data.get("result") or []:
                    if not isinstance(update, dict):
                        continue
                    update_id = update.get("update_id")
                    if update_id is not None:
                        try:
                            self._offset = int(update_id) + 1
                        except (TypeError, ValueError):
                            pass
                    await self._dispatch_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("telegram poll loop error")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 1.5, 60.0)

    async def _progress_loop(self) -> None:
        interval = max(15.0, float(settings.telegram_progress_interval_seconds))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self.inbox.progress.poll_once(self._send_status)
            except Exception:  # noqa: BLE001
                log.exception("telegram progress loop error")

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        # Do not persist full Telegram payloads (user ids) into house memory.
        try:
            result = await self.inbox.handle_message(message)
        except Exception:  # noqa: BLE001
            log.exception("telegram handle_message failed")
            return
        if result.reply:
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            try:
                chat_id = int(chat.get("id"))
            except (TypeError, ValueError):
                return
            reply_to = message.get("message_id")
            try:
                reply_to_id = int(reply_to) if reply_to is not None else None
            except (TypeError, ValueError):
                reply_to_id = None
            await self._send_status(chat_id, result.reply, reply_to_message_id=reply_to_id)

    async def _send_status(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        data = await self.client.send_message(
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
        )
        if data.get("ok"):
            result = data.get("result") or {}
            mid = result.get("message_id")
            if mid is not None:
                try:
                    self.inbox.remember_outbound(chat_id, int(mid))
                except (TypeError, ValueError):
                    pass
        else:
            log.info("telegram status post failed: %s", redact(str(data.get("error"))))

    async def handle_webhook_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Optional localhost webhook path — not the default transport."""
        if not settings.telegram_configured:
            return {"ok": False, "error": "telegram not configured"}
        if not settings.telegram_webhook_local:
            return {"ok": False, "error": "local webhook disabled"}
        await self._dispatch_update(update)
        return {"ok": True}


telegram_inbox = TelegramInboxService()
