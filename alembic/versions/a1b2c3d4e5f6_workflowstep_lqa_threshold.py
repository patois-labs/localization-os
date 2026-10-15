"""workflowstep.lqa_threshold (configurable LQA step pass gate)

Revision ID: a1b2c3d4e5f6
Revises: a9f3c1e7d5b2
Create Date: 2026-08-08 09:00:00.000000

Adds ONE additive, NULLABLE column to `workflowstep`:

  workflowstep.lqa_threshold — nullable INTEGER. The numeric pass bar an LQA step's
                               gate uses. NULL (every legacy row) means the built-in
                               default: a card PASSES when score >= 90 AND it carries no
                               major/critical error. A set value (0-100) makes the step
                               PASS only at score >= that value; the no-major-error policy
                               is UNCHANGED — only the numeric bar is configurable, which
                               is exactly the "an LQA step that needs to pass with a 94"
                               example this feature exists to express.

WHY NULLABLE, NO SERVER DEFAULT AND NO BACKFILL: the default is a BEHAVIOUR (score >= 90),
not a value that has to be written down. Backfilling every step to the literal 90 would
record a decision nobody made and would make a future "NULL means inherit the workflow's
default" reading impossible. NULL reads as LQA_PASS_THRESHOLD (90) in the one place that
consults the column (lqa._score via lqa.score_text, called from the orchestrator's two
LQA branches), so every legacy row is byte-identical to the pre-configurable gate.

DISTINCT FROM qe_threshold: qe_threshold is the QE ROUTING cutoff on an llm_agent step
(route produced segments below it to human review, default 70) and is a DEAD field on an
lqa step; lqa_threshold is the LQA reviewer PASS bar and is live ONLY on lqa steps. They
are separate mechanisms, so this is a separate column — reusing qe_threshold would have
changed every existing lqa step's effective gate from 90 to 70 (its non-null default).

Hand-written (no autogenerate) off the head a9f3c1e7d5b2, same convention as the sibling
additive-column revisions. Runs clean on SQLite (batch_alter_table) and Postgres (native
ALTER); the SQLite _ADDED_COLUMNS["workflowstep"] shim in app/db.py carries the SAME
column so a non-alembic SQLite boot adds it identically (the nullability-parity CI guard
compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'a9f3c1e7d5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lqa_threshold', sa.Integer(), nullable=True))
    # No backfill: NULL already reads as the built-in default (90) at the single
    # consumption point (lqa._score).


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('lqa_threshold')
