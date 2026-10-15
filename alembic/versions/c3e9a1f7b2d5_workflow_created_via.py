"""workflow created_via (origin stamp: manual | draft)

Revision ID: c3e9a1f7b2d5
Revises: c1d5e8f3a9b4
Create Date: 2026-07-18 09:30:00.000000

Adds Workflow.created_via (nullable TEXT): how a workflow was created —
"manual" (create_workflow / the blank-workflow path) or "draft"
(apply_workflow_draft / Design-a-workflow, including every multilingual variant it
fans out). NULL/absent = legacy/unknown, so every existing row backfills to NULL
(the UI renders no origin chip). SPEC-workflows-redesign Phase 3 corrected the
research's "origin is free from adapted_from_id" claim — origin needs this explicit
stamp. Additive column only (the migration-beyond-ADD-COLUMN caveat does not apply).
Nullable with no server_default (like org_id / customer_id), so legacy rows read NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e9a1f7b2d5'
down_revision: Union[str, None] = 'c1d5e8f3a9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflow', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_via', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflow', schema=None) as batch_op:
        batch_op.drop_column('created_via')
