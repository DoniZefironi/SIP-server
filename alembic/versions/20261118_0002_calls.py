"""Журнал звонков

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("linkedid", sa.String(64), nullable=False, unique=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("caller_raw", sa.String(64), nullable=True),
        sa.Column("caller", sa.String(20), nullable=True),
        sa.Column("sector", sa.SmallInteger(), nullable=True),
        sa.Column("sector_by_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("queue_joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ring_target", sa.String(255), nullable=True),
        sa.Column("answered_by", sa.String(255), nullable=True),
        sa.Column("wait_seconds", sa.Integer(), nullable=True),
        sa.Column("talk_seconds", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("message_reason", sa.String(32), nullable=True),
        sa.Column("recording", sa.String(512), nullable=True),
        sa.Column("message_file", sa.String(512), nullable=True),
    )
    op.create_index("ix_calls_started_at", "calls", ["started_at"])
    op.create_index("ix_calls_caller", "calls", ["caller"])
    op.create_index("ix_calls_sector", "calls", ["sector"])
    op.create_index("ix_calls_outcome", "calls", ["outcome"])


def downgrade() -> None:
    op.drop_table("calls")
