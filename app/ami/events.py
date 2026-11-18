"""Сохранение событий станции в базу — сырьё для будущего журнала звонков."""

from app.ami.client import Message
from app.db.models import CallEvent
from app.db.session import SessionLocal

# События, из которых потом собирается история звонка. Остальные (регистрации телефонов,
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
        # файл сообщения. Имя — в поле UserEvent: SavtCall, SavtRecording, SavtMessage.
        "UserEvent",
    }
)


async def store_event(message: Message) -> None:
    if message["Event"] not in STORED_EVENTS:
        return
    async with SessionLocal() as session:
        session.add(
            CallEvent(
                event=message["Event"],
                linkedid=message.get("Linkedid"),
                uniqueid=message.get("Uniqueid"),
                channel=message.get("Channel"),
                payload=message,
            )
        )
        await session.commit()
