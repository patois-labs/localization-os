"""LLMemory Phase 1 — memoryentry / memoryevent / memoryevidence (PROPOSAL-llmemory §3)

Revision ID: b3e7c1d94a26
Revises: d4b7e2f9a1c6
Create Date: 2026-07-31 12:00:00.000000

Adds the three LLMemory tables. Additive only — all three are EMPTY on upgrade, and
nothing outside app/services/llmemory.py reads them in this work package, so every
existing code path (prompts, orchestrator, translate, CLI, web) is byte-identical
after this migration.

  memoryentry (new)    — the typed, scoped, provenance-linked BELIEF. `customer_id` is
                         NOT NULL: the hard customer partition (proposal §2 "no
                         cross-customer memory ever") is a schema fact, not a query
                         convention, so a NULL-scoped memory cannot exist to leak.
                         `superseded_by_id` is a SELF-REFERENTIAL FK (the bi-temporal
                         chain); it is declared here exactly as the SQLModel class does.
  memoryevent (new)    — the lossless episode/provenance layer. `customer_id` IS
                         nullable here on purpose (a document in a customer-less project
                         still records honest events; the distiller skips them, which is
                         the safe direction).
  memoryevidence (new) — the entry<->event citation join.

WHY NO app/db.py `_ADDED_COLUMNS` ENTRY: that shim exists for new COLUMNS on EXISTING
tables, which create_all() will never add. These are new TABLES, which create_all()
DOES create on a non-alembic SQLite boot — the same posture as workflowgroup,
projectpairprovider, contenttype, glossary/glossarypair, workflowbatch and styleguide
(each documented in db.py's comments as "made by create_all() on SQLite (a new table,
not an altered one)"). Adding a shim entry would be a no-op at best and a schema fork at
worst. The nullability-parity CI guard (tests/verify_migrations.py) compares this
migration's output against the models column-for-column, which is what keeps the two
paths honest.

SELF-FK ON SQLITE: memoryentry.superseded_by_id references memoryentry.id inside the
same CREATE TABLE. That is legal on SQLite and Postgres alike (no deferred-constraint
gymnastics needed) because the column is nullable and is only ever stamped by an UPDATE
after the successor row exists.

INDEXES. Three plain single-column indexes mirror the model's `index=True` fields
(memoryentry.customer_id — every read filters on it; memoryentry.source_term_norm — the
distiller's and the recall matcher's lookup column; memoryevent.created_at — the
distiller's recency window and `invalidate --since`), plus memoryevidence's two FK
indexes and a NON-unique FUNCTIONAL scope index over (coalesce(org_id,-1), customer_id,
coalesce(project_id,-1)). The scope index is deliberately NOT unique, exactly like
ix_styleguide_scope: several entries legitimately share one scope (a candidate and the
entry it promotes to, a superseded chain, two conflicted values), so a unique constraint
would block the design rather than protect it.

Hand-written (no autogenerate) off the head d4b7e2f9a1c6, same convention as
d7f2a4c9e1b6_style_guides.py. Runs clean on BOTH SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e7c1d94a26'
down_revision: Union[str, None] = 'd4b7e2f9a1c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memoryentry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False, server_default='project_note'),
        sa.Column('status', sa.String(), nullable=False, server_default='candidate'),
        sa.Column('org_id', sa.Integer(), nullable=True),
        # NOT NULL: the hard customer partition.
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('source_locale', sa.String(), nullable=True),
        sa.Column('target_locale', sa.String(), nullable=True),
        sa.Column('source_term', sa.String(), nullable=False, server_default=''),
        sa.Column('target_term', sa.String(), nullable=False, server_default=''),
        sa.Column('source_term_norm', sa.String(), nullable=False, server_default=''),
        sa.Column('body', sa.String(), nullable=False, server_default=''),
        sa.Column('audience', sa.String(), nullable=True),
        sa.Column('intent', sa.String(), nullable=True),
        sa.Column('origin', sa.String(), nullable=False, server_default='manual'),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0'),
        sa.Column('evidence_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('superseded_at', sa.DateTime(), nullable=True),
        sa.Column('superseded_by_id', sa.Integer(), nullable=True),
        sa.Column('invalidated_at', sa.DateTime(), nullable=True),
        sa.Column('purged_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('use_count', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.ForeignKeyConstraint(['superseded_by_id'], ['memoryentry.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_memoryentry_customer_id', 'memoryentry',
                    ['customer_id'], unique=False)
    op.create_index('ix_memoryentry_source_term_norm', 'memoryentry',
                    ['source_term_norm'], unique=False)
    # NON-unique FUNCTIONAL scope index (COALESCE-collapsed nullable scope columns so it
    # is useful single-tenant too, on SQLite expression indexes and Postgres alike).
    op.create_index('ix_memoryentry_scope', 'memoryentry',
                    [sa.text('coalesce(org_id, -1)'),
                     sa.text('customer_id'),
                     sa.text('coalesce(project_id, -1)')],
                    unique=False)

    op.create_table(
        'memoryevent',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False, server_default='manual'),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('segment_id', sa.Integer(), nullable=True),
        sa.Column('workflow_run_id', sa.Integer(), nullable=True),
        sa.Column('source_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('target_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('before_text', sa.String(), nullable=False, server_default=''),
        sa.Column('after_text', sa.String(), nullable=False, server_default=''),
        sa.Column('dimension', sa.String(), nullable=False, server_default=''),
        sa.Column('severity', sa.String(), nullable=False, server_default=''),
        sa.Column('span', sa.String(), nullable=False, server_default=''),
        sa.Column('note', sa.String(), nullable=False, server_default=''),
        sa.Column('actor', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.ForeignKeyConstraint(['document_id'], ['document.id']),
        sa.ForeignKeyConstraint(['segment_id'], ['segment.id']),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflowrun.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_memoryevent_created_at', 'memoryevent',
                    ['created_at'], unique=False)

    op.create_table(
        'memoryevidence',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('memory_id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['memory_id'], ['memoryentry.id']),
        sa.ForeignKeyConstraint(['event_id'], ['memoryevent.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_memoryevidence_memory_id', 'memoryevidence',
                    ['memory_id'], unique=False)
    op.create_index('ix_memoryevidence_event_id', 'memoryevidence',
                    ['event_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_memoryevidence_event_id', table_name='memoryevidence')
    op.drop_index('ix_memoryevidence_memory_id', table_name='memoryevidence')
    op.drop_table('memoryevidence')
    op.drop_index('ix_memoryevent_created_at', table_name='memoryevent')
    op.drop_table('memoryevent')
    op.drop_index('ix_memoryentry_scope', table_name='memoryentry')
    op.drop_index('ix_memoryentry_source_term_norm', table_name='memoryentry')
    op.drop_index('ix_memoryentry_customer_id', table_name='memoryentry')
    op.drop_table('memoryentry')
