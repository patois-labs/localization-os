"""customer 360 (customers as first-class entities, P4 / item 5)

Revision ID: a1c7f3e9b4d2
Revises: e6b4d9c2a1f8
Create Date: 2026-07-03 20:00:00.000000

Adds the customer-360 columns (app/services/customers.py + the /customers detail
page):

  customer.settings                          — JSON preferences blob
  customer.active_pairs                       — JSON list of "src->tgt" pairs
  customer.default_provider                   — EngineProfile name/id inherited at
                                                project creation
  customer.default_workflow_id                — scoped default workflow (FK workflow)
  customer.default_segmentation_profile_id    — forward ref to P6 (no FK yet — the
                                                segmentationprofile table lands in P6)
  connection.customer_id                      — customer scope (FK customer, nullable
                                                = global/unassigned)

All additive + nullable → existing customers/connections backfill to NULL, which is
byte-identical to the pre-P4 behaviour (a customer with no settings/defaults and a
global connection). Mirrors the SQLite _ensure_columns() additive migration in
app/db.py.

Hand-written (no autogenerate) to avoid spurious drop_index noise — same pattern as
d2f5a8c1b6e9_project_status_lifecycle.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c7f3e9b4d2'
down_revision: Union[str, None] = 'e6b4d9c2a1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('settings', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('active_pairs', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('default_provider', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('default_workflow_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('default_segmentation_profile_id', sa.Integer(),
                                      nullable=True))
        batch_op.create_foreign_key('fk_customer_default_workflow', 'workflow',
                                    ['default_workflow_id'], ['id'])
    with op.batch_alter_table('connection', schema=None) as batch_op:
        batch_op.add_column(sa.Column('customer_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_connection_customer', 'customer',
                                    ['customer_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('connection', schema=None) as batch_op:
        batch_op.drop_constraint('fk_connection_customer', type_='foreignkey')
        batch_op.drop_column('customer_id')
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.drop_constraint('fk_customer_default_workflow', type_='foreignkey')
        batch_op.drop_column('default_segmentation_profile_id')
        batch_op.drop_column('default_workflow_id')
        batch_op.drop_column('default_provider')
        batch_op.drop_column('active_pairs')
        batch_op.drop_column('settings')
