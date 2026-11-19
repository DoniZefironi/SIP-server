import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.ami.client import AmiClient
from app.ami.events import store_event
from app.api import calls, events, health
from app.config import get_settings
from app.db.session import engine
from app.services.maintenance import run_maintenance
from app.services.recording_queue import run_recording_queue
from app.services.transcription import run_transcription


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app.state.ami = None
    ami_task: asyncio.Task[None] | None = None
    if settings.ami_enabled:
        client = AmiClient(
            settings.ami_host,
            settings.ami_port,
            settings.ami_username,
            settings.ami_secret,
            on_event=store_event,
            reconnect_delay=settings.ami_reconnect_delay,
            ping_interval=settings.ami_ping_interval,
        )
        app.state.ami = client
        ami_task = asyncio.create_task(client.run(), name="ami")

    maintenance_task: asyncio.Task[None] | None = None
    if settings.maintenance_enabled:
        maintenance_task = asyncio.create_task(run_maintenance(settings), name="maintenance")

    recording_task: asyncio.Task[None] | None = None
    if settings.recording_queue_enabled:
        recording_task = asyncio.create_task(run_recording_queue(settings), name="recording-queue")

    transcription_task: asyncio.Task[None] | None = None
    if settings.speechkit_enabled:
        transcription_task = asyncio.create_task(run_transcription(settings), name="transcription")

    try:
        yield
    finally:
        for task in (ami_task, maintenance_task, recording_task, transcription_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await engine.dispose()


app = FastAPI(title="Телефония SAVT", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(events.router, prefix="/api")
app.include_router(calls.router, prefix="/api")
