import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.db.models import Call, RecordingJob
from app.db.session import SessionLocal
from app.services.journal import OUTCOME_BY_MESSAGE_REASON, Outcome
from app.services.recordings import AudioKind, ready_recording

log = logging.getLogger(__name__)

_SCAN_INTERVAL = 15.0
_BATCH_SIZE = 100
_CONVERSATION_OUTCOMES = (Outcome.ANSWERED, Outcome.BOT_ANSWERED)


def _ready_jobs(calls: Sequence[Call], root: Path) -> list[dict[str, int | str]]:
    jobs = []
    for call in calls:
        kind: AudioKind = "mix" if call.outcome in _CONVERSATION_OUTCOMES else "message"
        if ready_recording(call, kind, root) is not None:
            bitrix_status = "skipped" if call.outcome == Outcome.BOT_ANSWERED else "pending"
            jobs.append({
                "call_id": call.id,
                "audio_kind": kind,
                "bitrix_routing_status": bitrix_status,
            })
    return jobs


async def enqueue_ready_recordings(settings: Settings) -> int:
    """Добавить готовые записи из журнала, включая файлы, появившиеся после отбоя."""
    oldest = datetime.now(UTC) - timedelta(days=settings.recordings_retention_days)
    after_id = 0
    queued = 0
    while True:
        statement = (
            select(Call)
            .where(
                Call.id > after_id,
                Call.ended_at >= oldest,
                or_(
                    and_(Call.outcome.in_(_CONVERSATION_OUTCOMES), Call.recording.is_not(None)),
                    and_(
                        Call.outcome.in_(OUTCOME_BY_MESSAGE_REASON.values()),
                        Call.message_file.is_not(None),
                    ),
                ),
                ~select(RecordingJob.call_id).where(RecordingJob.call_id == Call.id).exists(),
            )
            .order_by(Call.id)
            .limit(_BATCH_SIZE)
        )
        async with SessionLocal() as session:
            calls = (await session.scalars(statement)).all()
        if not calls:
            return queued

        after_id = calls[-1].id
        jobs = await asyncio.to_thread(_ready_jobs, calls, settings.recordings_dir)
        if jobs:
            async with SessionLocal.begin() as session:
                result = await session.scalars(
                    insert(RecordingJob)
                    .values(jobs)
                    .on_conflict_do_nothing(index_elements=[RecordingJob.call_id])
                    .returning(RecordingJob.call_id)
                )
                queued += len(result.all())


async def run_recording_queue(settings: Settings) -> None:
    while True:
        try:
            queued = await enqueue_ready_recordings(settings)
            if queued:
                log.info("В очередь обработки добавлено записей: %s", queued)
        except Exception:
            log.exception("Не удалось обновить очередь обработки записей")
        await asyncio.sleep(_SCAN_INTERVAL)
