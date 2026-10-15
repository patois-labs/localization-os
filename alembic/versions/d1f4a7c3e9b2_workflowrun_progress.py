"""workflowrun in-step progress counters (GitHub #101)

Revision ID: d1f4a7c3e9b2
Revises: a7d4e1c9b2f6
Create Date: 2026-09-06 04:30:00.000000

SIX additive columns on workflowrun, the missing live signal for a long step:

  workflowrun.progress_done       (INTEGER NOT NULL DEFAULT 0)  units finished so far
  workflowrun.progress_total      (INTEGER NOT NULL DEFAULT 0)  units this step will do
  workflowrun.progress_unit       (VARCHAR NOT NULL DEFAULT '') "segments" | "chunks" | ''
  workflowrun.progress_step       (VARCHAR NOT NULL DEFAULT '') the step it belongs to
  workflowrun.progress_step_index (INTEGER NOT NULL DEFAULT 0)  that step's order_index
  workflowrun.progress_at         (DATETIME, nullable)          when the counter last moved

The counter is PER STEP: each long step publishes a fresh 0-of-M, so a bare `progress_done`
legitimately decreases at a step boundary and `progress_unit` changes under it. The two step
columns attribute the current N-of-M so a monitor can tell a transition from a regression.

A StepRun row is only written when a step ENDS, so a classic run over a multi-segment
document reported `step_states: []` / `translated: 0` on /api, the CLI and the SSE stream
for the entire 10-25 minutes of its llm_agent step -- indistinguishable from a wedge. The
orchestrator now publishes these counters from inside the step (per segment as the fan-out
completes, per chunk on the FULL_TEXT path) and all three surfaces read them.

DEFAULT-VALUED, NOT NULLABLE for the five scalars (the used_tm_match shape): every legacy
row backfills 0/0/''/''/0 = "no progress recorded", which the surfaces render as absent rather
than as a false 0%. `progress_at` is nullable for the same reason (NULL = never published).
Nothing reads these columns to make a decision -- they are telemetry -- so a pre-upgrade row
behaves exactly as before.

Mirrors the SQLite _ensure_columns() additive shim in app/db.py + the model
(nullability-parity CI guard). Hand-written (no autogenerate), same convention as
a7d4e1c9b2f6, off which it chains as the single head.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1f4a7c3e9b2'
down_revision: Union[str, None] = 'a7d4e1c9b2f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('progress_done', sa.Integer(), nullable=False, server_default='0')
        )
        batch_op.add_column(
            sa.Column('progress_total', sa.Integer(), nullable=False, server_default='0')
        )
        batch_op.add_column(
            sa.Column('progress_unit', sa.String(), nullable=False, server_default='')
        )
        batch_op.add_column(
            sa.Column('progress_step', sa.String(), nullable=False, server_default='')
        )
        batch_op.add_column(
            sa.Column('progress_step_index', sa.Integer(), nullable=False, server_default='0')
        )
        batch_op.add_column(sa.Column('progress_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('progress_at')
        batch_op.drop_column('progress_step_index')
        batch_op.drop_column('progress_step')
        batch_op.drop_column('progress_unit')
        batch_op.drop_column('progress_total')
        batch_op.drop_column('progress_done')
