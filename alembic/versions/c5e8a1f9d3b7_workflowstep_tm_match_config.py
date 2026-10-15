"""workflowstep tm_match per-step config + workflowrun used_tm_match marker (Batch 15 T1)

Revision ID: c5e8a1f9d3b7
Revises: b4d9f1a7c2e6
Create Date: 2026-08-30 12:00:00.000000

SPEC-tm-match-step-config §5.1 / §5.1b / §17 (Batch 15 Train 1). Additive only.

WorkflowStep gains SIX nullable-inherit tm_match policy overrides (§5.1) — NULLABLE,
NO server_default, so every existing row backfills to NULL = inherit the live global =
today's exact behavior byte-for-byte (LAW-TM-3). Mirrors the tm_protect nullable-tristate
precedent, NOT the NOT-NULL use_image_context shape:
  - tm_fill_threshold      (Integer, nullable): min match % to FILL; NULL -> global
  - tm_fuzzy_lock_mode     (String,  nullable): "none"|"soft"|"policy"; NULL -> global
  - tm_lock_ice            (Boolean, nullable): override; NULL -> global tm_lock_ice
  - tm_hundreds_enter_lqa  (Boolean, nullable): override; NULL -> global
  - tm_context_floor       (Integer, nullable): override; NULL -> global tm_context_floor
  - tm_pool_selection      (JSON,    nullable): readable project ids; NULL/[] -> all readable

WorkflowRun gains ONE NOT-NULL boolean marker (§5.1b / N2), server_default '0' (the
qe_gate / use_image_context DEFAULT-FALSE bool shape): a durable, monotonic per-run fact
stamped when the tm_match branch runs (both segmented + FULL_TEXT). Legacy runs backfill
False so a pre-Batch-15 tm_match run is NOT retroactively gated (§11/P5).

Chained off the Batch 14 T2 use_image_context head (b4d9f1a7c2e6). Mirrors the SQLite
_ADDED_COLUMNS additive shim in app/db.py + the model (nullability-parity CI guard).
Hand-written (no autogenerate) to avoid spurious index noise, same convention as
b4d9f1a7c2e6_workflowstep_use_image_context.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e8a1f9d3b7'
down_revision: Union[str, None] = 'b4d9f1a7c2e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tm_fill_threshold', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tm_fuzzy_lock_mode', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('tm_lock_ice', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('tm_hundreds_enter_lqa', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('tm_context_floor', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tm_pool_selection', sa.JSON(), nullable=True))
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('used_tm_match', sa.Boolean(), nullable=False, server_default='0')
        )


def downgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('used_tm_match')
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('tm_pool_selection')
        batch_op.drop_column('tm_context_floor')
        batch_op.drop_column('tm_hundreds_enter_lqa')
        batch_op.drop_column('tm_lock_ice')
        batch_op.drop_column('tm_fuzzy_lock_mode')
        batch_op.drop_column('tm_fill_threshold')
