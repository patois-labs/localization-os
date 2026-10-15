"""multi-org P8: prompttemplate.org_id (pre-gate isolation cleanup)

Revision ID: b9d3e2f4c1a8
Revises: a8c4e1f9b2d7
Create Date: 2026-07-03 15:00:00.000000

Multi-org SaaS groundwork, phase 8 (pre-gate cleanup — the flagged leak):

  - prompttemplate.org_id (Integer, nullable, FK organization.id) — prompt
    templates were app-global, so under the sacred gate one customer's saved
    prompts would surface to another. org_id closes that leak: templates are
    stamped with the creator's org and the /templates list is org-scoped. Nullable
    so existing rows backfill to the Default org (db._ensure_default_org, mirrored
    on the SQLite path). INERT single-tenant — the gate governs enforcement, the
    column just carries the org.

Hand-written (no autogenerate) off the P7 head a8c4e1f9b2d7 to avoid the spurious
drop_index noise autogenerate emits for the app/db.py hot-path indexes. Mirrors the
SQLite _ensure_columns() additive migration for prompttemplate.org_id.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9d3e2f4c1a8'
down_revision: Union[str, None] = 'a8c4e1f9b2d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # org_id on prompttemplate (nullable FK -> organization.id), mirroring the P0
    # convention for the 6 root tables.
    with op.batch_alter_table('prompttemplate', schema=None) as batch_op:
        batch_op.add_column(sa.Column('org_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_prompttemplate_org_id_organization',
                                    'organization', ['org_id'], ['id'])

    # Stamp the single 'Default' org onto existing rows. Mirrors
    # db._ensure_default_org; idempotent via the org_id IS NULL guard.
    conn = op.get_bind()
    org_row = conn.execute(
        sa.text("SELECT id FROM organization ORDER BY id LIMIT 1")).fetchone()
    if org_row is not None:
        conn.execute(
            sa.text("UPDATE prompttemplate SET org_id = :o WHERE org_id IS NULL"),
            {"o": org_row[0]})


def downgrade() -> None:
    with op.batch_alter_table('prompttemplate', schema=None) as batch_op:
        batch_op.drop_column('org_id')
