from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CallEvent(Base):
    """Одно событие станции. linkedid общий у всех каналов одного звонка."""

    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Время хранится в UTC (timestamptz), по Минску его показывает фронт.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    event: Mapped[str] = mapped_column(String(64))
    linkedid: Mapped[str | None] = mapped_column(String(64), index=True)
    uniqueid: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, str]] = mapped_column(JSONB)


class Call(Base):
    """Звонок с линии в журнале. Собирается из событий станции (app/services/journal.py)."""

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    linkedid: Mapped[str] = mapped_column(String(64), unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Номер как его передала станция и он же в виде +375…; None — скрыт или не номер.
    caller_raw: Mapped[str | None] = mapped_column(String(64))
    caller: Mapped[str | None] = mapped_column(String(20), index=True)

    sector: Mapped[int | None] = mapped_column(SmallInteger, index=True)
    # True — клиент не выбрал сектор и попал в сектор по умолчанию после неудачных попыток.
    sector_by_default: Mapped[bool] = mapped_column(Boolean, default=False)

    queue_joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Последний телефон, на который звонила станция (PJSIP/101 или мобильный через линию).
    ring_target: Mapped[str | None] = mapped_column(String(255))
    answered_by: Mapped[str | None] = mapped_column(String(255))
    wait_seconds: Mapped[int | None] = mapped_column(Integer)
    talk_seconds: Mapped[int | None] = mapped_column(Integer)

    outcome: Mapped[str | None] = mapped_column(String(32), index=True)
    # Почему звонок ушёл в сообщение: afterhours, queue_full, no_answer.
    message_reason: Mapped[str | None] = mapped_column(String(32))

    # Начало пути записи разговора: к нему добавляются .wav, -client.wav, -staff.wav.
    recording: Mapped[str | None] = mapped_column(String(512))
    message_file: Mapped[str | None] = mapped_column(String(512))


class RecordingJob(Base):
    """Готовая запись, поставленная в очередь обработки."""

    __tablename__ = "recording_jobs"
    __table_args__ = (
        CheckConstraint("audio_kind IN ('mix', 'message')", name="ck_recording_jobs_audio_kind"),
        CheckConstraint(
            "transcription_status IN "
            "('pending', 'submitting', 'processing', 'completed', 'failed')",
            name="ck_recording_jobs_transcription_status",
        ),
        Index("ix_recording_jobs_transcription_due", "transcription_status", "next_attempt_at"),
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    audio_kind: Mapped[str] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    transcription_status: Mapped[str] = mapped_column(String(10), server_default="pending")
    operation_id: Mapped[str | None] = mapped_column(String(255))
    transcript: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(255))
