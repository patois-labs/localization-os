"""conditional-workflow phase 1 durable signal (steprun score/gate/outcome_index + workflowrun context)

Revision ID: b7e3d9c2f5a1
Revises: a9e2c7f4b1d8
Create Date: 2026-07-12 15:00:00.000000

Phase 1 of SPEC-conditional-workflows (§3.2 / §3.3): add the durable outcome
signal + per-run scratchpad columns. Populated (steprun.score/gate by the LQA + QE
branches) but READ BY NOBODY until Phase 2/3 — a pure, byte-stable foundation.

  steprun:
    - score (FLOAT, nullable, default NULL): the step's numeric outcome (LQA score
      or the run-level QE aggregate) when it produced one; else NULL.
    - gate (VARCHAR, nullable, default NULL): "passed" | "failed" | NULL — the
      canonical pass/fail a condition reads.
    - outcome_index (INTEGER NOT NULL, server_default '0'): the step's visit number
      in this run (0 this phase — no loops yet).
  workflowrun:
    - context (JSON, nullable, default NULL): per-run scratchpad that survives
      pause/resume. NULL until the first CONDITIONAL feature writes it — a plain run
      never does, so existing/plain runs stay byte-identical.

All columns default to a neutral value (NULL, or 0 for the NOT-NULL outcome_index)
so existing rows are byte-identical. Mirrors the SQLite _ensure_columns() additive
migration in app/db.py (steprun + workflowrun lists). Hand-written (no autogenerate)
to avoid spurious index noise, same convention as
a9e2c7f4b1d8_workflowstep_tm_examples.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3d9c2f5a1'
down_revision: Union[str, None] = 'a9e2c7f4b1d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('steprun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('score', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('gate', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('outcome_index', sa.Integer(),
                                      nullable=False, server_default='0'))
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('context', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('context')
    with op.batch_alter_table('steprun', schema=None) as batch_op:
        batch_op.drop_column('outcome_index')
        batch_op.drop_column('gate')
        batch_op.drop_column('score')
