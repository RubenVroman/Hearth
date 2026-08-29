"""Strip secrets before anything is stored, embedded, exported, or shown."""

from __future__ import annotations

import re
from typing import Any, Callable, Match

REDACTED = "[redacted]"

_ASSIGN_KEYS = (
    "OPENAI_API_KEY",
    "HA_TOKEN",
    "PLEX_TOKEN",
    "RADARR_API_KEY",
    "SONARR_API_KEY",
    "OVERSEERR_API_KEY",
    "HEARTH_TOKEN",
    "APP_SECRET_KEY",
    "HEARTH_ADMIN_PASSWORD",
    "HEARTH_ADMIN_EMAIL",
    "HEARTH_COS_WEBHOOK_KEY",
    "HEARTH_COS_WEBHOOK",
)

_LIVE_ATTRS = (
    "openai_api_key",
    "ha_token",
    "plex_token",
    "radarr_api_key",
    "sonarr_api_key",
    "overseerr_api_key",
    "token",
    "app_secret_key",
    "admin_password",
    "cos_webhook_key",
    "cos_webhook",
)

Replacement = str | Callable[[Match[str]], str]

_PATTERNS: tuple[tuple[re.Pattern[str], Replacement], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"), REDACTED),
    (re.compile(r"\bek_[A-Za-z0-9_-]{8,}\b"), REDACTED),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        REDACTED,
    ),
    (re.compile(r"(?i)\b(authorization:\s*bearer\s+)\S+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b(x-auth-token|x-hearth-token)\s*[:=]\s*\S+"), r"\1=" + REDACTED),
    (
        re.compile(r"(?i)\b(" + "|".join(_ASSIGN_KEYS) + r")\s*=\s*\S+"),
        lambda m: f"{m.group(1)}={REDACTED}",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key|password|passwd|token)\s*[:=]\s*([^\s,;]+)"
        ),
        lambda m: f"{m.group(1)}={REDACTED}",
    ),
)


def _live_secrets() -> list[str]:
    try:
        from hearth.config import settings
    except Exception:  # noqa: BLE001
        return []
    found: list[str] = []
    for attr in _LIVE_ATTRS:
        value = getattr(settings, attr, "")
        if isinstance(value, str) and len(value.strip()) >= 8:
            found.append(value.strip())
    return found


def redact(text: str | None) -> str:
    """Return text with API keys, tokens, passwords, and .env assignments removed."""
    if not text:
        return ""
    out = str(text)
    for secret in _live_secrets():
        if secret and secret in out:
            out = out.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_obj(value: Any) -> Any:
    """Redact strings inside nested dict/list payloads (tool results, export)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    return value
