"""Add invitation revocation timestamp and durable org ownership.

Revision ID: c129a7e4b601
Revises: e2b6c9f4a713
Create Date: 2026-09-14

#129: Invitations need revocation and ownership independent of the inviter's
email. Existing rows retain their tokens; NULL ownership is operator-only with
multi-org enabled. No historical org is guessed from mutable user identities.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c129a7e4b601"
down_revision: str | None = "e2b6c9f4a713"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("invite") as batch_op:
        batch_op.add_column(sa.Column("revoked_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("org_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invite") as batch_op:
        batch_op.drop_column("org_id")
        batch_op.drop_column("revoked_at")
