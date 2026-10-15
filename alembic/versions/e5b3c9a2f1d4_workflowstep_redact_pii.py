"""workflowstep redact_pii

Revision ID: e5b3c9a2f1d4
Revises: d4a1f2c7e8b9
Create Date: 2026-07-03 10:00:00.000000

Adds WorkflowStep.redact_pii — a per-step choice to scrub PII before that step's
engine call and restore it after (mirrors translate.py's redact->engine->restore).
Raw PII never lands in Segment.target_text, the TM, StepRun input/output, or stats
when a step opts in. server_default='0' backfills existing steps to OFF (no change
in behavior for existing workflows unless the global pii_redaction setting is on,
which still forces redaction). Mirrors the SQLite _ensure_columns() additive
migration in app/db.py.

Hand-written (no autogenerate) to avoid spurious drop_index noise.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b3c9a2f1d4'
down_revision: Union[str, None] = 'd4a1f2c7e8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('redact_pii', sa.Boolean(), nullable=False,
                                      server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('redact_pii')
