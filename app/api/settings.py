from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.ami.client import AmiError
from app.services.telephony_settings import StationSettings, TelephonySettings

router = APIRouter(prefix="/settings", tags=["settings"])


class Holidays(BaseModel):
    dates: list[date]


async def _station(request: Request) -> AsyncIterator[StationSettings]:
    station: StationSettings | None = request.app.state.station_settings
    if station is None:
        raise HTTPException(503, "Связь с Asterisk недоступна")
    try:
        yield station
    except (AmiError, TimeoutError, OSError):
        raise HTTPException(503, "Asterisk не подтвердил операцию с настройками") from None


Station = Annotated[StationSettings, Depends(_station)]


@router.get("/telephony")
async def read_settings(station: Station) -> TelephonySettings:
    """Действующие общие настройки входящих звонков в часовом поясе Europe/Minsk."""
    return await station.read()


@router.put("/telephony")
async def save_settings(settings: TelephonySettings, station: Station) -> TelephonySettings:
    """Применить общие настройки одним изменением AstDB."""
    return await station.save(settings)


@router.get("/holidays")
async def holidays(station: Station) -> Holidays:
    """Даты, в которые входящие идут по сценарию нерабочего времени."""
    return Holidays(dates=await station.holidays())


@router.put("/holidays/{day}", status_code=204)
async def add_holiday(day: date, station: Station) -> None:
    await station.add_holiday(day)


@router.delete("/holidays/{day}", status_code=204)
async def delete_holiday(day: date, station: Station) -> None:
    await station.delete_holiday(day)
