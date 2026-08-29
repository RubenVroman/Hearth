"""Auth routes: Gridways POST /auth/token plus Zuster Annie refresh/logout cookies."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session

from hearth.auth.core import (
    authenticate_password,
    generate_access_token,
    generate_refresh_token,
    get_user_from_token,
    user_and_session,
)
from hearth.auth.db import get_session
from hearth.auth.models import UserDb
from hearth.auth.session import REFRESH_COOKIE, clear_session_cookies, set_session_cookies
from hearth.config import settings

auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


class TokenBody(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


def _token_payload(user: UserDb, access: str) -> dict[str, Any]:
    return {
        "access_token": access,
        "token_type": "X-Auth-Token",
        "email": user.email,
        "expires_in": settings.access_token_expire_minutes * 60,
    }


@auth_router.post("/token")
def create_token(body: TokenBody, response: Response, session: Session = Depends(get_session)) -> dict[str, Any]:
    user = authenticate_password(session, body.email, body.password)
    access = generate_access_token(user)
    refresh = generate_refresh_token(user)
    set_session_cookies(response, refresh)
    return _token_payload(user, access)


@auth_router.post("/session/refresh")
def refresh_session(request: Request, response: Response, session: Session = Depends(get_session)) -> dict[str, Any]:
    refresh_jwt = request.cookies.get(REFRESH_COOKIE) or ""
    if not refresh_jwt:
        raise HTTPException(status_code=401, detail="unauthorized")
    user = get_user_from_token(refresh_jwt, session, expected_scope="refresh")
    access = generate_access_token(user)
    rotated = generate_refresh_token(user)
    set_session_cookies(response, rotated)
    return _token_payload(user, access)


@auth_router.post("/session/logout")
def logout(response: Response) -> dict[str, Any]:
    clear_session_cookies(response)
    return {"ok": True}


@auth_router.get("/me")
def me(pair: tuple[UserDb, Session] = Depends(user_and_session)) -> dict[str, Any]:
    user, _session = pair
    return {
        "email": user.email,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
    }
