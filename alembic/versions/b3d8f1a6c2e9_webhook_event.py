"""connector webhook idempotency ledger (WebhookEvent)

Revision ID: b3d8f1a6c2e9
Revises: a2b7c4e9f1d6
Create Date: 2026-07-06 01:00:00.000000

Creates the `webhookevent` table backing the generalized connector webhook intake
(CONNECTOR_FRAMEWORK_DESIGN §3.4). One row per (connection, event id) the intake
has ALREADY accepted, so a provider RETRY is deduped (the second delivery ACKs 200
but enqueues nothing).

The UNIQUE (connection_id, event_id) index is what makes intake race-safe: two
concurrent deliveries of the same event both try to INSERT; exactly one wins, the
other hits IntegrityError and is treated as a dup (webhooks.record_event). Mirrors
the SQLite path: SQLModel.metadata.create_all builds the table and
app/db.py `_ensure_indexes` creates ux_webhookevent_conn_event.

Hand-written (no autogenerate) off the pair-providers head a2b7c4e9f1d6 to avoid
the spurious drop_index noise autogenerate emits for the app/db.py hot-path
indexes — same pattern as the sibling revisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3d8f1a6c2e9'
down_revision: Union[str, None] = 'a2b7c4e9f1d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'webhookevent',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False, server_default=''),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connection.id']),
    )
    op.create_index('ix_webhookevent_connection_id', 'webhookevent', ['connection_id'])
    op.create_index('ix_webhookevent_event_id', 'webhookevent', ['event_id'])
    # The idempotency guarantee: one accepted event per (connection, event id).
    op.create_index('ux_webhookevent_conn_event', 'webhookevent',
                    ['connection_id', 'event_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_webhookevent_conn_event', table_name='webhookevent')
    op.drop_index('ix_webhookevent_event_id', table_name='webhookevent')
    op.drop_index('ix_webhookevent_connection_id', table_name='webhookevent')
    op.drop_table('webhookevent')
