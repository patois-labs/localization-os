"""key-based project type: project_type, stringkey, keychangelog, segment key columns

Revision ID: b6f1d0a7c34e
Revises: a4e7d2c8b91f
Create Date: 2026-08-03 09:00:00.000000

SPEC-key-based-projects section 2 -- the WHOLE schema half of Batch 4 in ONE revision:

  project.project_type        NOT NULL, server_default 'document'. The content_type_id
                              idiom: a new dimension whose default leaves every existing
                              path unchanged. NOT NULL because "which kind of project is
                              this" is a value, never an absence -- the read sites are
                              predicates that must never have to reason about NULL.
  stringkey                   the key IDENTITY table (metadata only; values are Segments).
  keychangelog                the Delta API's append-only feed WITH TOMBSTONES.
  segment.key_id              nullable FK -> stringkey. NULL on every existing row.
  segment.plural_category     nullable CLDR form name. NULL on every existing row.

TWO PARTIAL UNIQUE INDEXES, which are the load-bearing part of this revision:

  ux_stringkey_identity        (project_id, coalesce(branch_id,0), namespace, name,
                               context) WHERE deleted_at IS NULL
      Key identity is the one decision that cannot be refactored later (research 01
      section 9.6), so the FULL tuple is enforced from day one -- including branch_id,
      which is always NULL in v1 and exists precisely so branching needs no identity
      change later. PARTIAL on deleted_at IS NULL because keys are SOFT-deleted (the
      delta feed's tombstones require it): a hard unique index would let a deleted key
      block re-creating its own name forever.

  ux_segment_key_locale_form   (key_id, target_locale, coalesce(plural_category,''))
                               WHERE key_id IS NOT NULL
      One value row per (key, locale, plural form). PARTIAL on key_id IS NOT NULL so
      DOCUMENT segments -- every row that exists today -- are not in the index at all
      and keep ux_segment_doc_locale_order as their only uniqueness rule. coalesce()
      because SQL treats NULLs as DISTINCT, which would otherwise let two non-plural
      rows for the same (key, locale) coexist.

  Partial + expression indexes work on BOTH SQLite and Postgres (the two engines CI
  runs), which is why the identity rule can live in DDL instead of in application code.

WHY NO DATA MIGRATION: there is nothing to migrate. Zero keys exist; every project
becomes 'document'; every segment's two new columns are NULL. A fresh install that
never creates a key-based project is behaviourally identical to a4e7d2c8b91f, which is
the invariant tests/verify_keys_inertness.py pins.

Hand-written (no autogenerate) off the head a4e7d2c8b91f, same convention as its
siblings. Runs clean on SQLite (batch_alter_table) and Postgres (native ALTER); the
app/db.py shim carries the SAME columns and the SAME two unique indexes so a
non-alembic SQLite boot builds an identical schema (the nullability-parity CI guard in
tests/verify_migrations.py compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6f1d0a7c34e'
down_revision: Union[str, None] = 'a4e7d2c8b91f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. project.project_type -------------------------------------------------
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_type', sa.String(), nullable=False,
                                      server_default='document'))

    # --- 2. stringkey -------------------------------------------------------------
    op.create_table(
        'stringkey',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=True),
        sa.Column('namespace', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('context', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('char_limit', sa.Integer(), nullable=True),
        sa.Column('is_plural', sa.Boolean(), nullable=False),
        sa.Column('plural_arg_name', sa.String(), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('source_file', sa.String(), nullable=False),
        sa.Column('is_hidden', sa.Boolean(), nullable=False),
        sa.Column('is_archived', sa.Boolean(), nullable=False),
        sa.Column('custom', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('translations_modified_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_stringkey_project_id', 'stringkey', ['project_id'],
                    unique=False)
    # The identity tuple. sa.text() for the coalesce() expression member; the WHERE
    # goes through the dialect kwargs so the SAME call emits a partial index on both
    # backends.
    op.create_index('ux_stringkey_identity', 'stringkey',
                    ['project_id', sa.text('coalesce(branch_id, 0)'), 'namespace',
                     'name', 'context'],
                    unique=True,
                    sqlite_where=sa.text('deleted_at IS NULL'),
                    postgresql_where=sa.text('deleted_at IS NULL'))

    # --- 3. keychangelog ----------------------------------------------------------
    # key_id carries NO ForeignKeyConstraint ON PURPOSE: a tombstone must outlive the
    # key it describes.
    op.create_table(
        'keychangelog',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('entity', sa.String(), nullable=False),
        sa.Column('key_id', sa.Integer(), nullable=False),
        sa.Column('namespace', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('target_locale', sa.String(), nullable=True),
        sa.Column('plural_category', sa.String(), nullable=False),
        sa.Column('op', sa.String(), nullable=False),
        sa.Column('changed_at', sa.DateTime(), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_keychangelog_project_id', 'keychangelog', ['project_id'],
                    unique=False)
    # The delta read is exactly `WHERE project_id = ? AND id > ? ORDER BY id`.
    op.create_index('ix_keychangelog_project_cursor', 'keychangelog',
                    ['project_id', 'id'], unique=False)

    # --- 4. segment.key_id / segment.plural_category -------------------------------
    with op.batch_alter_table('segment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('plural_category', sa.String(), nullable=True))
        batch_op.create_foreign_key('fk_segment_key_id', 'stringkey',
                                    ['key_id'], ['id'])
    op.create_index('ix_segment_key_id', 'segment', ['key_id'], unique=False)
    op.create_index('ux_segment_key_locale_form', 'segment',
                    ['key_id', 'target_locale',
                     sa.text("coalesce(plural_category, '')")],
                    unique=True,
                    sqlite_where=sa.text('key_id IS NOT NULL'),
                    postgresql_where=sa.text('key_id IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('ux_segment_key_locale_form', table_name='segment')
    op.drop_index('ix_segment_key_id', table_name='segment')
    with op.batch_alter_table('segment', schema=None) as batch_op:
        batch_op.drop_constraint('fk_segment_key_id', type_='foreignkey')
        batch_op.drop_column('plural_category')
        batch_op.drop_column('key_id')
    op.drop_index('ix_keychangelog_project_cursor', table_name='keychangelog')
    op.drop_index('ix_keychangelog_project_id', table_name='keychangelog')
    op.drop_table('keychangelog')
    op.drop_index('ux_stringkey_identity', table_name='stringkey')
    op.drop_index('ix_stringkey_project_id', table_name='stringkey')
    op.drop_table('stringkey')
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('project_type')
