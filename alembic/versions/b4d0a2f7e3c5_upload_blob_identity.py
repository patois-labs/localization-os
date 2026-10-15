"""Immutable upload blob identity (#399).

Revision ID: b4d0a2f7e3c5
Revises: a3c9f1e6d2b4

Legacy rows stay NULL and resolve to their existing id.format keys. New uploads
persist a UUID key before writing bytes, so a reused row ID cannot overwrite a
pending deletion or have its new content destroyed by that retry.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b4d0a2f7e3c5"
down_revision: Union[str, None] = "a3c9f1e6d2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("document", schema=None) as batch_op:
        batch_op.add_column(sa.Column("upload_blob_key", sa.String(), nullable=True))


def downgrade() -> None:
    # Operational rollback requires the pre-deploy DB/uploads snapshot: an older
    # application cannot read UUID uploads after this column is removed.
    with op.batch_alter_table("document", schema=None) as batch_op:
        batch_op.drop_column("upload_blob_key")
