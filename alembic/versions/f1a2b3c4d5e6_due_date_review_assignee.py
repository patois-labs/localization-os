"""project due date + workflow-step review assignee

Revision ID: f1a2b3c4d5e6
Revises: e9a1c4b7f2d8
Create Date: 2026-07-05 14:00:00.000000

Two additive columns, one revision (Feature A + Feature B share this change):

  - project.due_date (DateTime, nullable) — an OPTIONAL project due date. NULL =
    no due date = the default (most projects are fully automated and need none).
    Stored as a naive-UTC datetime at the day boundary (the _utcnow convention) so
    existing datetime comparisons work.

  - workflowstep.assignee_email (String, non-null default "") — the reviewer
    emailed when a human_review step pauses. "" = no assignee = no email (v1 is
    explicit-assignee-only). Inert on non-human_review steps.

NO backfill beyond the column defaults: NULL (due_date) and "" (assignee_email)
are the correct values for every existing row, so a pre-change DB boots + reads the
new columns unchanged. Mirrors the SQLite _ensure_columns() additive migration in
app/db.py.

Hand-written (no autogenerate) off the R2-3 head e9a1c4b7f2d8 to avoid the spurious
drop_index noise autogenerate emits for the app/db.py hot-path indexes — same
pattern as d2f5a8c1b6e9_project_status_lifecycle.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e9a1c4b7f2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # project.due_date (nullable DateTime). NULL = no due date.
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('due_date', sa.DateTime(), nullable=True))
    # workflowstep.assignee_email (non-null String, default ""). "" = no assignee.
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assignee_email', sa.String(), nullable=False,
                                      server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('assignee_email')
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('due_date')
