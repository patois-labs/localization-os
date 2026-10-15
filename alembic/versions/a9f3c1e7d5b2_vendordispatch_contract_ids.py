"""vendordispatch.contract_ids - the return contract id ledger (Wave B)

Revision ID: a9f3c1e7d5b2
Revises: e1f4c7a9d2b6
Create Date: 2026-08-08 18:00:00.000000

ONE nullable JSON column on `vendordispatch` (PROPOSAL-external-processing-step Wave B,
the guarded RETURN). It carries `{"dispatched": [...], "returned": [...]}`:

  dispatched — the exact segment-id multiset the outbound contract carried, written
               ONCE at dispatch. Not derivable later: the document's segment rows drift
               after dispatch (a later import can add ids the vendor was never sent), so
               a "no extras / no unknown ids" gate derived from live rows would admit
               them.
  returned   — the ids ALREADY ADMITTED by earlier PARTIAL returns. Not derivable from
               segment state either: a stored target cannot say whether THIS return
               produced it or it was already there, so a duplicate return would be
               invisible.

Additive + nullable: every pre-existing (Wave A) VendorDispatch row backfills NULL. A
NULL ledger is treated by the return path as "un-accountable" and REFUSES the return
(fail-closed) rather than guessing — a Wave-A leg dispatched before this column existed
has no recorded contract, and admitting content against a guessed id set is exactly the
failure this column exists to prevent.

The app/db.py `_ADDED_COLUMNS` shim carries the same column so a non-alembic SQLite boot
(and the hermetic test create_all path) builds it identically -- the nullability-parity
CI guard compares the two.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9f3c1e7d5b2'
down_revision: Union[str, None] = 'e1f4c7a9d2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('vendordispatch', schema=None) as batch_op:
        batch_op.add_column(sa.Column('contract_ids', sa.JSON(), nullable=True))
    # No backfill: NULL already reads as "no recorded contract" (fail-closed).


def downgrade() -> None:
    with op.batch_alter_table('vendordispatch', schema=None) as batch_op:
        batch_op.drop_column('contract_ids')
