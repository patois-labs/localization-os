"""glossary content type — per-customer domain scoping for glossaries (phase 2)

Revision ID: e5d9c3b7a2f4
Revises: d4c8b2a6f1e3
Create Date: 2026-07-08 15:00:00.000000

Adds the ContentType phase-2 schema (SPEC-content-types §3): a nullable FK on the
first-class Glossary so a glossary can be scoped to one ContentType.

  glossary.content_type_id       — nullable FK: NULL = the shared "General" bucket
                                   (byte-identical to today — every existing glossary
                                   backfills to NULL and applies to every project);
                                   non-NULL = the glossary is offered/auto-attached
                                   only to projects of that content type.

Additive: every existing Glossary backfills the new column to NULL (byte-identical to
the pre-content-type behaviour). Mirrors the SQLite _ensure_columns() additive-column
pattern in app/db.py.

Hand-written (no autogenerate) off the phase-1 head d4c8b2a6f1e3, same convention as
d4c8b2a6f1e3_content_types.py. Runs clean on BOTH SQLite (batch_alter_table) and
Postgres (native ALTER). The nullable-FK column is added as a PLAIN nullable column
(no ForeignKeyConstraint in the ALTER) exactly like the glossary-module + phase-1
revisions added their nullable-FK columns: the SQLModel class carries the real FK (so
create_all + a fresh Postgres build it and SQLite's PRAGMA foreign_keys=ON enforces
it), while the SQLite-safe ALTER stays a bare ADD COLUMN.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5d9c3b7a2f4'
down_revision: Union[str, None] = 'd4c8b2a6f1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('glossary', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_type_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('glossary', schema=None) as batch_op:
        batch_op.drop_column('content_type_id')
