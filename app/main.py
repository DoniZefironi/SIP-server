import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI

from app.ami.astdb import AstDB
from app.ami.client import AmiClient
from app.ami.events import store_event
from app.api import calls, departments, events, health, statistics
from app.api import settings as station_api
from app.config import get_settings
from app.db.session import engine
from app.services.bitrix import Bitrix
from app.services.bitrix_delivery import run_bitrix_delivery
from app.services.department_directory import DepartmentDirectory
from app.services.department_routes import DepartmentRoutes
from app.services.maintenance import run_maintenance
from app.services.recording_queue import run_recording_queue
from app.services.summarization import run_summarization
from app.services.telephony_settings import StationSettings
from app.services.transcription import run_transcription


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app.state.ami = None
    app.state.department_routes = None
    app.state.station_settings = None
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
        app.state.department_routes = DepartmentRoutes(client)
        app.state.station_settings = StationSettings(AstDB(client))
        ami_task = asyncio.create_task(client.run(), name="ami")

    app.state.department_directory = None
    directory_http = None
    webhook = settings.bitrix_webhook_url.get_secret_value().strip()
    if settings.bitrix_directory_enabled and webhook:
        directory_http = httpx.AsyncClient(timeout=10)
        try:
            app.state.department_directory = DepartmentDirectory(Bitrix(directory_http, webhook))
        except ValueError:
            logging.getLogger(__name__).warning("Некорректный вебхук справочника Bitrix24")

    maintenance_task: asyncio.Task[None] | None = None
    if settings.maintenance_enabled:
        maintenance_task = asyncio.create_task(run_maintenance(settings), name="maintenance")

    recording_task: asyncio.Task[None] | None = None
    if settings.recording_queue_enabled:
        recording_task = asyncio.create_task(run_recording_queue(settings), name="recording-queue")

    transcription_task: asyncio.Task[None] | None = None
    if settings.speechkit_enabled:
        transcription_task = asyncio.create_task(run_transcription(settings), name="transcription")

    summary_task: asyncio.Task[None] | None = None
    if settings.yandexgpt_enabled:
        summary_task = asyncio.create_task(run_summarization(settings), name="summarization")

    bitrix_task: asyncio.Task[None] | None = None
    if settings.bitrix_enabled:
        bitrix_task = asyncio.create_task(run_bitrix_delivery(settings), name="bitrix-delivery")

    try:
        yield
    finally:
        for task in (
            ami_task,
            maintenance_task,
            recording_task,
            transcription_task,
            summary_task,
            bitrix_task,
        ):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if directory_http is not None:
            await directory_http.aclose()
        await engine.dispose()


app = FastAPI(title="Телефония SAVT", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(events.router, prefix="/api")
app.include_router(calls.router, prefix="/api")
app.include_router(statistics.router, prefix="/api")
app.include_router(departments.router, prefix="/api")
app.include_router(station_api.router, prefix="/api")
