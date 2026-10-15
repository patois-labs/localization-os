"""TM usage tracking (use_count + last_used_at on segmenttm)

Revision ID: e3f1a9c7b6d4
Revises: d2e6b8c4a1f7
Create Date: 2026-07-06 15:00:00.000000

The Translation Memory module (the /tm sidebar tab + `tm` CLI group + /api/tm
read/delete) needs to answer "when is a TM entry used and how often" — the genuine
new machinery. This adds two columns to segmenttm:

  use_count     — INTEGER NOT NULL DEFAULT 0. Bumped by ONE in-place UPDATE per
                  applied TM hit at every APPLY site (translate segmented 100%,
                  workflow step-0 100%, apply-tm-to-document 100%). Fuzzy/semantic
                  SUGGESTIONS are NOT counted (only a target that is actually
                  written to a segment increments it — see tm_module.bump_usage).
  last_used_at  — DATETIME NULL. The timestamp of the most recent application; NULL
                  on a never-used entry (the "unused since X" / "never used"
                  lifecycle filters key off it).

All additive: every existing SegmentTM row backfills use_count 0 + last_used_at
NULL (an as-yet-unused entry), byte-identical to the pre-module behaviour (the
translate path never read these columns before). No new table, no FK — the entry
model itself carries the counters (the entry is the unit of reuse; per-project
usage attribution is a documented DEFER and would need a side-table later).

Mirrors the app/db.py _ensure_columns() additive-column pattern for the SQLite
init_db path. Runs clean on BOTH SQLite (batch_alter_table) and Postgres (native
ALTER ADD COLUMN with a server_default so the NOT NULL backfill is atomic).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f1a9c7b6d4'
down_revision: Union[str, None] = 'd2e6b8c4a1f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('segmenttm', schema=None) as batch_op:
        # server_default '0' so existing rows backfill atomically under NOT NULL on
        # Postgres; the ORM default (0) governs new rows in the app.
        batch_op.add_column(sa.Column('use_count', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('last_used_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('segmenttm', schema=None) as batch_op:
        batch_op.drop_column('last_used_at')
        batch_op.drop_column('use_count')
