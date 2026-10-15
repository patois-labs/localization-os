"""password policy: user.password_changed_at, user.password_must_change, passwordhistory

Revision ID: e5a1c8d3f7b2
Revises: a7e3c9d2b5f1
Create Date: 2026-09-25 09:00:00.000000

The password policy for local accounts (HECVAT AAAI-03). Columns mirror the models
exactly (verify_alembic_authority compares alembic-head vs create_all(models) for
column set and nullability):

  user.password_changed_at   DATETIME NULL. When the local password was last set; the
                             password_max_age_days rule counts from it. NULL = never
                             expires (no local password).
  user.password_must_change  BOOLEAN NOT NULL DEFAULT 0. Set by an admin reset; the
                             person must choose a new password at the next sign-in.

  passwordhistory            one previous local password per row, as its Argon2id hash:
    id             INTEGER  PK, NOT NULL
    user_id        INTEGER  NOT NULL, FK user.id ON DELETE CASCADE
                            (index ix_passwordhistory_user_id)
    password_hash  VARCHAR  NOT NULL
    created_at     DATETIME NOT NULL

BACK-COMPAT: every pre-existing row WITH a local password is backfilled to
password_changed_at = CURRENT_TIMESTAMP, so an admin who later turns max age on starts
every existing account's clock at the upgrade, never retroactively (no mass lockout to
the change page). Rows without a password (SSO/LDAP/SCIM) stay NULL. The SQLite
non-alembic path carries the same backfill in app/db.py's just-added branch.
password_must_change defaults 0 for every row, so nobody is sent to the change page by
the upgrade. The history table starts empty: the reuse rule also checks the CURRENT
hash on the user row, so it protects existing accounts from the first change.

Hand-written off the head a7e3c9d2b5f1. SQLite via batch_alter_table, Postgres native.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a1c8d3f7b2'
down_revision: Union[str, None] = 'a7e3c9d2b5f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_changed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column('password_must_change', sa.Boolean(), nullable=False, server_default='0')
        )
    op.execute(
        'UPDATE "user" SET password_changed_at = CURRENT_TIMESTAMP '
        "WHERE password_changed_at IS NULL AND password_hash IS NOT NULL "
        "AND password_hash <> ''"
    )
    op.create_table(
        'passwordhistory',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            'user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('password_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_passwordhistory_user_id', 'passwordhistory', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_passwordhistory_user_id', table_name='passwordhistory')
    op.drop_table('passwordhistory')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('password_must_change')
        batch_op.drop_column('password_changed_at')
