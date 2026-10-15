"""workflowstep.memory_mode (LLMemory Phase 1 — per-step recall opt-out)

Revision ID: f2a9c4d81b73
Revises: b3e7c1d94a26
Create Date: 2026-07-31 15:00:00.000000

Adds ONE additive, NULLABLE column to `workflowstep`:

  workflowstep.memory_mode — nullable VARCHAR. The per-step LLMemory opt-out
                             (PROPOSAL-llmemory §6: "WorkflowStep.memory_mode tristate
                             (auto | off) for opt-out, default auto"). NULL and 'auto'
                             are the SAME state — this step participates in memory
                             recall normally; 'off' means NO memory block is ever
                             injected into this step's prompts, whatever the global
                             `llmemory_recall_enabled` switch says.

WHY NULLABLE, NO SERVER DEFAULT AND NO BACKFILL: 'auto' is the default BEHAVIOUR, not a
value that has to be written down. Backfilling every existing step to the string 'auto'
would rewrite the whole table to record a decision nobody made, and would make a future
"NULL means inherit from the workflow" reading impossible. NULL reads as auto in the one
place that consults the column (`orchestrator._memory_mode_off`), so every legacy row is
byte-identical to pre-LLMemory.

OPT-OUT ONLY — the column can never widen recall. Eligibility (a translate step; never
an LQA/judge step, never the correction-loop fixer — proposal §2) is decided by the
orchestrator's step-role predicate, and memory_mode is consulted AFTER that gate, only
ever to say no. 'auto' on an LQA step is therefore inert, exactly as `tm_protect` is
dead on a non-llm_agent step.

Hand-written (no autogenerate) off the head b3e7c1d94a26 (the LLMemory Phase-1 tables),
same convention as the sibling additive-column revisions. Runs clean on SQLite
(batch_alter_table) and Postgres (native ALTER); the SQLite _ADDED_COLUMNS["workflowstep"]
shim in app/db.py carries the SAME column so a non-alembic SQLite boot adds it
identically (the nullability-parity CI guard compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a9c4d81b73'
down_revision: Union[str, None] = 'b3e7c1d94a26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('memory_mode', sa.String(), nullable=True))
    # No backfill: NULL already reads as 'auto' at the single consumption point.


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('memory_mode')
