"""context package phase 1 — schema + storage (SPEC-context-metadata)

Revision ID: c8a3f1e7d9b4
Revises: b1d4f7c2e9a3
Create Date: 2026-07-12 09:00:00.000000

Adds the Context Package phase-1 schema (SPEC-context-metadata phase 1: "Schema +
storage + API intake"). All additive — every existing row backfills the new JSON
columns to NULL (byte-identical to the pre-context behaviour), and the new table is
empty on upgrade.

  document.context      — nullable JSON: the request/doc-level TranslationContext that
                          arrived with the document (API `context` field / connector
                          override / web upload form). Written at ingestion, used at
                          prompt time. NULL = no context (today's behaviour).
  segment.meta          — nullable JSON: per-unit metadata. Column ships NOW; actively
  segmenttm.meta          written in later phases (phase 2 unit overrides; phase 4 TM
  glossaryentry.meta      provenance + TBX term fields). NULL today = byte-identical;
                          nothing reads them this phase.
  contextprofile (new)  — standing "context card" rows: a stored context package + a
                          scope (org/customer/content_type/project). One profile per
                          EXACT scope combo per org, enforced by a FUNCTIONAL unique
                          index over (coalesce(org_id,-1), coalesce(customer_id,-1),
                          coalesce(content_type_id,-1), coalesce(project_id,-1)) —
                          NULL-collapsed so it fires even single-tenant, on BOTH SQLite
                          (expression indexes) and Postgres. Mirrors ContentType's
                          ux_contenttype_scope_name precedent.

Hand-written (no autogenerate) off the head b1d4f7c2e9a3, same convention as
d4c8b2a6f1e3_content_types.py. Runs clean on BOTH SQLite (batch_alter_table) and
Postgres (native ALTER). The four nullable-FK columns on contextprofile carry real
FKs in the SQLModel class (create_all + Postgres build them, SQLite PRAGMA
foreign_keys=ON enforces them) while this create_table declares them explicitly; the
additive JSON columns on existing tables are PLAIN nullable ADD COLUMNs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8a3f1e7d9b4'
down_revision: Union[str, None] = 'b1d4f7c2e9a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New JSON columns on existing tables (all nullable → existing rows backfill NULL).
    with op.batch_alter_table('document', schema=None) as batch_op:
        batch_op.add_column(sa.Column('context', sa.JSON(), nullable=True))
    with op.batch_alter_table('segment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meta', sa.JSON(), nullable=True))
    with op.batch_alter_table('segmenttm', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meta', sa.JSON(), nullable=True))
    with op.batch_alter_table('glossaryentry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meta', sa.JSON(), nullable=True))

    # The standing-cards table.
    op.create_table(
        'contextprofile',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('content_type_id', sa.Integer(), nullable=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(), nullable=False, server_default=''),
        sa.Column('authority', sa.String(), nullable=False, server_default='operator'),
        sa.Column('context', sa.JSON(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['content_type_id'], ['contenttype.id']),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # FUNCTIONAL unique index (NOT a plain UNIQUE, which is INERT single-tenant:
    # org_id is NULL with the multi-org gate off and SQL treats NULLs as DISTINCT).
    # COALESCE collapses the NULL scope columns so the DB actually enforces one card
    # per exact scope combo even single-tenant, on BOTH SQLite (expression indexes)
    # and Postgres. Mirrors ux_contenttype_scope_name.
    op.create_index('ux_contextprofile_scope', 'contextprofile',
                    [sa.text('coalesce(org_id, -1)'),
                     sa.text('coalesce(customer_id, -1)'),
                     sa.text('coalesce(content_type_id, -1)'),
                     sa.text('coalesce(project_id, -1)')],
                    unique=True)


def downgrade() -> None:
    op.drop_index('ux_contextprofile_scope', table_name='contextprofile')
    op.drop_table('contextprofile')
    with op.batch_alter_table('glossaryentry', schema=None) as batch_op:
        batch_op.drop_column('meta')
    with op.batch_alter_table('segmenttm', schema=None) as batch_op:
        batch_op.drop_column('meta')
    with op.batch_alter_table('segment', schema=None) as batch_op:
        batch_op.drop_column('meta')
    with op.batch_alter_table('document', schema=None) as batch_op:
        batch_op.drop_column('context')
