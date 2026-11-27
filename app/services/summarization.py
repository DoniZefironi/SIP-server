import asyncio
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db.models import RecordingJob
from app.db.session import SessionLocal
from app.services.aiai import AiaiError, aiai_connection
from app.services.summary_model import ChatSummary, SummaryError
from app.services.transcription import Status as TranscriptionStatus
from app.services.yandexgpt import YandexGpt

log = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 60
_RETRY_DELAY = timedelta(seconds=15)
_MAX_RETRIES = 5


class SummaryStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


def _fail_or_retry(job: RecordingJob, error: SummaryError) -> None:
    job.summary_error = str(error)
    if error.retryable and job.summary_retry_count < _MAX_RETRIES:
        job.summary_next_attempt_at = datetime.now(UTC) + _RETRY_DELAY * (
            2**job.summary_retry_count
        )
        job.summary_retry_count += 1
        return
    job.summary_status = SummaryStatus.FAILED
    log.warning("Не удалось составить выжимку звонка %s: %s", job.call_id, error)


async def summarize_next(client: ChatSummary) -> bool:
    async with SessionLocal.begin() as session:
        job = await session.scalar(
            select(RecordingJob)
            .where(
                RecordingJob.transcription_status == TranscriptionStatus.COMPLETED,
                RecordingJob.summary_status == SummaryStatus.PENDING,
                RecordingJob.summary_next_attempt_at <= datetime.now(UTC),
            )
            .order_by(RecordingJob.summary_next_attempt_at, RecordingJob.call_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return False

        transcript = (job.transcript or "").strip()
        if not transcript:
            job.summary_status = SummaryStatus.SKIPPED
            return True

        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                summary = await client.summarize(transcript)
        except TimeoutError:
            _fail_or_retry(job, SummaryError(
                f"Истекло время ожидания {client.provider}", retryable=True,
            ))
            return True
        except SummaryError as error:
            _fail_or_retry(job, error)
            return True

        job.summary = summary.model_dump()
        job.summary_status = SummaryStatus.COMPLETED
        job.summary_error = None
        return True


def _summary_client(settings: Settings, http: httpx.AsyncClient) -> ChatSummary | None:
    if settings.summary_provider == "aiai":
        model = settings.aiai_model.strip()
        if not model:
            log.error("Для AIAI.BY нужен TELEPHONY_AIAI_MODEL")
            return None
        try:
            base_url, headers = aiai_connection(settings)
        except AiaiError as error:
            log.error("%s", error)
            return None
        http.headers.update(headers)
        return ChatSummary(
            http,
            endpoint=f"{base_url}/chat/completions",
            model=model,
            provider="AIAI.BY",
        )

    key = settings.yandexgpt_api_key.get_secret_value().strip()
    folder_id = settings.yandexgpt_folder_id.strip()
    if not (key and folder_id):
        log.error("YandexGPT включён, но для него не заданы ключ API и каталог")
        return None
    http.headers["Authorization"] = f"Api-Key {key}"
    return YandexGpt(http, folder_id)


async def run_summarization(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        client = _summary_client(settings, http)
        if client is None:
            return
        log.info("Провайдер выжимок звонков: %s", client.provider)
        while True:
            worked = False
            try:
                worked = await summarize_next(client)
            except Exception:
                log.exception("Не удалось обработать задачу составления выжимки")
            await asyncio.sleep(1 if worked else 3)
