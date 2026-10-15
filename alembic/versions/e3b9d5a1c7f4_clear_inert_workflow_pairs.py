"""clear the inert language pair on UNGROUPED workflows (language-agnostic workflows, T3, D4)

Revision ID: e3b9d5a1c7f4
Revises: d1a8c5e3b7f2
Create Date: 2026-10-02 15:00:00.000000

DATA-ONLY revision: no schema change. One UPDATE:

  UPDATE workflow SET source_locale = NULL, target_locale = NULL
   WHERE group_id IS NULL AND (source_locale IS NOT NULL OR target_locale IS NOT NULL)

WHY: the engine never reads workflow.source_locale / target_locale while running; the only
reader is group dispatch (resolve_variant), which picks a variant INSIDE a group by pair.
An UNGROUPED workflow therefore never needed a pair, but older "Design a workflow" builds
stamped one anyway. It does nothing, yet it filtered the glossary dropdown and made the
workflow look language-paired (ADR 0047, decision D4). This clears it once.

SCOPE: only rows with group_id IS NULL change. A grouped variant keeps its exact pair (group
dispatch depends on it). A blank string ('') also means "no pair" to the app (every reader
tests truthiness), so '' on an ungrouped row is cleared to NULL too: `IS NOT NULL` matches ''.
A row that is already NULL on both columns is not touched.

IDEMPOTENT: after one run no ungrouped row matches the WHERE, so a second run changes nothing.
PORTABLE: plain UPDATE with IS NULL / IS NOT NULL, identical on SQLite and Postgres.

DOWNGRADE IS A DOCUMENTED NO-OP: the cleared values carried no behaviour and cannot be
reconstructed (the original stamp is not recorded anywhere). Downgrading only moves the
alembic pointer back; it never restores a pair.

Pre-alembic (legacy create_all) SQLite installs skip non-additive revisions by design (see the
init_db warning); a data-only revision has no column for the _ADDED_COLUMNS shim to mirror,
so nothing is mirrored in app/db.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3b9d5a1c7f4'
down_revision: Union[str, None] = 'd1a8c5e3b7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE workflow SET source_locale = NULL, target_locale = NULL "
            "WHERE group_id IS NULL "
            "AND (source_locale IS NOT NULL OR target_locale IS NOT NULL)"
        )
    )


def downgrade() -> None:
    # Documented no-op: the cleared pair was inert and cannot be reconstructed.
    pass
