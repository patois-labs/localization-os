"""TM content type — per-content-type domain scoping for translation memory (phase 3)

Revision ID: f2a9d4c1b7e8
Revises: e5d9c3b7a2f4
Create Date: 2026-07-08 18:00:00.000000

Adds the ContentType phase-3 schema (SPEC-content-types §4 + §6): a nullable, indexed
FK on the three TM stores + the Connection, so a TM row carries the content type it was
written under and reads hard-scope to own-type + the shared "General" (NULL) bucket.

  segmenttm.content_type_id        — nullable FK: stamped from the owning project at
  fulltexttm.content_type_id         TM-write time (write_back / ft_store / import_tmx).
  segmentembedding.content_type_id   The embedding row denormalizes its parent SegmentTM's
                                     value. NULL = the shared General bucket (byte-
                                     identical to today — every existing row backfills
                                     NULL, and an untyped project reads exactly the NULL
                                     set). Read scope: own-type + NULL; see tm._tm_scope.
  connection.content_type_id       — nullable FK: the default content type copied onto
                                     the auto-created sync project in store._ensure_project.

All additive: every existing row backfills the new column to NULL (byte-identical to the
pre-content-type behaviour). Mirrors the SQLite _ensure_columns() additive-column pattern
in app/db.py.

Hand-written (no autogenerate) off the phase-2 head e5d9c3b7a2f4, same convention as
e5d9c3b7a2f4_glossary_content_type.py. Runs clean on BOTH SQLite (batch_alter_table) and
Postgres (native ALTER). The nullable-FK columns are added as PLAIN nullable columns (no
ForeignKeyConstraint in the ALTER) exactly like the phase-1/2 revisions: the SQLModel
classes carry the real FK (so create_all + a fresh Postgres build it and SQLite's PRAGMA
foreign_keys=ON enforces it), while the SQLite-safe ALTER stays a bare ADD COLUMN. The
single-column content_type_id indexes match app/db.py _INDEXES by name.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a9d4c1b7e8'
down_revision: Union[str, None] = 'e5d9c3b7a2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('segmenttm', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_type_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('fulltexttm', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_type_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('segmentembedding', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_type_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('connection', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_type_id', sa.Integer(), nullable=True))
    op.create_index('ix_segmenttm_content_type', 'segmenttm', ['content_type_id'])
    op.create_index('ix_fulltexttm_content_type', 'fulltexttm', ['content_type_id'])
    op.create_index('ix_segmentembedding_content_type', 'segmentembedding',
                    ['content_type_id'])
    op.create_index('ix_connection_content_type', 'connection', ['content_type_id'])


def downgrade() -> None:
    op.drop_index('ix_connection_content_type', table_name='connection')
    op.drop_index('ix_segmentembedding_content_type', table_name='segmentembedding')
    op.drop_index('ix_fulltexttm_content_type', table_name='fulltexttm')
    op.drop_index('ix_segmenttm_content_type', table_name='segmenttm')
    with op.batch_alter_table('connection', schema=None) as batch_op:
        batch_op.drop_column('content_type_id')
    with op.batch_alter_table('segmentembedding', schema=None) as batch_op:
        batch_op.drop_column('content_type_id')
    with op.batch_alter_table('fulltexttm', schema=None) as batch_op:
        batch_op.drop_column('content_type_id')
    with op.batch_alter_table('segmenttm', schema=None) as batch_op:
        batch_op.drop_column('content_type_id')
