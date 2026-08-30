"""Identity + JWT. Slim copy of GridwaysBackend app/auth/core.py (no Google, orgs, email)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException
from passlib.context import CryptContext
from sqlmodel import Session, select

from hearth.auth.db import get_session
from hearth.auth.models import UserDb
from hearth.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SCOPE_ACCESS = "access"
SCOPE_REFRESH = "refresh"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return pwd_context.verify(password, hashed_password)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_token(user: UserDb, *, scope: str, expire_delta: timedelta) -> str:
    if not settings.app_secret_key.strip():
        raise HTTPException(status_code=503, detail="APP_SECRET_KEY unset")
    now = _utcnow()
    payload = {
        "sub": user.email,
        "iat": now,
        "exp": now + expire_delta,
        "scope": scope,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=settings.algorithm)


def generate_access_token(user: UserDb) -> str:
    minutes = max(1, settings.access_token_expire_minutes)
    return generate_token(user, scope=SCOPE_ACCESS, expire_delta=timedelta(minutes=minutes))


def generate_refresh_token(user: UserDb) -> str:
    days = max(1, settings.refresh_token_expire_days)
    return generate_token(user, scope=SCOPE_REFRESH, expire_delta=timedelta(days=days))


def get_user_by_email(session: Session, email: str) -> UserDb | None:
    return session.exec(select(UserDb).where(UserDb.email == email.lower().strip())).first()


def authenticate_password(session: Session, email: str, password: str) -> UserDb:
    user = get_user_by_email(session, email)
    if user is None or not user.hashed_password:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="User is blocked")
    if not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user.last_login = _utcnow()
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def get_user_from_token(token: str, session: Session, *, expected_scope: str) -> UserDb:
    if not settings.app_secret_key.strip():
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        payload = jwt.decode(token, settings.app_secret_key, algorithms=[settings.algorithm])
    except jwt.exceptions.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if payload.get("scope") != expected_scope:
        raise HTTPException(status_code=401, detail="Invalid token")
    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = get_user_by_email(session, str(email))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="User is blocked")
    return user


def user_and_session(
    x_auth_token: Annotated[str | None, Header()] = None,
    session: Session = Depends(get_session),
) -> tuple[UserDb, Session]:
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="unauthorized")
    user = get_user_from_token(x_auth_token, session, expected_scope=SCOPE_ACCESS)
    return user, session


def require_admin(
    pair: tuple[UserDb, Session] = Depends(user_and_session),
) -> tuple[UserDb, Session]:
    user, session = pair
    if not user.is_superuser:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user, session
