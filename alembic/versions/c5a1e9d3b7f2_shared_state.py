"""shared state table (S3c, N web processes, ADR 0097)

Revision ID: c5a1e9d3b7f2
Revises: b4d8e2f6a9c1
Create Date: 2026-10-23 12:00:00.000000

Adds one table for app/services/shared_state.py:

  sharedstate (new table) - transient cross-process key/value state, written only when
      LOC_TMS_WEB_PROCESSES > 1: rate-limit window counters, consumed SAML assertion ids
      and short-lived encrypted one-time values. Columns: key (primary key, namespaced
      text), value (text), counter (integer), expires_at (unix epoch seconds, indexed for
      the sweep). Rows are deleted by an opportunistic sweep once expired.

All additive: the new table is empty on upgrade and no existing table is touched. Mirrors
models.SharedState (the column-completeness guard in tests/verify_migrations.py pins the
set). Classified EXPLICIT_EXCLUSIONS in app/services/data_export_part2.py. Hand-written off
the head b4d8e2f6a9c1.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5a1e9d3b7f2"
down_revision: Union[str, None] = "b4d8e2f6a9c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sharedstate",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("counter", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_sharedstate_expires_at", "sharedstate", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sharedstate_expires_at", table_name="sharedstate")
    op.drop_table("sharedstate")
