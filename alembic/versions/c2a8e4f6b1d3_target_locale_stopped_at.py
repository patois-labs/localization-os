"""connected projects: a bound project's stopped target language

Revision ID: c2a8e4f6b1d3
Revises: e1b7c3a9d5f2
Create Date: 2026-10-13 21:00:00.000000

One additive column (no new table):

  targetlocale.stopped_at   nullable DATETIME. A target language of a project that has a content
                            source is never deleted when a person removes it: the row stays and
                            this timestamp marks it as stopped (runs stop pulling, translating
                            and writing it). NULL = active, which is every existing row.

Downgrade: NOT a batch rebuild. A SQLite batch drop_column recreates the whole table and keeps
only the indexes SQLAlchemy can reflect (any expression index added to targetlocale later would
silently vanish, the ux_project_inbox_key landmine). SQLite >= 3.35 has a native ALTER TABLE ...
DROP COLUMN that leaves every index in place (the 2a98273c3c99 precedent); Postgres drops the
column in place.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c2a8e4f6b1d3'
down_revision: Union[str, None] = 'e1b7c3a9d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('targetlocale', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stopped_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        # Native DROP COLUMN (SQLite >= 3.35) rather than a batch table rebuild.
        op.execute('ALTER TABLE targetlocale DROP COLUMN stopped_at')
    else:
        op.drop_column('targetlocale', 'stopped_at')
