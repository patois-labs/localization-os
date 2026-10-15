"""workflowstep MQM-aligned QE score + routing gate (roadmap gap #1 + #6)

Revision ID: a7d2e5c9b3f4
Revises: f4c8b2a9e1d7
Create Date: 2026-07-06 12:00:00.000000

Adds three per-step columns implementing the consensus HITL pattern (Phrase QPS /
XTM Intelligent Score / Smartling LQE): score every produced segment 0-100 with an
MQM-aligned LLM judge, route only low-confidence ones to a human.

  - qe_gate (bool, default 0): when ON, score each segment the step produces and
    route those below qe_threshold to human review (SegStatus.NEEDS_REVIEW).
  - qe_threshold (int, default 70): the pass/route cutoff (score >= threshold
    proceeds; below routes to review).
  - qe_provider (str, nullable): optional engine picker for the judge; NULL reuses
    the step's own provider (cheap-by-default).

All default OFF / neutral so an existing step's behaviour is byte-identical (no
judge call, unchanged prompt/events). Mirrors the SQLite _ensure_columns() additive
migration in app/db.py. Hand-written (no autogenerate) to avoid drop_index noise.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d2e5c9b3f4'
down_revision: Union[str, None] = 'f4c8b2a9e1d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('qe_gate', sa.Boolean(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('qe_threshold', sa.Integer(),
                                      nullable=False, server_default='70'))
        batch_op.add_column(sa.Column('qe_provider', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('qe_provider')
        batch_op.drop_column('qe_threshold')
        batch_op.drop_column('qe_gate')
