"""Обработка событий станции: сырое событие в базу и обновление журнала звонков."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.ami.client import Message
from app.db.models import Call, CallEvent
from app.db.session import SessionLocal
from app.services.journal import apply_event

# События, из которых собирается история звонка. Остальные (регистрации телефонов,
# служебные) в базу не пишем.
STORED_EVENTS = frozenset(
    {
        "Newchannel",
        "Newstate",
        "DialBegin",
        "DialEnd",
        "BridgeEnter",
        "BridgeLeave",
        "Hangup",
        "QueueCallerJoin",
        "QueueCallerLeave",
        "QueueCallerAbandon",
        "AgentCalled",
        "AgentConnect",
        "AgentComplete",
        "AgentRingNoAnswer",
        # События сценария станции (UserEvent в extensions.conf): этап звонка, файл записи,
        # телефон сектора, файл сообщения. Имя — в поле UserEvent.
        "UserEvent",
    }
)


async def store_event(message: Message) -> None:
    if message["Event"] not in STORED_EVENTS:
        return
    now = datetime.now(UTC)
    linkedid = message.get("Linkedid")
    async with SessionLocal() as session:
        session.add(
            CallEvent(
                received_at=now,
                event=message["Event"],
                linkedid=linkedid,
                uniqueid=message.get("Uniqueid"),
                channel=message.get("Channel"),
                payload=message,
            )
        )
        if linkedid:
            call = await session.scalar(select(Call).where(Call.linkedid == linkedid))
            updated = apply_event(call, message, now)
            if updated is not None and call is None:
                session.add(updated)
        await session.commit()
