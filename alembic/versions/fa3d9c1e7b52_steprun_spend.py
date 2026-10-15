"""steprun.cost_usd / tokens_in / tokens_out (real per-workflow spend tracking)

Revision ID: fa3d9c1e7b52
Revises: f9c2e7a4d1b8
Create Date: 2026-07-20 12:00:00.000000

Adds three ADDITIVE, NULLABLE columns to `steprun` so a workflow run captures its
REAL spend at the source (feat/workflow-spend):

  steprun.cost_usd    — nullable FLOAT.   USD cost this step-visit accrued across
                        every completion call it made (per-segment fan-out + QE-judge
                        included). The SAME figure the spend ledger recorded (it comes
                        from run_completion, which returns the exact cost
                        spend.record_usage added — 0.0 for a MockProvider/LocalProvider-
                        exempt call, an honest zero).
  steprun.tokens_in   — nullable INTEGER. Input (+ cache) tokens fed in.
  steprun.tokens_out  — nullable INTEGER. Output tokens produced.

WHY NULLABLE, NO DEFAULT: NULL is the HONEST "pre-tracking / unknown" state. Every
StepRun written before this feature reads NULL (legacy), and the reports
`--group-by workflow` axis buckets a run whose StepRuns are ALL NULL as `(untracked)`
— never silently zeroed, never the old most-recent-WorkflowRun approximation. Only
runs executed AFTER this migration stamp real values (0.0 + real tokens on a mock,
real cost on a live provider). There is therefore NO backfill (unlike the onboarding
flag): a legacy run's spend is genuinely unknown and must READ as NULL, not 0.

Hand-written (no autogenerate) off the head f9c2e7a4d1b8, same convention as the
sibling additive-column revisions. Runs clean on SQLite (batch_alter_table) and
Postgres (native ALTER); the SQLite _ADDED_COLUMNS["steprun"] shim in app/db.py
carries the SAME three columns so a non-alembic SQLite boot adds them identically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fa3d9c1e7b52'
down_revision: Union[str, None] = 'f9c2e7a4d1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('steprun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cost_usd', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('tokens_in', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tokens_out', sa.Integer(), nullable=True))
    # No backfill: a legacy StepRun's real spend is UNKNOWN and must read NULL, which
    # the reports workflow axis surfaces as `(untracked)` (never a silent zero).


def downgrade() -> None:
    with op.batch_alter_table('steprun', schema=None) as batch_op:
        batch_op.drop_column('tokens_out')
        batch_op.drop_column('tokens_in')
        batch_op.drop_column('cost_usd')
