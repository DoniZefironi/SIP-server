import asyncio
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.config import get_settings
from app.db.models import BitrixDelivery, Call, RecordingJob
from app.db.session import get_session
from app.services.bitrix import Bitrix, BitrixError
from app.services.call_history import (
    TAB_CONDITIONS,
    VOICEMAIL_LEFT,
    CallTab,
    call_count_columns,
    history_conditions,
)
from app.services.journal import Direction, Outcome
from app.services.phones import crm_phone_variants, normalize_phone
from app.services.recordings import AudioKind, ready_recording
from app.services.savtassist import CallObjectLookup
from app.services.yandexgpt import CallSummary

router = APIRouter(tags=["calls"])


class CallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    ended_at: datetime | None
    caller: str | None
    caller_raw: str | None
    sector: int | None
    sector_name: str | None
    sector_number: str | None
    sector_by_default: bool
    direction: Direction
    outcome: str | None
    answered_by: str | None
    initiated_by: str | None
    employee_name: str | None
    employee_number: str | None
    wait_seconds: int | None
    talk_seconds: int | None
    has_recording: bool
    has_message: bool
    voicemail_left: bool


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    destination: str
    target_id: int
    status: str
    external_id: int | None
    last_error: str | None


class CallDetail(CallOut):
    object_lookup: CallObjectLookup | None
    transcription_status: str | None
    transcript: str | None
    summary_status: str | None
    summary: CallSummary | None
    bitrix_routing_status: str | None
    bitrix_deliveries: list[DeliveryOut]


class CallPage(BaseModel):
    items: list[CallOut]
    total: int


class CallCounts(BaseModel):
    incoming: int
    outgoing: int
    missed: int
    missed_with_voicemail: int
    missed_without_voicemail: int


class ClientContact(BaseModel):
    id: int
    name: str
    url: str


class ClientContacts(BaseModel):
    items: list[ClientContact]


class NotFoundOut(BaseModel):
    detail: str


_WAV_CONTENT = {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}
_EMPLOYEE_NUMBER = re.compile(r"(?:PJSIP/)?(\+?[0-9]{1,15})(?:@[A-Za-z0-9_.-]+)?", re.I)


def _to_out(call: Call, voicemail_left: bool) -> CallOut:
    settings = get_settings()
    sector = call.sector
    employee = call.initiated_by or call.answered_by
    match = _EMPLOYEE_NUMBER.fullmatch(employee) if employee else None
    number = match[1] if match else None
    return CallOut.model_validate(
        {
            **{f: getattr(call, f) for f in CallOut.model_fields if hasattr(call, f)},
            "sector_name": settings.sector_names.get(sector) if sector is not None else None,
            "sector_number": settings.sector_numbers.get(sector) if sector is not None else None,
            "employee_name": settings.employee_names.get(employee) if employee else None,
            "employee_number": (normalize_phone(number) or number) if number else None,
            "has_recording": ready_recording(call, "mix", settings.recordings_dir) is not None,
            "has_message": ready_recording(call, "message", settings.recordings_dir) is not None,
            "voicemail_left": voicemail_left,
        }
    )


def _history_conditions(
    sector: Annotated[int | None, Query(ge=1, le=9)] = None,
    caller: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[ColumnElement[bool]]:
    return history_conditions(sector=sector, caller=caller, date_from=date_from, date_to=date_to)


@router.get("/calls")
async def list_calls(
    session: Annotated[AsyncSession, Depends(get_session)],
    conditions: Annotated[list[ColumnElement[bool]], Depends(_history_conditions)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    outcome: Outcome | None = None,
    tab: CallTab | None = None,
    voicemail: Annotated[bool | None, Query(description="Фильтр пропущенных по голосовому")] = None,
) -> CallPage:
    """Журнал звонков, новые сверху. Время в UTC."""
    if voicemail is not None:
        if tab not in (None, CallTab.MISSED):
            raise HTTPException(422, "Фильтр голосовых сообщений применяется к пропущенным")
        tab = CallTab.MISSED
        conditions.append(VOICEMAIL_LEFT if voicemail else ~VOICEMAIL_LEFT)
    if tab is not None:
        conditions.append(TAB_CONDITIONS[tab])
    if outcome is not None:
        conditions.append(Call.outcome == outcome)

    total = await session.scalar(select(func.count()).select_from(Call).where(*conditions))
    rows = await session.execute(
        select(Call, VOICEMAIL_LEFT)
        .where(*conditions)
        .order_by(Call.id.desc())
        .limit(limit)
        .offset(offset)
    )
    calls = rows.all()
    items = await run_in_threadpool(
        lambda: [_to_out(call, voicemail_left) for call, voicemail_left in calls]
    )
    return CallPage(items=items, total=total or 0)


@router.get("/calls/counts")
async def count_calls(
    session: Annotated[AsyncSession, Depends(get_session)],
    conditions: Annotated[list[ColumnElement[bool]], Depends(_history_conditions)],
) -> CallCounts:
    """Счётчики вкладок за выбранный период с учётом отдела и номера клиента."""
    result = await session.execute(
        select(*call_count_columns()).select_from(Call).where(*conditions)
    )
    return CallCounts.model_validate(result.mappings().one())


@router.get(
    "/calls/{call_id}",
    responses={404: {"model": NotFoundOut, "description": "Звонок не найден"}},
)
async def get_call(
    call_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> CallDetail:
    call = await session.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "звонок не найден")
    job = await session.get(RecordingJob, call_id)
    deliveries = []
    if job is not None:
        deliveries = (
            await session.scalars(
                select(BitrixDelivery)
                .where(BitrixDelivery.call_id == call_id)
                .order_by(BitrixDelivery.destination, BitrixDelivery.target_id)
            )
        ).all()
    voicemail_left = job is not None and job.audio_kind == "message"
    details = await run_in_threadpool(_to_out, call, voicemail_left)
    return CallDetail(
        **details.model_dump(),
        object_lookup=call.object_lookup,
        transcription_status=job.transcription_status if job is not None else None,
        transcript=job.transcript if job is not None else None,
        summary_status=job.summary_status if job is not None else None,
        summary=job.summary if job is not None else None,
        bitrix_routing_status=job.bitrix_routing_status if job is not None else None,
        bitrix_deliveries=[DeliveryOut.model_validate(delivery) for delivery in deliveries],
    )


@router.get("/calls/{call_id}/contacts")
async def get_call_contacts(
    call_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ClientContacts:
    """Карточки контактов Bitrix24 по номеру клиента из звонка."""
    async with session.begin():
        call = await session.get(Call, call_id)
        if call is None:
            raise HTTPException(404, "Звонок не найден")
        raw_number = normalize_phone(call.caller_raw)
        number = call.caller or raw_number
        numbers = crm_phone_variants(number, call.caller_raw if raw_number == number else None)
    if not numbers:
        return ClientContacts(items=[])

    bitrix: Bitrix | None = getattr(request.app.state, "bitrix", None)
    if bitrix is None:
        raise HTTPException(503, "Не настроен поиск контактов Bitrix24")
    try:
        async with asyncio.timeout(8):
            ids = await bitrix.find_by_phone("contact", numbers)
            cards = await bitrix.caller_cards("contact", ids)
    except (BitrixError, TimeoutError):
        raise HTTPException(502, "Не удалось найти контакты в Bitrix24") from None

    return ClientContacts(items=[
        ClientContact(id=card.id, name=card.label, url=bitrix.card_url("contact", card.id))
        for card in cards
    ])


@router.get(
    "/calls/{call_id}/audio/{kind}",
    response_class=FileResponse,
    responses={
        200: {"content": _WAV_CONTENT, "description": "WAV-запись"},
        206: {"content": _WAV_CONTENT, "description": "Часть WAV-записи по заголовку Range"},
        404: {
            "model": NotFoundOut,
            "content": {"application/json": {}},
            "description": "Запись недоступна",
        },
        416: {"description": "Запрошенный диапазон выходит за границы файла"},
    },
)
async def get_audio(
    call_id: int, kind: AudioKind, session: Annotated[AsyncSession, Depends(get_session)]
) -> FileResponse:
    """Запись: mix — общая, client — клиент, staff — сотрудник, message — сообщение."""
    call = await session.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "записи нет")
    path = await run_in_threadpool(ready_recording, call, kind, get_settings().recordings_dir)
    if path is None:
        raise HTTPException(404, "записи нет")
    return FileResponse(path, media_type="audio/wav")
