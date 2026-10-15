"""conditional-workflow phase 2 pre-step guard (workflowstep.run_condition)

Revision ID: c9f4a7b2e1d8
Revises: b7e3d9c2f5a1
Create Date: 2026-07-12 18:00:00.000000

Phase 2 of SPEC-conditional-workflows (§3.1 / §5): add the optional PRE-STEP GUARD
column. A step's run_condition, when set, decides whether the step RUNS or is SKIPPED
based on the durable Phase-1 outcome of the immediately-prior LQA / QE-gated step.

  workflowstep:
    - run_condition (JSON, nullable, default NULL): the structured, allowlisted guard
      {"ref": "prev_lqa"|"prev_qe", "op": "passed"|"failed"|"score_gte"|"score_lt",
       "value": <number|null>}. NULL = ALWAYS run -> every existing row is byte-
      identical, and a plain (non-conditional) run never evaluates it.

Additive + neutral-defaulted (NULL) so every existing WorkflowStep row is byte-
identical. Mirrors the SQLite _ensure_columns() additive migration in app/db.py
(workflowstep list) + the model field WorkflowStep.run_condition. Hand-written (no
autogenerate) to avoid spurious index noise, same convention as
b7e3d9c2f5a1_condwf_phase1_durable_signal.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f4a7b2e1d8'
down_revision: Union[str, None] = 'b7e3d9c2f5a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('run_condition', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('run_condition')
