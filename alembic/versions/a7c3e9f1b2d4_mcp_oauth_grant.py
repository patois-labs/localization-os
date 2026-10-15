"""mcp oauth grant (opt-in MCP OAuth authorization server, #753)

Revision ID: a7c3e9f1b2d4
Revises: e4a7c2b9d1f6
Create Date: 2026-10-11 12:00:00.000000

Adds the mcpoauthgrant table: one consented MCP OAuth grant per row (user, client, redirect
URI, granted scopes, the RFC 8707 resource the tokens are bound to) carrying the CURRENT token
generation as SHA-256 digests only (code, MCP access token, exchanged API token, refresh token,
previous refresh token for reuse detection) and the revoked_at tombstone.

All additive: a new, empty table; no existing table is touched. Mirrors the SQLite create_all()
new-table + alembic-authority pattern (app/db.py) and models.McpOAuthGrant; the
column-completeness guard in tests/verify_migrations.py pins this revision's column set to the
model. Hand-written off the head e4a7c2b9d1f6.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9f1b2d4'
down_revision: Union[str, None] = 'e4a7c2b9d1f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXED = ('user_id', 'code_hash', 'access_hash', 'api_hash', 'refresh_hash',
            'prev_refresh_hash')


def upgrade() -> None:
    op.create_table(
        'mcpoauthgrant',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.String(), nullable=False, server_default=''),
        sa.Column('client_name', sa.String(), nullable=False, server_default=''),
        sa.Column('redirect_uri', sa.String(), nullable=False, server_default=''),
        sa.Column('scopes', sa.String(), nullable=False, server_default='api'),
        sa.Column('resource', sa.String(), nullable=False, server_default=''),
        sa.Column('code_hash', sa.String(), nullable=True),
        sa.Column('code_challenge', sa.String(), nullable=False, server_default=''),
        sa.Column('code_expires_at', sa.DateTime(), nullable=True),
        sa.Column('code_redeemed', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('access_hash', sa.String(), nullable=True),
        sa.Column('api_hash', sa.String(), nullable=True),
        sa.Column('access_expires_at', sa.DateTime(), nullable=True),
        sa.Column('refresh_hash', sa.String(), nullable=True),
        sa.Column('prev_refresh_hash', sa.String(), nullable=True),
        sa.Column('refresh_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in _INDEXED:
        op.create_index(f'ix_mcpoauthgrant_{col}', 'mcpoauthgrant', [col], unique=False)


def downgrade() -> None:
    for col in _INDEXED:
        op.drop_index(f'ix_mcpoauthgrant_{col}', table_name='mcpoauthgrant')
    op.drop_table('mcpoauthgrant')
