"""custom project fields: customfielddef + projectfieldvalue (SPEC-project-automation part C)

Revision ID: c1f0a7d4e2b6
Revises: a7c3e9f1b2d4
Create Date: 2026-10-13 12:00:00.000000

TWO new tables, no column added to an existing table (so no app/db.py `_ADDED_COLUMNS`
entry; create_all builds them on the non-alembic SQLite path).

NOTE FOR THE FINAL MERGER: this revision is one of several added in parallel (project
automation, notifications, custom fields). Re-chain `down_revision` after the others land
so the history keeps exactly ONE head.

    customfielddef      per-org / per-customer typed field definitions for projects
    projectfieldvalue   one typed value per (project, field)

The functional unique index mirrors ux_contenttype_scope_name: coalesce() collapses the
NULL org and NULL customer so the database really enforces key uniqueness PER SCOPE
(org, customer) single-tenant, and lower() makes it case-insensitive, on SQLite
(expression indexes) and Postgres alike. A global-versus-customer clash is refused by the
service (a project of that customer would hold two definitions under one key).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1f0a7d4e2b6'
down_revision: Union[str, None] = 'a7c3e9f1b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'customfielddef',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organization.id'), nullable=True),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customer.id'), nullable=True),
        sa.Column('entity', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('options_json', sa.String(), nullable=False),
        sa.Column('key_pattern', sa.String(), nullable=False),
        sa.Column('link_system', sa.String(), nullable=False),
        sa.Column('help_text', sa.String(), nullable=False),
        sa.Column('required', sa.Boolean(), nullable=False),
        sa.Column('show_in_list', sa.Boolean(), nullable=False),
        sa.Column('filterable', sa.Boolean(), nullable=False),
        sa.Column('list_order', sa.Integer(), nullable=False),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_customfielddef_customer_id', 'customfielddef', ['customer_id'])
    op.create_index(
        'ux_customfielddef_scope_key',
        'customfielddef',
        [
            sa.text('coalesce(org_id, -1)'),
            sa.text('coalesce(customer_id, -1)'),
            sa.text('entity'),
            sa.text('lower(key)'),
        ],
        unique=True,
    )
    op.create_table(
        'projectfieldvalue',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id'), nullable=False),
        sa.Column('field_id', sa.Integer(), sa.ForeignKey('customfielddef.id'), nullable=False),
        sa.Column('value_text', sa.String(), nullable=True),
        sa.Column('value_key', sa.String(), nullable=True),
        sa.Column('value_num', sa.Float(), nullable=True),
        sa.Column('value_date', sa.DateTime(), nullable=True),
        sa.Column('value_json', sa.String(), nullable=True),
        sa.Column('updated_by', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('project_id', 'field_id', name='ux_projectfieldvalue_project_field'),
    )
    op.create_index('ix_projectfieldvalue_project_id', 'projectfieldvalue', ['project_id'])
    op.create_index('ix_projectfieldvalue_field_id', 'projectfieldvalue', ['field_id'])
    op.create_index(
        'ix_projectfieldvalue_field_key', 'projectfieldvalue', ['field_id', 'value_key']
    )
    op.create_index(
        'ix_projectfieldvalue_field_num', 'projectfieldvalue', ['field_id', 'value_num']
    )
    op.create_index(
        'ix_projectfieldvalue_field_date', 'projectfieldvalue', ['field_id', 'value_date']
    )


def downgrade() -> None:
    op.drop_index('ix_projectfieldvalue_field_date', table_name='projectfieldvalue')
    op.drop_index('ix_projectfieldvalue_field_num', table_name='projectfieldvalue')
    op.drop_index('ix_projectfieldvalue_field_key', table_name='projectfieldvalue')
    op.drop_index('ix_projectfieldvalue_field_id', table_name='projectfieldvalue')
    op.drop_index('ix_projectfieldvalue_project_id', table_name='projectfieldvalue')
    op.drop_table('projectfieldvalue')
    op.drop_index('ux_customfielddef_scope_key', table_name='customfielddef')
    op.drop_index('ix_customfielddef_customer_id', table_name='customfielddef')
    op.drop_table('customfielddef')
