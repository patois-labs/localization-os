"""Separate TM retention activity from creation and translation usage.

Revision ID: a276b2c3d4e5
Revises: e8a1c4b7d9f2
"""

from alembic import op
import sqlalchemy as sa

revision: str = "a276b2c3d4e5"
down_revision: str | None = "e8a1c4b7d9f2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Retention scans bounded primary-key ranges; standalone activity indexes
    # cannot serve its coalesce predicate and would only add write overhead.
    op.add_column("segmenttm", sa.Column("activity_at", sa.DateTime(), nullable=True))
    op.add_column("fulltexttm", sa.Column("activity_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("fulltexttm", "activity_at")
    op.drop_column("segmenttm", "activity_at")
