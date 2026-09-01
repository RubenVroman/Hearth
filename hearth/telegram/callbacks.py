"""Compact authenticated Telegram callback data.

Telegram limits ``callback_data`` to 64 UTF-8 bytes. The format here carries
only immutable request coordinates and authenticates them for one chat. No
server-side cache is required to trust a button after a restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import time
from dataclasses import dataclass

from hearth.telegram.models import MediaType

MAX_CALLBACK_BYTES = 64
_VERSION = "h1"
_DOMAIN = b"hearth.telegram.callback.v1\x00"
_BASE36 = re.compile(r"[0-9a-z]+")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{16}")


class CallbackError(ValueError):
    """Base class for callback validation failures."""


class InvalidCallback(CallbackError):
    """The callback is malformed or its signature does not match."""


class ExpiredCallback(CallbackError):
    """The callback was authentic but has expired."""


@dataclass(frozen=True, slots=True)
class RequestCallback:
    media_type: MediaType
    tmdb_id: int
    season: int | None
    expires_at: int


def _to_base36(value: int) -> str:
    if value < 0:
        raise ValueError("base36 values must be non-negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def _from_base36(value: str, *, field: str) -> int:
    if (
        not value
        or not _BASE36.fullmatch(value)
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise InvalidCallback(f"invalid callback {field}")
    try:
        return int(value, 36)
    except ValueError as exc:  # defensive; the regex already constrains it
        raise InvalidCallback(f"invalid callback {field}") from exc


class CallbackCodec:
    """Encode and validate chat-bound media request buttons."""

    def __init__(self, secret: str | bytes, *, ttl_seconds: int = 86_400) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if not secret_bytes:
            raise ValueError("callback secret must not be empty")
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("callback TTL must be positive")
        self.ttl_seconds = int(ttl_seconds)
        # A derived, domain-specific key prevents cross-protocol signature reuse.
        self._key = hmac.new(secret_bytes, _DOMAIN + b"key", hashlib.sha256).digest()

    def encode(
        self,
        media_type: MediaType,
        tmdb_id: int,
        chat_id: int,
        *,
        season: int | None = None,
        now: float | None = None,
    ) -> str:
        if media_type not in {"movie", "tv"}:
            raise ValueError("media_type must be movie or tv")
        if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or tmdb_id <= 0:
            raise ValueError("TMDB id must be a positive integer")
        if season is not None:
            if media_type != "tv":
                raise ValueError("a season can only be set for TV")
            if isinstance(season, bool) or not isinstance(season, int) or not 0 <= season <= 999:
                raise ValueError("season must be between 0 and 999")
        if isinstance(chat_id, bool):
            raise ValueError("chat_id must be an integer")
        try:
            bound_chat_id = int(chat_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("chat_id must be an integer") from exc

        current = time.time() if now is None else float(now)
        if not math.isfinite(current) or current < 0:
            raise ValueError("current time must be a finite Unix timestamp")
        expires_minute = math.ceil((current + self.ttl_seconds) / 60)
        kind = "m" if media_type == "movie" else "t"
        season_part = "-" if season is None else _to_base36(season)
        payload = ".".join(
            (
                _VERSION,
                kind,
                _to_base36(tmdb_id),
                season_part,
                _to_base36(expires_minute),
            )
        )
        signature = self._signature(payload, bound_chat_id)
        encoded = f"{payload}.{signature}"
        if len(encoded.encode("utf-8")) > MAX_CALLBACK_BYTES:
            raise ValueError("callback data exceeds Telegram's 64-byte limit")
        return encoded

    def encode_request(
        self,
        media_type: MediaType,
        tmdb_id: int,
        chat_id: int,
        *,
        season: int | None = None,
        now: float | None = None,
    ) -> str:
        """Named alias that reads clearly at button construction sites."""
        return self.encode(media_type, tmdb_id, chat_id, season=season, now=now)

    def decode(
        self,
        data: str,
        chat_id: int,
        *,
        now: float | None = None,
    ) -> RequestCallback:
        if not isinstance(data, str):
            raise InvalidCallback("callback data must be text")
        if isinstance(chat_id, bool):
            raise InvalidCallback("invalid callback chat")
        try:
            raw = data.encode("ascii")
        except UnicodeEncodeError as exc:
            raise InvalidCallback("callback data must be ASCII") from exc
        if not raw or len(raw) > MAX_CALLBACK_BYTES:
            raise InvalidCallback("invalid callback data length")

        parts = data.split(".")
        if len(parts) != 6:
            raise InvalidCallback("invalid callback shape")
        version, kind, tmdb_part, season_part, expiry_part, supplied_signature = parts
        if version != _VERSION or kind not in {"m", "t"}:
            raise InvalidCallback("unsupported callback")
        if not _SIGNATURE.fullmatch(supplied_signature):
            raise InvalidCallback("invalid callback signature")

        try:
            bound_chat_id = int(chat_id)
        except (TypeError, ValueError) as exc:
            raise InvalidCallback("invalid callback chat") from exc
        payload = ".".join(parts[:5])
        expected_signature = self._signature(payload, bound_chat_id)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidCallback("invalid callback signature")

        tmdb_id = _from_base36(tmdb_part, field="TMDB id")
        if tmdb_id <= 0:
            raise InvalidCallback("invalid callback TMDB id")
        expires_minute = _from_base36(expiry_part, field="expiry")
        expires_at = expires_minute * 60
        current = time.time() if now is None else float(now)
        if not math.isfinite(current) or current < 0:
            raise InvalidCallback("invalid callback time")
        if current >= expires_at:
            raise ExpiredCallback("this button has expired")

        season: int | None
        if season_part == "-":
            season = None
        else:
            season = _from_base36(season_part, field="season")
            if season > 999 or kind != "t":
                raise InvalidCallback("invalid callback season")

        return RequestCallback(
            media_type="movie" if kind == "m" else "tv",
            tmdb_id=tmdb_id,
            season=season,
            expires_at=expires_at,
        )

    def decode_request(
        self,
        data: str,
        chat_id: int,
        *,
        now: float | None = None,
    ) -> RequestCallback:
        return self.decode(data, chat_id, now=now)

    def _signature(self, payload: str, chat_id: int) -> str:
        signed = (
            _DOMAIN
            + b"chat="
            + str(chat_id).encode("ascii")
            + b"\x00"
            + payload.encode("ascii")
        )
        digest = hmac.new(self._key, signed, hashlib.sha256).digest()[:12]
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
