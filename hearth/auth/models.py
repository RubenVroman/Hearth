from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserDb(SQLModel, table=True):
    """One house superuser. No orgs, no signup, no Stripe."""

    __tablename__ = "userdb"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(index=True, unique=True)
    hashed_password: str
    is_active: bool = True
    is_superuser: bool = True
    last_login: datetime | None = Field(default_factory=_utcnow)
