"""Клиент AMI — текстового интерфейса Asterisk (TCP, порт 5038).

Сообщение AMI — это строки «Ключ: Значение», разделённые CRLF, с пустой строкой в конце.
Клиент только слушает события и шлёт действия; на ход звонков он не влияет, поэтому его
падение или отсутствие связи звонки не затрагивает.
"""

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress

log = logging.getLogger(__name__)

Message = dict[str, str]
EventHandler = Callable[[Message], Awaitable[None]]

_READ_LIMIT = 1024 * 1024
_EVENT_QUEUE_SIZE = 10_000


class AmiError(Exception):
    pass


def parse_message(raw: bytes) -> Message:
    """Разобрать одно сообщение. При повторе ключа остаётся последнее значение."""
    message: Message = {}
    for line in raw.decode("utf-8", errors="replace").split("\r\n"):
        key, sep, value = line.partition(":")
        if sep:
            message[key.strip()] = value.strip()
    return message


def encode_action(fields: Mapping[str, str]) -> bytes:
    lines = []
    for key, value in fields.items():
        if "\r" in f"{key}{value}" or "\n" in f"{key}{value}":
            # Перевод строки в значении позволил бы подсунуть станции чужое действие.
            raise ValueError(f"перевод строки в поле AMI {key!r}")
        lines.append(f"{key}: {value}\r\n")
    return "".join(lines).encode() + b"\r\n"


class AmiClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        secret: str,
        on_event: EventHandler,
        *,
        reconnect_delay: float = 5.0,
        ping_interval: float = 20.0,
        action_timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._secret = secret
        self._on_event = on_event
        self._reconnect_delay = reconnect_delay
        self._ping_interval = ping_interval
        self._action_timeout = action_timeout

        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[Message]] = {}
        self._lists: dict[str, tuple[list[Message], asyncio.Future[None]]] = {}
        self._action_ids = itertools.count(1)
        self._events: asyncio.Queue[Message] = asyncio.Queue(maxsize=_EVENT_QUEUE_SIZE)
        self.connected = False

    async def run(self) -> None:
        """Держать соединение, пока задачу не отменят: при обрыве подключаться заново."""
        dispatcher = asyncio.create_task(self._dispatch(), name="ami-dispatch")
        try:
            while True:
                try:
                    await self._session()
                except Exception as exc:  # noqa: BLE001 — любая ошибка сеанса ведёт к переподключению
                    log.warning("AMI: соединение потеряно (%s: %s)", type(exc).__name__, exc)
                await asyncio.sleep(self._reconnect_delay)
        finally:
            dispatcher.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher

    async def send_action(self, fields: Mapping[str, str]) -> Message:
        """Отправить действие и дождаться ответа станции."""
        return await self._send(fields)

    async def list_action(self, fields: Mapping[str, str]) -> tuple[Message, list[Message]]:
        """Получить ответ и все события списка, относящиеся к его ActionID."""
        events: list[Message] = []
        return await self._send(fields, events), events

    async def _send(
        self, fields: Mapping[str, str], events: list[Message] | None = None
    ) -> Message:
        if self._writer is None:
            raise AmiError("нет соединения с AMI")
        action_id = str(next(self._action_ids))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Message] = loop.create_future()
        self._pending[action_id] = future
        finished: asyncio.Future[None] | None = None
        if events is not None:
            finished = loop.create_future()
            self._lists[action_id] = (events, finished)
        try:
            async with asyncio.timeout(self._action_timeout):
                self._writer.write(encode_action({**fields, "ActionID": action_id}))
                await self._writer.drain()
                reply = await future
                if finished is not None and reply.get("Response") == "Success":
                    await finished
                return reply
        finally:
            self._pending.pop(action_id, None)
            self._lists.pop(action_id, None)
            # Чтение могло завершиться ошибкой, пока запись в сокет ещё шла.
            for pending in (future, finished):
                if pending is not None and pending.done() and not pending.cancelled():
                    pending.exception()

    async def _session(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port, limit=_READ_LIMIT),
            self._action_timeout,
        )
        self._writer = writer
        read_task: asyncio.Task[None] | None = None
        try:
            banner = await asyncio.wait_for(reader.readline(), self._action_timeout)
            if not banner.startswith(b"Asterisk Call Manager"):
                raise AmiError(f"неожиданное приветствие: {banner[:60]!r}")

            read_task = asyncio.create_task(self._read_loop(reader), name="ami-read")
            reply = await self.send_action(
                {"Action": "Login", "Username": self._username, "Secret": self._secret}
            )
            if reply.get("Response") != "Success":
                raise AmiError(f"вход отклонён: {reply.get('Message', 'без пояснения')}")

            self.connected = True
            log.info("AMI: подключено к %s:%s", self._host, self._port)

            while True:
                done, _ = await asyncio.wait({read_task}, timeout=self._ping_interval)
                if done:
                    read_task.result()
                    raise AmiError("станция закрыла соединение")
                await self.send_action({"Action": "Ping"})
        finally:
            self.connected = False
            self._writer = None
            if read_task is not None:
                read_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await read_task
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                message = parse_message(await reader.readuntil(b"\r\n\r\n"))
                action_id = message.get("ActionID", "")
                if "Response" in message:
                    future = self._pending.get(action_id)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif "Event" in message:
                    result = self._lists.get(action_id)
                    if result is None:
                        await self._events.put(message)
                    else:
                        events, finished = result
                        if message.get("EventList", "").lower() == "complete":
                            if not finished.done():
                                finished.set_result(None)
                        else:
                            events.append(message)
        finally:
            # Чтение остановилось — ответов уже не будет, не заставляем ждать таймаута.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AmiError("соединение с AMI прервано"))
            for action_id, (_, finished) in self._lists.items():
                response = self._pending[action_id]
                if (
                    not finished.done()
                    and response.done()
                    and not response.cancelled()
                    and response.exception() is None
                    and response.result().get("Response") == "Success"
                ):
                    finished.set_exception(AmiError("соединение с AMI прервано"))

    async def _dispatch(self) -> None:
        while True:
            message = await self._events.get()
            try:
                await self._on_event(message)
            except Exception:
                log.exception("AMI: ошибка обработки события %s", message.get("Event"))
