import asyncio
import logging
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ami.client import AmiError
from app.db.models import BitrixContact
from app.db.session import get_session
from app.services.bitrix_contacts import ContactRouting
from app.services.phones import normalize_phone

router = APIRouter(prefix="/contacts", tags=["contacts"])
log = logging.getLogger(__name__)
Database = Annotated[AsyncSession, Depends(get_session)]


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phones: list[str]
    skip_bot: bool


class ContactsPage(BaseModel):
    items: list[ContactOut]
    total: int
    synced_at: datetime | None


class SkipBotUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skip_bot: StrictBool


class SkipBotResult(ContactOut):
    applied: bool


@router.get("")
async def list_contacts(
    db: Database,
    q: Annotated[str, Query(max_length=128)] = "",
    skip_bot: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ContactsPage:
    """Справочник контактов Bitrix24 и локальные метки пропуска бота."""
    conditions = []
    if skip_bot is not None:
        conditions.append(BitrixContact.skip_bot == skip_bot)
    if term := q.strip():
        matches = [BitrixContact.name.icontains(term, autoescape=True)]
        if re.fullmatch(r"\+?[0-9().\s-]+", term):
            digits = re.sub(r"\D", "", normalize_phone(term) or term)
            if digits:
                phones = cast(BitrixContact.phones, Text)
                matches.append(phones.contains(digits))
                if digits.startswith("80") and len(digits) > 2:
                    matches.append(phones.contains(f"375{digits[2:]}"))
        conditions.append(or_(*matches))
    async with db.begin():
        total = await db.scalar(select(func.count()).select_from(BitrixContact).where(*conditions))
        rows = await db.scalars(
            select(BitrixContact).where(*conditions)
            .order_by(func.lower(BitrixContact.name), BitrixContact.id).offset(offset).limit(limit)
        )
        synced_at = await db.scalar(select(func.max(BitrixContact.synced_at)))
        return ContactsPage(
            items=[ContactOut.model_validate(contact) for contact in rows],
            total=total or 0,
            synced_at=synced_at,
        )


@router.put("/{contact_id}/skip-bot")
async def set_skip_bot(
    request: Request,
    contact_id: Annotated[int, Path(gt=0, lt=2**63)],
    data: SkipBotUpdate,
    db: Database,
) -> SkipBotResult:
    """Сохранить метку и применить актуальный список номеров в Asterisk."""
    async with db.begin():
        contact = await db.get(BitrixContact, contact_id, with_for_update=True)
        if contact is None:
            raise HTTPException(404, "Контакт не найден")
        if data.skip_bot and not contact.phones:
            raise HTTPException(409, "У контакта нет номера телефона для маршрутизации")
        contact.skip_bot = data.skip_bot
        result = ContactOut.model_validate(contact)

    routing: ContactRouting | None = getattr(request.app.state, "contact_routing", None)
    applied = False
    if routing is not None:
        try:
            async with asyncio.timeout(5):
                await routing.sync()
            applied = True
        except (AmiError, OSError, TimeoutError, SQLAlchemyError) as exc:
            log.warning("Метка сохранена, применение в Asterisk отложено (%s)", type(exc).__name__)
    return SkipBotResult(**result.model_dump(), applied=applied)
