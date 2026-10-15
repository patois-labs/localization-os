"""workflowstep F2 engine-layer upgrades (adaptive feedback + doc-context pre-pass)

Revision ID: f4c8b2a9e1d7
Revises: e3f1a9c7b6d4
Create Date: 2026-07-06 09:00:00.000000

Adds three per-step columns implementing CONNECTORS_FIRST_ROADMAP §F2 items 1-2
(ModernMT/Lara-derived prompt-context enrichment):
  - adaptive_feedback (bool, default 0): F2.1 in-run adaptive feedback — feed
    earlier confirmed source->target pairs of the SAME document into later
    segments' prompts so terminology/style self-propagate.
  - adaptive_feedback_window (int, default 5): the rolling window size for F2.1.
  - doc_context_prepass (bool, default 0): F2.2 document-context pre-pass — one
    cheap pre-pass call extracts a doc-level brief prepended (stable) to every
    segment prompt in the run.

All three default OFF / to the neutral window so an existing step's assembled
prompt is byte-identical (zero behavior change). Mirrors the SQLite
_ensure_columns() additive migration in app/db.py. Hand-written (no autogenerate)
to avoid spurious drop_index noise.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c8b2a9e1d7'
down_revision: Union[str, None] = 'e3f1a9c7b6d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('adaptive_feedback', sa.Boolean(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('adaptive_feedback_window', sa.Integer(),
                                      nullable=False, server_default='5'))
        batch_op.add_column(sa.Column('doc_context_prepass', sa.Boolean(),
                                      nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('doc_context_prepass')
        batch_op.drop_column('adaptive_feedback_window')
        batch_op.drop_column('adaptive_feedback')
