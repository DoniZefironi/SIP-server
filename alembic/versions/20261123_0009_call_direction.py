"""Направление вызова и инициатор исходящего звонка."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calls",
        sa.Column("direction", sa.String(8), nullable=False, server_default="incoming"),
    )
    op.add_column("calls", sa.Column("initiated_by", sa.String(255), nullable=True))
    op.create_check_constraint(
        "ck_calls_direction", "calls", "direction IN ('incoming', 'outgoing')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_calls_direction", "calls", type_="check")
    op.drop_column("calls", "initiated_by")
    op.drop_column("calls", "direction")
