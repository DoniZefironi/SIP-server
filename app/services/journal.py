"""Сборка журнала звонков из событий станции.

Звонок — это все каналы с общим Linkedid. Строку журнала создаёт событие сценария
SavtCall (Stage: start или blocked), остальные события её дополняют. Звонки без этих событий
(внутренние, с номера на номер) в журнал не попадают.
"""

from datetime import datetime
from enum import StrEnum

from app.ami.client import Message
from app.db.models import Call
from app.services.phones import normalize_phone


class Outcome(StrEnum):
    ANSWERED = "answered"
    MESSAGE_AFTERHOURS = "message_afterhours"
    MESSAGE_QUEUE_FULL = "message_queue_full"
    MESSAGE_NO_ANSWER = "message_no_answer"
    ABANDONED_MENU = "abandoned_menu"
    ABANDONED_QUEUE = "abandoned_queue"
    BLOCKED = "blocked"


_MESSAGE_OUTCOMES = {
    "afterhours": Outcome.MESSAGE_AFTERHOURS,
    "queue_full": Outcome.MESSAGE_QUEUE_FULL,
    "no_answer": Outcome.MESSAGE_NO_ANSWER,
}


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _new_call(message: Message, now: datetime) -> Call:
    number = message.get("Number")
    return Call(
        linkedid=message["Linkedid"],
        started_at=now,
        caller_raw=number,
        caller=normalize_phone(number),
        sector_by_default=False,
    )


def apply_event(call: Call | None, message: Message, now: datetime) -> Call | None:
    """Учесть событие в звонке. Возвращает звонок, который нужно сохранить, или None."""
    event = message.get("Event")

    if event == "UserEvent" and message.get("UserEvent") == "SavtCall":
        stage = message.get("Stage")
        if call is None and stage in ("start", "blocked"):
            call = _new_call(message, now)
        if call is None:
            return None
        match stage:
            case "blocked":
                call.outcome = Outcome.BLOCKED
                call.ended_at = now
            case "afterhours":
                call.message_reason = "afterhours"
            case "menu_default":
                call.sector_by_default = True
            case "sector":
                call.sector = _int(message.get("Sector"))
            case "queue_exit":
                full = message.get("Status") == "FULL"
                call.message_reason = "queue_full" if full else "no_answer"
        return call

    if call is None:
        return None

    match event:
        case "UserEvent":
            match message.get("UserEvent"):
                case "SavtRecording":
                    call.recording = message.get("File")
                case "SavtRing":
                    call.ring_target = message.get("Target")
                case "SavtMessage":
                    call.message_file = message.get("File")
                    call.outcome = _MESSAGE_OUTCOMES.get(
                        call.message_reason or "no_answer", Outcome.MESSAGE_NO_ANSWER
                    )
                case _:
                    return None
        case "QueueCallerJoin":
            call.queue_joined_at = now
        case "AgentConnect":
            call.answered_at = now
            call.answered_by = call.ring_target or message.get("Interface")
            call.wait_seconds = _int(message.get("HoldTime"))
            call.outcome = Outcome.ANSWERED
        case "AgentComplete":
            call.talk_seconds = _int(message.get("TalkTime"))
        case "QueueCallerAbandon":
            call.wait_seconds = _int(message.get("HoldTime"))
            call.outcome = call.outcome or Outcome.ABANDONED_QUEUE
        case "Hangup":
            # Звонок кончился, когда положен первый канал (у него Uniqueid совпадает
            # с Linkedid). Отбои каналов к телефонам сектора звонок не завершают.
            if message.get("Uniqueid") != call.linkedid:
                return None
            call.ended_at = now
            if call.outcome is None:
                if call.answered_at is not None:
                    call.outcome = Outcome.ANSWERED
                elif call.sector is None:
                    call.outcome = Outcome.ABANDONED_MENU
                else:
                    call.outcome = Outcome.ABANDONED_QUEUE
            if call.outcome == Outcome.ANSWERED and call.talk_seconds is None:
                if call.answered_at is not None:
                    call.talk_seconds = int((now - call.answered_at).total_seconds())
        case _:
            return None
    return call
