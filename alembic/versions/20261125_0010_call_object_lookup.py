"""Результат поиска объекта или шкафа для звонка."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("object_lookup", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "object_lookup")
