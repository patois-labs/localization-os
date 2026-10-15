"""project automation (MVP part A: project to connection binding, schedules, claims)

Revision ID: d9a4f2c7e1b8
Revises: c5d8f2a6b9e3
Create Date: 2026-10-13 09:00:00.000000

Adds two NEW tables, no existing table is touched:

* projectautomation: one row per project (unique project_id) binding it to a connection and
  carrying its run schedule (UTC times of day and days of week, or an interval), delta or full
  mode, pause state, the consecutive-failure counter and the optional per-run USD cap.
* projectbindingclaim: write-back ownership of one external item when one connection feeds
  several projects (unique connection_id + external_id + locale: one claim per item and target locale).

MERGE NOTE: three feature branches add a revision off a7c3e9f1b2d4 in parallel (project
automation, notifications, custom fields). The LAST to merge re-points its down_revision at the
then-current head; the revision id of this file does not change. Mirrors models.ProjectAutomation
and models.ProjectBindingClaim; tests/verify_migrations.py pins the column sets.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a4f2c7e1b8'
down_revision: Union[str, None] = 'c5d8f2a6b9e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'projectautomation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('run_connection_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('mode', sa.String(), nullable=False, server_default='delta'),
        sa.Column('times', sa.JSON(), nullable=True),
        sa.Column('days', sa.JSON(), nullable=True),
        sa.Column('interval_minutes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_at', sa.DateTime(), nullable=True),
        sa.Column('last_at', sa.DateTime(), nullable=True),
        sa.Column('last_job_id', sa.Integer(), nullable=True),
        sa.Column('last_status', sa.String(), nullable=False, server_default=''),
        sa.Column('last_message', sa.String(), nullable=False, server_default=''),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('auto_pause_after', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('paused_at', sa.DateTime(), nullable=True),
        sa.Column('paused_reason', sa.String(), nullable=False, server_default=''),
        sa.Column('run_cap_usd', sa.Float(), nullable=True),
        sa.Column('scope', sa.JSON(), nullable=True),
        sa.Column('full_cursor', sa.JSON(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['connection_id'], ['connection.id']),
        sa.ForeignKeyConstraint(['run_connection_id'], ['connection.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', name='ux_projectautomation_project_id'),
    )
    op.create_index('ix_projectautomation_project_id', 'projectautomation', ['project_id'])
    op.create_index('ix_projectautomation_connection_id', 'projectautomation', ['connection_id'])
    op.create_index(
        'ix_projectautomation_run_connection_id', 'projectautomation', ['run_connection_id']
    )

    op.create_table(
        'projectbindingclaim',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('external_id', sa.String(), nullable=False),
        sa.Column('locale', sa.String(), nullable=False, server_default=''),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connection.id']),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'connection_id', 'external_id', 'locale', name='ux_projectbindingclaim_item'
        ),
    )
    op.create_index('ix_projectbindingclaim_connection_id', 'projectbindingclaim', ['connection_id'])
    op.create_index('ix_projectbindingclaim_external_id', 'projectbindingclaim', ['external_id'])
    op.create_index('ix_projectbindingclaim_project_id', 'projectbindingclaim', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_projectbindingclaim_project_id', table_name='projectbindingclaim')
    op.drop_index('ix_projectbindingclaim_external_id', table_name='projectbindingclaim')
    op.drop_index('ix_projectbindingclaim_connection_id', table_name='projectbindingclaim')
    op.drop_table('projectbindingclaim')
    op.drop_index('ix_projectautomation_run_connection_id', table_name='projectautomation')
    op.drop_index('ix_projectautomation_connection_id', table_name='projectautomation')
    op.drop_index('ix_projectautomation_project_id', table_name='projectautomation')
    op.drop_table('projectautomation')
