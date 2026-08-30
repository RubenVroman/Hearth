"""Request gate: login session or HEARTH_TOKEN. No query-string house token."""

from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket
from sqlmodel import Session

from hearth.auth.core import SCOPE_ACCESS, SCOPE_REFRESH, get_user_from_token
from hearth.auth.db import get_engine
from hearth.auth.session import REFRESH_COOKIE
from hearth.config import settings

PUBLIC_EXACT = {
    "/health",
    "/login",
    "/auth/token",
    "/auth/session/refresh",
    "/auth/session/logout",
    "/manifest.webmanifest",
    "/sw.js",
    "/apple-touch-icon.png",
}
PUBLIC_PREFIXES = ("/static/",)

# Browser <img> / <link> cannot send X-Auth-Token. Same-origin GETs for these
# paths may authenticate with the refresh cookie (same model as GET "/").
_REFRESH_OK_GET_EXACT = frozenset({"/", "/api/media/art"})
_REFRESH_OK_GET_PREFIXES = ("/api/plex/thumb/",)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def refresh_cookie_ok_for_request(method: str, path: str) -> bool:
    """Whether a GET/HEAD may use the httpOnly refresh cookie alone."""
    if method not in {"GET", "HEAD"}:
        return False
    if path in _REFRESH_OK_GET_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _REFRESH_OK_GET_PREFIXES)


def machine_token_ok(value: str) -> bool:
    token = settings.token.strip()
    return bool(token) and value == token


def request_machine_token(request: Request) -> str:
    return request.headers.get("x-hearth-token", "")


def request_access_token(request: Request) -> str:
    return request.headers.get("x-auth-token", "")


def session_ok(access_jwt: str = "", refresh_jwt: str = "", *, allow_refresh: bool = False) -> bool:
    if not settings.app_secret_key.strip():
        return False
    with Session(get_engine()) as session:
        if access_jwt:
            try:
                get_user_from_token(access_jwt, session, expected_scope=SCOPE_ACCESS)
                return True
            except HTTPException:
                pass
        if allow_refresh and refresh_jwt:
            try:
                get_user_from_token(refresh_jwt, session, expected_scope=SCOPE_REFRESH)
                return True
            except HTTPException:
                pass
    return False


def http_authorized(request: Request) -> bool:
    if machine_token_ok(request_machine_token(request)):
        return True
    refresh = request.cookies.get(REFRESH_COOKIE) or ""
    allow_refresh = refresh_cookie_ok_for_request(request.method, request.url.path)
    return session_ok(request_access_token(request), refresh, allow_refresh=allow_refresh)


def ws_authorized(websocket: WebSocket) -> bool:
    if machine_token_ok(websocket.headers.get("x-hearth-token", "")):
        return True
    access = websocket.headers.get("x-auth-token", "")
    refresh = websocket.cookies.get(REFRESH_COOKIE) or ""
    return session_ok(access, refresh, allow_refresh=True)
