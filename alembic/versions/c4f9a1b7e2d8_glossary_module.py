"""dedicated glossary module (Phase 5-B)

Revision ID: c4f9a1b7e2d8
Revises: b3d8f1a6c2e9
Create Date: 2026-07-06 12:00:00.000000

Adds the first-class Glossary module schema (app/services/glossary.py module APIs
+ the /glossaries sidebar tab + the workflow-step glossary dropdown):

  glossary (new table)      — a named, org-owned, optionally customer-scoped
                              termbase container with an approval lifecycle
                              (status draft|approved, approved_by/approved_at,
                              created_by/created_at).
  glossarypair (new table)  — one row per (glossary_id, source_locale,
                              target_locale) — which src->tgt pairs a glossary
                              applies to (mirrors projectpairprovider storage).
  glossaryentry.glossary_id — nullable FK: NULL = a LEGACY per-project entry
                              (byte-identical to today, matched by project_id and
                              never listed in the module); non-NULL = OWNED by a
                              first-class Glossary.
  workflowstep.glossary_id  — nullable FK: the APPROVED module Glossary a step
                              pins (NULL = today's project-scoped match_terms).

All additive: every existing GlossaryEntry backfills glossary_id NULL (a legacy
project entry) and every existing WorkflowStep backfills glossary_id NULL (project-
scoped glossary matching) — byte-identical to the pre-module behaviour. The two new
tables are empty on upgrade. Mirrors the SQLite _ensure_columns() additive column +
create_all() new-table pattern in app/db.py.

Hand-written (no autogenerate) to avoid spurious drop_index noise, matching
a2b7c4e9f1d6_pair_providers.py. Runs clean on BOTH SQLite (batch_alter_table) and
Postgres (native ALTER).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4f9a1b7e2d8'
down_revision: Union[str, None] = 'b3d8f1a6c2e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'glossary',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='draft'),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('approved_by', sa.String(), nullable=False, server_default=''),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'glossarypair',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('glossary_id', sa.Integer(), nullable=False),
        sa.Column('source_locale', sa.String(), nullable=False),
        sa.Column('target_locale', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['glossary_id'], ['glossary.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_glossarypair_glossary_id', 'glossarypair',
                    ['glossary_id'], unique=False)
    with op.batch_alter_table('glossaryentry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('glossary_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_glossaryentry_glossary_id', ['glossary_id'],
                              unique=False)
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('glossary_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('glossary_id')
    with op.batch_alter_table('glossaryentry', schema=None) as batch_op:
        batch_op.drop_index('ix_glossaryentry_glossary_id')
        batch_op.drop_column('glossary_id')
    op.drop_index('ix_glossarypair_glossary_id', table_name='glossarypair')
    op.drop_table('glossarypair')
    op.drop_table('glossary')
