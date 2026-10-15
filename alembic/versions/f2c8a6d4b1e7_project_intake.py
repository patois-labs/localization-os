"""connected projects: project.intake

Revision ID: f2c8a6d4b1e7
Revises: c2a8e4f6b1d3
Create Date: 2026-10-13 20:00:00.000000

One additive column (no new table):

  project.intake   VARCHAR NOT NULL DEFAULT 'upload': where the project's content comes from,
                   'upload' (every existing row, byte-identical to before) or 'connected'
                   (pulled from a connected system; uploads are hidden and refused).
                   Mirrors models.Project.intake and the app/db.py _ADDED_COLUMNS shim.

Downgrade: NOT a batch rebuild. The project table carries the expression-based unique index
ux_project_inbox_key (coalesce(org_id,-1), inbox_key), which SQLAlchemy's SQLite batch mode
cannot reflect, so a batch drop_column table rebuild silently drops it. SQLite >= 3.35 has a
native ALTER TABLE ... DROP COLUMN, which keeps every index (the 2a98273c3c99 and
e4b7c1a9d2f6 precedent); Postgres drops the column in place. Guard: verify_project_sources
test_g downgrade round-trip and verify_migrations downgrade index check.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f2c8a6d4b1e7'
down_revision: Union[str, None] = 'c2a8e4f6b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('intake', sa.String(), nullable=False, server_default='upload')
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        # Native DROP COLUMN (SQLite >= 3.35) rather than a batch rebuild, which would
        # silently drop project's expression-based ux_project_inbox_key index.
        op.execute('ALTER TABLE project DROP COLUMN intake')
    else:
        op.drop_column('project', 'intake')
