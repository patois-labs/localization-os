"""workflowrun notes (why a run ended, for restart/staleness recovery)

Revision ID: c4b7e1f92a3d
Revises: b6f1d0a7c34e
Create Date: 2026-08-03 18:00:00.000000

GitHub #44. A WorkflowRun had no place to record WHY it ended, so a run failed by
something outside the step machinery -- a server restart that killed the executing
thread, or the staleness sweep in `start_run` -- read as a bare FAILED with no
StepRun error to explain it, which is indistinguishable from "never started". This
is the single-run half of the `workflowbatch.notes` column Phase 5a already had.

NULLABLE with NO server_default, matching the model field
(WorkflowRun.notes: Optional[str] = None) for the nullability-parity CI guard
(verify_alembic_authority). Every existing row backfills to NULL = "no reason
recorded" = byte-identical to today. A plain additive ADD COLUMN, mirrored by the
SQLite _ensure_columns() shim in app/db.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4b7e1f92a3d'
down_revision: Union[str, None] = 'b6f1d0a7c34e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notes', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('notes')
