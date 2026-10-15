"""key image (per-string in-context image for vision-model translation context, Batch 14 T1)

Revision ID: f7b3c1d9e2a4
Revises: e2b7d1f4a9c3
Create Date: 2026-08-27 15:00:00.000000

Adds the Batch-14 per-string image-context table:

  keyimage (new table) -- one row per raw in-context image attached to a StringKey. The
      pixels ride the translation call to a vision-capable engine as CONTEXT (never OCR'd).
      key_id -> stringkey.id and a DENORMALIZED project_id -> project.id tenant anchor
      (set server-side, LAW-IMG-2); a server-derived flat blob_key ("keyimage_<uuid>.png"),
      the raw-upload content_hash (within-key dedup), the STORED (downscaled PNG) rendition
      metadata (mime/fmt/byte_size/width/height) + the original dims, caption, ordinal,
      is_primary/send_all_images selection flags, the orphan-free `ready` flag (LAW-IMG-16),
      uploaded_by (GDPR sweep) + uploaded_at, and deleted_at (soft delete).

All additive: the new table is empty on upgrade and no existing table is touched. Mirrors
the SQLite create_all() new-table + Postgres-alembic-authority pattern (app/db.py) and
models.KeyImage; the column-completeness + nullability-parity CI guards
(tests/verify_migrations.py) assert this revision's column set + nullability equal the
model's. Hand-written (no autogenerate) off the head e2b7d1f4a9c3, same convention as the
sibling additive revisions. Server_defaults on the NOT-NULL columns mirror the model's
field defaults so create_all and the migration agree (the Project.project_type lesson).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7b3c1d9e2a4'
down_revision: Union[str, None] = 'e2b7d1f4a9c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'keyimage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('blob_key', sa.String(), nullable=False, server_default=''),
        sa.Column('content_hash', sa.String(), nullable=False, server_default=''),
        sa.Column('mime', sa.String(), nullable=False, server_default='image/png'),
        sa.Column('fmt', sa.String(), nullable=False, server_default='png'),
        sa.Column('orig_fmt', sa.String(), nullable=False, server_default=''),
        sa.Column('byte_size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('width', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('height', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('orig_width', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('orig_height', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('caption', sa.String(), nullable=False, server_default=''),
        sa.Column('ordinal', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('send_all_images', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('ready', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('uploaded_by', sa.String(), nullable=False, server_default=''),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['key_id'], ['stringkey.id']),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_keyimage_key_id', 'keyimage', ['key_id'], unique=False)
    op.create_index('ix_keyimage_project_id', 'keyimage', ['project_id'], unique=False)
    op.create_index('ix_keyimage_content_hash', 'keyimage', ['content_hash'], unique=False)
    # Partial-unique DB backstops for the atomic cap + dedup (LAW-IMG-11, H-1): unique over
    # the LIVE rows only (WHERE deleted_at IS NULL, the ux_stringkey_identity idiom) so a
    # soft-deleted row never blocks a re-attach. Both directions (sqlite_where +
    # postgresql_where) so the create_all/SQLite path and the alembic/Postgres path agree.
    op.create_index(
        'ux_keyimage_key_hash', 'keyimage', ['key_id', 'content_hash'], unique=True,
        sqlite_where=sa.text('deleted_at IS NULL'),
        postgresql_where=sa.text('deleted_at IS NULL'),
    )
    op.create_index(
        'ux_keyimage_key_ordinal', 'keyimage', ['key_id', 'ordinal'], unique=True,
        sqlite_where=sa.text('deleted_at IS NULL'),
        postgresql_where=sa.text('deleted_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('ux_keyimage_key_ordinal', table_name='keyimage')
    op.drop_index('ux_keyimage_key_hash', table_name='keyimage')
    op.drop_index('ix_keyimage_content_hash', table_name='keyimage')
    op.drop_index('ix_keyimage_project_id', table_name='keyimage')
    op.drop_index('ix_keyimage_key_id', table_name='keyimage')
    op.drop_table('keyimage')
