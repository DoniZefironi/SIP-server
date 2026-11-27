import asyncio
import logging
import wave
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Call, RecordingJob
from app.db.session import SessionLocal
from app.services.aiai import AiaiError, AiaiSpeech
from app.services.aiai_recordings import recognize_tracks, recording_paths
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


def _fail_or_retry(job: RecordingJob, error: SpeechKitError | AiaiError) -> None:
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
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            transcript = await client.poll(
                job.operation_id,
                dialogue=job.dialogue_channels,
                staff_label="Ася" if call.outcome == Outcome.BOT_ANSWERED else "Сотрудник",
                bot_before_ms=_bot_before_ms(call),
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


def _bot_before_ms(call: Call) -> int | None:
    if call.bot_started_at is None or call.answered_at is None:
        return None
    milliseconds = int((call.answered_at - call.bot_started_at).total_seconds() * 1000)
    return milliseconds if milliseconds > 0 else None


async def _next_job(session: AsyncSession) -> RecordingJob | None:
    return await session.scalar(
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


async def transcribe_next(settings: Settings, client: SpeechKit) -> bool:
    async with SessionLocal.begin() as session:
        job = await _next_job(session)
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


async def _transcribe_aiai_next(settings: Settings, client: AiaiSpeech) -> bool:
    async with SessionLocal.begin() as session:
        job = await _next_job(session)
        if job is None:
            return False
        if job.transcription_status == Status.SUBMITTING:
            _fail_or_retry(job, AiaiError("Отправка прервалась до сохранения расшифровки"))
            return True
        call = await session.get(Call, job.call_id)
        if call is None:
            return True
        try:
            paths = await asyncio.to_thread(
                recording_paths, call, job.audio_kind, settings.recordings_dir,
            )
        except AiaiError as error:
            _fail_or_retry(job, error)
            return True
        call_id = job.call_id
        staff_label = "Ася" if call.outcome == Outcome.BOT_ANSWERED else "Сотрудник"
        bot_before_ms = _bot_before_ms(call)
        job.transcription_status = Status.SUBMITTING
        job.operation_id = None
        job.next_attempt_at = datetime.now(UTC) + _SUBMISSION_WINDOW
        job.dialogue_channels = len(paths) == 2

    error = None
    transcript = ""
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            transcript = await recognize_tracks(
                client, paths, staff_label=staff_label, bot_before_ms=bot_before_ms,
            )
    except TimeoutError:
        error = AiaiError("Результат отправки в AIAI.BY неизвестен: истекло время ожидания")
    except AiaiError as exc:
        error = exc

    async with SessionLocal.begin() as session:
        job = await session.get(RecordingJob, call_id, with_for_update=True)
        if job is not None and job.transcription_status == Status.SUBMITTING:
            if error is None:
                job.transcript = transcript
                job.transcription_status = Status.COMPLETED
                job.last_error = None
                job.retry_count = 0
            else:
                _fail_or_retry(job, error)
    return True


async def run_transcription(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        if settings.speech_provider == "aiai":
            try:
                client = AiaiSpeech(http, settings)
            except AiaiError as error:
                log.error("%s", error)
                return
            transcribe = _transcribe_aiai_next
        else:
            key = settings.speechkit_api_key.get_secret_value().strip()
            if not key:
                log.error("SpeechKit включён, но TELEPHONY_SPEECHKIT_API_KEY пуст")
                return
            http.headers["Authorization"] = f"Api-Key {key}"
            client = SpeechKit(http)
            transcribe = transcribe_next
        log.info("Провайдер распознавания записей: %s", settings.speech_provider)
        while True:
            worked = False
            try:
                worked = await transcribe(settings, client)
            except Exception:
                log.exception("Не удалось обработать задачу расшифровки")
            await asyncio.sleep(1 if worked else 3)
