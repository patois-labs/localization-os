"""workflowstep per-step image context toggle (Batch 14 T2, SPEC §25)

Revision ID: b4d9f1a7c2e6
Revises: f7b3c1d9e2a4
Create Date: 2026-08-27 12:00:00.000000

Adds one per-step column for the per-`WorkflowStep` image-context toggle (the maintainer's
customizable-breadth ratification, SPEC-string-image-context §25):
  - use_image_context (bool, default 1/ON): whether THIS llm_agent step sees the
    string's attached images when the project engine is vision-capable. DEFAULT TRUE
    preserves the ratified "every LLM_AGENT step sees the image" behavior byte-for-byte
    until an author opts an individual step out; inert on non-llm_agent step types (the
    orchestrator eligibility predicate excludes them by type regardless).

DEFAULT-TRUE `server_default='1'` (the tm_examples precedent, NOT the qe_gate '0'
precedent) backfills every existing row to ON. Chained off the Batch 14 T1 KeyImage
head (f7b3c1d9e2a4) because T2 forks after T1 merges. Mirrors the SQLite
_ensure_columns() additive shim in app/db.py + the model (nullability-parity CI guard).
Hand-written (no autogenerate) to avoid spurious index noise, same convention as
a9e2c7f4b1d8_workflowstep_tm_examples.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d9f1a7c2e6'
down_revision: Union[str, None] = 'f7b3c1d9e2a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('use_image_context', sa.Boolean(),
                                      nullable=False, server_default='1'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('use_image_context')
