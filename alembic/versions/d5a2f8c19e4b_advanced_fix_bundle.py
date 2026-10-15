"""advanced fix bundle: staged key_id + abandoned, run heartbeat_at

Revision ID: d5a2f8c19e4b
Revises: a3f7c1e9d2b8
Create Date: 2026-09-04 16:00:00.000000

Three additive columns for the Advanced Workflows fix bundle (BACKLOG 2026-09-03 /
2026-09-01), all nullable / DEFAULT so they are inert on a live upgrade:

  advancedstagedtarget.key_id (INTEGER, nullable): the resolved StringKey id target.propose
    stamps so deliver resolves the exact key by id instead of re-resolving unit_key by NAME in
    the DEFAULT namespace (which silently skipped every unit of a namespaced keys project). NULL
    = none carried -> deliver falls back to a namespace::name parse of unit_key.
  advancedstagedtarget.abandoned (BOOLEAN, default 0): the orphaned-run sweep sets this on every
    staged row of a run it marks FAILED "stop: orphaned" so a dead run's proposals can never
    deliver.
  advancedrun.heartbeat_at (DATETIME, nullable): a liveness stamp the harness loop touches each
    iteration + tool call so the periodic sweep can tell a live RUNNING run from a dead one.
  advancedstagedtarget.namespace (VARCHAR, default ''): the unit's recorded namespace (fix
    round 1 H1/M3) so deliver resolves the exact NAME in it, then the default, NEVER splitting
    unit_key on `::` (a nested-JSON key name contains `::` too).
  advancedmission.requested_fences (JSON, default '{}'): the explicitly-requested fences (fix
    round 1 M2) so mission_view re-derives the clamp notices on every read (durable across all
    four surfaces + the plan transcript, not just the one-shot create response).

Chained off the current single head (a3f7c1e9d2b8, workflowstep concept-glossary). Mirrors the
SQLite _ensure_columns() additive shim in app/db.py + the models (nullability-parity CI guard).
Hand-written (no autogenerate), same convention as a3f7c1e9d2b8.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5a2f8c19e4b'
down_revision: Union[str, None] = 'a3f7c1e9d2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('abandoned', sa.Boolean(),
                                      nullable=False, server_default='0'))
        # Advanced fix bundle round 1 (H1/M3): the unit's recorded namespace, so deliver
        # resolves the exact NAME in it (then the default) without splitting unit_key on `::`.
        batch_op.add_column(sa.Column('namespace', sa.String(),
                                      nullable=False, server_default=''))
    with op.batch_alter_table('advancedrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('advancedmission', schema=None) as batch_op:
        # Advanced fix bundle round 1 (M2): the explicitly-requested fences, so mission_view
        # re-derives the clamp notices on every read (durable across all four surfaces).
        batch_op.add_column(sa.Column('requested_fences', sa.JSON(),
                                      nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('advancedmission', schema=None) as batch_op:
        batch_op.drop_column('requested_fences')
    with op.batch_alter_table('advancedrun', schema=None) as batch_op:
        batch_op.drop_column('heartbeat_at')
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.drop_column('namespace')
        batch_op.drop_column('abandoned')
        batch_op.drop_column('key_id')
