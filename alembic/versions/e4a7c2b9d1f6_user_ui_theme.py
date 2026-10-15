"""user.ui_theme (ADR 0063: per-user Light / Dark / Match my computer)

Revision ID: e4a7c2b9d1f6
Revises: b8e2f4a6c1d3
Create Date: 2026-10-08 13:00:00.000000

Adds the signed-in user's appearance choice to `user`:

  user.ui_theme  -- VARCHAR NOT NULL, server_default "".

"light" or "dark"; "" means Match my computer (follow the operating system). Set from
Account settings, the account menu, POST /api/account/theme and `account set-theme`;
rendered into every signed-in page before first paint. Every pre-existing row reads ""
(Match my computer), the same default the per-browser toggle had.

Hand-written (no autogenerate), additive column only. Runs clean on SQLite
(batch_alter_table) and Postgres (native ALTER). The SQLite non-alembic boot path carries
the same column in db._ADDED_COLUMNS["user"].
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a7c2b9d1f6'
down_revision: Union[str, None] = 'b8e2f4a6c1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'ui_theme',
                sa.String(),
                nullable=False,
                server_default='',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('ui_theme')
