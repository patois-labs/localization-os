"""engine reliability: restart resume, step-visit idempotency, transient-retry backoff

Revision ID: b4d8e2f6a9c1
Revises: b8d4f2a6c1e9
Create Date: 2026-10-22 23:30:00.000000

S1 of the engine redesign (ADR 0090). Additive nullable columns plus one unique index:

  workflowrun.owner           VARCHAR NULL   the execution holding the run right now
  workflowrun.heartbeat_at    DATETIME NULL  renewed about every 30 s while it executes
  workflowrun.resume_count    INTEGER NULL   restart resumes so far
  steprun.idempotency_key     VARCHAR NULL   "<run>:<cursor>:<seq>" of a completed visit
  steprun.checkpoint          JSON NULL      resumable run state right after that visit
  job.retries                 INTEGER NULL   transient retries so far
  jobqueue.not_before         DATETIME NULL  retry backoff: not claimable before this
  jobqueue.last_error         VARCHAR NULL   the transient error behind the latest retry
  ux_steprun_idempotency_key  UNIQUE (steprun.idempotency_key); NULLs never collide

Every existing row is NULL, so behaviour is unchanged until new code writes them.
Mirrors app/models.py and the app/db.py _ADDED_COLUMNS / _UNIQUE_INDEXES shim.

Downgrade: native ALTER TABLE ... DROP COLUMN on SQLite (a batch rebuild would drop
expression-based indexes; the f2c8a6d4b1e7 precedent).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b4d8e2f6a9c1'
down_revision: Union[str, None] = 'b8d4f2a6c1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('workflowrun', 'owner', sa.String()),
    ('workflowrun', 'heartbeat_at', sa.DateTime()),
    ('workflowrun', 'resume_count', sa.Integer()),
    ('steprun', 'idempotency_key', sa.String()),
    ('steprun', 'checkpoint', sa.JSON()),
    ('job', 'retries', sa.Integer()),
    ('jobqueue', 'not_before', sa.DateTime()),
    ('jobqueue', 'last_error', sa.String()),
)


def _existing(table):
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return None
    return {c['name'] for c in insp.get_columns(table)}


def upgrade() -> None:
    for table, name, type_ in _COLUMNS:
        have = _existing(table)
        if have is None or name in have:
            continue
        op.add_column(table, sa.Column(name, type_, nullable=True))
    if _existing('steprun') is not None:
        idx = {i['name'] for i in sa.inspect(op.get_bind()).get_indexes('steprun')}
        if 'ux_steprun_idempotency_key' not in idx:
            op.create_index('ux_steprun_idempotency_key', 'steprun', ['idempotency_key'],
                            unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _existing('steprun') is not None:
        idx = {i['name'] for i in sa.inspect(bind).get_indexes('steprun')}
        if 'ux_steprun_idempotency_key' in idx:
            op.drop_index('ux_steprun_idempotency_key', table_name='steprun')
    for table, name, _type in reversed(_COLUMNS):
        have = _existing(table)
        if have is None or name not in have:
            continue
        if bind.dialect.name == 'sqlite':
            op.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
        else:
            op.drop_column(table, name)
