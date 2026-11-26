"""Пользователи, сессии и ограничения входа."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint("role IN ('viewer', 'manager', 'admin')", name="ck_auth_users_role"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("auth_users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("access_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_idle_expires_at", "auth_sessions", ["idle_expires_at"])
    op.create_table(
        "auth_refresh_tokens",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_auth_refresh_tokens_session_id", "auth_refresh_tokens", ["session_id"])
    op.create_table(
        "auth_login_limits",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_login_limits_expires_at", "auth_login_limits", ["expires_at"])


def downgrade() -> None:
    op.drop_table("auth_login_limits")
    op.drop_table("auth_refresh_tokens")
    op.drop_table("auth_sessions")
    op.drop_table("auth_users")
