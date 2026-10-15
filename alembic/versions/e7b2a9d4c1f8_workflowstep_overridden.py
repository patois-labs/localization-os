"""workflowstep overridden (shared-base + override-delta variant model, 4b)

Revision ID: e7b2a9d4c1f8
Revises: d7f2a4c9e1b6
Create Date: 2026-07-18 11:00:00.000000

Adds WorkflowStep.overridden (BOOLEAN NOT NULL DEFAULT 0) for
SPEC-workflows-redesign Phase 4b (shared-base + override-delta). overridden=1
means a VARIANT step was deliberately diverged (PINNED) — base-edit propagation
never touches it (the sacred don't-clobber-a-pinned-locale invariant).

server_default='0' backfills every existing step to "not pinned". This is SAFE by
construction: propagation ALSO requires a non-NULL adapted_from_id (a LIVE
provenance link, populated on every clone only since 4b), and a LEGACY variant has
adapted_from_id NULL — so a legacy row is never a propagation target regardless of
overridden. TWO-KEY safety (adapted_from_id link AND overridden==False) is what
makes the False default harmless on the backfill.

Additive column only (the migration-beyond-ADD-COLUMN caveat does not apply).
Mirrors the model (WorkflowStep.overridden: bool = False) and the SQLite
_ensure_columns() shim in app/db.py (nullability-parity CI guard). Hand-written (no
autogenerate) to avoid spurious drop_index noise, like the sibling revisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b2a9d4c1f8'
down_revision: Union[str, None] = 'd7f2a4c9e1b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('overridden', sa.Boolean(), nullable=False,
                                      server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('overridden')
