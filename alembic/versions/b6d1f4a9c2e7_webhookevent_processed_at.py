"""webhookevent.processed_at dedup settlement marker (D4)

Revision ID: b6d1f4a9c2e7
Revises: a7d2e5c9b3f4
Create Date: 2026-07-07 00:00:00.000000

Adds `processed_at` to the connector webhook idempotency ledger (webhookevent).

Audit finding D4: the dedup previously
keyed retryability on `job_id IS NULL`, but the DEFAULT Jira button-trigger posture
records PENDING and enqueues NO job, so job_id stayed NULL forever and every replay
of one captured, validly-signed POST was re-accepted as fresh (unbounded replay).
`processed_at` marks an event SETTLED (accepted-and-recorded) regardless of whether
a job was enqueued; dedup keys on THAT. An event recorded but never settled (a crash
between record_event and settle_event) keeps processed_at NULL and stays retryable,
so the crash-window recovery is preserved.

One-time backfill: existing ledger rows were ALREADY accepted, so they are settled
(processed_at = received_at). Hand-written (no autogenerate) off the qe-gate head
a7d2e5c9b3f4, same convention as the sibling webhook_event revision b3d8f1a6c2e9.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6d1f4a9c2e7'
down_revision: Union[str, None] = 'a7d2e5c9b3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('webhookevent',
                  sa.Column('processed_at', sa.DateTime(), nullable=True))
    # Existing rows were already accepted -> settled. New (post-migration)
    # crash-window rows stay NULL = retryable.
    op.execute("UPDATE webhookevent SET processed_at = received_at "
               "WHERE processed_at IS NULL")


def downgrade() -> None:
    op.drop_column('webhookevent', 'processed_at')
