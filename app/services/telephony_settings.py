import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ami.astdb import AstDB
from app.ami.client import AmiError
from app.services.department_routes import SECTORS

type Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FAMILY = "savt"
_SETTINGS_KEY = "telephony_settings"
_HOLIDAYS = "savt-holiday"

# Позиции совпадают с третьим аргументом sub-setting в extensions.conf.
_DEFAULTS = {
    "work_days": "mon-fri",
    "work_hours": "0800-1659",
    "menu_tries": "3",
    "menu_timeout": "6",
    "default_sector": "1",
    "queue_wait": "0",
    "ring_time": "20",
    "msg_maxlen": "120",
    "msg_silence": "5",
}
_CLOCK = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
_STATION_HOURS = re.compile(r"([01][0-9]|2[0-3])([0-5][0-9])-([01][0-9]|2[0-3])([0-5][0-9])")
_MAX_SECONDS = 2**31 - 1


class TelephonySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_days: list[Weekday] = Field(min_length=1, max_length=7)
    opens_at: str = Field(pattern=f"^{_CLOCK}$")
    closes_at: str = Field(pattern=f"^(?:{_CLOCK}|24:00)$")
    menu_attempts: int = Field(ge=0, le=_MAX_SECONDS)
    menu_timeout_seconds: int = Field(ge=0, le=_MAX_SECONDS)
    default_sector: int = Field(ge=SECTORS.start, lt=SECTORS.stop)
    queue_wait_seconds: int | None = Field(ge=1, le=_MAX_SECONDS)
    ring_time_seconds: int = Field(ge=0, le=_MAX_SECONDS)
    voicemail_max_seconds: int = Field(ge=0, le=_MAX_SECONDS)
    voicemail_silence_seconds: int = Field(ge=0, le=_MAX_SECONDS)

    @field_validator("work_days")
    @classmethod
    def order_days(cls, days: list[Weekday]) -> list[Weekday]:
        if len(set(days)) != len(days):
            raise ValueError("Рабочие дни должны быть уникальными")
        return [day for day in _WEEKDAYS if day in days]

    @field_validator("closes_at")
    @classmethod
    def end_of_day(cls, value: str) -> str:
        return "24:00" if value == "00:00" else value

    @model_validator(mode="after")
    def validate_hours(self) -> "TelephonySettings":
        if self.opens_at == self.closes_at:
            raise ValueError("Для круглосуточной работы укажите 00:00–24:00")
        return self


class StationSettings:
    def __init__(self, db: AstDB) -> None:
        self._db = db

    async def read(self) -> TelephonySettings:
        values = await self._db.list_family(_FAMILY)
        packed = values.get(_SETTINGS_KEY, "").split("|")
        settings = {
            key: values.get(key, default)
            for key, default in _DEFAULTS.items()
        }
        settings.update(
            (key, value) for key, value in zip(_DEFAULTS, packed, strict=False) if value
        )
        try:
            return _parse_settings(settings)
        except ValueError:
            raise AmiError("Asterisk вернул неверный формат общих настроек") from None

    async def save(self, settings: TelephonySettings) -> TelephonySettings:
        await self._db.put(_FAMILY, _SETTINGS_KEY, _pack_settings(settings))
        return settings

    async def holidays(self) -> list[date]:
        values = await self._db.list_family(_HOLIDAYS)
        dates = []
        for key in values:
            if re.fullmatch(r"[0-9]{8}", key):
                try:
                    dates.append(date.fromisoformat(key))
                except ValueError:
                    continue
        return sorted(dates)

    async def add_holiday(self, value: date) -> None:
        await self._db.put(_HOLIDAYS, value.isoformat().replace("-", ""), "1")

    async def delete_holiday(self, value: date) -> None:
        await self._db.delete(_HOLIDAYS, value.isoformat().replace("-", ""))


def _work_days(value: str) -> list[Weekday]:
    if value == "*":
        return list(_WEEKDAYS)
    days = set()
    for item in value.lower().split("&"):
        start, _, end = item.partition("-")
        first = _WEEKDAYS.index(start)
        last = _WEEKDAYS.index(end or start)
        days.update(_WEEKDAYS[(first + offset) % 7] for offset in range((last - first) % 7 + 1))
    return [day for day in _WEEKDAYS if day in days]


def _work_hours(value: str) -> tuple[str, str]:
    match = _STATION_HOURS.fullmatch(value)
    if match is None:
        raise ValueError("Неверный формат рабочих часов AstDB")
    start_hour, start_minute, end_hour, end_minute = match.groups()
    end = int(end_hour) * 60 + int(end_minute) + 1
    start = int(start_hour) * 60 + int(start_minute)
    if start == end % 1440:
        return "00:00", "24:00"
    return f"{start_hour}:{start_minute}", f"{end // 60:02d}:{end % 60:02d}"


def _parse_settings(values: dict[str, str]) -> TelephonySettings:
    opens_at, closes_at = _work_hours(values["work_hours"])
    return TelephonySettings(
        work_days=_work_days(values["work_days"]),
        opens_at=opens_at,
        closes_at=closes_at,
        menu_attempts=int(values["menu_tries"]),
        menu_timeout_seconds=int(values["menu_timeout"]),
        default_sector=int(values["default_sector"]),
        queue_wait_seconds=int(values["queue_wait"] or "0") or None,
        ring_time_seconds=int(values["ring_time"]),
        voicemail_max_seconds=int(values["msg_maxlen"]),
        voicemail_silence_seconds=int(values["msg_silence"]),
    )


def _pack_settings(settings: TelephonySettings) -> str:
    # В GotoIfTime конец интервала включительный. В API время закрытия исключительное.
    hour, minute = map(int, settings.closes_at.split(":"))
    end = hour * 60 + minute - 1
    hours = f"{settings.opens_at.replace(':', '')}-{end // 60:02d}{end % 60:02d}"
    return "|".join(
        (
            "&".join(settings.work_days),
            hours,
            str(settings.menu_attempts),
            str(settings.menu_timeout_seconds),
            str(settings.default_sector),
            str(settings.queue_wait_seconds or 0),
            str(settings.ring_time_seconds),
            str(settings.voicemail_max_seconds),
            str(settings.voicemail_silence_seconds),
        )
    )
