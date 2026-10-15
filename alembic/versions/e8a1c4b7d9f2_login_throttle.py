"""Persistent account password lockout (#222).

Revision ID: e8a1c4b7d9f2
Revises: d4e8b2a1c9f6
"""

import sqlalchemy as sa

from alembic import op

revision: str = "e8a1c4b7d9f2"
down_revision: str = "d4e8b2a1c9f6"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "loginthrottle",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.String(), nullable=False, server_default="[]"),
        sa.Column("notification", sa.String(), nullable=False, server_default=""),
        sa.Column("canonical_name", sa.String(), nullable=False, server_default=""),
        sa.Column("locked_until", sa.Float(), nullable=False, server_default="0"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_loginthrottle_expires_at", "loginthrottle", ["expires_at"])
    op.create_index("ix_loginthrottle_canonical_name", "loginthrottle", ["canonical_name"])


def downgrade() -> None:
    op.drop_table("loginthrottle")
