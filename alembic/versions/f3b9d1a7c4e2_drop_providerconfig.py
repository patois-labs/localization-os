"""drop the dead providerconfig table

Revision ID: f3b9d1a7c4e2
Revises: e1c4f7a9d2b6
Create Date: 2026-07-16 12:00:00.000000

The providerconfig table (baseline schema) was never written or read by any code
path — engine configuration lives entirely in EngineProfile + Setting rows. The
SQLModel class is removed alongside this migration, so both the create_all (SQLite
default) path and the alembic-head (Postgres authority) path converge on a schema
with no providerconfig table. Plain DROP (no data — the table was never populated).

Downgrade recreates the table exactly as fcc52f1978bf_baseline_schema.py defined it.
Hand-written (no autogenerate), same convention as the surrounding revisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b9d1a7c4e2'
down_revision: Union[str, None] = 'e1c4f7a9d2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('providerconfig')


def downgrade() -> None:
    op.create_table(
        'providerconfig',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('default_model', sa.String(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
