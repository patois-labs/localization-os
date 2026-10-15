"""api_key.expires_at (optional API key expiry, #270 r2-apiauth-10)

Revision ID: c4e7f2b9a1d3
Revises: c3d5e7f9a1b2
Create Date: 2026-10-02 10:00:00.000000

Adds an optional expiry to per-user API keys:

  api_key.expires_at  -- nullable DATETIME. NULL = NO expiry (the pre-existing
                         behaviour; every already-issued key keeps working),
                         past-or-equal the timestamp the key is refused exactly
                         like a revoked one (apikeys.verify_key).

PURELY ADDITIVE, neutral-defaulted: a plain nullable column with no backfill.
There is deliberately NO UPDATE here -- stamping every existing key would be
a security regression (the whole point of this column is that nothing already
issued expires because of it). The SQLite _ADDED_COLUMNS["api_key"] shim in
app/db.py carries the same column so a non-alembic SQLite boot sees it too.

Hand-written (no autogenerate) off the head c3d5e7f9a1b2, same convention as
the sibling additive-column revisions. Runs clean on SQLite (batch_alter_table)
and Postgres (native ALTER).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e7f2b9a1d3'
down_revision: Union[str, None] = 'c3d5e7f9a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The SQLModel table is `apikey` (no underscore) -- see the baseline revision's
    # op.create_table('apikey', ...) and ApiKey.__tablename__; the `api_key` spelling is
    # only the setting/shim vocabulary, never the table.
    with op.batch_alter_table('apikey', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expires_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apikey', schema=None) as batch_op:
        # drop_column takes the column NAME (a string), not a sa.Column object.
        batch_op.drop_column('expires_at')
