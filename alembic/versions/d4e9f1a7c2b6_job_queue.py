"""durable job-queue ledger (JobQueue) -- sprint Train S (STRETCH)

Revision ID: d4e9f1a7c2b6
Revises: a1f4c8b2d6e9
Create Date: 2026-08-24 14:00:00.000000

Creates the `jobqueue` table backing the DURABLE, DB-backed job queue selected by the
host-only `job_queue_backend="db"` Setting (app/ports/jobqueue.py). One row per dispatched
Job. A worker CLAIMS a row with an atomic `UPDATE...WHERE state='pending'` compare-and-swap
(so two workers -- even across nodes -- can never double-claim), stamps a worker + lease, and
on restart a `running` row whose lease EXPIRED returns to `pending` (its work re-runs, not
silently failed). `kind`/`payload` mirror the Job at enqueue time for the HA seam; the
authoritative execution still runs off jobs._execute reading the Job row.

DORMANT on the default `job_queue_backend="memory"` backend: the in-memory adapter never
touches this table, so a default install is byte-identical to pre-Train-S -- the table is
created but stays empty.

Column set + nullability MIRROR the app/models.py `JobQueue` model EXACTLY (the
alembic-head vs create_all authority parity, verify_alembic_authority (a)): SQLModel's
create_all builds this table on the IS_TEST / SQLite-shim path, this migration builds it on
the alembic path, and the two must not diverge. NO foreign key on job_id (a queue entry may
outlive a purged/erased Job without an FK cascade). Hand-written (no autogenerate) off the Q3
#74 head a1f4c8b2d6e9 -- same pattern as the sibling create-table revisions.

downgrade drops the indexes then the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e9f1a7c2b6'
down_revision: Union[str, None] = 'a1f4c8b2d6e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'jobqueue',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('state', sa.String(), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('worker', sa.String(), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
        sa.Column('enqueued_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    # Match the model's index=True fields (SQLModel/create_all names them ix_<table>_<col>).
    op.create_index('ix_jobqueue_job_id', 'jobqueue', ['job_id'])
    op.create_index('ix_jobqueue_state', 'jobqueue', ['state'])
    op.create_index('ix_jobqueue_enqueued_at', 'jobqueue', ['enqueued_at'])


def downgrade() -> None:
    op.drop_index('ix_jobqueue_enqueued_at', table_name='jobqueue')
    op.drop_index('ix_jobqueue_state', table_name='jobqueue')
    op.drop_index('ix_jobqueue_job_id', table_name='jobqueue')
    op.drop_table('jobqueue')
