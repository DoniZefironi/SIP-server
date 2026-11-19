import asyncio
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db.models import RecordingJob
from app.db.session import SessionLocal
from app.services.transcription import Status as TranscriptionStatus
from app.services.yandexgpt import YandexGpt, YandexGptError

log = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 60
_RETRY_DELAY = timedelta(seconds=15)
_MAX_RETRIES = 5


class SummaryStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


def _fail_or_retry(job: RecordingJob, error: YandexGptError) -> None:
    job.summary_error = str(error)
    if error.retryable and job.summary_retry_count < _MAX_RETRIES:
        job.summary_next_attempt_at = datetime.now(UTC) + _RETRY_DELAY * (
            2**job.summary_retry_count
        )
        job.summary_retry_count += 1
        return
    job.summary_status = SummaryStatus.FAILED
    log.warning("Не удалось составить выжимку звонка %s: %s", job.call_id, error)


async def summarize_next(client: YandexGpt) -> bool:
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
            _fail_or_retry(job, YandexGptError("Истекло время ожидания YandexGPT", retryable=True))
            return True
        except YandexGptError as error:
            _fail_or_retry(job, error)
            return True

        job.summary = summary.model_dump()
        job.summary_status = SummaryStatus.COMPLETED
        job.summary_error = None
        return True


async def run_summarization(settings: Settings) -> None:
    key = settings.yandexgpt_api_key.get_secret_value().strip()
    folder_id = settings.yandexgpt_folder_id.strip()
    if not (key and folder_id):
        log.error("YandexGPT включён, но для него не заданы ключ API и каталог")
        return

    async with httpx.AsyncClient(headers={"Authorization": f"Api-Key {key}"}, timeout=30) as http:
        client = YandexGpt(http, folder_id)
        while True:
            worked = False
            try:
                worked = await summarize_next(client)
            except Exception:
                log.exception("Не удалось обработать задачу составления выжимки")
            await asyncio.sleep(1 if worked else 3)
