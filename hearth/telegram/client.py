"""Thin Telegram Bot API client (long-polling getUpdates + sendMessage)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from hearth.config import settings
from hearth.memory.redact import redact

log = logging.getLogger("hearth.telegram")

API_ROOT = "https://api.telegram.org"


class TelegramBotClient:
    def __init__(self, token: str | None = None) -> None:
        self._token = (token if token is not None else settings.telegram_bot_token).strip()
        self._client: httpx.AsyncClient | None = None
        self.bot_user_id: int | None = None
        self.bot_username: str = ""

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _base(self) -> str:
        # Token stays server-side; never log the full URL with token.
        return f"{API_ROOT}/bot{self._token}"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            return {"ok": False, "error": "telegram not configured"}
        client = await self._http()
        url = f"{self._base()}/{method}"
        try:
            response = await client.post(url, json=payload or {})
            data = response.json()
            if not isinstance(data, dict):
                return {"ok": False, "error": "invalid telegram response"}
            if not data.get("ok"):
                # Redact any accidental token echo from Telegram error descriptions.
                desc = redact(str(data.get("description") or "telegram api error"))
                log.warning("telegram %s failed: %s", method, desc)
                return {"ok": False, "error": desc, "error_code": data.get("error_code")}
            return data
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram %s error: %s", method, redact(str(exc)))
            return {"ok": False, "error": redact(str(exc))}

    async def get_me(self) -> dict[str, Any]:
        data = await self._call("getMe")
        if data.get("ok"):
            result = data.get("result") or {}
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
        body: dict[str, Any] = {
            "timeout": max(0, min(int(timeout), 50)),
            "limit": max(1, min(int(limit), 100)),
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            body["offset"] = int(offset)
        return await self._call("getUpdates", body)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        disable_notification: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": (text or "")[:3500],
            "disable_notification": disable_notification,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            body["reply_to_message_id"] = int(reply_to_message_id)
        data = await self._call("sendMessage", body)
        if not data.get("ok"):
            log.info("telegram send failed chat=%s err=%s", chat_id, data.get("error"))
        return data

    async def delete_webhook(self) -> dict[str, Any]:
        """Ensure long-polling works (webhook would block getUpdates)."""
        return await self._call("deleteWebhook", {"drop_pending_updates": False})
