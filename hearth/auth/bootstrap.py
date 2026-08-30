"""Create the first house superuser when the users table is empty."""

from __future__ import annotations

from sqlmodel import Session, select

from hearth.auth.core import hash_password
from hearth.auth.db import get_engine
from hearth.auth.models import UserDb
from hearth.config import settings


def bootstrap_admin() -> None:
    email = settings.admin_email.strip().lower()
    password = settings.admin_password
    if not email or not password:
        return
    with Session(get_engine()) as session:
        existing = session.exec(select(UserDb)).first()
        if existing is not None:
            return
        session.add(
            UserDb(
                email=email,
                hashed_password=hash_password(password),
                is_active=True,
                is_superuser=True,
            )
        )
        session.commit()
