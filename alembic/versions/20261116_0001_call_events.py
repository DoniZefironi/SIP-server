"""Таблица событий станции

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("linkedid", sa.String(64), nullable=True),
        sa.Column("uniqueid", sa.String(64), nullable=True),
        sa.Column("channel", sa.String(255), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_call_events_received_at", "call_events", ["received_at"])
    op.create_index("ix_call_events_linkedid", "call_events", ["linkedid"])


def downgrade() -> None:
    op.drop_table("call_events")
