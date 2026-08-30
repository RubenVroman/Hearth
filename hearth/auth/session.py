"""Browser session cookies. Pattern from Zuster Annie (same-origin FastAPI + UI)."""

from __future__ import annotations

from fastapi import Response

from hearth.config import settings

REFRESH_COOKIE = "refresh_token"
HINT_COOKIE = "session_hint"


def _samesite() -> str:
    value = (settings.cookie_samesite or "lax").lower()
    if value not in {"lax", "strict", "none"}:
        return "lax"
    return value


def set_session_cookies(response: Response, refresh_jwt: str) -> None:
    max_age = max(1, settings.refresh_token_expire_days) * 24 * 60 * 60
    shared = {
        "max_age": max_age,
        "path": "/",
        "secure": settings.cookie_secure,
        "samesite": _samesite(),
    }
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_jwt,
        httponly=True,
        **shared,
    )
    response.set_cookie(
        HINT_COOKIE,
        "1",
        httponly=False,
        **shared,
    )


def clear_session_cookies(response: Response) -> None:
    shared = {
        "path": "/",
        "secure": settings.cookie_secure,
        "samesite": _samesite(),
    }
    response.delete_cookie(REFRESH_COOKIE, **shared)
    response.delete_cookie(HINT_COOKIE, **shared)
