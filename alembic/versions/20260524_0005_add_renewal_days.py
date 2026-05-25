from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260524_0005"
down_revision = "20260524_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tg_users", sa.Column("renewal_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tg_users", "renewal_days")
