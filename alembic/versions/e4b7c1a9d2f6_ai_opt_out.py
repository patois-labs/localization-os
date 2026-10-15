"""AI opt-out: project.ai_opt_out + customer.ai_opt_out + evaldataset source project

Revision ID: e4b7c1a9d2f6
Revises: e5a1c8d3f7b2
Create Date: 2026-09-25 04:30:00.000000

HECVAT DPAI-08 (opt out of AI use). Two purely additive boolean columns:

  project.ai_opt_out   NOT NULL, server_default '0'. True = no content of this project
                       is sent to any AI engine or model (MT, LLM steps, QE/LQA judges,
                       auto-fix, Assistant, embeddings, LLMemory, OCR/vision).
  customer.ai_opt_out  NOT NULL, server_default '0'. True = the same for EVERY project of
                       the customer (current and future); a project cannot undo it.

  evaldataset.source_project_id / source_customer_id  nullable INTEGER (provenance, not
                       FKs, like source_document_id). The Lab snapshot's source project and
                       customer, so the opt-out check survives the source document being
                       deleted. Backfilled from the document where it still exists and
                       predates the dataset; otherwise NULL, and the check fails closed.

Every existing project/customer row backfills to 0 = AI allowed = byte-identical to today. The SAME
columns are added on the SQLite init_db/_ensure_columns path via
app/db._ADDED_COLUMNS["project"]/["customer"], so all three build paths agree.

Plain op.add_column (NO batch_alter_table): the project table carries the expression
index ux_project_inbox_key, which a SQLite batch rebuild silently drops (see
2a98273c3c99).

Re-parented onto e5a1c8d3f7b2 (password policy), which merged first off the same parent
a7e3c9d2b5f1, so the chain stays linear with exactly one head. Native ADD/DROP COLUMN needs no rebuild on SQLite >= 3.35 or Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4b7c1a9d2f6'
down_revision: Union[str, None] = 'e5a1c8d3f7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The backfill of evaldataset.source_project_id / source_customer_id. Kept verbatim in
# step with app/db._EVALDATASET_SOURCE_BACKFILL_SQL (the SQLite shim path): only a source
# document that still exists AND predates the dataset is trusted; anything else stays NULL
# and the opt-out check fails closed on it.
_BACKFILL = (
    "UPDATE evaldataset SET "
    "source_project_id = (SELECT d.project_id FROM document d "
    "WHERE d.id = evaldataset.source_document_id "
    "AND d.created_at <= evaldataset.created_at), "
    "source_customer_id = (SELECT p.customer_id FROM document d "
    "JOIN project p ON p.id = d.project_id "
    "WHERE d.id = evaldataset.source_document_id "
    "AND d.created_at <= evaldataset.created_at) "
    "WHERE source_document_id IS NOT NULL AND source_project_id IS NULL"
)


def upgrade() -> None:
    op.add_column('project', sa.Column('ai_opt_out', sa.Boolean(), nullable=False,
                                       server_default='0'))
    op.add_column('customer', sa.Column('ai_opt_out', sa.Boolean(), nullable=False,
                                        server_default='0'))
    # A Lab snapshot's source project/customer (bare provenance ints, like
    # source_document_id), so the opt-out check survives the document being deleted.
    op.add_column('evaldataset', sa.Column('source_project_id', sa.Integer(), nullable=True))
    op.add_column('evaldataset', sa.Column('source_customer_id', sa.Integer(), nullable=True))
    op.execute(_BACKFILL)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        # Native DROP COLUMN (SQLite >= 3.35) rather than a batch rebuild, which would
        # silently drop project's expression-based ux_project_inbox_key index.
        op.execute('ALTER TABLE evaldataset DROP COLUMN source_customer_id')
        op.execute('ALTER TABLE evaldataset DROP COLUMN source_project_id')
        op.execute('ALTER TABLE customer DROP COLUMN ai_opt_out')
        op.execute('ALTER TABLE project DROP COLUMN ai_opt_out')
    else:
        op.drop_column('evaldataset', 'source_customer_id')
        op.drop_column('evaldataset', 'source_project_id')
        op.drop_column('customer', 'ai_opt_out')
        op.drop_column('project', 'ai_opt_out')
