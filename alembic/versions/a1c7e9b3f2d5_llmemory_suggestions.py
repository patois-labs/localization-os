"""LLMemory Phase 3 — memorysuggestion / memorysuggestionevidence (SPEC-llmemory-phase2 §4)

Revision ID: a1c7e9b3f2d5
Revises: d4e9f1a7c2b6
Create Date: 2026-08-25 14:00:00.000000

Adds the two Train-A workflow-suggestion-queue tables. Additive only — both are EMPTY on
upgrade and nothing outside app/services/llmemory_suggestions.py + its surfaces reads them,
so every existing code path is byte-identical after this migration.

  memorysuggestion (new)         — a deterministically-drafted proposal to change a
                                   workflow's routing/step-order/config, each citing the
                                   evidence that justifies it + a human-readable diff.
                                   `customer_id` NOT NULL (the hard partition, proposal §2)
                                   and `workflow_id` NOT NULL (the apply target). `superseded_
                                   by_id` is a SELF-REFERENTIAL FK (the bi-temporal chain),
                                   declared here exactly as the SQLModel class does.
  memorysuggestionevidence (new) — the suggestion<->{event|memory|lqa_result} citation join;
                                   all three ref FKs nullable (exactly one set per row).

WHY NO app/db.py `_ADDED_COLUMNS` ENTRY: that shim exists for new COLUMNS on EXISTING
tables, which create_all() will never add. These are new TABLES, which create_all() DOES
create on a non-alembic SQLite boot — the same posture as the Phase-1 memory tables
(b3e7c1d94a26). The nullability-parity CI guard (tests/verify_migrations.py) compares this
migration's output against the models column-for-column, keeping the two paths honest.

SELF-FK ON SQLITE: memorysuggestion.superseded_by_id references memorysuggestion.id inside
the same CREATE TABLE — legal on SQLite and Postgres alike (nullable, stamped by a later
UPDATE), mirroring the memoryentry precedent.

INDEXES mirror the model's `index=True` fields (customer_id — every read filters on it;
suggestion_key — the dedup lookup; workflow_id — the apply-target lookup; created_at — the
newest-first ordering) plus memorysuggestionevidence's suggestion_id FK index and a
NON-unique FUNCTIONAL scope index over (coalesce(org_id,-1), customer_id, coalesce(project_
id,-1)), deliberately NOT unique like ix_memoryentry_scope.

Hand-written (no autogenerate) off the head d4e9f1a7c2b6. Runs clean on BOTH SQLite and
Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c7e9b3f2d5'
down_revision: Union[str, None] = 'd4e9f1a7c2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memorysuggestion',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False, server_default=''),
        sa.Column('status', sa.String(), nullable=False, server_default='proposed'),
        sa.Column('org_id', sa.Integer(), nullable=True),
        # NOT NULL: the hard customer partition.
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=True),
        # NOT NULL: the apply target.
        sa.Column('workflow_id', sa.Integer(), nullable=False),
        sa.Column('target_step_id', sa.Integer(), nullable=True),
        sa.Column('base_fingerprint', sa.String(), nullable=False, server_default=''),
        sa.Column('suggestion_key', sa.String(), nullable=False, server_default=''),
        sa.Column('rationale', sa.String(), nullable=False, server_default=''),
        sa.Column('proposed_change', sa.JSON(), nullable=True),
        sa.Column('diff_text', sa.String(), nullable=False, server_default=''),
        sa.Column('evidence_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False, server_default='system'),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('reviewed_by', sa.String(), nullable=False, server_default=''),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('applied_by', sa.String(), nullable=False, server_default=''),
        sa.Column('applied_snapshot', sa.JSON(), nullable=True),
        sa.Column('superseded_at', sa.DateTime(), nullable=True),
        sa.Column('superseded_by_id', sa.Integer(), nullable=True),
        sa.Column('reject_reason', sa.String(), nullable=False, server_default=''),
        sa.Column('blocked_kind', sa.String(), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflow.id']),
        sa.ForeignKeyConstraint(['target_step_id'], ['workflowstep.id']),
        sa.ForeignKeyConstraint(['superseded_by_id'], ['memorysuggestion.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_memorysuggestion_customer_id', 'memorysuggestion',
                    ['customer_id'], unique=False)
    op.create_index('ix_memorysuggestion_suggestion_key', 'memorysuggestion',
                    ['suggestion_key'], unique=False)
    op.create_index('ix_memorysuggestion_workflow_id', 'memorysuggestion',
                    ['workflow_id'], unique=False)
    op.create_index('ix_memorysuggestion_created_at', 'memorysuggestion',
                    ['created_at'], unique=False)
    # NON-unique FUNCTIONAL scope index (COALESCE-collapsed nullable scope columns), the
    # ix_memoryentry_scope shape.
    # SQLITE REFLECTION TRAP (see c7f1a2b93d84): this index is EXPRESSION-based, and SQLAlchemy
    # CANNOT reflect an expression index. Any FUTURE `op.batch_alter_table('memorysuggestion')`
    # (or on memorysuggestionevidence, if it ever grows one) rebuilds the table by reflection
    # and will SILENTLY DROP this index -- such a migration MUST explicitly re-create
    # ix_memorysuggestion_scope after the batch block (idempotent, if_not_exists=True), exactly
    # as c7f1a2b93d84 does for ix_memoryentry_scope.
    op.create_index('ix_memorysuggestion_scope', 'memorysuggestion',
                    [sa.text('coalesce(org_id, -1)'),
                     sa.text('customer_id'),
                     sa.text('coalesce(project_id, -1)')],
                    unique=False)

    op.create_table(
        'memorysuggestionevidence',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('suggestion_id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('memory_id', sa.Integer(), nullable=True),
        sa.Column('lqa_result_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['suggestion_id'], ['memorysuggestion.id']),
        sa.ForeignKeyConstraint(['event_id'], ['memoryevent.id']),
        sa.ForeignKeyConstraint(['memory_id'], ['memoryentry.id']),
        sa.ForeignKeyConstraint(['lqa_result_id'], ['lqaresult.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_memorysuggestionevidence_suggestion_id', 'memorysuggestionevidence',
                    ['suggestion_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_memorysuggestionevidence_suggestion_id',
                  table_name='memorysuggestionevidence')
    op.drop_table('memorysuggestionevidence')
    op.drop_index('ix_memorysuggestion_scope', table_name='memorysuggestion')
    op.drop_index('ix_memorysuggestion_created_at', table_name='memorysuggestion')
    op.drop_index('ix_memorysuggestion_workflow_id', table_name='memorysuggestion')
    op.drop_index('ix_memorysuggestion_suggestion_key', table_name='memorysuggestion')
    op.drop_index('ix_memorysuggestion_customer_id', table_name='memorysuggestion')
    op.drop_table('memorysuggestion')
