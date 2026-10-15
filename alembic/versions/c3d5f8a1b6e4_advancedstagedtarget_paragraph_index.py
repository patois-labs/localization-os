"""advancedstagedtarget paragraph_index (Advanced Workflows P3 T3, SPEC §8 FULL_TEXT)

Revision ID: c3d5f8a1b6e4
Revises: b7e3f1a9c2d4
Create Date: 2026-09-05 09:00:00.000000

Adds ONE column to the EXISTING advancedstagedtarget table so a conductor run can deliver a
FULL_TEXT DOCUMENT by paragraph decomposition (P3 T3):

  - paragraph_index (INTEGER, nullable, NO index, NO ForeignKey): a FULL_TEXT paragraph unit's
    0-based POSITION in Document.skeleton["units"]. It is the identity fact LAW-FT-3 binds and
    re-checks, doubles as the `_deliver` dispatch signal (non-NULL <=> a FULL_TEXT paragraph row),
    and orders the positional reassembly. NULL on every keys row, every SEGMENTED document row, and
    every pre-T3 row -> byte-identical.

A BARE FK-less column is deliberate (the segment_id precedent): batch_alter_table rebuilds the
table on SQLite and a real FK would materialize an ENFORCED constraint the app/db.py
ALTER TABLE ... ADD COLUMN boot shim can never reproduce. NO index: nothing queries by
paragraph_index (`_deliver` fetches all rows for a run by run_id and groups in memory), so
_INDEXES is untouched and all three schema paths (create_all from a plain Field, this revision,
the app/db.py _ADDED_COLUMNS shim) agree on a bare nullable integer. Chained off b7e3f1a9c2d4
(the run-190 steprun_failure_shape revision), the single head at merge; hand-written (no
autogenerate), same convention as b9e3d7c1f5a2_advancedstagedtarget_segment_id.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d5f8a1b6e4'
down_revision: Union[str, None] = 'b7e3f1a9c2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paragraph_index', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.drop_column('paragraph_index')
