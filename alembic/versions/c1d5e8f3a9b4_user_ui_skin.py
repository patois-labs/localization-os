"""user ui_skin (per-user UI skin preference)

Revision ID: c1d5e8f3a9b4
Revises: f3b9d1a7c4e2
Create Date: 2026-07-16 22:45:00.000000

Adds User.ui_skin (nullable TEXT): the signed-in user's chosen UI skin —
one of {kiln, instrument, ledger}. NULL/absent = kiln (the default), so every
existing row backfills to the Kiln render with no data change. Additive column
only (the migration-beyond-ADD-COLUMN caveat does not apply). Nullable with no
server_default (like org_id / customer_id), so legacy rows read NULL = kiln.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d5e8f3a9b4'
down_revision: Union[str, None] = 'f3b9d1a7c4e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ui_skin', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('ui_skin')
