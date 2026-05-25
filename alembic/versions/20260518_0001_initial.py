from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260518_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_settings",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "redeem_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "code_type",
            sa.Enum("REGISTRATION", "RENEWAL", "WHITELIST", name="redeemcodetype", native_enum=False),
            nullable=False,
        ),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_redeem_codes_code"), "redeem_codes", ["code"], unique=True)
    op.create_table(
        "tg_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("abs_user_id", sa.String(length=128), nullable=True),
        sa.Column("abs_username", sa.String(length=128), nullable=True),
        sa.Column("registration_credits", sa.Integer(), nullable=False),
        sa.Column("is_whitelisted", sa.Boolean(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("last_checkin_date", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_disabled", sa.Boolean(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tg_users_telegram_id"), "tg_users", ["telegram_id"], unique=True)
    op.create_unique_constraint("uq_tg_users_abs_user_id", "tg_users", ["abs_user_id"])
    op.create_unique_constraint("uq_tg_users_abs_username", "tg_users", ["abs_username"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("target_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "rebind_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("requester_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("abs_user_id", sa.String(length=128), nullable=False),
        sa.Column("abs_username", sa.String(length=128), nullable=False),
        sa.Column("current_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "REJECTED", name="rebindrequeststatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("review_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("review_message_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["requester_telegram_id"], ["tg_users.telegram_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_rebind_requests_abs_user_id"),
        "rebind_requests",
        ["abs_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rebind_requests_requester_telegram_id"),
        "rebind_requests",
        ["requester_telegram_id"],
        unique=False,
    )
    op.create_table(
        "redeem_code_uses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code_id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["code_id"], ["redeem_codes.id"]),
        sa.ForeignKeyConstraint(["telegram_id"], ["tg_users.telegram_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_id", "telegram_id", name="uq_code_user"),
    )


def downgrade() -> None:
    op.drop_table("redeem_code_uses")
    op.drop_index(op.f("ix_rebind_requests_requester_telegram_id"), table_name="rebind_requests")
    op.drop_index(op.f("ix_rebind_requests_abs_user_id"), table_name="rebind_requests")
    op.drop_table("rebind_requests")
    op.drop_table("audit_logs")
    op.drop_constraint("uq_tg_users_abs_username", "tg_users", type_="unique")
    op.drop_constraint("uq_tg_users_abs_user_id", "tg_users", type_="unique")
    op.drop_index(op.f("ix_tg_users_telegram_id"), table_name="tg_users")
    op.drop_table("tg_users")
    op.drop_index(op.f("ix_redeem_codes_code"), table_name="redeem_codes")
    op.drop_table("redeem_codes")
    op.drop_table("bot_settings")
