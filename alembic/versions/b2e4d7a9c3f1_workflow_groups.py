"""multilingual workflow groups (P5 / item 4)

Revision ID: b2e4d7a9c3f1
Revises: a1c7f3e9b4d2
Create Date: 2026-07-03 22:00:00.000000

Adds the P5 multilingual-workflow schema (app/services/workflows.py group ops +
the /workflows group view + `workflow group`/`workflow copy` CLI):

  workflowgroup (new table)          — a per-customer-or-global container for a
                                        family of per-language-pair Workflow variants
  workflow.group_id                  — FK workflowgroup (nullable = ungrouped/legacy)
  workflow.source_locale             — the variant's source (nullable = universal)
  workflow.target_locale             — the variant's target (nullable = universal)
  workflow.is_default                — this variant is the group's fallback default
  workflowstep.adapted_from_id       — FK workflowstep (LLM-adapted-copy provenance)
  workflowstep.needs_review          — flag a linguist post-edit on an adapted copy

All additive + nullable/defaulted → existing workflows backfill to group_id NULL
(ungrouped/universal) and is_default 0, behaving byte-identically (dispatch never
touches an ungrouped workflow). Mirrors the SQLite _ensure_columns() additive
migration + the new-table create_all() in app/db.py.

Hand-written (no autogenerate) to avoid spurious drop_index noise — same pattern as
a1c7f3e9b4d2_customer_360.py. The self-referential workflowstep.adapted_from_id FK
is added WITHOUT a batch create_foreign_key on SQLite (batch table rebuild can't
name a self-FK cleanly); the column ships plain nullable — provenance only, never
constraint-enforced (a deleted source step just leaves a dangling id, which the app
tolerates). On Postgres the FK is created normally.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2e4d7a9c3f1'
down_revision: Union[str, None] = 'a1c7f3e9b4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'workflowgroup',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('description', sa.String(), nullable=False, server_default=''),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('workflow', schema=None) as batch_op:
        batch_op.add_column(sa.Column('group_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('source_locale', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('target_locale', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('is_default', sa.Boolean(), nullable=False,
                                      server_default='0'))
        batch_op.create_foreign_key('fk_workflow_group', 'workflowgroup',
                                    ['group_id'], ['id'])
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('adapted_from_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('needs_review', sa.Boolean(), nullable=False,
                                      server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('needs_review')
        batch_op.drop_column('adapted_from_id')
    with op.batch_alter_table('workflow', schema=None) as batch_op:
        batch_op.drop_constraint('fk_workflow_group', type_='foreignkey')
        batch_op.drop_column('is_default')
        batch_op.drop_column('target_locale')
        batch_op.drop_column('source_locale')
        batch_op.drop_column('group_id')
    op.drop_table('workflowgroup')
