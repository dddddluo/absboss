from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260522_0003"
down_revision = "20260521_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tg_users", sa.Column("abs_password", sa.String(length=128), nullable=True))
    op.create_table(
        "registration_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("abs_username", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "PROCESSING",
                "DONE",
                "FAILED",
                name="registrationqueuestatus",
                native_enum=False,
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_password", sa.String(length=128), nullable=True),
        sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notification_delivered", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registration_queue_telegram_id",
        "registration_queue",
        ["telegram_id"],
        unique=False,
    )
    op.create_index(
        "ix_registration_queue_status",
        "registration_queue",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_registration_queue_created_at",
        "registration_queue",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_registration_queue_created_at", table_name="registration_queue")
    op.drop_index("ix_registration_queue_status", table_name="registration_queue")
    op.drop_index("ix_registration_queue_telegram_id", table_name="registration_queue")
    op.drop_table("registration_queue")
    op.drop_column("tg_users", "abs_password")
