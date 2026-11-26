"""Начало записи бота и подтверждение решения вопроса."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("bot_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("calls", sa.Column("bot_resolved", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "bot_resolved")
    op.drop_column("calls", "bot_started_at")
