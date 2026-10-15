"""workflowstep.tm_protect (LQA safety floor Phase 1, D1 — TM provenance guard)

Revision ID: d4b7e2f9a1c6
Revises: c7e1a9d4b62f
Create Date: 2026-07-28 12:00:00.000000

Adds ONE additive, NULLABLE column to `workflowstep`:

  workflowstep.tm_protect — nullable BOOLEAN. The per-step TRISTATE override for the
                            TM provenance guard (PROPOSAL-lqa-glossary-adjudication
                            §4.D1). NULL = the correction-loop-shaped DEFAULT (protect
                            `tm_origin` segments only on a step gated on
                            `prev_lqa:failed` or reached by an LQA `on_fail` branch);
                            TRUE = always protect; FALSE = never protect.

WHY NULLABLE, NO SERVER DEFAULT: NULL is a REAL, meaningful third state here (not an
"unknown"), so every legacy row must read NULL and inherit the shape-based default.
Backfilling it to 0 or 1 would silently pin every existing step to an explicit
override and defeat the design. There is therefore NO backfill, and a workflow with no
LQA step resolves NULL -> no protection -> byte-identical runs.

Hand-written (no autogenerate) off the head c7e1a9d4b62f, same convention as the
sibling additive-column revisions. Runs clean on SQLite (batch_alter_table) and
Postgres (native ALTER); the SQLite _ADDED_COLUMNS["workflowstep"] shim in app/db.py
carries the SAME column so a non-alembic SQLite boot adds it identically (the
nullability-parity CI guard compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4b7e2f9a1c6'
down_revision: Union[str, None] = 'c7e1a9d4b62f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tm_protect', sa.Boolean(), nullable=True))
    # No backfill: NULL is the meaningful "inherit the correction-loop default" state.


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('tm_protect')
