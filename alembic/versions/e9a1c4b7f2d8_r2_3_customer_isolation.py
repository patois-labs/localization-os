"""R2-3: audit-log customer isolation + customer-bound users

Revision ID: e9a1c4b7f2d8
Revises: d8f2c6b4a7e1
Create Date: 2026-07-05 12:00:00.000000

R2-3 (item 3) — customer-scoped audit isolation + the customer-bound-user concept:

  - auditlog.customer_id (Integer, nullable, FK customer.id) — NULL = a SYSTEM /
    internal event. A pure FILTER/VIEW column that lives OUTSIDE the HMAC-chained
    payload (audit._compute_hash hashes prev|ts|actor|action|target|detail|ip;
    customer_id is NOT among them), so adding it does NOT change any existing
    row_hash and the whole append-only chain keeps verifying byte-identically over
    old + new rows. Existing rows read NULL = system (the accepted semantics — no
    retroactive backfill of historical rows).

  - user.customer_id (Integer, nullable, FK customer.id) — NULL = INTERNAL STAFF
    (today's behaviour, byte-identical). A non-NULL value makes the account
    CUSTOMER-BOUND (translator/linguist/viewer only; visibility + /audit confined
    to that one customer). Nullable so every existing user reads NULL = staff.

NO backfill: NULL is the correct value for both columns on every existing row, so
(unlike the org_id migrations) there is no UPDATE step — the columns just carry
the new scope.

Hand-written (no autogenerate) off the P7 head d8f2c6b4a7e1 to avoid the spurious
drop_index noise autogenerate emits for the app/db.py hot-path indexes. Mirrors the
SQLite _ensure_columns() additive migration for auditlog.customer_id +
user.customer_id.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9a1c4b7f2d8'
down_revision: Union[str, None] = 'd8f2c6b4a7e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # auditlog.customer_id (nullable FK -> customer.id). A view/filter column
    # outside the HMAC payload — old chains keep verifying.
    with op.batch_alter_table('auditlog', schema=None) as batch_op:
        batch_op.add_column(sa.Column('customer_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_auditlog_customer_id_customer',
                                    'customer', ['customer_id'], ['id'])
    # user.customer_id (nullable FK -> customer.id). NULL = internal staff.
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('customer_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_user_customer_id_customer',
                                    'customer', ['customer_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('customer_id')
    with op.batch_alter_table('auditlog', schema=None) as batch_op:
        batch_op.drop_column('customer_id')
