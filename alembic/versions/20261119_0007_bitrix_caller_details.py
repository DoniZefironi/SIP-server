"""Данные звонящего для уведомлений Bitrix24.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bitrix_deliveries", sa.Column("caller_details", sa.Text()))


def downgrade() -> None:
    op.drop_column("bitrix_deliveries", "caller_details")
