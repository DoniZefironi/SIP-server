from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, computed_field
from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.api.calls import CallCounts
from app.db.models import Call
from app.db.session import get_session
from app.services.call_history import (
    TAB_CONDITIONS,
    CallTab,
    call_count_columns,
    history_conditions,
)

router = APIRouter(prefix="/statistics", tags=["statistics"])
_TIMEZONE = "Europe/Minsk"


class GroupBy(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class CallVolume(CallCounts):
    @computed_field
    @property
    def total(self) -> int:
        return self.incoming + self.outgoing + self.missed


class CallBucket(CallVolume):
    bucket_start: date


class CallVolumeReport(BaseModel):
    group_by: GroupBy
    timezone: str
    totals: CallVolume
    buckets: list[CallBucket]


class CallerStats(BaseModel):
    caller: str
    calls: int
    missed: int
    last_called_at: datetime


class CallerStatsPage(BaseModel):
    items: list[CallerStats]
    total: int


def _report_conditions(
    date_from: AwareDatetime,
    date_to: AwareDatetime,
    sector: Annotated[int | None, Query(ge=1, le=9)] = None,
) -> list[ColumnElement[bool]]:
    if date_from >= date_to:
        raise HTTPException(422, "Начало периода должно предшествовать концу")
    return history_conditions(sector=sector, date_from=date_from, date_to=date_to)


@router.get("/calls")
async def call_volume(
    session: Annotated[AsyncSession, Depends(get_session)],
    conditions: Annotated[list[ColumnElement[bool]], Depends(_report_conditions)],
    group_by: GroupBy = GroupBy.DAY,
) -> CallVolumeReport:
    """Динамика звонков по календарю Минска и итоги за выбранный период."""
    local_time = func.timezone(_TIMEZONE, Call.started_at)
    bucket = cast(func.date_trunc(group_by.value, local_time), Date).label("bucket_start")
    result = await session.execute(
        select(bucket, *call_count_columns())
        .select_from(Call)
        .where(*conditions, or_(*TAB_CONDITIONS.values()))
        .group_by("bucket_start")
        .order_by("bucket_start")
    )
    buckets = [CallBucket.model_validate(row) for row in result.mappings()]
    totals = CallVolume.model_validate(
        {field: sum(getattr(item, field) for item in buckets) for field in CallCounts.model_fields}
    )
    return CallVolumeReport(group_by=group_by, timezone=_TIMEZONE, totals=totals, buckets=buckets)


@router.get("/callers")
async def caller_statistics(
    session: Annotated[AsyncSession, Depends(get_session)],
    conditions: Annotated[list[ColumnElement[bool]], Depends(_report_conditions)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CallerStatsPage:
    """Номера клиентов по количеству входящих, включая пропущенные."""
    missed = TAB_CONDITIONS[CallTab.MISSED]
    conditions.extend(
        (Call.caller.is_not(None), or_(TAB_CONDITIONS[CallTab.INCOMING], missed))
    )
    calls = func.count().label("calls")
    last_called_at = func.max(Call.started_at).label("last_called_at")
    total = await session.scalar(
        select(func.count(func.distinct(Call.caller))).where(*conditions)
    )
    result = await session.execute(
        select(Call.caller, calls, func.count().filter(missed).label("missed"), last_called_at)
        .where(*conditions)
        .group_by(Call.caller)
        .order_by(calls.desc(), last_called_at.desc(), Call.caller)
        .limit(limit)
        .offset(offset)
    )
    return CallerStatsPage(
        items=[CallerStats.model_validate(row) for row in result.mappings()], total=total or 0
    )
