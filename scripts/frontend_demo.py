"""Локальный стенд для разработки фронтенда."""

import asyncio
import os
import wave
from datetime import UTC, datetime, timedelta
from math import sin, tau
from pathlib import Path
from secrets import token_urlsafe
from struct import pack
from tempfile import NamedTemporaryFile

import uvicorn
from alembic.config import Config
from sqlalchemy.dialects.postgresql import insert

from alembic import command
from app.db.models import AuthUser, BitrixDelivery, Call, RecordingJob
from app.services.auth import Role, hash_password, revoke_sessions
from app.services.journal import Direction, Outcome

_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_RATE = 8000
_RECORDING_SECONDS = 10


def _write_wav(path: Path, segments: list[tuple[int, int, int]]) -> None:
    samples = [0] * (_SAMPLE_RATE * _RECORDING_SECONDS)
    for frequency, start, end in segments:
        for index in range(start * _SAMPLE_RATE, end * _SAMPLE_RATE):
            samples[index] = round(3000 * sin(tau * frequency * index / _SAMPLE_RATE))

    temporary_path = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with wave.open(temporary, "wb") as wav:
                wav.setparams((1, 2, _SAMPLE_RATE, 0, "NONE", "not compressed"))
                wav.writeframes(pack(f"<{len(samples)}h", *samples))
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def _seed(recordings: Path, demo_password: str) -> None:
    from app.db.session import SessionLocal, engine

    now = datetime.now(UTC)
    client = [(330, 0, 3)]
    staff = [(440, 4, 7)]
    _write_wav(recordings / "conversation.wav", client + staff)
    _write_wav(recordings / "conversation-client.wav", client)
    _write_wav(recordings / "conversation-staff.wav", staff)
    _write_wav(recordings / "message.wav", client)

    calls = (
        dict(outcome=Outcome.ANSWERED, sector=1, recording=str(recordings / "conversation")),
        dict(outcome=Outcome.MESSAGE_AFTERHOURS, message_file=str(recordings / "message.wav")),
        dict(outcome=Outcome.ABANDONED_QUEUE, sector=3, wait_seconds=20),
        dict(outcome=Outcome.ANSWERED, sector=4, recording=str(recordings / "conversation")),
        dict(outcome=Outcome.ANSWERED, sector=4, recording=str(recordings / "conversation")),
        dict(outcome=None, sector=2),
        dict(
            outcome=Outcome.ANSWERED,
            direction=Direction.OUTGOING,
            initiated_by="PJSIP/104",
            sector=4,
            recording=str(recordings / "conversation"),
        ),
    )
    jobs = (
        dict(
            call_id=1,
            audio_kind="mix",
            dialogue_channels=True,
            transcription_status="completed",
            transcript="[00:00] Клиент: Уточняю срок.\n[00:04] Сотрудник: Ответим сегодня.",
            summary_status="completed",
            summary=dict(
                topic="Клиент уточнил срок выполнения.",
                agreements=["Сообщить срок сегодня."],
                next_steps=["Проверить дату и перезвонить."],
                deal_code=None,
            ),
            bitrix_routing_status="resolved",
        ),
        dict(
            call_id=2,
            audio_kind="message",
            transcription_status="completed",
            transcript="Добрый день, прошу перезвонить.",
        ),
        dict(call_id=4, audio_kind="mix", transcription_status="processing"),
        dict(call_id=5, audio_kind="mix", transcription_status="failed"),
    )
    try:
        password_hash = await hash_password(demo_password)
        async with SessionLocal.begin() as session:
            demo_user_id = await session.scalar(
                insert(AuthUser)
                .values(username="demo-admin", password_hash=password_hash, role=Role.ADMIN)
                .on_conflict_do_update(
                    index_elements=[AuthUser.username],
                    set_={"password_hash": password_hash, "role": Role.ADMIN, "is_active": True},
                )
                .returning(AuthUser.id)
            )
            await revoke_sessions(session, demo_user_id)
            for call_id, call in enumerate(calls, 1):
                started = now - timedelta(minutes=(len(calls) - call_id) * 5, seconds=40)
                values = dict(call)
                outcome = values["outcome"]
                if outcome == Outcome.ANSWERED:
                    values.update(
                        wait_seconds=7,
                        talk_seconds=_RECORDING_SECONDS,
                    )
                    if values.get("direction") != Direction.OUTGOING:
                        values["answered_by"] = "PJSIP/101" if call_id == 1 else "PJSIP/104"
                number = f"+120255501{call_id:02d}"
                await session.execute(
                    insert(Call)
                    .values(
                        id=call_id,
                        linkedid=f"frontend-demo-{call_id}",
                        started_at=started,
                        ended_at=started + timedelta(seconds=30) if outcome else None,
                        caller=number,
                        caller_raw=number,
                        **values,
                    )
                    .on_conflict_do_nothing()
                )
            for job in jobs:
                await session.execute(insert(RecordingJob).values(**job).on_conflict_do_nothing())
            await session.execute(
                insert(BitrixDelivery)
                .values(call_id=1, destination="chat", target_id=10, status="sent", external_id=90)
                .on_conflict_do_nothing()
            )
    finally:
        await engine.dispose()


def main() -> None:
    demo_dir = _ROOT / ".frontend-demo"
    recordings = demo_dir / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    os.chdir(demo_dir)
    os.environ.update(
        TELEPHONY_DB_HOST="127.0.0.1",
        TELEPHONY_DB_PORT="15432",
        TELEPHONY_DB_NAME="telephony_demo",
        TELEPHONY_DB_USER="telephony_demo",
        TELEPHONY_DB_PASSWORD="telephony_demo_local",
        TELEPHONY_AUTH_SECURE_COOKIES="false",
        TELEPHONY_RECORDINGS_DIR=str(recordings),
        TELEPHONY_AMI_ENABLED="false",
        TELEPHONY_RECORDING_QUEUE_ENABLED="false",
        TELEPHONY_MAINTENANCE_ENABLED="false",
        TELEPHONY_SPEECHKIT_ENABLED="false",
        TELEPHONY_YANDEXGPT_ENABLED="false",
        TELEPHONY_BITRIX_ENABLED="false",
        TELEPHONY_BITRIX_DIRECTORY_ENABLED="false",
        TELEPHONY_SECTOR_NUMBERS='{"1": "+12025550150", "4": "+12025550153"}',
        TELEPHONY_EMPLOYEE_NAMES='{"PJSIP/101": "Ирина Морозова", "PJSIP/104": "Алексей Волков"}',
    )
    config = Config(_ROOT / "alembic.ini")
    config.set_main_option("script_location", "%(here)s/alembic")
    command.upgrade(config, "head")
    demo_password = token_urlsafe(24)
    asyncio.run(_seed(recordings, demo_password))
    print(f"Локальный стенд. Логин demo-admin, пароль {demo_password}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=18081)


if __name__ == "__main__":
    main()
