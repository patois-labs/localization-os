"""user.sso_issuer + user.sso_subject (identity-01: OIDC subject binding)

Revision ID: c5e2a8d4f1b7
Revises: b4d0a2f7e3c5
Create Date: 2026-10-04 23:00:00.000000

Adds the OIDC subject binding to `user`:

  user.sso_issuer   -- nullable VARCHAR: the OIDC issuer the account is bound to.
  user.sso_subject  -- nullable VARCHAR, indexed (ix_user_sso_subject): that issuer's
                       stable `sub` for the account.

OIDC sign-in used to map an id_token to an account by the email claim alone. It now
looks the account up by (issuer, sub) first, and links by email only once, when the
email is verified, recording the binding.

Back-compat: both columns are NULL on every pre-existing row (= not bound yet). No
backfill is possible (the install never stored `sub`). An existing SSO account binds
on its next OIDC sign-in that carries a verified email (or when the admin has turned
on sso_trust_unverified_email for an IdP that never sends email_verified). Nothing
else reads these columns, so every other sign-in path is unchanged.

Hand-written off the head b4d0a2f7e3c5. Native ALTER ADD COLUMN (no batch_alter_table,
so the #158 expression index ux_user_email_lower is never rebuilt). The SQLite
non-alembic boot path carries the same columns in db._ADDED_COLUMNS["user"] and the
index in db._INDEXES.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e2a8d4f1b7'
down_revision: Union[str, None] = 'b4d0a2f7e3c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user', sa.Column('sso_issuer', sa.String(), nullable=True))
    op.add_column('user', sa.Column('sso_subject', sa.String(), nullable=True))
    op.create_index('ix_user_sso_subject', 'user', ['sso_subject'])


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index('ix_user_sso_subject', table_name='user')
    if bind.dialect.name == 'sqlite':
        op.execute('ALTER TABLE "user" DROP COLUMN sso_subject')
        op.execute('ALTER TABLE "user" DROP COLUMN sso_issuer')
    else:
        op.drop_column('user', 'sso_subject')
        op.drop_column('user', 'sso_issuer')
