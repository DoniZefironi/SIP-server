import asyncio
import logging
import wave
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db.models import Call, RecordingJob
from app.db.session import SessionLocal
from app.services.journal import Outcome
from app.services.recordings import ready_recording, stereo_wav
from app.services.speechkit import SpeechKit, SpeechKitError

log = logging.getLogger(__name__)

_POLL_DELAY = timedelta(seconds=15)
_REQUEST_TIMEOUT = 90
_SUBMISSION_WINDOW = timedelta(seconds=120)
_MAX_RETRIES = 5
_MAX_WAV_BYTES = 60_000_000  # Лимит SpeechKit v3 для файла в теле запроса.


class Status(StrEnum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def _wav_content(call: Call, kind: str, root: Path) -> tuple[str, bool]:
    if kind == "mix":
        left = ready_recording(call, "client", root)
        right = ready_recording(call, "staff", root)
        if left is not None and right is not None:
            try:
                stereo = stereo_wav(left, right, _MAX_WAV_BYTES)
            except (OSError, EOFError, wave.Error):
                stereo = None
            if stereo is not None:
                return b64encode(stereo).decode("ascii"), True

    path = ready_recording(call, "message" if kind == "message" else "mix", root)
    if path is None:
        raise SpeechKitError("Запись недоступна", retryable=True)
    try:
        with path.open("rb") as wav:
            if path.stat().st_size > _MAX_WAV_BYTES:
                raise SpeechKitError("Запись превышает лимит SpeechKit (60 МБ)")
            data = wav.read(_MAX_WAV_BYTES + 1)
    except OSError as exc:
        raise SpeechKitError("Запись недоступна", retryable=True) from exc
    if len(data) > _MAX_WAV_BYTES:
        raise SpeechKitError("Запись превышает лимит SpeechKit (60 МБ)")
    return b64encode(data).decode("ascii"), False


def _fail_or_retry(job: RecordingJob, error: SpeechKitError) -> None:
    job.last_error = str(error)
    if error.retryable and job.retry_count < _MAX_RETRIES:
        job.next_attempt_at = datetime.now(UTC) + _POLL_DELAY * (2**job.retry_count)
        job.retry_count += 1
        if job.transcription_status == Status.SUBMITTING:
            job.transcription_status = Status.PENDING
        return

    job.transcription_status = Status.FAILED
    log.warning("Расшифровка звонка %s остановлена: %s", job.call_id, error)


async def _submit(call_id: int, content: str, client: SpeechKit) -> None:
    operation_id = None
    error = None
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            operation_id = await client.submit(content)
    except TimeoutError:
        error = SpeechKitError("Результат отправки в SpeechKit неизвестен: истекло время ожидания")
    except SpeechKitError as exc:
        error = exc

    async with SessionLocal.begin() as session:
        job = await session.get(RecordingJob, call_id, with_for_update=True)
        if job is None or job.transcription_status != Status.SUBMITTING:
            return
        if error is not None:
            _fail_or_retry(job, error)
            return
        job.operation_id = operation_id
        job.transcription_status = Status.PROCESSING
        job.next_attempt_at = datetime.now(UTC) + _POLL_DELAY
        job.retry_count = 0
        job.last_error = None


async def _poll(job: RecordingJob, call: Call, client: SpeechKit) -> None:
    try:
        if not job.operation_id:
            raise SpeechKitError("Отсутствует идентификатор операции SpeechKit")
        bot_before_ms = None
        if call.bot_started_at is not None and call.answered_at is not None:
            milliseconds = int((call.answered_at - call.bot_started_at).total_seconds() * 1000)
            if milliseconds > 0:
                bot_before_ms = milliseconds
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            transcript = await client.poll(
                job.operation_id,
                dialogue=job.dialogue_channels,
                staff_label="Ася" if call.outcome == Outcome.BOT_ANSWERED else "Сотрудник",
                bot_before_ms=bot_before_ms,
            )
    except TimeoutError:
        _fail_or_retry(job, SpeechKitError("Истекло время ожидания SpeechKit", retryable=True))
        return
    except SpeechKitError as error:
        _fail_or_retry(job, error)
        return

    job.retry_count = 0
    job.last_error = None
    if transcript is None:
        job.next_attempt_at = datetime.now(UTC) + _POLL_DELAY
        return
    job.transcript = transcript
    job.transcription_status = Status.COMPLETED


async def transcribe_next(settings: Settings, client: SpeechKit) -> bool:
    async with SessionLocal.begin() as session:
        job = await session.scalar(
            select(RecordingJob)
            .where(
                RecordingJob.transcription_status.in_(
                    (Status.PENDING, Status.SUBMITTING, Status.PROCESSING)
                ),
                RecordingJob.next_attempt_at <= datetime.now(UTC),
            )
            .order_by(RecordingJob.next_attempt_at, RecordingJob.call_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return False
        if job.transcription_status == Status.SUBMITTING:
            _fail_or_retry(job, SpeechKitError("Отправка прервалась до сохранения ID операции"))
            return True

        call = await session.get(Call, job.call_id)
        if call is None:
            return True
        if job.transcription_status == Status.PROCESSING:
            await _poll(job, call, client)
            return True
        try:
            content, dialogue_channels = await asyncio.to_thread(
                _wav_content, call, job.audio_kind, settings.recordings_dir
            )
        except SpeechKitError as error:
            _fail_or_retry(job, error)
            return True

        # Сохраняем факт отправки до вызова платного API. Если ответ потеряется,
        # повторная отправка может создать вторую операцию для той же записи.
        job.transcription_status = Status.SUBMITTING
        job.dialogue_channels = dialogue_channels
        job.next_attempt_at = datetime.now(UTC) + _SUBMISSION_WINDOW
        call_id = job.call_id

    await _submit(call_id, content, client)
    return True


async def run_transcription(settings: Settings) -> None:
    key = settings.speechkit_api_key.get_secret_value().strip()
    if not key:
        log.error("SpeechKit включён, но TELEPHONY_SPEECHKIT_API_KEY пуст")
        return

    async with httpx.AsyncClient(headers={"Authorization": f"Api-Key {key}"}, timeout=30) as http:
        client = SpeechKit(http)
        while True:
            worked = False
            try:
                worked = await transcribe_next(settings, client)
            except Exception:
                log.exception("Не удалось обработать задачу расшифровки")
            await asyncio.sleep(1 if worked else 3)
