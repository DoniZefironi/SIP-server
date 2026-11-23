"""Клиент AMI проверяется на поддельной станции: настоящий Asterisk для тестов не нужен."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import pytest

from app.ami.client import AmiClient, Message, encode_action, parse_message

Script = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def read_action(reader: asyncio.StreamReader) -> Message:
    return parse_message(await reader.readuntil(b"\r\n\r\n"))


def reply(action: Message, **fields: str) -> bytes:
    return encode_action({**fields, "ActionID": action["ActionID"]})


@asynccontextmanager
async def fake_asterisk(script: Script) -> AsyncIterator[int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"Asterisk Call Manager/9.0.0\r\n")
        with suppress(asyncio.IncompleteReadError, ConnectionError):
            await script(reader, writer)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


@asynccontextmanager
async def running_client(port: int, events: list[Message], secret: str = "right"):
    async def on_event(message: Message) -> None:
        events.append(message)

    client = AmiClient(
        "127.0.0.1",
        port,
        "telephony",
        secret,
        on_event,
        reconnect_delay=0.05,
        ping_interval=0.1,
        action_timeout=1.0,
    )
    task = asyncio.create_task(client.run())
    try:
        yield client
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def accept_login(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    login = await read_action(reader)
    assert login["Action"] == "Login"
    ok = login["Secret"] == "right"
    writer.write(reply(login, Response="Success" if ok else "Error", Message="Auth"))
    await writer.drain()


async def answer_pings(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        action = await read_action(reader)
        writer.write(reply(action, Response="Success", Ping="Pong"))
        await writer.drain()


def test_parse_message() -> None:
    raw = b"Event: Hangup\r\nChannel: PJSIP/101-00000001\r\nCause-txt: Normal: clearing\r\n\r\n"
    assert parse_message(raw) == {
        "Event": "Hangup",
        "Channel": "PJSIP/101-00000001",
        "Cause-txt": "Normal: clearing",
    }


def test_encode_action_rejects_line_breaks() -> None:
    with pytest.raises(ValueError):
        encode_action({"Action": "DBPut", "Val": "1\r\nAction: Command"})


async def test_login_and_event_delivery() -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await accept_login(reader, writer)
        writer.write(encode_action({"Event": "Newchannel", "Linkedid": "1.1"}))
        await writer.drain()
        await answer_pings(reader, writer)

    events: list[Message] = []
    async with fake_asterisk(script) as port, running_client(port, events) as client:
        await wait_until(lambda: len(events) == 1)
        assert client.connected
        assert events[0]["Event"] == "Newchannel"
        assert events[0]["Linkedid"] == "1.1"

        # Соединение переживает несколько циклов проверки связи.
        await asyncio.sleep(0.35)
        assert client.connected


async def test_list_action_collects_only_matching_events() -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await accept_login(reader, writer)
        request = await read_action(reader)
        writer.write(reply(request, Response="Success", EventList="start"))
        writer.write(reply(request, Event="DBGetResponse", Val="routes"))
        writer.write(encode_action({"Event": "Hangup", "Linkedid": "1.1"}))
        writer.write(reply(request, Event="DBGetComplete", EventList="Complete"))
        await answer_pings(reader, writer)

    events: list[Message] = []
    async with fake_asterisk(script) as port, running_client(port, events) as client:
        await wait_until(lambda: client.connected)
        response, entries = await client.list_action({"Action": "DBGet"})
        assert response["Response"] == "Success"
        assert [(entry["Event"], entry["Val"]) for entry in entries] == [
            ("DBGetResponse", "routes")
        ]
        await wait_until(lambda: len(events) == 1)
        assert events[0]["Event"] == "Hangup"


async def test_wrong_secret_never_connects() -> None:
    logins = 0

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal logins
        await accept_login(reader, writer)
        logins += 1

    events: list[Message] = []
    async with fake_asterisk(script) as port, running_client(port, events, "wrong") as client:
        await wait_until(lambda: logins >= 2)
        assert not client.connected


async def test_reconnects_after_drop() -> None:
    sessions = 0

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal sessions
        await accept_login(reader, writer)
        sessions += 1
        if sessions == 1:
            return  # станция оборвала первое соединение
        writer.write(encode_action({"Event": "Hangup", "Linkedid": "2.1"}))
        await writer.drain()
        await answer_pings(reader, writer)

    events: list[Message] = []
    async with fake_asterisk(script) as port, running_client(port, events) as client:
        await wait_until(lambda: len(events) == 1)
        assert sessions == 2
        assert client.connected


async def test_silent_station_is_detected() -> None:
    """Станция приняла вход и замолчала: клиент должен заметить и переподключиться."""
    sessions = 0

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal sessions
        await accept_login(reader, writer)
        sessions += 1
        await asyncio.sleep(30)

    events: list[Message] = []
    async with fake_asterisk(script) as port, running_client(port, events):
        await wait_until(lambda: sessions >= 2, timeout=5.0)


async def test_handler_error_does_not_stop_events() -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await accept_login(reader, writer)
        for name in ("Broken", "Hangup"):
            writer.write(encode_action({"Event": name}))
        await writer.drain()
        await answer_pings(reader, writer)

    seen: list[str] = []

    async def on_event(message: Message) -> None:
        if message["Event"] == "Broken":
            raise RuntimeError("сбой обработчика")
        seen.append(message["Event"])

    async with fake_asterisk(script) as port:
        client = AmiClient("127.0.0.1", port, "telephony", "right", on_event, ping_interval=0.1)
        task = asyncio.create_task(client.run())
        try:
            await wait_until(lambda: seen == ["Hangup"])
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
