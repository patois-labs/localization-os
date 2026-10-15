"""project status lifecycle (P1, items 2+3)

Revision ID: d2f5a8c1b6e9
Revises: c1e8a4b7d2f3
Create Date: 2026-07-03 12:00:00.000000

Adds Project.status / status_changed_at / completed_at — the first-class project
lifecycle (app/services/lifecycle.py). status backfills existing rows to
'in_translation' (the pre-P1 implicit state) via server_default; the two
timestamps are nullable (NULL = never transitioned / never completed). Mirrors the
SQLite _ensure_columns() additive migration in app/db.py.

Hand-written (no autogenerate) to avoid spurious drop_index noise — same pattern
as e5b3c9a2f1d4_workflowstep_redact_pii.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2f5a8c1b6e9'
down_revision: Union[str, None] = 'c1e8a4b7d2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(), nullable=False,
                                      server_default='in_translation'))
        batch_op.add_column(sa.Column('status_changed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('completed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('completed_at')
        batch_op.drop_column('status_changed_at')
        batch_op.drop_column('status')
