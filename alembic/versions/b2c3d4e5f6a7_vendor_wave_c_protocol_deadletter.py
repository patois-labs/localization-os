"""vendor Wave C: transport protocol seam + dead-letter/backoff (interop R1 + R5)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-08 21:00:00.000000

Four additive columns across two tables, all NULLABLE / server-defaulted so every
legacy row is byte-identical to the pre-Wave-C behavior:

  vendor.protocol (VARCHAR DEFAULT 'native') — the transport-protocol seam (interop
    R1, Wave C seam only). Names the transport implementation a dispatch/return uses;
    `native` is today's exact behavior and the ONLY registered protocol (per-LSP
    adapters are later waves). NULL/'' reads as 'native' at the single dispatch-by-
    protocol seam. The vendor TABLE already exists from Wave A (revision
    e1f4c7a9d2b6), so this is an ALTER.

  vendordispatch.attempt_count (INTEGER DEFAULT 0) — transient-failure retries so far.
  vendordispatch.next_attempt_at (DATETIME, nullable) — earliest instant the scheduler
    may retry a RETRY_WAIT leg (exponential backoff).
  vendordispatch.last_error (VARCHAR DEFAULT '') — the SANITIZED last transient reason
    (no URLs with credentials, no secrets) rendered on the leg panels.
  vendordispatch.retry_contract (JSON, nullable) — the exact outbound contract dict held
    ONLY while a leg is RETRY_WAIT, so a scheduled retry re-sends the byte-identical
    package (rebuilding from live Segment rows is the drift contract_ids exists to
    prevent). Cleared to NULL the moment the leg reaches DISPATCHED or any terminal
    status. NULL on every non-retrying leg.

The dead-letter/backoff feature (interop R5) parks a leg in the new RETRY_WAIT status
on a TRANSIENT transport failure (network/timeout/non-2xx) and, on exhausting the
bounded backoff window, flips it to the new DEAD_LETTER status AND escalates to human
review (decision 3 — vendor unreachability never hard-fails). Both new statuses are
plain string values on the existing `status` column; no enum/CHECK to migrate.

Hand-written (no autogenerate) off the head a1b2c3d4e5f6, same convention as the
sibling additive-column revisions. Runs clean on SQLite (batch_alter_table) and
Postgres (native ALTER). The app/db.py _ADDED_COLUMNS shim carries the SAME columns so
a non-alembic SQLite boot (and the hermetic create_all test path) builds them
identically — the nullability-parity CI guard compares the two.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('vendor', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'protocol', sa.String(), nullable=False, server_default='native'))
    with op.batch_alter_table('vendordispatch', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'attempt_count', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('next_attempt_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(
            'last_error', sa.String(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('retry_contract', sa.JSON(), nullable=True))
    # No backfill: the server defaults land 'native' / 0 / '' on every legacy row, which
    # is exactly the pre-Wave-C reading at the single consumption point.


def downgrade() -> None:
    # M8 (fix round): the Wave-C statuses do not exist in the pre-Wave-C vocabulary, so a
    # downgrade must NORMALIZE any leg carrying one before the columns that describe it are
    # dropped -- otherwise a retry_wait / dead_letter row is stranded with a status the old
    # code cannot interpret. retry_wait -> dispatched (a leg still awaiting delivery reads as
    # a normal open dispatch), dead_letter -> escalated (a leg escalated to a human is the
    # pre-Wave-C ESCALATED terminal). Runs clean on SQLite + Postgres (plain UPDATE).
    op.execute(
        "UPDATE vendordispatch SET status='dispatched' WHERE status='retry_wait'")
    op.execute(
        "UPDATE vendordispatch SET status='escalated' WHERE status='dead_letter'")
    with op.batch_alter_table('vendordispatch', schema=None) as batch_op:
        batch_op.drop_column('retry_contract')
        batch_op.drop_column('last_error')
        batch_op.drop_column('next_attempt_at')
        batch_op.drop_column('attempt_count')
    with op.batch_alter_table('vendor', schema=None) as batch_op:
        batch_op.drop_column('protocol')
