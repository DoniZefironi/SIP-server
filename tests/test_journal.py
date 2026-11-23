"""Сборка журнала: последовательности событий как от станции (сценарий из asterisk/)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Call
from app.services.journal import Direction, Outcome, apply_event

LINKED = "1794907850.6"
T0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def user(name: str, **fields: str) -> dict[str, str]:
    return {"Event": "UserEvent", "UserEvent": name, "Linkedid": LINKED, **fields}


def stage(name: str, **fields: str) -> dict[str, str]:
    return user("SavtCall", Stage=name, **fields)


def hangup(uniqueid: str = LINKED) -> dict[str, str]:
    return {"Event": "Hangup", "Linkedid": LINKED, "Uniqueid": uniqueid}


def run(*events: dict[str, str]) -> Call | None:
    call = None
    for i, event in enumerate(events):
        updated = apply_event(call, event, T0 + timedelta(seconds=i * 10))
        call = updated or call
    return call


def test_answered_call() -> None:
    call = run(
        stage("start", Number="80291234567"),
        stage("sector", Sector="2"),
        user("SavtRecording", File="/srv/recordings/calls/x"),
        {"Event": "QueueCallerJoin", "Linkedid": LINKED},
        user("SavtRing", Target="PJSIP/102"),
        {"Event": "AgentConnect", "Linkedid": LINKED, "HoldTime": "12", "Interface": "Local/2"},
        {"Event": "AgentComplete", "Linkedid": LINKED, "TalkTime": "95"},
        hangup(),
    )
    assert call is not None
    assert call.caller == "+375291234567"
    assert call.caller_raw == "80291234567"
    assert call.sector == 2
    assert call.direction == Direction.INCOMING
    assert call.outcome == Outcome.ANSWERED
    assert call.answered_by == "PJSIP/102"
    assert call.wait_seconds == 12
    assert call.talk_seconds == 95
    assert call.recording == "/srv/recordings/calls/x"
    assert call.ended_at == T0 + timedelta(seconds=70)


def test_queue_full_goes_to_message() -> None:
    call = run(
        stage("start", Number="375291234567"),
        stage("sector", Sector="1"),
        stage("queue_exit", Status="FULL"),
        user("SavtMessage", File="/srv/recordings/messages/m.wav"),
        hangup(),
    )
    assert call.outcome == Outcome.MESSAGE_QUEUE_FULL
    assert call.message_file == "/srv/recordings/messages/m.wav"


def test_wait_timeout_goes_to_message() -> None:
    call = run(
        stage("start", Number="101"),
        stage("menu_default"),
        stage("sector", Sector="1"),
        {"Event": "QueueCallerJoin", "Linkedid": LINKED},
        stage("queue_exit", Status="TIMEOUT"),
        user("SavtMessage", File="m.wav"),
        hangup(),
    )
    assert call.outcome == Outcome.MESSAGE_NO_ANSWER
    assert call.sector_by_default
    assert call.caller is None  # внутренний номер — не телефон
    assert call.caller_raw == "101"


def test_after_hours_message() -> None:
    call = run(
        stage("start", Number="+375291234567"),
        stage("afterhours"),
        user("SavtMessage", File="m.wav"),
        hangup(),
    )
    assert call.outcome == Outcome.MESSAGE_AFTERHOURS
    assert call.sector is None


def test_blocked_number() -> None:
    call = run(stage("blocked", Number="375297654321"), hangup())
    assert call.outcome == Outcome.BLOCKED
    assert call.caller == "+375297654321"


def test_hung_up_in_menu() -> None:
    call = run(stage("start", Number="375291234567"), hangup())
    assert call.outcome == Outcome.ABANDONED_MENU


def test_hung_up_in_queue() -> None:
    call = run(
        stage("start", Number="375291234567"),
        stage("sector", Sector="3"),
        {"Event": "QueueCallerJoin", "Linkedid": LINKED},
        {"Event": "QueueCallerAbandon", "Linkedid": LINKED, "HoldTime": "40"},
        hangup(),
    )
    assert call.outcome == Outcome.ABANDONED_QUEUE
    assert call.wait_seconds == 40


def test_sector_phone_hangup_does_not_end_call() -> None:
    call = run(
        stage("start", Number="375291234567"),
        stage("sector", Sector="1"),
        hangup(uniqueid="1794907850.9"),  # отбой канала к телефону сектора
    )
    assert call.ended_at is None
    assert call.outcome is None


def test_events_without_start_are_ignored() -> None:
    assert apply_event(None, hangup(), T0) is None
    assert apply_event(None, stage("sector", Sector="1"), T0) is None
    assert apply_event(None, user("SavtRecording", File="x"), T0) is None


def dial_end(status: str, uniqueid: str = LINKED) -> dict[str, str]:
    return {"Event": "DialEnd", "Linkedid": LINKED, "Uniqueid": uniqueid, "DialStatus": status}


def test_outgoing_call_tracks_client_and_initiator() -> None:
    call = run(
        stage("outgoing_start", Number="+375291234567", Staff="PJSIP/104", Sector="4"),
        user("SavtRecording", File="/srv/recordings/calls/x"),
        dial_end("ANSWER", uniqueid="1794907850.9"),
        dial_end("ANSWER"),
        hangup(),
    )
    assert call is not None
    assert call.direction == Direction.OUTGOING
    assert call.caller == "+375291234567"
    assert call.initiated_by == "PJSIP/104"
    assert call.sector == 4
    assert call.outcome == Outcome.ANSWERED
    assert call.wait_seconds == 30
    assert call.talk_seconds == 10
    assert call.recording == "/srv/recordings/calls/x"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("BUSY", Outcome.OUTGOING_BUSY),
        ("NOANSWER", Outcome.OUTGOING_NO_ANSWER),
        ("CANCEL", Outcome.OUTGOING_CANCELLED),
        ("CHANUNAVAIL", Outcome.OUTGOING_FAILED),
    ],
)
def test_unanswered_outgoing_stays_outgoing(status: str, outcome: Outcome) -> None:
    call = run(stage("outgoing_start", Number="+375291234567"), dial_end(status), hangup())
    assert call is not None
    assert call.direction == Direction.OUTGOING
    assert call.outcome == outcome
    assert call.talk_seconds is None


def test_dial_to_employee_does_not_create_an_outgoing_call() -> None:
    call = run(
        stage("start", Number="+375291234567"),
        stage("sector", Sector="4"),
        dial_end("NOANSWER"),
        hangup(),
    )
    assert call is not None
    assert call.direction == Direction.INCOMING
    assert call.outcome == Outcome.ABANDONED_QUEUE
