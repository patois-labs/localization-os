"""multi-org P7: organization.active suspension flag (no-op gate)

Revision ID: a8c4e1f9b2d7
Revises: f7a2c9d1b3e6
Create Date: 2026-07-03 13:00:00.000000

Multi-org SaaS groundwork, phase 7 (org management):

  - organization.active (Boolean, NOT NULL, server_default='1') — a per-org
    suspension flag. A suspended (active=0) org's users cannot log in and its API
    tokens are rejected — but ONLY when the sacred gate (multi_org_enabled) is ON.
    server_default='1' backfills every existing org to ACTIVE, so nothing changes
    for single-tenant installs (the gate is off; access.org_active() is a no-op).

Hand-written (no autogenerate) off the P0 head f7a2c9d1b3e6 to avoid the spurious
drop_index noise autogenerate emits for the app/db.py hot-path indexes. Mirrors the
SQLite _ensure_columns() additive migration for organization.active.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8c4e1f9b2d7'
down_revision: Union[str, None] = 'f7a2c9d1b3e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # active on organization (NOT NULL + server_default so existing orgs backfill
    # to ACTIVE — byte-identical single-tenant, matching the SQLite path).
    with op.batch_alter_table('organization', schema=None) as batch_op:
        batch_op.add_column(sa.Column('active', sa.Boolean(), nullable=False,
                                      server_default='1'))


def downgrade() -> None:
    with op.batch_alter_table('organization', schema=None) as batch_op:
        batch_op.drop_column('active')
