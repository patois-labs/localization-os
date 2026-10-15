"""vendordispatch.protocol — snapshot the transport protocol on the dispatch leg (F2)

Revision ID: d4e5f6a7b8c9
Revises: 2a98273c3c99
Create Date: 2026-08-10 09:00:00.000000

ONE purely-additive NULLABLE column:

  vendordispatch.protocol (VARCHAR, nullable, NO server default) — the transport protocol
    this leg was DISPATCHED under, captured from vendor_protocol(vendor) at dispatch time.
    Every protocol-sensitive consumer (retry re-dispatch, the poll refusal, pollable_legs,
    leg_to_dict.pollable, the rework re-dispatch) keys off THIS leg snapshot rather than the
    vendor's LIVE protocol, so a vendor flipped native<->manual while a leg is parked can
    never retroactively change how that already-dispatched leg is treated (no phantom manual
    "delivery" of a native retry; no native poll / bearer forwarding for a package a manual
    leg never sent).

DELIBERATELY NULLABLE WITH NO SERVER DEFAULT (unlike vendor.protocol, which defaults
'native'): a legacy leg row backfills to NULL, and vendor_dispatch.leg_protocol reads NULL
as "fall back to the vendor's live protocol" — byte-identical to the pre-F2 behavior where
every consumer already read the vendor's live protocol. Baking 'native' onto legacy rows
would instead assert a snapshot those legs never actually took, so NULL is the honest value.

Hand-written (no autogenerate) off the head 2a98273c3c99, same additive-column convention as
the sibling vendordispatch revisions. Plain op.add_column emits a native ALTER ... ADD COLUMN
on both SQLite and Postgres (no table rebuild, so vendordispatch's indexes survive), and the
SAME column is added on the non-alembic SQLite init_db/_ensure_columns path via
app/db._ADDED_COLUMNS["vendordispatch"] so all three build paths agree — the nullability-
parity CI guard compares them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = '2a98273c3c99'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain add_column (no batch): a native ALTER ... ADD COLUMN on SQLite + Postgres, so the
    # vendordispatch table is never rebuilt. NULLABLE, no server default -> every legacy leg
    # backfills to NULL (leg_protocol then falls back to the vendor's live protocol).
    op.add_column('vendordispatch', sa.Column('protocol', sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        # Native DROP COLUMN (SQLite >= 3.35) rather than a batch rebuild.
        op.execute('ALTER TABLE vendordispatch DROP COLUMN protocol')
    else:
        op.drop_column('vendordispatch', 'protocol')
