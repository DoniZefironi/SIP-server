"""Выжимки звонков

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recording_jobs",
        sa.Column("summary_status", sa.String(10), nullable=False, server_default="pending"),
    )
    op.add_column("recording_jobs", sa.Column("summary", JSONB()))
    op.add_column(
        "recording_jobs",
        sa.Column(
            "summary_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "recording_jobs",
        sa.Column("summary_retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("recording_jobs", sa.Column("summary_error", sa.String(255)))
    op.create_check_constraint(
        "ck_recording_jobs_summary_status",
        "recording_jobs",
        "summary_status IN ('pending', 'completed', 'skipped', 'failed')",
    )
    op.create_index(
        "ix_recording_jobs_summary_due",
        "recording_jobs",
        ["summary_status", "summary_next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_recording_jobs_summary_due", table_name="recording_jobs")
    op.drop_constraint("ck_recording_jobs_summary_status", "recording_jobs", type_="check")
    for column in (
        "summary_error",
        "summary_retry_count",
        "summary_next_attempt_at",
        "summary",
        "summary_status",
    ):
        op.drop_column("recording_jobs", column)
