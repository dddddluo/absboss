from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260524_0004"
down_revision = "20260522_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tg_users", sa.Column("tg_display_name", sa.String(length=256), nullable=True))
    op.create_table(
        "listening_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("abs_session_id", sa.String(length=128), nullable=False),
        sa.Column("abs_user_id", sa.String(length=128), nullable=False),
        sa.Column("library_item_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("display_title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("abs_session_id", name="uq_listening_sessions_abs_session_id"),
    )
    op.create_index(
        "ix_listening_sessions_abs_session_id",
        "listening_sessions",
        ["abs_session_id"],
        unique=True,
    )
    op.create_index(
        "ix_listening_sessions_abs_user_id",
        "listening_sessions",
        ["abs_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_listening_sessions_played_at",
        "listening_sessions",
        ["played_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_listening_sessions_played_at", table_name="listening_sessions")
    op.drop_index("ix_listening_sessions_abs_user_id", table_name="listening_sessions")
    op.drop_index("ix_listening_sessions_abs_session_id", table_name="listening_sessions")
    op.drop_table("listening_sessions")
    op.drop_column("tg_users", "tg_display_name")
