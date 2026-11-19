"""Маршрутизация и доставка выжимок в Bitrix24.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recording_jobs",
        sa.Column("bitrix_routing_status", sa.String(8), nullable=False, server_default="pending"),
    )
    op.add_column(
        "recording_jobs",
        sa.Column(
            "bitrix_route_due_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "recording_jobs",
        sa.Column("bitrix_route_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_recording_jobs_bitrix_routing_status",
        "recording_jobs",
        "bitrix_routing_status IN ('pending', 'resolved', 'skipped', 'failed')",
    )
    op.create_index(
        "ix_recording_jobs_bitrix_route_due",
        "recording_jobs",
        ["bitrix_routing_status", "bitrix_route_due_at"],
    )
    op.create_table(
        "bitrix_deliveries",
        sa.Column(
            "call_id",
            sa.BigInteger(),
            sa.ForeignKey("recording_jobs.call_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("destination", sa.String(7), primary_key=True),
        sa.Column("target_id", sa.BigInteger(), primary_key=True),
        sa.Column("status", sa.String(9), nullable=False, server_default="pending"),
        sa.Column("external_id", sa.BigInteger()),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(255)),
        sa.CheckConstraint(
            "destination IN ('deal', 'contact', 'company', 'chat')",
            name="ck_bitrix_deliveries_destination",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'uncertain')",
            name="ck_bitrix_deliveries_status",
        ),
    )
    op.create_index("ix_bitrix_deliveries_due", "bitrix_deliveries", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("bitrix_deliveries")
    op.drop_index("ix_recording_jobs_bitrix_route_due", table_name="recording_jobs")
    op.drop_constraint("ck_recording_jobs_bitrix_routing_status", "recording_jobs", type_="check")
    for column in ("bitrix_route_attempts", "bitrix_route_due_at", "bitrix_routing_status"):
        op.drop_column("recording_jobs", column)
