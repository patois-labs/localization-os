"""multi-org P0: org_id + is_superadmin schema (no-op gate)

Revision ID: f7a2c9d1b3e6
Revises: e5b3c9a2f1d4
Create Date: 2026-07-03 12:00:00.000000

Multi-org SaaS groundwork, phase 0 (schema + gate only, NO behavior change):

  - user.is_superadmin (Boolean, NOT NULL, server_default='0') — a super-admin /
    operator flag, NOT a 7th RBAC role. In the customer app + /api a super-admin
    resolves to their HOME org like any org-admin; is_superadmin is only a ticket
    to the (future) back-office door. server_default='0' backfills every existing
    user to OFF. P8 flags the first admin — NOT this migration.
  - org_id (Integer, nullable, FK organization.id) on the 6 root-owned tables that
    lacked it: customer, connection, workflow, languageset, routingrule,
    departmentbudget (project + user already carry it from the baseline). Nullable
    so the add is safe; upgrade() then stamps the single 'Default' org id onto
    every existing row of those 6 tables, mirroring the SQLite _ensure_default_org
    backfill in app/db.py.

Nothing reads any of this yet: the multi_org_enabled setting defaults "0" (the
sacred gate) so the app stays byte-identical to single-tenant today. Mirrors the
SQLite _ensure_columns() additive migration + _ensure_default_org backfill.

Hand-written (no autogenerate) to avoid the spurious drop_index noise autogenerate
emits for the hot-path indexes (ix_segmenttm_lookup / ix_fulltexttm_lookup /
ix_segment_doc_locale / ix_segmentembedding_lookup) and the UNIQUE ones, which are
carried by app/db.py + the baseline but not declared on the SQLModel metadata.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a2c9d1b3e6'
down_revision: Union[str, None] = 'e5b3c9a2f1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The 6 root-owned tables gaining org_id (project + user already have it).
_ORG_TABLES = (
    "customer", "connection", "workflow",
    "languageset", "routingrule", "departmentbudget",
)


def upgrade() -> None:
    # is_superadmin on user (workflowstep-redact_pii style: NOT NULL + server_default
    # so existing rows backfill to OFF).
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_superadmin', sa.Boolean(), nullable=False,
                                      server_default='0'))

    # org_id (nullable FK) on the 6 root tables.
    for table in _ORG_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('org_id', sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                f'fk_{table}_org_id_organization', 'organization',
                ['org_id'], ['id'])

    # Stamp the single 'Default' org (seeded by the baseline) onto existing rows.
    # Mirrors db._ensure_default_org; idempotent via the org_id IS NULL guard.
    conn = op.get_bind()
    org_row = conn.execute(
        sa.text("SELECT id FROM organization ORDER BY id LIMIT 1")).fetchone()
    if org_row is not None:
        org_id = org_row[0]
        for table in _ORG_TABLES:
            conn.execute(
                sa.text(f"UPDATE {table} SET org_id = :o WHERE org_id IS NULL"),
                {"o": org_id})


def downgrade() -> None:
    for table in _ORG_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column('org_id')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('is_superadmin')
