"""conditional-workflow phase 3 branching + bounded loop-back (workflowstep.key/branch/max_visits)

Revision ID: e1c4f7a9d2b6
Revises: d7f3a1c9e2b5
Create Date: 2026-07-13 12:00:00.000000

Phase 3 of SPEC-conditional-workflows (§3.1 / §5; maintainer decisions D1/D3/D4): add the
three optional columns that turn a linear workflow into a bounded execution graph.

  workflowstep:
    - key (VARCHAR, nullable, default NULL): a human-authorable stable slug, UNIQUE
      within a workflow. Branch targets name a step by this key (survives reorder /
      rename). NULL = an unnamed step (byte-identical to today).
    - branch (JSON, nullable, default NULL): post-step routing on a gate-producing step
      {"on_pass": <target>, "on_fail": <target>} where target is "next"|"end"|
      {"step": "<key>"} (an earlier key = a bounded loop-back). NULL = fall through.
    - max_visits (INTEGER, nullable, default NULL): per-step per-run execution ceiling
      for loop safety. NULL = inherit the run-level default (settings workflow_max_visits).

Additive + neutral-defaulted (NULL) so every existing WorkflowStep row is byte-
identical and a plain (non-branching) run never reads them. Mirrors the SQLite
_ensure_columns() additive migration in app/db.py (workflowstep list) + the model
fields WorkflowStep.key/branch/max_visits. Hand-written (no autogenerate), same
convention as c9f4a7b2e1d8_condwf_phase2_run_condition.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1c4f7a9d2b6'
down_revision: Union[str, None] = 'd7f3a1c9e2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('branch', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('max_visits', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('max_visits')
        batch_op.drop_column('branch')
        batch_op.drop_column('key')
