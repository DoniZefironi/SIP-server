import re
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Call, RecordingJob
from app.services.journal import MISSED_OUTCOMES, Direction, Outcome
from app.services.phones import normalize_phone


class CallTab(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    MISSED = "missed"


TAB_CONDITIONS = {
    CallTab.INCOMING: (Call.direction == Direction.INCOMING)
    & Call.outcome.in_((Outcome.ANSWERED, Outcome.BOT_ANSWERED, Outcome.EMERGENCY_REFERRED)),
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

_PHONE_INPUT = re.compile(r"\+?[0-9\s().–—-]*")
_NON_DIGITS = r"[^0-9]"


def _caller_condition(value: str) -> ColumnElement[bool] | None:
    query = value.strip()
    if not query:
        return None
    if not _PHONE_INPUT.fullmatch(query):
        return Call.caller == query

    digits = re.sub(_NON_DIGITS, "", query)
    if not digits:
        return None

    raw_digits = func.regexp_replace(Call.caller_raw, _NON_DIGITS, "", "g")
    if normalized := normalize_phone(query):
        raw_matches = [digits, normalized[1:]]
        if normalized.startswith("+375"):
            raw_matches.append(f"80{normalized[4:]}")
        return or_(Call.caller == normalized, raw_digits.in_(raw_matches))

    if query.startswith("+"):
        matches = [Call.caller.startswith(f"+{digits}"), raw_digits.startswith(digits)]
        if digits.startswith("375") and len(digits) > 3:
            matches.append(raw_digits.startswith(f"80{digits[3:]}"))
        return or_(*matches)

    matches = [Call.caller.contains(digits), raw_digits.contains(digits)]
    # Частичный белорусский номер может начинаться с междугороднего префикса 80.
    if digits.startswith("80") and len(digits) > 2:
        matches.append(Call.caller.startswith(f"+375{digits[2:]}"))
        matches.append(raw_digits.startswith(f"375{digits[2:]}"))
    return or_(*matches)


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
    if caller is not None and (match := _caller_condition(caller)) is not None:
        conditions.append(match)
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
