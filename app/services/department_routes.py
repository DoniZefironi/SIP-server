import asyncio
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ami.astdb import AstDB
from app.ami.client import AmiClient, AmiError
from app.db.models import Call
from app.services.bitrix import positive_id
from app.services.phones import normalize_phone

SECTORS = range(1, 6)


class BusyMode(StrEnum):
    QUEUE = "queue"
    MESSAGE = "message"


class DepartmentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sector: int = Field(ge=SECTORS.start, lt=SECTORS.stop)
    primary_number: str = Field(max_length=64)
    additional_numbers: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=9
    )
    busy_mode: BusyMode = BusyMode.QUEUE
    queue_wait_seconds: int | None = Field(default=None, ge=1, le=3600)

    @field_validator("primary_number")
    @classmethod
    def validate_number(cls, value: str) -> str:
        phone = normalize_phone(value)
        if phone is None:
            raise ValueError("Введите телефон с кодом города или оператора")
        return phone

    @field_validator("additional_numbers")
    @classmethod
    def validate_additional_numbers(cls, values: list[str]) -> list[str]:
        return [cls.validate_number(value) for value in values]

    @model_validator(mode="after")
    def unique_numbers(self) -> "DepartmentSettings":
        numbers = [self.primary_number, *self.additional_numbers]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Номера отдела должны быть разными")
        return self


class SectorOccupied(Exception):
    pass


class DepartmentRoutes:
    def __init__(self, ami: AmiClient) -> None:
        self._ami = ami
        self._db = AstDB(ami)
        self._lock = asyncio.Lock()

    async def read(self) -> dict[int, DepartmentSettings]:
        value = await self._db.get("savt", "department_routes")
        return {} if value is None else _parse_routes(value)

    async def save(self, department_id: int, settings: DepartmentSettings | None) -> None:
        async with self._lock:
            routes = await self.read()
            if settings is None:
                if routes.pop(department_id, None) is None:
                    return
            else:
                if any(
                    existing.sector == settings.sector and owner != department_id
                    for owner, existing in routes.items()
                ):
                    raise SectorOccupied
                routes[department_id] = settings
            await self._db.put("savt", "department_routes", _format_routes(routes))

    async def active_calls(self, session: AsyncSession) -> dict[int, int]:
        self._require_connection()
        reply, channels = await self._ami.list_action({"Action": "CoreShowChannels"})
        if reply.get("Response") != "Success":
            raise AmiError("AMI отклонил запрос активных каналов")
        active_linkedids = {
            linkedid for channel in channels
            if channel.get("Event") == "CoreShowChannel"
            and (linkedid := channel.get("Linkedid"))
            and channel.get("BridgeId", "") not in ("", "<none>")
        }
        if not active_linkedids:
            return {}
        result = await session.execute(
            select(Call.sector, func.count())
            .where(
                Call.linkedid.in_(active_linkedids),
                Call.sector.in_(SECTORS),
                Call.answered_at.is_not(None),
                Call.ended_at.is_(None),
            )
            .group_by(Call.sector)
        )
        return {sector: count for sector, count in result.all()}

    def _require_connection(self) -> None:
        if not self._ami.connected:
            raise AmiError("Нет соединения с AMI")


def _format_routes(routes: dict[int, DepartmentSettings]) -> str:
    slots = dict.fromkeys(SECTORS, "-")
    for department_id, settings in routes.items():
        # Asterisk читает поля 2–4. Порядок полей совпадает с CUT в extensions.conf.
        slots[settings.sector] = "^".join(
            (
                str(department_id),
                settings.primary_number,
                settings.busy_mode.value,
                str(settings.queue_wait_seconds or 0),
                "~".join(settings.additional_numbers),
            )
        )
    return "|".join(slots.values())


def _parse_routes(value: str) -> dict[int, DepartmentSettings]:
    slots = value.split("|")
    if len(slots) != len(SECTORS):
        raise AmiError("Неверный формат маршрутов отделов в AstDB")
    routes = {}
    for sector, slot in zip(SECTORS, slots, strict=True):
        if slot == "-":
            continue
        try:
            raw_id, primary, mode, wait, additional = slot.split("^")
            department_id = positive_id(raw_id)
            if department_id is None or department_id in routes:
                raise ValueError
            routes[department_id] = DepartmentSettings(
                sector=sector,
                primary_number=primary,
                busy_mode=BusyMode(mode),
                queue_wait_seconds=int(wait) or None,
                additional_numbers=additional.split("~") if additional else [],
            )
        except ValueError:
            raise AmiError("Неверный формат маршрутов отделов в AstDB") from None
    return routes
