"""Durable per-identity assist call allowances.

Revision ID: d4e8b2a1c9f6
Revises: c3d9a6f1e2b7
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e8b2a1c9f6"
down_revision: str | None = "c3d9a6f1e2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistallowance",
        sa.Column("actor_hash", sa.String(), nullable=False),
        sa.Column("day", sa.String(), nullable=False),
        sa.Column("daily_calls", sa.Integer(), nullable=False),
        sa.Column("minute_started", sa.Float(), nullable=False),
        sa.Column("minute_calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("actor_hash"),
    )


def downgrade() -> None:
    op.drop_table("assistallowance")
