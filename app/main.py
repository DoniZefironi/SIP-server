import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse

from app.ami.astdb import AstDB
from app.ami.client import AmiClient
from app.ami.events import store_event
from app.api import auth, call_objects, calls, contacts, departments, events, health, statistics
from app.api import settings as station_api
from app.config import get_settings
from app.db.session import engine
from app.services.bitrix import Bitrix
from app.services.bitrix_contacts import ContactRouting, run_contact_import
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
    app.state.contact_routing = None
    ami_task: asyncio.Task[None] | None = None
    contact_routing_task: asyncio.Task[None] | None = None
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
        app.state.contact_routing = ContactRouting(AstDB(client))
        contact_routing_task = asyncio.create_task(
            app.state.contact_routing.run(), name="contact-routing",
        )

    app.state.department_directory = None
    app.state.bitrix = None
    bitrix_http = None
    contact_import_task: asyncio.Task[None] | None = None
    webhook = settings.bitrix_webhook_url.get_secret_value().strip()
    if webhook:
        bitrix_http = httpx.AsyncClient(timeout=10)
        try:
            app.state.bitrix = Bitrix(bitrix_http, webhook)
            if settings.bitrix_directory_enabled:
                app.state.department_directory = DepartmentDirectory(app.state.bitrix)
            if settings.bitrix_contacts_enabled:
                contact_import_task = asyncio.create_task(
                    run_contact_import(app.state.bitrix), name="bitrix-contacts",
                )
        except ValueError:
            logging.getLogger(__name__).warning("Некорректный вебхук Bitrix24")

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

    app.state.voice_http = httpx.AsyncClient(
        timeout=5, limits=httpx.Limits(keepalive_expiry=60)
    )
    try:
        yield
    finally:
        for task in (
            contact_import_task,
            contact_routing_task,
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
        if bitrix_http is not None:
            await bitrix_http.aclose()
        await app.state.voice_http.aclose()
        del app.state.voice_http
        await engine.dispose()


app = FastAPI(
    title="Телефония SAVT", version="0.1.0", lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.include_router(health.router)
app.include_router(auth.router, prefix="/api/auth")
app.include_router(events.router, prefix="/api", dependencies=[Depends(auth.admin)])
app.include_router(calls.router, prefix="/api", dependencies=[Depends(auth.principal)])
app.include_router(contacts.router, prefix="/api", dependencies=[Depends(auth.manager)])
app.include_router(statistics.router, prefix="/api", dependencies=[Depends(auth.principal)])
app.include_router(departments.router, prefix="/api", dependencies=[Depends(auth.principal)])
app.include_router(station_api.router, prefix="/api", dependencies=[Depends(auth.manager)])
app.include_router(call_objects.router)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/", "/internal/")) or request.url.path in {
        "/docs", "/redoc", "/openapi.json",
    }:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/auth/"):
        return JSONResponse({"detail": "Некорректные поля запроса"}, status_code=422)
    return await request_validation_exception_handler(request, exc)


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(auth.admin)])
async def openapi():
    return app.openapi()


@app.get("/docs", include_in_schema=False, dependencies=[Depends(auth.admin)])
async def docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title)


@app.get("/redoc", include_in_schema=False, dependencies=[Depends(auth.admin)])
async def redoc():
    return get_redoc_html(openapi_url="/openapi.json", title=app.title)
