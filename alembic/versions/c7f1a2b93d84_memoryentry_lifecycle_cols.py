"""memoryentry lifecycle columns (LLMemory Phase 2 — janitor + adjudication markers)

Revision ID: c7f1a2b93d84
Revises: a1c7e9b3f2d5
Create Date: 2026-08-25 21:00:00.000000

Adds THREE additive columns to `memoryentry` (SPEC-llmemory-phase2 §5.6a), all
byte-identical-on-backfill so every existing row and every pre-Phase-2 code path is
unchanged after the migration:

  memoryentry.suppressed_until — nullable DATETIME. The janitor's SUPPRESSION MARKER
                                 (§5.2): stamped when a belief is DECAYED (by last-used)
                                 or a CANDIDATE is EXPIRED. `llmemory._upsert_term`'s
                                 re-emergence branch refuses to re-mint a suppressed
                                 belief unless genuinely NEW evidence (created after this
                                 window) arrives — the fix that stops a scheduled distill
                                 from flapping a just-decayed belief straight back. It is
                                 ALSO the "aged out" discriminator (`suppressed_until IS
                                 NOT NULL`) that tells a janitor-decayed entry apart from a
                                 human `invalidate` (both leave superseded_at NULL). NULL
                                 on every legacy row = never suppressed = today's behaviour.
  memoryentry.adjudicated_at   — nullable DATETIME. The human-ADJUDICATION MARKER (§5.5):
                                 stamped on the WINNER by `resolve_conflict`. `_distill_
                                 terms`' disagreement branch short-circuits for a key with
                                 a live adjudication (no newer post-adjudication evidence),
                                 so distill never flips a human's chosen winner back or
                                 mints CONFLICTED duplicates of the losers. NULL = never
                                 adjudicated = today's behaviour.
  memoryentry.adjudicated_by   — VARCHAR DEFAULT '' — the actor email of the human who
                                 resolved the conflict (a NEW attribution column that
                                 gdpr.erase_user sweeps, §5.6). '' on every legacy row.

WHY A SHIM ENTRY TOO (unlike the b3e7c1d94a26 TABLES): these are new COLUMNS on an
EXISTING table, which create_all() will never add — so app/db.py:_ADDED_COLUMNS carries
the SAME three columns for a non-alembic SQLite boot (the f2a9c4d81b73_workflowstep_
memory_mode precedent). The nullability-parity CI guard (tests/verify_migrations.py)
compares this migration's output against the model column-for-column.

Hand-written (no autogenerate) off the head a1c7e9b3f2d5 (Train A llmemory_suggestions;
re-chained from d4e9f1a7c2b6 job_queue at the Train-A rebase, §9), same convention as
the sibling additive-column revisions. Runs clean on SQLite (batch_alter_table) and
Postgres (native ALTER).

SQLITE EXPRESSION-INDEX TRAP (warning for any FUTURE migration on this table): memoryentry
carries an EXPRESSION-based index, ix_memoryentry_scope (coalesce(org_id,-1), customer_id,
coalesce(project_id,-1)). SQLAlchemy CANNOT reflect an expression index, so any
`op.batch_alter_table('memoryentry')` rebuilds the table by reflection and SILENTLY DROPS it.
This revision re-creates it after BOTH its upgrade and downgrade batches (`_recreate_scope_
index`); any future batch alter here MUST do the same. Train A's ix_memorysuggestion_scope has
the identical shape and the same trap (see a1c7e9b3f2d5).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7f1a2b93d84'
down_revision: Union[str, None] = 'a1c7e9b3f2d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The NON-unique FUNCTIONAL scope index, exactly as b3e7c1d94a26 (the LLMemory Phase-1 tables)
# first created it. Kept here as a constant so BOTH upgrade and downgrade re-create the SAME
# shape after their batch rebuilds (see the SQLite reflection trap below).
_SCOPE_INDEX_COLS = [
    sa.text('coalesce(org_id, -1)'),
    sa.text('customer_id'),
    sa.text('coalesce(project_id, -1)'),
]


def _recreate_scope_index() -> None:
    """Re-create ix_memoryentry_scope after a batch rebuild.

    SQLITE BATCH REBUILD TRAP: on SQLite `op.batch_alter_table` copies the table to a new one
    and recreates its indexes by REFLECTION — but ix_memoryentry_scope is EXPRESSION-based
    (coalesce(org_id,-1), customer_id, coalesce(project_id,-1)) and SQLAlchemy cannot reflect
    an expression index ("SAWarning: Skipped unsupported reflection of expression-based
    index"), so the batch silently DROPS it and never puts it back. We re-create it explicitly.

    IDEMPOTENT (`if_not_exists=True`) so this is safe on BOTH dialects and on any re-run: on
    Postgres the native ALTER never dropped the index (no table rebuild), so the re-create is a
    no-op; on SQLite the batch dropped it, so the re-create restores it. A dev DB that already
    stamped this revision before the fix keeps whatever index state it has — re-running the
    migration is not required, but IS harmless here."""
    op.create_index(
        'ix_memoryentry_scope', 'memoryentry', _SCOPE_INDEX_COLS, unique=False, if_not_exists=True
    )


def upgrade() -> None:
    with op.batch_alter_table('memoryentry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('suppressed_until', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('adjudicated_at', sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column('adjudicated_by', sa.String(), nullable=False, server_default='')
        )
    # No backfill beyond the '' server default: NULL suppressed_until/adjudicated_at already
    # read as "never suppressed / never adjudicated" at every consumption point.
    _recreate_scope_index()  # the batch above dropped it on SQLite (expression-index trap)


def downgrade() -> None:
    with op.batch_alter_table('memoryentry', schema=None) as batch_op:
        batch_op.drop_column('adjudicated_by')
        batch_op.drop_column('adjudicated_at')
        batch_op.drop_column('suppressed_until')
    _recreate_scope_index()  # the batch above dropped it on SQLite (expression-index trap)
