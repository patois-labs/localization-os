"""TM-matching locks: segment.lock_band + segment.lock_hard

Revision ID: c7e1a9f4b2d6
Revises: e1f4a9c72b60
Create Date: 2026-08-07 09:00:00.000000

PROPOSAL-tm-matching-workflow-step §8 (schema half of the lock state). A `tm_match`
workflow step fills a qualifying TM match and LOCKS the segment; the lock is enforced
at the sole write seam (quality.commit_mt_output) so later steps never overwrite it.

  segment.lock_band   nullable VARCHAR. NULL on every existing row = unlocked =
                      byte-identical. "ice" | "100" once a tm_match step locks a row.
  segment.lock_hard   NOT NULL, server_default '0'. Boolean value (never an absence,
                      like is_default / organization.active): True = hard lock (write
                      refused at the seam for machine origins), False = soft lock.

Provenance (supplying TM id, entry id, matched-at) rides segment.meta["tm_lock"] — no
column, the per-unit metadata JSON already exists.

WHY NO DATA MIGRATION: every existing segment stays unlocked (lock_band NULL,
lock_hard 0), which is exactly today's behaviour. The tm_locks_enabled kill switch
(default on) makes the seam enforcement inert until a segment is actually locked.

Hand-written (no autogenerate) off the head e1f4a9c72b60, same convention as its
siblings. Uses a PLAIN op.add_column / op.drop_column (NOT batch_alter_table): a native
`ALTER TABLE ADD/DROP COLUMN` on SQLite (>=3.35) and Postgres alike, with no table rebuild
-- batch mode would copy the segment table and drop its partial expression index
(ux_segment_key_locale_form), which a later downgrade's DROP INDEX then fails on. The
app/db.py shim carries the SAME two columns so a non-alembic SQLite boot builds an
identical schema (the nullability-parity CI guard in tests/verify_migrations.py
compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e1a9f4b2d6'
down_revision: Union[str, None] = 'e1f4a9c72b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PLAIN add_column (NOT batch_alter_table): a native `ALTER TABLE ADD COLUMN` on
    # both SQLite (>=3.35) and Postgres, with NO table rebuild. batch_alter_table would
    # copy the segment table and DROP the partial expression index
    # ux_segment_key_locale_form (SQLite batch mode mishandles partial indexes), which a
    # later downgrade's explicit DROP INDEX then fails on -- the verify_keys_inertness
    # regression. Pure add-column needs no rebuild, so we avoid batch entirely.
    op.add_column('segment', sa.Column('lock_band', sa.String(), nullable=True))
    op.add_column('segment', sa.Column('lock_hard', sa.Boolean(), nullable=False,
                                       server_default='0'))


def downgrade() -> None:
    # Native DROP COLUMN (SQLite >=3.35, Postgres) -- again NO batch, so the partial
    # index on segment is left intact for the migrations that own it further down.
    op.drop_column('segment', 'lock_hard')
    op.drop_column('segment', 'lock_band')
