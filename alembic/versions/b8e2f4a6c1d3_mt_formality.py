"""MT formality: customer / project / document maps and the run record (#617 item 7)

Revision ID: b8e2f4a6c1d3
Revises: d7b3e9f1a5c2
Create Date: 2026-10-08 10:00:00.000000

Four purely additive, nullable JSON columns, no backfill (ADR 0062):

  customer.formality     {language: register}, the customer default per target language
  project.formality      {target_locale: register}, the project setting
  document.formality     {target_locale: register}, the document override
  workflowrun.formality  {register, source, target_locale, label}, the register that applied

NULL everywhere on every existing row = no instruction = today's prompts, byte for byte. The SQLite
init_db path carries the same columns in app/db._ADDED_COLUMNS. Plain op.add_column (NO
batch_alter_table): the project table carries the expression index ux_project_inbox_key, which a
SQLite batch rebuild silently drops (see 2a98273c3c99).

Hand-written off the head d7b3e9f1a5c2.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e2f4a6c1d3'
down_revision: Union[str, None] = 'd7b3e9f1a5c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('customer', 'project', 'document', 'workflowrun')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column('formality', sa.JSON(), nullable=True))


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, 'formality')
