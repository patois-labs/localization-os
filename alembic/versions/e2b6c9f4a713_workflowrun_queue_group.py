"""workflowrun.queue_group — the durable fan-out queue (gate A round 3)

Revision ID: e2b6c9f4a713
Revises: b5e7a9c1d3f6
Create Date: 2026-09-06 21:10:00.000000

ONE additive column on workflowrun:

  workflowrun.queue_group (VARCHAR, nullable)  the fan-out this row was queued by

A key project's "translate all" is (target locales x namespace documents) runs — 72 on a
12-locale, 6-document project — and the install admits LOC_TMS_MAX_CONCURRENT_RUNS at a
time. Until now the excess lived in an IN-MEMORY queue: invisible on every surface, lost on
a restart, uncancellable, and silently dropped when a launch raised. It is now DURABLE — a
WorkflowRun row per pending launch, created up front in RunStatus.QUEUED, holding no
capacity slot and no pooled connection — and this column names the fan-out that created it.

It is what makes the queue honest on three axes: the launch response and the audit row can
name the rows this action queued (so a partial launch is never reported as a whole one);
the single admission scheduler serves fan-outs ROUND-ROBIN by this token, so one wide
"translate all" cannot starve another operator's; and a `wait=True` CLI drain can find its
own rows again.

NULLABLE with no default (the `batch_id` / `notes` shape): every legacy row and every
ordinary single launch reads NULL, and nothing branches on NULL beyond "this row was not
queued by a fan-out", so a pre-upgrade install behaves exactly as before.

The QUEUED status itself needs no migration — `workflowrun.status` is a plain string
column with no DB-level enum (see models.RunStatus).

Mirrors the SQLite _ensure_columns() additive shim in app/db.py + the model
(nullability-parity CI guard). Hand-written (no autogenerate), same convention as
d1f4a7c3e9b2. Chained after batch dispatch intent b5e7a9c1d3f6 as the single head.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e2b6c9f4a713"
down_revision: str | None = "b5e7a9c1d3f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('queue_group', sa.String(), nullable=True))
    op.create_index('ix_workflowrun_status_id', 'workflowrun', ['status', 'id'])
    op.create_index('ix_workflowrun_batch_id_status', 'workflowrun', ['batch_id', 'status'])


def downgrade() -> None:
    op.drop_index('ix_workflowrun_status_id', table_name='workflowrun')
    op.drop_index('ix_workflowrun_batch_id_status', table_name='workflowrun')
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('queue_group')
