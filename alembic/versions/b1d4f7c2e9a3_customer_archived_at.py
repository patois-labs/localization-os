"""customer archive — nullable archived_at so a customer can be retired from pickers

Revision ID: b1d4f7c2e9a3
Revises: a3f7c1e9d5b2
Create Date: 2026-07-10 09:00:00.000000

SPEC-customer-archive: customer DELETE fails closed on any audit history (kept in
perpetuity — the maintainer confirmed), so archive is the way to RETIRE a customer that has
history. This revision adds the single column the feature keys on:

  customer.archived_at            — nullable timestamp. NULL = ACTIVE (every existing
                                    customer backfills to NULL, byte-identical). A
                                    non-NULL value marks the customer ARCHIVED: it
                                    disappears from every "available customer" picker
                                    (project creation, connection/glossary/content-
                                    type/workflow scoping, user binding) and from the
                                    default customers-list view, while its existing
                                    projects/connections/bound users keep working
                                    untouched (archive is a selection/listing concern,
                                    not a kill switch).

Additive + purely nullable — no backfill, no index (the column is only ever read as
`archived_at IS NULL` on the small customer table). The SAME column is added on the
SQLite init_db/_ensure_columns path via app/db._ADDED_COLUMNS["customer"], so all
three build paths (fresh create_all, SQLite upgrade, alembic/Postgres upgrade) agree.
Hand-written off the head a3f7c1e9d5b2; a PLAIN nullable column like the other
additive nullable columns (no FK).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1d4f7c2e9a3'
down_revision: Union[str, None] = 'a3f7c1e9d5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.drop_column('archived_at')
