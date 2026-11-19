"""Очередь обработки записей

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recording_jobs",
        sa.Column(
            "call_id",
            sa.BigInteger(),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("audio_kind", sa.String(7), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("audio_kind IN ('mix', 'message')", name="ck_recording_jobs_audio_kind"),
    )


def downgrade() -> None:
    op.drop_table("recording_jobs")
