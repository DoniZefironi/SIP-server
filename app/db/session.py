import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

log = logging.getLogger(__name__)

# Соединение с базой открывается при первом запросе, а не при импорте.
engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def database_is_up(timeout: float = 3.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 — проверка состояния не должна падать сама
        log.warning("База недоступна (%s: %s)", type(exc).__name__, exc)
        return False
