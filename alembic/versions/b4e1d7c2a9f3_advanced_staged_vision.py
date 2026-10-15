"""advanced staged target: structured vision + adjudicated provenance (P1 polish)

Revision ID: b4e1d7c2a9f3
Revises: c9f2a7b4e8d1
Create Date: 2026-09-01 21:30:00.000000

Two ADDITIVE columns on the advanced tier's OWN staged-target table (the classic engine is
untouched -- SPEC-advanced-workflows §2 HARD RULE):

  vision       -- "read" | "none" | "unavailable": whether the unit's screenshot was actually
                  read for this target. It was previously PACKED into `model_used` as a debug
                  string ("adjudicated=True vision=read"), which then rendered in the delivery
                  manifest's model slot, so a reader saw that string where a model id belongs.
  adjudicated  -- whether a second opinion settled this target (same reason).

`model_used` goes back to holding a model id. Both columns are nullable / defaulted, so the
upgrade is a no-op for existing rows (an already-delivered run keeps its packed string in
model_used; nothing re-parses it). Mirrors models.AdvancedStagedTarget + the create_all()
new-table path in app/db.py, so the nullability-parity guard (tests/verify_migrations.py)
holds on BOTH SQLite and Postgres. Hand-written off the head c9f2a7b4e8d1.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e1d7c2a9f3"
down_revision: Union[str, None] = "c9f2a7b4e8d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("advancedstagedtarget", sa.Column("vision", sa.String(), nullable=True))
    op.add_column(
        "advancedstagedtarget",
        sa.Column("adjudicated", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("advancedstagedtarget", "adjudicated")
    op.drop_column("advancedstagedtarget", "vision")
