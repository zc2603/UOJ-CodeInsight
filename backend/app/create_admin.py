from __future__ import annotations

import argparse
import asyncio
import getpass

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AdminUser
from app.security import hash_secret


async def create_admin(username: str, password: str) -> None:
    async with SessionLocal() as db:
        existing = (
            await db.execute(select(AdminUser).where(AdminUser.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            raise SystemExit(f"admin user {username!r} already exists")
        db.add(AdminUser(username=username, password_hash=hash_secret(password)))
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a teacher account")
    parser.add_argument("username")
    args = parser.parse_args()
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("passwords do not match")
    if len(password) < 10:
        raise SystemExit("password must contain at least 10 characters")
    asyncio.run(create_admin(args.username, password))
    print(f"Created teacher account {args.username!r}.")


if __name__ == "__main__":
    main()
