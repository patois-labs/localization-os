"""project automation round 2: claim hand-over bookkeeping

Revision ID: e1b7c3a9d5f2
Revises: d9a4f2c7e1b8
Create Date: 2026-10-13 15:00:00.000000

Two additive columns (no new table):

  projectbindingclaim.wanted_by   nullable INTEGER, the project whose last complete run was
                                  refused this pair (the receiver of a hand-over).
  projectautomation.blocked_sig   VARCHAR '', the claim-block signature the project was last
                                  notified about (a message goes out when it changes).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e1b7c3a9d5f2'
down_revision: Union[str, None] = 'd9a4f2c7e1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('projectbindingclaim', schema=None) as batch_op:
        batch_op.add_column(sa.Column('wanted_by', sa.Integer(), nullable=True))
        batch_op.create_index('ix_projectbindingclaim_wanted_by', ['wanted_by'])
    with op.batch_alter_table('projectautomation', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('blocked_sig', sa.String(), nullable=False, server_default='')
        )


def downgrade() -> None:
    with op.batch_alter_table('projectautomation', schema=None) as batch_op:
        batch_op.drop_column('blocked_sig')
    with op.batch_alter_table('projectbindingclaim', schema=None) as batch_op:
        batch_op.drop_index('ix_projectbindingclaim_wanted_by')
        batch_op.drop_column('wanted_by')
