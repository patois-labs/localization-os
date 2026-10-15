"""steprun failure shape + workflowstep.over_translation_guard (run-190 Items C, D)

Revision ID: b7e3f1a9c2d4
Revises: b9e3d7c1f5a2
Create Date: 2026-09-04 18:00:00.000000

Run 190 Item C — surface the per-step FAILURE SHAPE (attempts, provider, model, finish
reason, provider error class, output length, elapsed) on the run view / GET /api/runs/{id}
without an SSH into the host's LLM Ledger. Three ADDITIVE, NULLABLE columns on `steprun`:

  steprun.finish_reason — nullable VARCHAR. The terminal provider outcome's finish reason
                          for this step visit ("length" for the run-190 thinking burn,
                          "stop" on a clean success, "max_tokens", ...). Threaded up from
                          run_completion's result dict. NULL = unknown / pre-tracking.
  steprun.error_class   — nullable VARCHAR. The terminal exception TYPE NAME (e.g.
                          "ThinkingBurn", "ProviderTruncated", "UnderTranslationError") —
                          the structured half of the free-text `error` column, so a reader
                          can classify a failure without string-sniffing. NULL = success /
                          pre-tracking.
  steprun.output_chars  — nullable INTEGER. Characters of translation text this step visit
                          actually produced (0 on the empty-output thinking burn). NULL =
                          pre-tracking.

WHY NULLABLE, NO DEFAULT: NULL is the honest "pre-tracking / unknown" state (the same
posture as the sibling steprun.cost_usd/tokens_* spend columns). Every StepRun written
before this migration reads NULL, and no backfill is possible — a legacy run's finish
reason / error class is genuinely unknown and must READ as NULL, never a fabricated value.
Only runs executed AFTER this migration stamp real values.

Hand-written (no autogenerate) off the head b9e3d7c1f5a2, same convention as the sibling
additive-column revisions. Runs clean on SQLite (batch_alter_table) and Postgres (native
ALTER); the SQLite _ADDED_COLUMNS["steprun"] shim in app/db.py carries the SAME three
columns so a non-alembic SQLite boot adds them identically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3f1a9c2d4'
down_revision: Union[str, None] = 'b9e3d7c1f5a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('steprun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('finish_reason', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('error_class', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('output_chars', sa.Integer(), nullable=True))
    # No backfill: a legacy StepRun's finish reason / error class / output length is
    # UNKNOWN and must read NULL (never a fabricated value).
    # run-190 Item D / M5 (gate round 1): per-step opt-out of the over-translation
    # (page-dump) guard on workflowstep. NULLABLE tri-state (the tm_* override shape):
    # NULL -> follow the global OVER_TRANSLATION_MAX_RATIO knob (default ON); 0 -> this
    # step opts out; 1 -> explicitly ON. Every existing row backfills NULL = ON,
    # byte-identical. Mirrors the model + the _ADDED_COLUMNS["workflowstep"] SQLite shim.
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('over_translation_guard', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('over_translation_guard')
    with op.batch_alter_table('steprun', schema=None) as batch_op:
        batch_op.drop_column('output_chars')
        batch_op.drop_column('error_class')
        batch_op.drop_column('finish_reason')
