import logging
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ami.client import AmiError
from app.api.auth import manager
from app.db.session import get_session
from app.services.bitrix import BitrixError
from app.services.department_directory import (
    TELEPHONY_DEPARTMENT_IDS,
    Department,
    DepartmentDirectory,
    Directory,
    Employee,
)
from app.services.department_routes import (
    SECTORS,
    DepartmentRoutes,
    DepartmentSettings,
    SectorOccupied,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/departments", tags=["departments"])
DepartmentId = Annotated[int, Path(gt=0, lt=2**63)]
Session = Annotated[AsyncSession, Depends(get_session)]
_STATION_ERRORS = (AmiError, TimeoutError, OSError)


class CallState(StrEnum):
    IN_CALL = "in_call"
    IDLE = "idle"
    UNKNOWN = "unknown"
    UNCONFIGURED = "unconfigured"


class DepartmentItem(Department):
    directory_present: bool
    employee_count: int
    settings: DepartmentSettings | None
    call_status: CallState
    active_calls: int | None


class DepartmentDetail(DepartmentItem):
    employees: list[Employee]
    settings_available: bool


class Departments(BaseModel):
    items: list[DepartmentItem]
    sectors: list[int]
    settings_available: bool


async def _directory(request: Request) -> Directory:
    service: DepartmentDirectory | None = request.app.state.department_directory
    if service is None:
        raise HTTPException(503, "Для справочника отделов нужен вебхук Bitrix24")
    try:
        return await service.get()
    except BitrixError as exc:
        raise HTTPException(502, str(exc)) from None


def _routes(request: Request) -> DepartmentRoutes | None:
    return request.app.state.department_routes


async def _routing_state(
    routes: DepartmentRoutes | None, session: AsyncSession
) -> tuple[dict[int, DepartmentSettings] | None, dict[int, int] | None]:
    if routes is None:
        return None, None
    try:
        settings = {
            department_id: route
            for department_id, route in (await routes.read()).items()
            if department_id in TELEPHONY_DEPARTMENT_IDS
        }
    except _STATION_ERRORS as exc:
        log.warning("Маршруты отделов недоступны (%s)", type(exc).__name__)
        return None, None
    try:
        return settings, await routes.active_calls(session) if settings else {}
    except (*_STATION_ERRORS, SQLAlchemyError) as exc:
        log.warning("Статус звонков по отделам недоступен (%s)", type(exc).__name__)
        return settings, None


def _item(
    department_id: int,
    directory: Directory,
    settings: dict[int, DepartmentSettings] | None,
    active: dict[int, int] | None,
) -> DepartmentItem:
    department = directory.departments.get(department_id)
    route = settings.get(department_id) if settings is not None else None
    count = None
    status = CallState.UNKNOWN
    if route is None and settings is not None:
        status = CallState.UNCONFIGURED
    if route is not None and active is not None:
        count = active.get(route.sector, 0)
        status = CallState.IN_CALL if count else CallState.IDLE
    return DepartmentItem(
        id=department_id,
        name=department.name if department is not None else f"Отдел {department_id}",
        parent_id=department.parent_id if department is not None else None,
        directory_present=department is not None,
        employee_count=len(directory.employees.get(department_id, [])),
        settings=route,
        call_status=status,
        active_calls=count,
    )


@router.get("")
async def departments(request: Request, session: Session) -> Departments:
    directory = await _directory(request)
    settings, active = await _routing_state(_routes(request), session)
    ids = dict.fromkeys(directory.departments)
    ids.update(dict.fromkeys(settings or {}))
    return Departments(
        items=[_item(department_id, directory, settings, active) for department_id in ids],
        sectors=list(SECTORS),
        settings_available=settings is not None,
    )


@router.get("/{department_id}", dependencies=[Depends(manager)])
async def department(
    department_id: DepartmentId, request: Request, session: Session
) -> DepartmentDetail:
    if department_id not in TELEPHONY_DEPARTMENT_IDS:
        raise HTTPException(404, "Отдел не найден")
    directory = await _directory(request)
    settings, active = await _routing_state(_routes(request), session)
    if department_id not in directory.departments and department_id not in (settings or {}):
        raise HTTPException(404, "Отдел не найден")
    item = _item(department_id, directory, settings, active)
    return DepartmentDetail(
        **item.model_dump(),
        employees=directory.employees.get(department_id, []),
        settings_available=settings is not None,
    )


async def _save(
    request: Request, department_id: int, settings: DepartmentSettings | None
) -> None:
    routes = _routes(request)
    if routes is None:
        raise HTTPException(503, "Связь с Asterisk недоступна")
    try:
        await routes.save(department_id, settings)
    except SectorOccupied:
        raise HTTPException(409, "Сектор уже привязан к другому отделу") from None
    except _STATION_ERRORS:
        raise HTTPException(503, "Asterisk не подтвердил сохранение настроек") from None


@router.put("/{department_id}/settings", dependencies=[Depends(manager)])
async def save_settings(
    department_id: DepartmentId, settings: DepartmentSettings, request: Request
) -> DepartmentSettings:
    if department_id not in TELEPHONY_DEPARTMENT_IDS or department_id not in (
        await _directory(request)
    ).departments:
        raise HTTPException(404, "Отдел не найден в Bitrix24")
    await _save(request, department_id, settings)
    return settings


@router.delete("/{department_id}/settings", status_code=204, dependencies=[Depends(manager)])
async def delete_settings(department_id: DepartmentId, request: Request) -> None:
    await _save(request, department_id, None)
