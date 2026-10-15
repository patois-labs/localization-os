"""workflowrun target_locale

Revision ID: d4a1f2c7e8b9
Revises: c3d9e7f1a2b8
Create Date: 2026-07-03 02:30:00.000000

Adds WorkflowRun.target_locale so a run remembers the locale it was launched for.
Before this, the run view + resume re-inferred the locale as the first target
locale on the document, which was WRONG on a multi-target document (resume could
advance a human-review step into the wrong locale). server_default='' backfills
existing rows to blank (the code falls back to the old inference for those).
Mirrors the SQLite _ensure_columns() additive migration in app/db.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a1f2c7e8b9'
down_revision: Union[str, None] = 'c3d9e7f1a2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('target_locale', sa.String(), nullable=False,
                                      server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('target_locale')
