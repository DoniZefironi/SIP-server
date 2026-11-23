from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Call, RecordingJob
from app.services.journal import MISSED_OUTCOMES, Direction, Outcome
from app.services.phones import normalize_phone


class CallTab(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    MISSED = "missed"


TAB_CONDITIONS = {
    CallTab.INCOMING: (Call.direction == Direction.INCOMING) & (Call.outcome == Outcome.ANSWERED),
    CallTab.OUTGOING: Call.direction == Direction.OUTGOING,
    CallTab.MISSED: (
        (Call.direction == Direction.INCOMING)
        & Call.ended_at.is_not(None)
        & Call.outcome.in_(MISSED_OUTCOMES)
    ),
}

# Задание создаётся после проверки записанного WAV и остаётся после удаления файла.
VOICEMAIL_LEFT = (
    select(RecordingJob.call_id)
    .where(RecordingJob.call_id == Call.id, RecordingJob.audio_kind == "message")
    .exists()
)


def history_conditions(
    *,
    sector: int | None = None,
    caller: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[ColumnElement[bool]]:
    conditions = []
    if sector is not None:
        conditions.append(Call.sector == sector)
    if caller:
        conditions.append(Call.caller == (normalize_phone(caller) or caller))
    if date_from is not None:
        conditions.append(Call.started_at >= date_from)
    if date_to is not None:
        conditions.append(Call.started_at < date_to)
    return conditions


def call_count_columns() -> tuple[ColumnElement[int], ...]:
    missed = TAB_CONDITIONS[CallTab.MISSED]
    return (
        *(
            func.count().filter(condition).label(tab.value)
            for tab, condition in TAB_CONDITIONS.items()
        ),
        func.count().filter(missed, VOICEMAIL_LEFT).label("missed_with_voicemail"),
        func.count().filter(missed, ~VOICEMAIL_LEFT).label("missed_without_voicemail"),
    )
