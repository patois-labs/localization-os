"""workflowrun created_by (run attribution for the unified jobs history)

Revision ID: d7f3a1c9e2b5
Revises: c9f4a7b2e1d8
Create Date: 2026-07-13 12:00:00.000000

Adds WorkflowRun.created_by so the unified /jobs history log can attribute a
workflow run to whoever launched it (a run had no actor column before, so the
history page showed "-"). Threaded through every run-creation seam (web
start_run, CLI run_workflow, the orchestrator stream fns); a resume does NOT
re-stamp it.

server_default='' backfills every existing row to blank (renders "-", exactly as
before). NOT NULL to match the non-Optional model field WorkflowRun.created_by
(str = ""), mirroring the sibling d4a1f2c7e8b9 target_locale migration + the
nullability-parity CI guard (verify_alembic_authority). A plain additive ADD
COLUMN, mirrored by the SQLite _ensure_columns() shim in app/db.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f3a1c9e2b5'
down_revision: Union[str, None] = 'c9f4a7b2e1d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_by', sa.String(), nullable=False,
                                      server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('created_by')
