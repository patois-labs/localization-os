"""job.cost_usd (the exact USD the ledger booked for each Job, #364)

Revision ID: d1a8c5e3b7f2
Revises: c4e7f2b9a1d3
Create Date: 2026-10-02 12:00:00.000000

Adds one ADDITIVE, NULLABLE column to `job`:

  job.cost_usd  -- nullable FLOAT. The USD the spend ledger booked while the job ran
                   (run_completion / complete_direct / record_usage, failover and failed
                   attempts included, a free engine's calls 0.0). Reports, the project
                   budget meter and the project_check refusal prefer it over re-pricing the
                   job's tokens by the engine's CURRENT profile.

WHY NULLABLE, NO DEFAULT, NO BACKFILL: NULL is the honest "unknown" for every Job written
before this column (their spend was never recorded per job), and reports._job_cost prices
those exactly as before. Stamping a backfill would invent a figure.

Hand-written off the head c4e7f2b9a1d3, same convention as the sibling additive-column
revisions. Runs clean on SQLite (batch_alter_table) and Postgres (native ALTER); the
_ADDED_COLUMNS["job"] shim in app/db.py carries the same column for a non-alembic boot.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1a8c5e3b7f2'
down_revision: Union[str, None] = 'c4e7f2b9a1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cost_usd', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.drop_column('cost_usd')
