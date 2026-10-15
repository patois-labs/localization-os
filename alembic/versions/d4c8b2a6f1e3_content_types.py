"""content types — per-customer domain scoping for glossaries + TM (phase 1)

Revision ID: d4c8b2a6f1e3
Revises: c7e9f2a1b8d3
Create Date: 2026-07-08 12:00:00.000000

Adds the ContentType phase-1 schema (SPEC-content-types §7): a small, first-class,
org/customer-scoped taxonomy object (e.g. "Marketing", "Product UI", "Legal") that a
project binds to at creation, driving the domain scoping of its language assets.

  contenttype (new table)        — a named, org-owned, optionally customer-scoped
                                   taxonomy object with a description + created_by/
                                   created_at. UNIQUE (org_id, customer_id, name).
  project.content_type_id        — nullable FK: NULL = the shared "General" bucket
                                   (byte-identical to today); non-NULL = the project
                                   is bound to one ContentType.
  customer.default_content_type_id — nullable FK: the customer's default content
                                   type, inherited at project creation (same pattern
                                   as default_provider).

All additive: every existing Project + Customer backfills the new column to NULL
(byte-identical to the pre-content-type behaviour), and the new table is empty on
upgrade. Mirrors the SQLite _ensure_columns() additive-column + create_all()
new-table pattern in app/db.py.

Hand-written (no autogenerate) off the head c7e9f2a1b8d3, same convention as
c4f9a1b7e2d8_glossary_module.py. Runs clean on BOTH SQLite (batch_alter_table) and
Postgres (native ALTER). The two nullable-FK columns on the existing tables are added
as PLAIN nullable columns (no ForeignKeyConstraint in the ALTER) exactly like the
glossary-module revision added glossaryentry.glossary_id / workflowstep.glossary_id:
the SQLModel classes carry the real FK (so create_all + a fresh Postgres build it and
SQLite's PRAGMA foreign_keys=ON enforces it), while the SQLite-safe ALTER stays a
bare ADD COLUMN.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4c8b2a6f1e3'
down_revision: Union[str, None] = 'c7e9f2a1b8d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'contenttype',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=False, server_default=''),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # FUNCTIONAL unique index (NOT a plain UNIQUE(org_id, customer_id, name), which is
    # INERT single-tenant: org_id is NULL with the multi-org gate off and SQL treats
    # NULLs as DISTINCT, so it would never fire). COALESCE collapses the NULLs and
    # lower() case-folds, so the DB actually enforces scope+name uniqueness even
    # single-tenant, on BOTH SQLite (expression indexes since 3.9) and Postgres.
    op.create_index('ux_contenttype_scope_name', 'contenttype',
                    [sa.text('coalesce(org_id, -1)'),
                     sa.text('coalesce(customer_id, -1)'),
                     sa.text('lower(name)')],
                    unique=True)
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_type_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('default_content_type_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.drop_column('default_content_type_id')
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('content_type_id')
    op.drop_index('ux_contenttype_scope_name', table_name='contenttype')
    op.drop_table('contenttype')
