"""workflowstep TM few-shot examples (SPEC-context-metadata phase 2B)

Revision ID: a9e2c7f4b1d8
Revises: c8a3f1e7d9b4
Create Date: 2026-07-12 12:00:00.000000

Adds two per-step columns for the TM-few-shot quality lever (SPEC-context-metadata
phase 2 "Part B" — the #1 evidenced quality lever from CONTEXT-METADATA-RESEARCH §1):
  - tm_examples (bool, default 1/ON): render the top-N FUZZY TM matches for each
    segment as fenced example pairs in this step's prompt. Relevance-gated (a
    similarity floor + budget cap) so a bad/absent match injects NOTHING.
  - tm_examples_window (int, default 3): how many top matches to inject, clamped
    0-5 at read time (0 disables, like the toggle off).

Default ON so the lever is live, but a step with NO qualifying fuzzy match (or the
toggle OFF / window 0) renders nothing — the assembled prompt is byte-identical to
pre-2B until a match clears the floor. Mirrors the SQLite _ensure_columns() additive
migration in app/db.py. Hand-written (no autogenerate) to avoid spurious index noise,
same convention as f4c8b2a9e1d7_workflowstep_f2_engine_upgrades.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9e2c7f4b1d8'
down_revision: Union[str, None] = 'c8a3f1e7d9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tm_examples', sa.Boolean(),
                                      nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('tm_examples_window', sa.Integer(),
                                      nullable=False, server_default='3'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('tm_examples_window')
        batch_op.drop_column('tm_examples')
