"""Создать администратора с локальной консоли сервера."""

import argparse
import asyncio
import sys
from getpass import getpass

from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert

from app.api.auth import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, CreateUser
from app.db.models import AuthUser
from app.db.session import SessionLocal, engine
from app.services.auth import Role, hash_password, lock_account_management


async def create(data: CreateUser) -> int:
    try:
        password_hash = await hash_password(data.password.get_secret_value())
        async with SessionLocal.begin() as db:
            await lock_account_management(db)
            user_id = await db.scalar(
                insert(AuthUser)
                .values(username=data.username, password_hash=password_hash, role=Role.ADMIN)
                .on_conflict_do_nothing(index_elements=[AuthUser.username])
                .returning(AuthUser.id)
            )
            if user_id is None:
                raise SystemExit("Такой логин уже существует.")
            return user_id
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit("Запустите команду в интерактивном терминале.")
    password = getpass("Пароль администратора: ")
    if password != getpass("Повторите пароль: "):
        raise SystemExit("Пароли различаются.")
    try:
        data = CreateUser(username=args.username, password=password, role=Role.ADMIN)
    except ValidationError:
        raise SystemExit(
            f"Логин — 3–64 символа, пароль — {PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} символов."
        ) from None
    print(f"Создан администратор {data.username} (ID {asyncio.run(create(data))}).")


if __name__ == "__main__":
    main()
