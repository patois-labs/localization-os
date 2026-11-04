"""jobqueue.serial_key: one running TM import per project (ADR 0105)

Revision ID: e5a1c9d3b7f2
Revises: d6b3f8a2c4e1
Create Date: 2026-10-24 18:00:00.000000

S7c found seven chunk uploads of one TMX running as seven tm_import jobs at once on Postgres (web
and worker processes); four failed on ux_segmenttm_upsert_key and most of the units were lost.
This revision is the queue half of the fix (the write half is tm._import_tmx_write):

  jobqueue.serial_key            VARCHAR NULL   "tm_import:<project id>" for a TM import, NULL for
                                                every other kind
  ux_jobqueue_serial_running     UNIQUE (serial_key) WHERE state = 'running': the hard guarantee,
                                                at most one running row per key (NULL keys never
                                                collide)
  ix_jobqueue_serial_active      (serial_key) WHERE serial_key IS NOT NULL AND state IN
                                                ('pending', 'running'): the claim's per-key look-up

The claim (app/ports/jobqueue.py) skips a pending row whose key has a running row or an older
pending row, so one project's imports run one at a time in submission order, and a waiting import
holds no worker slot. Plain SQL partial indexes, valid on SQLite and Postgres.

BACKFILL: pending and running tm_import queue rows get their key from the payload's project id (so
an import queued before the upgrade is serialized too). If the pre-fix state left several RUNNING
rows of one project, the oldest keeps the key and the others stay NULL (they finish as they would
have; the unique index must be creatable). Rows of any other kind and finished rows stay NULL.

Mirrors models.JobQueue.serial_key and the app/db.py _ADDED_COLUMNS / _INDEXES / _UNIQUE_INDEXES shim.

Downgrade: drop both indexes, then native ALTER TABLE ... DROP COLUMN (a SQLite batch rebuild would
drop expression indexes; the 2a98273c3c99 precedent).
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e5a1c9d3b7f2'
down_revision: Union[str, None] = 'd6b3f8a2c4e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table):
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return None
    return {c['name'] for c in insp.get_columns(table)}


def _key(payload):
    """The serial key of a tm_import payload (a dict on Postgres JSON, text on SQLite)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    try:
        return 'tm_import:%d' % int(payload.get('project_id'))
    except (TypeError, ValueError):
        return None


def upgrade() -> None:
    have = _columns('jobqueue')
    if have is None:
        return
    if 'serial_key' not in have:
        op.add_column('jobqueue', sa.Column('serial_key', sa.String(), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, payload, state FROM jobqueue "
        "WHERE kind = 'tm_import' AND state IN ('pending', 'running') ORDER BY id"
    )).fetchall()
    running_taken = set()
    for qid, payload, state in rows:
        key = _key(payload)
        if key is None:
            continue
        if state == 'running':
            if key in running_taken:
                continue
            running_taken.add(key)
        bind.execute(sa.text("UPDATE jobqueue SET serial_key = :k WHERE id = :i"),
                     {'k': key, 'i': qid})
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_jobqueue_serial_running "
               "ON jobqueue (serial_key) WHERE state = 'running'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobqueue_serial_active ON jobqueue (serial_key) "
               "WHERE serial_key IS NOT NULL AND state IN ('pending', 'running')")


def downgrade() -> None:
    have = _columns('jobqueue')
    if have is None:
        return
    op.execute("DROP INDEX IF EXISTS ux_jobqueue_serial_running")
    op.execute("DROP INDEX IF EXISTS ix_jobqueue_serial_active")
    if 'serial_key' in have:
        op.execute('ALTER TABLE jobqueue DROP COLUMN serial_key')
