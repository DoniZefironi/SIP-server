"""Обслуживание раз в час: пустые записи, срок хранения записей и сырых событий."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete

from app.config import Settings
from app.db.models import CallEvent
from app.db.session import SessionLocal

log = logging.getLogger(__name__)

# Пустой WAV — это только заголовок, 44 байта. Такие файлы остаются от звонков без разговора.
EMPTY_WAV_SIZE = 44
# Пока клиент ждёт в очереди, файл записи тоже пустой: трогаем только старые.
EMPTY_GRACE_SECONDS = 3600
SUBDIRS = ("calls", "messages")


def clean_recordings(root: Path, retention_days: int, now: float | None = None) -> tuple[int, int]:
    """Удалить пустые записи и записи старше срока хранения. Возвращает (пустых, старых)."""
    now = time.time() if now is None else now
    expire_before = now - retention_days * 86400
    empty = expired = 0
    for subdir in SUBDIRS:
        folder = root / subdir
        if not folder.is_dir():
            continue
        for path in folder.glob("*.wav"):
            try:
                stat = path.lstat()
            except FileNotFoundError:
                continue
            if not path.is_file() or path.is_symlink():
                continue
            if stat.st_mtime < expire_before:
                path.unlink(missing_ok=True)
                expired += 1
            elif (
                subdir == "calls"
                and stat.st_size <= EMPTY_WAV_SIZE
                and stat.st_mtime < now - EMPTY_GRACE_SECONDS
            ):
                path.unlink(missing_ok=True)
                empty += 1
    return empty, expired


async def clean_events(retention_days: int) -> int:
    border = datetime.now(UTC) - timedelta(days=retention_days)
    async with SessionLocal() as session:
        result = await session.execute(delete(CallEvent).where(CallEvent.received_at < border))
        await session.commit()
        return result.rowcount or 0


async def run_maintenance(settings: Settings) -> None:
    while True:
        try:
            if settings.recordings_dir.is_dir():
                empty, expired = await asyncio.to_thread(
                    clean_recordings, settings.recordings_dir, settings.recordings_retention_days
                )
                if empty or expired:
                    log.info("Записи: удалено пустых %s, старше срока %s", empty, expired)
            else:
                log.warning("Папка записей %s недоступна", settings.recordings_dir)
            removed = await clean_events(settings.events_retention_days)
            if removed:
                log.info("Удалено старых событий станции: %s", removed)
        except Exception:
            log.exception("Ошибка обслуживания")
        await asyncio.sleep(settings.maintenance_interval)
