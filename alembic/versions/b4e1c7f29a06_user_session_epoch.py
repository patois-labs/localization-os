"""user.session_epoch (#138: server-side session invalidation on logout)

Revision ID: b4e1c7f29a06
Revises: c129a7e4b601
Create Date: 2026-09-16 09:00:00.000000

Adds the #138 session generation counter to `user`:

  user.session_epoch  -- INTEGER NOT NULL, server_default "0".

The web session is a stateless itsdangerous-signed cookie, so `logout()` could only
clear the CLIENT copy: a captured or cached pre-logout cookie stayed usable until it
expired. Every session now carries the epoch it was minted under (`session["se"]`)
and `security.session_user` refuses a cookie whose epoch is missing or does not match
this column. `logout()` and `change_password()` bump it, so one logout kills every
outstanding cookie for that user on every device -- with no session table and no
blacklist, just one integer per user.

Back-compat: NOT NULL with server_default "0" means every pre-existing row reads 0,
so an upgraded install is byte-identical until someone logs out. The ONE visible
one-time effect is at deploy: sessions minted BEFORE the upgrade carry no `se` key at
all and are refused once, so anyone signed in at deploy time signs in again. That
happens exactly once, never repeatedly.

Hand-written (no autogenerate) off the head c129a7e4b601, same convention as the
sibling additive-column revisions. Runs clean on SQLite (batch_alter_table) and
Postgres (native ALTER). The SQLite non-alembic boot path carries the same column in
db._ADDED_COLUMNS["user"].
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e1c7f29a06'
down_revision: Union[str, None] = 'c129a7e4b601'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'session_epoch',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('session_epoch')
