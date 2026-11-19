"""Расшифровка записей

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recording_jobs",
        sa.Column("transcription_status", sa.String(10), nullable=False, server_default="pending"),
    )
    op.add_column("recording_jobs", sa.Column("operation_id", sa.String(255)))
    op.add_column("recording_jobs", sa.Column("transcript", sa.Text()))
    op.add_column(
        "recording_jobs",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "recording_jobs", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("recording_jobs", sa.Column("last_error", sa.String(255)))
    op.create_check_constraint(
        "ck_recording_jobs_transcription_status",
        "recording_jobs",
        "transcription_status IN ('pending', 'submitting', 'processing', 'completed', 'failed')",
    )
    op.create_index(
        "ix_recording_jobs_transcription_due",
        "recording_jobs",
        ["transcription_status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_recording_jobs_transcription_due", table_name="recording_jobs")
    op.drop_constraint("ck_recording_jobs_transcription_status", "recording_jobs", type_="check")
    for column in (
        "last_error",
        "retry_count",
        "next_attempt_at",
        "transcript",
        "operation_id",
        "transcription_status",
    ):
        op.drop_column("recording_jobs", column)
