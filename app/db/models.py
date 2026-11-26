from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
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
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuthUser(Base):
    __tablename__ = "auth_users"
    __table_args__ = (
        CheckConstraint("role IN ('viewer', 'manager', 'admin')", name="ck_auth_users_role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(8))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True,
    )
    access_digest: Mapped[str] = mapped_column(String(64), unique=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthRefreshToken(Base):
    __tablename__ = "auth_refresh_tokens"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), index=True,
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthLoginLimit(Base):
    __tablename__ = "auth_login_limits"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class BitrixContact(Base):
    __tablename__ = "bitrix_contacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)
    phones: Mapped[list[str]] = mapped_column(ARRAY(String(20)))
    skip_bot: Mapped[bool] = mapped_column(Boolean, server_default="false")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    __table_args__ = (
        CheckConstraint("direction IN ('incoming', 'outgoing')", name="ck_calls_direction"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    linkedid: Mapped[str] = mapped_column(String(64), unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str] = mapped_column(String(8), server_default="incoming")
    initiated_by: Mapped[str | None] = mapped_column(String(255))

    # Номер клиента. Для исходящего звонка это номер назначения.
    caller_raw: Mapped[str | None] = mapped_column(String(64))
    caller: Mapped[str | None] = mapped_column(String(20), index=True)
    object_lookup: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    bot_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bot_resolved: Mapped[bool | None] = mapped_column(Boolean)

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
        CheckConstraint(
            "summary_status IN ('pending', 'completed', 'skipped', 'failed')",
            name="ck_recording_jobs_summary_status",
        ),
        Index("ix_recording_jobs_summary_due", "summary_status", "summary_next_attempt_at"),
        CheckConstraint(
            "bitrix_routing_status IN ('pending', 'resolved', 'skipped', 'failed')",
            name="ck_recording_jobs_bitrix_routing_status",
        ),
        Index("ix_recording_jobs_bitrix_route_due", "bitrix_routing_status", "bitrix_route_due_at"),
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    audio_kind: Mapped[str] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    transcription_status: Mapped[str] = mapped_column(String(10), server_default="pending")
    operation_id: Mapped[str | None] = mapped_column(String(255))
    transcript: Mapped[str | None] = mapped_column(Text)
    dialogue_channels: Mapped[bool] = mapped_column(Boolean, server_default="false")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(255))
    summary_status: Mapped[str] = mapped_column(String(10), server_default="pending")
    summary: Mapped[dict[str, str | list[str] | None] | None] = mapped_column(JSONB)
    summary_next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    summary_retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    summary_error: Mapped[str | None] = mapped_column(String(255))
    bitrix_routing_status: Mapped[str] = mapped_column(String(8), server_default="pending")
    bitrix_route_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    bitrix_route_attempts: Mapped[int] = mapped_column(Integer, server_default="0")


class BitrixDelivery(Base):
    """Доставка одной выжимки в карточку CRM или личный чат."""

    __tablename__ = "bitrix_deliveries"
    __table_args__ = (
        CheckConstraint(
            "destination IN ('deal', 'contact', 'company', 'chat')",
            name="ck_bitrix_deliveries_destination",
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'uncertain')",
            name="ck_bitrix_deliveries_status",
        ),
        Index("ix_bitrix_deliveries_due", "status", "next_attempt_at"),
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("recording_jobs.call_id", ondelete="CASCADE"), primary_key=True
    )
    destination: Mapped[str] = mapped_column(String(7), primary_key=True)
    target_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(9), server_default="pending")
    external_id: Mapped[int | None] = mapped_column(BigInteger)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(255))
    caller_details: Mapped[str | None] = mapped_column(Text)
