from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, String, func
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
