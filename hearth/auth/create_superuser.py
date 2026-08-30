"""CLI: python -m hearth.auth.create_superuser — Gridways-style first user, interactive."""

from __future__ import annotations

from getpass import getpass

from sqlmodel import Session, select

from hearth.auth.core import hash_password
from hearth.auth.db import get_engine, init_db
from hearth.auth.models import UserDb


def main() -> None:
    init_db()
    print("Create a new Hearth superuser")
    email = input("Enter email: ").strip().lower()
    password = getpass("Enter password: ")
    confirm = getpass("Confirm password: ")
    if not email or not password:
        print("Email and password are required. Aborting.")
        return
    if password != confirm:
        print("Passwords do not match. Aborting.")
        return
    with Session(get_engine()) as session:
        if session.exec(select(UserDb).where(UserDb.email == email)).first() is not None:
            print("A user with that email already exists. Aborting.")
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
    print("Superuser created.")


if __name__ == "__main__":
    main()
