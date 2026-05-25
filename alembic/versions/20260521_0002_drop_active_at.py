from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260521_0002"
down_revision = "20260518_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tg_users", "active_at")


def downgrade() -> None:
    op.add_column("tg_users", sa.Column("active_at", sa.DateTime(timezone=True), nullable=True))
