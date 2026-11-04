"""semantic TM examples: per-customer and per-project switch

Revision ID: a7e3c9d1f4b2
Revises: f2c8a6d4b1e7
Create Date: 2026-10-22 12:00:00.000000

Two additive nullable columns (no new table):

  customer.semantic_examples   VARCHAR NULL: NULL = use the install default, 'on' or 'off'.
  project.semantic_examples    VARCHAR NULL: NULL = use the customer (then install) value,
                               'on' or 'off'.

Every existing row is NULL, so behaviour is byte-identical until someone sets a value.
Mirrors models.Customer/Project.semantic_examples and the app/db.py _ADDED_COLUMNS shim.

Downgrade: native ALTER TABLE ... DROP COLUMN on SQLite (a batch rebuild would drop the
project table's expression-based ux_project_inbox_key index; the f2c8a6d4b1e7 precedent).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7e3c9d1f4b2'
down_revision: Union[str, None] = 'f2c8a6d4b1e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('semantic_examples', sa.String(), nullable=True))
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('semantic_examples', sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for table in ('project', 'customer'):
        if bind.dialect.name == 'sqlite':
            op.execute(f'ALTER TABLE {table} DROP COLUMN semantic_examples')
        else:
            op.drop_column(table, 'semantic_examples')
