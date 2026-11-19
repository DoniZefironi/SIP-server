from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Call, RecordingJob
from app.db.session import get_session
from app.services.journal import Outcome
from app.services.phones import normalize_phone
from app.services.recordings import AudioKind, ready_recording

router = APIRouter(tags=["calls"])


class CallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    ended_at: datetime | None
    caller: str | None
    caller_raw: str | None
    sector: int | None
    sector_by_default: bool
    outcome: str | None
    answered_by: str | None
    wait_seconds: int | None
    talk_seconds: int | None
    has_recording: bool
    has_message: bool


class CallDetail(CallOut):
    transcription_status: str | None
    transcript: str | None


class CallPage(BaseModel):
    items: list[CallOut]
    total: int


def _to_out(call: Call) -> CallOut:
    root = get_settings().recordings_dir
    return CallOut.model_validate(
        {
            **{f: getattr(call, f) for f in CallOut.model_fields if hasattr(call, f)},
            "has_recording": ready_recording(call, "mix", root) is not None,
            "has_message": ready_recording(call, "message", root) is not None,
        }
    )


@router.get("/calls")
async def list_calls(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sector: Annotated[int | None, Query(ge=1, le=9)] = None,
    outcome: Outcome | None = None,
    caller: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> CallPage:
    """Журнал звонков, новые сверху. Время в UTC."""
    conditions = []
    if sector is not None:
        conditions.append(Call.sector == sector)
    if outcome is not None:
        conditions.append(Call.outcome == outcome)
    if caller:
        conditions.append(Call.caller == (normalize_phone(caller) or caller))
    if date_from is not None:
        conditions.append(Call.started_at >= date_from)
    if date_to is not None:
        conditions.append(Call.started_at < date_to)

    total = await session.scalar(select(func.count()).select_from(Call).where(*conditions))
    rows = await session.scalars(
        select(Call).where(*conditions).order_by(Call.id.desc()).limit(limit).offset(offset)
    )
    calls = rows.all()
    items = await run_in_threadpool(lambda: [_to_out(call) for call in calls])
    return CallPage(items=items, total=total or 0)


@router.get("/calls/{call_id}")
async def get_call(
    call_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> CallDetail:
    call = await session.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "звонок не найден")
    job = await session.get(RecordingJob, call_id)
    details = await run_in_threadpool(_to_out, call)
    return CallDetail(
        **details.model_dump(),
        transcription_status=job.transcription_status if job is not None else None,
        transcript=job.transcript if job is not None else None,
    )


@router.get("/calls/{call_id}/audio/{kind}")
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
