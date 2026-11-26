import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import batched
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.ami.astdb import AstDB
from app.ami.client import AmiError
from app.db.models import BitrixContact
from app.db.session import SessionLocal
from app.services.bitrix import Bitrix, BitrixError, positive_id
from app.services.phones import normalize_phone

log = logging.getLogger(__name__)
_IMPORT_INTERVAL = 15 * 60
_ROUTING_INTERVAL = 15
_FAMILY = "savt-skip-bot"


def _contact_values(item: dict[str, Any], synced_at: datetime) -> dict[str, Any]:
    contact_id = positive_id(item.get("id"))
    if contact_id is None:
        raise BitrixError("Bitrix24 вернул контакт без ID")
    name = " ".join(
        value.strip() for field in ("lastName", "name", "secondName")
        if isinstance(value := item.get(field), str) and value.strip()
    )
    multifields = item.get("fm")
    if multifields is None:
        multifields = []
    if not isinstance(multifields, list) or any(
        not isinstance(field, dict) for field in multifields
    ):
        raise BitrixError("Bitrix24 вернул неверные телефоны контакта")
    phones = sorted({
        number for field in multifields
        if field.get("typeId") == "PHONE"
        and isinstance(raw := field.get("value"), str)
        and (number := normalize_phone(raw)) is not None
    })
    return {
        "id": contact_id, "name": name or f"Контакт {contact_id}",
        "phones": phones, "synced_at": synced_at,
    }


async def import_contacts(bitrix: Bitrix) -> int:
    synced_at = datetime.now(UTC)
    contacts = []
    async for page in bitrix.contact_pages():
        contacts.extend(_contact_values(item, synced_at) for item in page)

    # Публикуем только полный справочник, сохраняя локальные метки при обновлении данных CRM.
    async with SessionLocal.begin() as db:
        for batch in batched(contacts, 500, strict=False):
            statement = insert(BitrixContact).values(batch)
            await db.execute(statement.on_conflict_do_update(
                index_elements=[BitrixContact.id],
                set_={
                    "name": statement.excluded.name,
                    "phones": statement.excluded.phones,
                    "synced_at": statement.excluded.synced_at,
                },
            ))
        await db.execute(delete(BitrixContact).where(BitrixContact.synced_at != synced_at))
    return len(contacts)


async def run_contact_import(bitrix: Bitrix) -> None:
    while True:
        try:
            total = await import_contacts(bitrix)
            log.info("Справочник Bitrix24 обновлён, контактов: %s", total)
        except BitrixError as exc:
            log.warning("Справочник контактов Bitrix24 не обновлён (%s)", exc)
        except Exception:
            log.exception("Не удалось сохранить справочник контактов")
        await asyncio.sleep(_IMPORT_INTERVAL)


def _station_numbers(phones: Sequence[list[str]]) -> set[str]:
    numbers = {phone.removeprefix("+") for row in phones for phone in row}
    domestic = {f"80{number[3:]}" for number in numbers if number.startswith("375")}
    return numbers | domestic


class ContactRouting:
    def __init__(self, astdb: AstDB) -> None:
        self._astdb = astdb
        self._lock = asyncio.Lock()

    async def sync(self) -> None:
        async with self._lock:
            async with SessionLocal() as db:
                phones = (await db.scalars(
                    select(BitrixContact.phones).where(BitrixContact.skip_bot.is_(True))
                )).all()
            desired = _station_numbers(phones)
            current = await self._astdb.list_family(_FAMILY)
            for number in sorted(desired - current.keys()):
                await self._astdb.put(_FAMILY, number, "1")
            for number in sorted(current.keys() - desired):
                await self._astdb.delete(_FAMILY, number)

    async def run(self) -> None:
        while True:
            try:
                await self.sync()
            except (AmiError, OSError, TimeoutError) as exc:
                log.warning("Список пропуска бота в Asterisk не обновлён (%s)", type(exc).__name__)
            except Exception:
                log.exception("Не удалось применить список пропуска бота")
            await asyncio.sleep(_ROUTING_INTERVAL)
