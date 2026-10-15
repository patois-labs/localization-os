"""notification channels, routes and outbox (project automation Part B)

Revision ID: c5d8f2a6b9e3
Revises: c1f0a7d4e2b6
Create Date: 2026-10-13 14:00:00.000000

Three NEW, empty tables (nothing existing is touched): notifychannel (an org-owned outbound
destination; the URL and signing secret live in the Fernet blob secret_enc), notifyroute (project >
content type > project type > customer > org default routing) and notifyoutbox (the outbox and
delivery log). Hand-written off a7c3e9f1b2d4, re-chained after c1f0a7d4e2b6 (custom fields) at merge. Other builders add their
own revisions in parallel: the final merger re-points down_revision so there is exactly one head.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d8f2a6b9e3'
down_revision: Union[str, None] = 'c1f0a7d4e2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'notifychannel',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False, server_default='slack'),
        sa.Column('name', sa.String(), nullable=False, server_default=''),
        sa.Column('config', sa.JSON(), nullable=True),
        sa.Column('secret_enc', sa.String(), nullable=False, server_default=''),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('last_test_at', sa.DateTime(), nullable=True),
        sa.Column('last_test_ok', sa.Boolean(), nullable=True),
        sa.Column('last_test_message', sa.String(), nullable=False, server_default=''),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_notifychannel_org_id', 'notifychannel', ['org_id'], unique=False)
    op.create_table(
        'notifyroute',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('channel_id', sa.Integer(), nullable=True),
        sa.Column('scope', sa.String(), nullable=False, server_default='org'),
        sa.Column('scope_ref', sa.String(), nullable=False, server_default=''),
        sa.Column('events', sa.JSON(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['notifychannel.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'scope', 'scope_ref', name='ux_notifyroute'),
    )
    op.create_index('ix_notifyroute_org_id', 'notifyroute', ['org_id'], unique=False)
    op.create_index('ix_notifyroute_channel_id', 'notifyroute', ['channel_id'], unique=False)
    op.create_table(
        'notifyoutbox',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('channel_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('event', sa.String(), nullable=False, server_default=''),
        sa.Column('dedup_key', sa.String(), nullable=False, server_default=''),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=False, server_default=''),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['channel_id'], ['notifychannel.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'dedup_key', name='ux_notifyoutbox_dedup'),
    )
    op.create_index('ix_notifyoutbox_org_id', 'notifyoutbox', ['org_id'], unique=False)
    op.create_index('ix_notifyoutbox_channel_id', 'notifyoutbox', ['channel_id'], unique=False)
    op.create_index('ix_notifyoutbox_project_id', 'notifyoutbox', ['project_id'], unique=False)
    op.create_index('ix_notifyoutbox_status', 'notifyoutbox', ['status'], unique=False)


def downgrade() -> None:
    for ix in ('status', 'project_id', 'channel_id', 'org_id'):
        op.drop_index(f'ix_notifyoutbox_{ix}', table_name='notifyoutbox')
    op.drop_table('notifyoutbox')
    for ix in ('channel_id', 'org_id'):
        op.drop_index(f'ix_notifyroute_{ix}', table_name='notifyroute')
    op.drop_table('notifyroute')
    op.drop_index('ix_notifychannel_org_id', table_name='notifychannel')
    op.drop_table('notifychannel')
