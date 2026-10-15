"""vendor Wave E: quote-approval gate + reject/rework (R3 + R4)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-09 12:00:00.000000

Nine additive columns on `vendordispatch`, all NULLABLE / server-defaulted so every
legacy row is byte-identical to the pre-Wave-E behavior (never quote-gated, never
reworked):

  R3 (quote-approval gate) — the estimate -> QUOTE -> actual reconciliation chain sits
  ALONGSIDE the existing estimate/actual columns; approving a quote records it, never
  overwriting the operator's scoping estimate:
    requires_quote (BOOLEAN DEFAULT 0) — snapshots the vendor's requires_quote_approval
      flag at dispatch time, so the leg's own machinery knows it is quote-gated.
    quoted_cost_usd (FLOAT, nullable) — the vendor/operator quote for this leg.
    quote_currency (VARCHAR DEFAULT '') — the quote's ISO-4217 currency label.
    quoted_at (DATETIME, nullable) — when a quote was recorded.
    quote_approved_at (DATETIME, nullable) — when an operator approved (QUOTED->DISPATCHED).
    quote_note (VARCHAR DEFAULT '') — vendor/operator quote note.

  R4 (reject/rework):
    rework_round (INTEGER DEFAULT 0) — 0 on an original dispatch, N on the Nth rework.
    rework_of (INTEGER, nullable) — the RETURNED leg a rework leg supersedes.
    rework_feedback (JSON, nullable) — the structured feedback the rework carried.

Two new leg statuses (QUOTED, REJECTED) are plain string values on the existing `status`
column; no enum/CHECK to migrate. Hand-written (no autogenerate) off the head
b2c3d4e5f6a7, same convention as the sibling additive-column revisions. Runs clean on
SQLite (batch_alter_table) and Postgres (native ALTER). The app/db.py _ADDED_COLUMNS shim
carries the SAME columns so a non-alembic SQLite boot (and the hermetic create_all test
path) builds them identically — the nullability-parity CI guard compares the two.

ACCEPTED RESIDUAL — `rework_of` FOREIGN KEY (fix-round finding #12, corrected rationale).
The model declares `rework_of` with `foreign_key="vendordispatch.id"`, so a create_all
install builds it as an ENFORCED self-referential FK (this install runs with
`PRAGMA foreign_keys=ON`); this ALTER adds it as a PLAIN INTEGER, because the standing
convention here is that NO `op.add_column` in alembic/versions carries a ForeignKey (an
inline-FK column is created with the table, never ALTER-added). So a create_all install
enforces `rework_of` and an alembic install does not — a divergence accepted, not fixed,
because: (a) vendor DELETE is fail-closed (`vendors.delete` refuses while any dispatch
references the vendor), and `rework_of` points at a sibling leg that is only ever
REJECTED/superseded, never deleted; and (b) the column feeds ONLY the rework-lineage badge
on the surfaces — nothing authorizes or gates on it. Adding the FK would force a batch
table-rebuild ALTER on the live prod SQLite DB for zero enforcement value.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('vendordispatch', schema=None) as batch_op:
        # sa.false(), NOT sa.text('0'): sa.text('0') emits `DEFAULT 0` (an integer), which
        # Postgres rejects on a BOOLEAN column ("default expression is of type integer"),
        # aborting the whole chain on every Postgres install past this revision. sa.false()
        # emits `DEFAULT false` on Postgres and the byte-identical `DEFAULT 0` on SQLite.
        batch_op.add_column(sa.Column(
            'requires_quote', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('quoted_cost_usd', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column(
            'quote_currency', sa.String(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('quoted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('quote_approved_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(
            'quote_note', sa.String(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column(
            'rework_round', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('rework_of', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('rework_feedback', sa.JSON(), nullable=True))
    # No backfill: the server defaults land 0 / NULL / '' on every legacy row, which is
    # exactly the pre-Wave-E reading (never quote-gated, never reworked).


def downgrade() -> None:
    # The Wave-E statuses do not exist in the pre-Wave-E vocabulary, so a downgrade must
    # NORMALIZE any leg carrying one before dropping the columns that describe it. QUOTED ->
    # dispatched (a leg awaiting a quote reads as a normal open dispatch); REJECTED ->
    # cancelled (finding #13: a rejected leg was SUPERSEDED by a rework leg -- mapping it to
    # 'returned' would DOUBLE-COUNT it against the rework leg in the Human-spend rollup, so it
    # reads as the superseded/aborted leg it is). Runs clean on SQLite + Postgres (plain UPDATE).
    op.execute("UPDATE vendordispatch SET status='dispatched' WHERE status='quoted'")
    op.execute("UPDATE vendordispatch SET status='cancelled' WHERE status='rejected'")
    with op.batch_alter_table('vendordispatch', schema=None) as batch_op:
        batch_op.drop_column('rework_feedback')
        batch_op.drop_column('rework_of')
        batch_op.drop_column('rework_round')
        batch_op.drop_column('quote_note')
        batch_op.drop_column('quote_approved_at')
        batch_op.drop_column('quoted_at')
        batch_op.drop_column('quote_currency')
        batch_op.drop_column('quoted_cost_usd')
        batch_op.drop_column('requires_quote')
