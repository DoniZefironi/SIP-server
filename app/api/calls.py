from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Call
from app.db.session import get_session
from app.services.journal import Outcome
from app.services.phones import normalize_phone

router = APIRouter(tags=["calls"])

AudioKind = Literal["mix", "client", "staff", "message"]
_SUFFIXES = {"mix": ".wav", "client": "-client.wav", "staff": "-staff.wav"}


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


class CallPage(BaseModel):
    items: list[CallOut]
    total: int


def _audio_path(call: Call, kind: AudioKind) -> Path | None:
    if kind == "message":
        raw = call.message_file
    else:
        raw = call.recording + _SUFFIXES[kind] if call.recording else None
    if not raw:
        return None
    root = get_settings().recordings_dir.resolve()
    path = Path(raw).resolve()
    # Путь пришёл от станции; отдаём только файлы из папки записей.
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _to_out(call: Call) -> CallOut:
    return CallOut.model_validate(
        {
            **{f: getattr(call, f) for f in CallOut.model_fields if hasattr(call, f)},
            "has_recording": _audio_path(call, "mix") is not None,
            "has_message": _audio_path(call, "message") is not None,
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
    return CallPage(items=[_to_out(row) for row in rows], total=total or 0)


@router.get("/calls/{call_id}")
async def get_call(call_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> CallOut:
    call = await session.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "звонок не найден")
    return _to_out(call)


@router.get("/calls/{call_id}/audio/{kind}")
async def get_audio(
    call_id: int, kind: AudioKind, session: Annotated[AsyncSession, Depends(get_session)]
) -> FileResponse:
    """Запись: mix — общая, client — клиент, staff — сотрудник, message — сообщение."""
    call = await session.get(Call, call_id)
    path = _audio_path(call, kind) if call is not None else None
    if path is None:
        raise HTTPException(404, "записи нет")
    return FileResponse(path, media_type="audio/wav")
