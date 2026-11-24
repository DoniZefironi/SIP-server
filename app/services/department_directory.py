import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.services.bitrix import Bitrix, BitrixError, positive_id

TELEPHONY_DEPARTMENT_IDS = (
    91,  # Коммерческий сектор
    69,  # Бухгалтерия
    25,  # Сектор разработки программного обеспечения
    27,  # Сектор наладки
    23,  # Сектор проектирования
)


class Employee(BaseModel):
    id: int
    name: str
    position: str | None
    photo_url: str | None


class Department(BaseModel):
    id: int
    name: str
    parent_id: int | None = None


@dataclass(slots=True)
class Directory:
    departments: dict[int, Department]
    employees: dict[int, list[Employee]]


class DepartmentDirectory:
    def __init__(self, bitrix: Bitrix, ttl: float = 60) -> None:
        self._bitrix = bitrix
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._directory: Directory | None = None
        self._expires_at = 0.0

    async def get(self) -> Directory:
        async with self._lock:
            if self._directory is not None and time.monotonic() < self._expires_at:
                return self._directory
            departments = await self._bitrix.departments(TELEPHONY_DEPARTMENT_IDS)
            users = await self._bitrix.directory_pages(
                "user.get",
                {
                    "filter": {"ACTIVE": True, "UF_DEPARTMENT": list(TELEPHONY_DEPARTMENT_IDS)},
                    "select": [
                        "ID", "NAME", "LAST_NAME", "SECOND_NAME", "WORK_POSITION",
                        "PERSONAL_PHOTO", "UF_DEPARTMENT", "USER_TYPE",
                    ],
                },
            )
            self._directory = _build_directory(departments, users)
            self._expires_at = time.monotonic() + self._ttl
            return self._directory


def _text(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    return (value.strip() or None) if isinstance(value, str) else None


def _build_directory(
    departments: list[dict[str, Any]], users: list[dict[str, Any]]
) -> Directory:
    by_id = {}
    for item in departments:
        department_id = positive_id(item.get("ID"))
        name = _text(item, "NAME")
        if department_id is None or name is None:
            raise BitrixError("Bitrix24 вернул подразделение без ID или названия")
        by_id[department_id] = Department(
            id=department_id, name=name, parent_id=positive_id(item.get("PARENT"))
        )
    for department in by_id.values():
        if department.parent_id not in by_id:
            department.parent_id = None

    employees: dict[int, dict[int, Employee]] = defaultdict(dict)
    for user in users:
        employee_id = positive_id(user.get("ID"))
        memberships = user.get("UF_DEPARTMENT")
        if employee_id is None or not isinstance(memberships, list):
            continue
        if user.get("USER_TYPE") not in (None, "employee"):
            continue
        name = " ".join(
            part for field in ("LAST_NAME", "NAME", "SECOND_NAME") if (part := _text(user, field))
        )
        employee = Employee(
            id=employee_id,
            name=name or f"Сотрудник {employee_id}",
            position=_text(user, "WORK_POSITION"),
            photo_url=_text(user, "PERSONAL_PHOTO"),
        )
        for membership in memberships:
            department_id = positive_id(membership)
            if department_id in by_id:
                employees[department_id][employee_id] = employee
    return Directory(
        departments=by_id,
        employees={
            department_id: sorted(members.values(), key=lambda employee: employee.name.casefold())
            for department_id, members in employees.items()
        },
    )
