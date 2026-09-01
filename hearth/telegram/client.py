"""Small, pooled async client for the Telegram Bot API.

The client deliberately does not raise API or transport errors into the update
loop. Every call returns Telegram's familiar ``{"ok": ...}`` envelope with a
sanitised ``error`` value on failure. Writes are never retried after an
ambiguous transport failure; only an explicit HTTP 429 is retried once using
Telegram's advertised ``retry_after`` delay.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from hearth.config import settings
from hearth.memory.redact import redact

log = logging.getLogger("hearth.telegram")

API_ROOT = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


class TelegramBotClient:
    """A reusable Telegram Bot API client.

    When ``token`` is omitted, the current setting is read for every call. This
    makes secret rotation and test overrides work without rebuilding the whole
    Telegram service. Supplying a token pins the instance to that token.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_root: str = API_ROOT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_override = token.strip() if token is not None else None
        self._api_root = api_root.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self.bot_user_id: int | None = None
        self.bot_username = ""

    @property
    def token(self) -> str:
        if self._token_override is not None:
            return self._token_override
        return settings.telegram_bot_token.strip()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _base(self) -> str:
        # The URL must never be logged: Bot API URLs contain the bot token.
        return f"{self._api_root}/bot{self.token}"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=5.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _safe_text(self, value: object) -> str:
        text = redact(str(value or "telegram api error"))
        token = self.token
        if token:
            text = text.replace(token, "[REDACTED]")
        return text[:500]

    def _failure(
        self,
        error: object,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
        outcome_unknown: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": self._safe_text(error)}
        if error_code is not None:
            result["error_code"] = error_code
        if retry_after is not None:
            result["parameters"] = {"retry_after": retry_after}
        if outcome_unknown:
            result["outcome_unknown"] = True
        return result

    @staticmethod
    def _retry_after(data: Mapping[str, Any]) -> int | None:
        parameters = data.get("parameters")
        if not isinstance(parameters, Mapping):
            return None
        try:
            delay = int(parameters.get("retry_after"))
        except (TypeError, ValueError):
            return None
        return max(0, delay)

    async def _call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        read_timeout: float = 15.0,
        ambiguous_write: bool = False,
    ) -> dict[str, Any]:
        if not self.configured:
            return self._failure("telegram not configured")

        client = await self._http()
        timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=15.0, pool=5.0)
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{self._base()}/{method}",
                    json=payload or {},
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001 - keep the polling loop alive
                # Whether Telegram received a POST is unknown, so never retry it.
                error = self._failure(
                    f"telegram transport error: {exc}",
                    outcome_unknown=ambiguous_write,
                )
                log.warning("telegram %s transport error: %s", method, error["error"])
                return error

            try:
                data = response.json()
            except (ValueError, TypeError):
                data = None
            if not isinstance(data, dict):
                error = self._failure(
                    "invalid telegram response",
                    error_code=response.status_code,
                    outcome_unknown=ambiguous_write,
                )
                log.warning("telegram %s returned non-JSON response", method)
                return error

            # A confirmed HTTP 429 is safe to retry according to the Bot API.
            retry_after = self._retry_after(data)
            if response.status_code == 429 and attempt == 0 and retry_after is not None:
                log.info("telegram %s rate limited; retrying in %ss", method, retry_after)
                await asyncio.sleep(retry_after)
                continue

            if not data.get("ok"):
                try:
                    error_code = int(data.get("error_code") or response.status_code)
                except (TypeError, ValueError):
                    error_code = response.status_code
                error = self._failure(
                    data.get("description") or "telegram api error",
                    error_code=error_code,
                    retry_after=retry_after,
                )
                log.warning("telegram %s rejected: %s", method, error["error"])
                return error
            return data

        return self._failure("telegram rate limit retry exhausted", error_code=429)

    async def get_me(self) -> dict[str, Any]:
        data = await self._call("getMe")
        if data.get("ok"):
            result = data.get("result")
            result = result if isinstance(result, dict) else {}
            try:
                self.bot_user_id = int(result.get("id"))
            except (TypeError, ValueError):
                self.bot_user_id = None
            self.bot_username = str(result.get("username") or "")
        return data

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 25,
        limit: int = 50,
    ) -> dict[str, Any]:
        poll_timeout = max(0, min(int(timeout), 50))
        body: dict[str, Any] = {
            "timeout": poll_timeout,
            "limit": max(1, min(int(limit), 100)),
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            body["offset"] = int(offset)
        # Telegram holds this request for poll_timeout seconds. Leave a
        # deliberate network margin so a healthy empty poll is not timed out.
        return await self._call("getUpdates", body, read_timeout=poll_timeout + 10.0)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": (text or "")[:MAX_MESSAGE_LENGTH],
            "disable_notification": bool(disable_notification),
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to_message_id is not None:
            body["reply_parameters"] = {
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return await self._call("sendMessage", body, ambiguous_write=True)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": (text or "")[:MAX_MESSAGE_LENGTH],
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return await self._call("editMessageText", body, ambiguous_write=True)

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "reply_markup": reply_markup or {"inline_keyboard": []},
            },
            ambiguous_write=True,
        )

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "callback_query_id": str(callback_query_id),
            "show_alert": bool(show_alert),
        }
        if text:
            body["text"] = text[:200]
        return await self._call("answerCallbackQuery", body)

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        """Disable a webhook before long polling; preserve pending updates by default."""
        return await self._call(
            "deleteWebhook",
            {"drop_pending_updates": bool(drop_pending_updates)},
        )
