"""Durable batch re-run intent and dispatcher ownership.

Revision ID: b5e7a9c1d3f6
Revises: d1f4a7c3e9b2
"""

from alembic import op
import sqlalchemy as sa

revision: str = "b5e7a9c1d3f6"
down_revision: str | None = "d1f4a7c3e9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflowbatch") as batch_op:
        batch_op.add_column(sa.Column("reopened_members", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("dispatcher_owner", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workflowbatch") as batch_op:
        batch_op.drop_column("dispatcher_owner")
        batch_op.drop_column("reopened_members")
