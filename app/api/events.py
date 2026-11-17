from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CallEvent
from app.db.session import get_session

router = APIRouter(tags=["events"])


class CallEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    received_at: datetime
    event: str
    linkedid: str | None
    uniqueid: str | None
    channel: str | None
    payload: dict[str, str]


@router.get("/events")
async def list_events(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    linkedid: str | None = None,
) -> list[CallEventOut]:
    """Последние события станции, новые сверху. Отладочный просмотр до появления журнала."""
    query = select(CallEvent).order_by(CallEvent.id.desc()).limit(limit)
    if linkedid is not None:
        query = query.where(CallEvent.linkedid == linkedid)
    rows = await session.scalars(query)
    return [CallEventOut.model_validate(row) for row in rows]
