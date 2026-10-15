"""style guides (SPEC-workflows-redesign Track B, B2 — the flagship asset)

Revision ID: d7f2a4c9e1b6
Revises: c3e9a1f7b2d5
Create Date: 2026-07-18 12:00:00.000000

Adds the StyleGuide table: a first-class, approvable, customer-scoped long-form style
guide packaged into the translation prompt. Additive only — the new table is empty on
upgrade, so every existing code path behaves byte-identically (no guide resolves ->
the {style_guide} placeholder renders to "" -> prompts are unchanged).

  styleguide (new)  — org-owned, optionally customer-scoped, content-type-attachable,
                      optionally per-pair, with a DRAFT/APPROVED lifecycle (mirroring
                      Glossary) and a long-form `body`. Scoped by a NON-unique FUNCTIONAL
                      index over (coalesce(org_id,-1), coalesce(customer_id,-1),
                      coalesce(content_type_id,-1)) — speeds the resolver's scope reads
                      without blocking multiple guides per scope (draft + approved
                      revisions coexist; the resolver breaks ties, not the DB). NON-unique
                      is the deliberate difference from ux_contextprofile_scope.

Hand-written (no autogenerate) off the head c3e9a1f7b2d5, same convention as
c8a3f1e7d9b4_context_package_p1.py. Runs clean on BOTH SQLite (batch/native create)
and Postgres. The four nullable-FK columns carry real FKs in the SQLModel class; this
create_table declares them explicitly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f2a4c9e1b6'
down_revision: Union[str, None] = 'c3e9a1f7b2d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'styleguide',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='draft'),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('content_type_id', sa.Integer(), nullable=True),
        sa.Column('source_locale', sa.String(), nullable=True),
        sa.Column('target_locale', sa.String(), nullable=True),
        sa.Column('body', sa.String(), nullable=False, server_default=''),
        sa.Column('approved_by', sa.String(), nullable=False, server_default=''),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['content_type_id'], ['contenttype.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # NON-unique FUNCTIONAL scope index (COALESCE-collapsed NULLs so it is useful even
    # single-tenant, on BOTH SQLite expression indexes and Postgres). Deliberately NOT
    # unique — several guides may share a scope; the resolver breaks ties.
    op.create_index('ix_styleguide_scope', 'styleguide',
                    [sa.text('coalesce(org_id, -1)'),
                     sa.text('coalesce(customer_id, -1)'),
                     sa.text('coalesce(content_type_id, -1)')],
                    unique=False)


def downgrade() -> None:
    op.drop_index('ix_styleguide_scope', table_name='styleguide')
    op.drop_table('styleguide')
