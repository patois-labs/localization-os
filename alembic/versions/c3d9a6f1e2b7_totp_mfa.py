"""TOTP multi-factor sign-in: usermfa + mfarecoverycode

Revision ID: c3d9a6f1e2b7
Revises: e4b7c1a9d2f6
Create Date: 2026-09-25 18:00:00.000000

Built-in TOTP two-step sign-in (RFC 6238) for local and LDAP accounts (HECVAT AAAI-17,
DCTR-14). Two NEW tables, nothing on an existing table changes. Columns mirror the models
exactly (verify_alembic_authority compares alembic-head vs create_all(models) for column
set and nullability):

  usermfa            one authenticator per account
    id            INTEGER  PK, NOT NULL
    user_id       INTEGER  NOT NULL, FK user.id ON DELETE CASCADE
                           (UNIQUE index ix_usermfa_user_id: at most one row per user)
    secret_enc    VARCHAR  NOT NULL  (Fernet ciphertext of the base32 secret)
    enabled_at    DATETIME NULL      (NULL = enrollment started, not confirmed)
    last_step     INTEGER  NOT NULL DEFAULT 0 (newest accepted 30 s step; replay guard)
    last_used_at  DATETIME NULL
    fail_count         INTEGER  NOT NULL DEFAULT 0 (per-account wrong codes in the window)
    fail_window_start  DATETIME NULL
    locked_until       DATETIME NULL     (code checks locked until then, any address)
    created_at    DATETIME NOT NULL

  mfarecoverycode    single-use recovery codes, Argon2id hashes
    id            INTEGER  PK, NOT NULL
    user_id       INTEGER  NOT NULL, FK user.id ON DELETE CASCADE
                           (index ix_mfarecoverycode_user_id)
    code_hash     VARCHAR  NOT NULL
    used_at       DATETIME NULL
    created_at    DATETIME NOT NULL

BACK-COMPAT: both tables start empty and the `mfa_required` setting defaults to "off", so
an upgraded install signs in exactly as before until someone enrolls or an admin turns the
requirement on. The SQLite non-alembic path gets the same tables from create_all().

Hand-written off the head e4b7c1a9d2f6. Plain create_table (no batch rebuild of anything).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d9a6f1e2b7'
down_revision: Union[str, None] = 'e4b7c1a9d2f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'usermfa',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            'user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('secret_enc', sa.String(), nullable=False),
        sa.Column('enabled_at', sa.DateTime(), nullable=True),
        sa.Column('last_step', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('fail_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('fail_window_start', sa.DateTime(), nullable=True),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_usermfa_user_id', 'usermfa', ['user_id'], unique=True)
    op.create_table(
        'mfarecoverycode',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            'user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('code_hash', sa.String(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_mfarecoverycode_user_id', 'mfarecoverycode', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_mfarecoverycode_user_id', table_name='mfarecoverycode')
    op.drop_table('mfarecoverycode')
    op.drop_index('ix_usermfa_user_id', table_name='usermfa')
    op.drop_table('usermfa')
