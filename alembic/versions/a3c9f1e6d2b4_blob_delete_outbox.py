"""blob delete outbox (#399 failed-erase retry identity)

Revision ID: a3c9f1e6d2b4
Revises: e3b9d5a1c7f4
Create Date: 2026-10-04 12:00:00.000000

Adds the finding-dbdata-07 fix table:

  blobdeleteoutbox (new table) — one row per blob whose deletion FAILED while its
      document row was erased: the canonical blob key (unique — a retried erase can
      never stack duplicate pending rows), the erased document's id as a VALUE (no FK:
      the Document row is deleted in the same transaction by design), an attempts
      counter, and created_at. retention.purge drains the ledger every sweep and
      deletes each row once its retry reports REMOVED or MISSING.

All additive: the new table is empty on upgrade and no existing table is touched.
Mirrors the SQLite create_all() new-table + Postgres-alembic-authority pattern
(app/db.py) and models.BlobDeleteOutbox; the column-completeness CI guard
(tests/verify_migrations.py) asserts this revision's column set equals the model's.
The server_default on the two NOT-NULL int columns mirrors the model's
sa_column_kwargs so create_all and the migration agree (the Project.project_type
lesson). Hand-written (no autogenerate) off the head e3b9d5a1c7f4.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3c9f1e6d2b4"
down_revision: Union[str, None] = "e3b9d5a1c7f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blobdeleteoutbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("blob_key", sa.String(), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_blobdeleteoutbox_blob_key", "blobdeleteoutbox", ["blob_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_blobdeleteoutbox_blob_key", table_name="blobdeleteoutbox")
    op.drop_table("blobdeleteoutbox")
