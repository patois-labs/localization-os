"""project + workflow archive — nullable archived_at so test residue can be retired

Revision ID: 2a98273c3c99
Revises: c3d4e5f6a7b8
Create Date: 2026-08-09 12:00:00.000000

Residual sweep (archive verbs for test residue). Mirrors the Customer.archived_at
precedent (b1d4f7c2e9a3): an operator can RETIRE a project / workflow — tucking test
residue out of the default listings + pickers and blocking it from launching new runs —
WITHOUT deleting it. Two purely-additive nullable columns:

  project.archived_at   — NULL = ACTIVE (every existing project backfills to NULL,
                          byte-identical). Non-NULL = ARCHIVED (hidden from default
                          listings/pickers, blocked from new runs, preserved + reversible).
  workflow.archived_at  — same semantics, plus exclusion from dispatch/variant resolution.

NOTE (revision id): this migration was originally hand-keyed c3d4e5f6a7b8 off b2c3d4e5f6a7
in a parallel worktree; Vendor Wave E independently pattern-picked the SAME id
(c3d4e5f6a7b8_vendor_wave_e_quote_rework). Re-keyed to 2a98273c3c99 on top of Wave E so the
chain stays LINEAR: b2c3d4e5f6a7 -> c3d4e5f6a7b8 (Wave E) -> 2a98273c3c99 (this).

DO NOT USE op.batch_alter_table ON `project` (the customer-archive twin's idiom is fine
for `customer` but WRONG here): the project table carries the EXPRESSION-based unique
index ux_project_inbox_key (coalesce(org_id,-1), inbox_key). SQLAlchemy's SQLite batch
mode CANNOT reflect an expression-based index, so a batch table-rebuild silently DROPS it
("SAWarning: Skipped unsupported reflection of expression-based index" -> the inbox
uniqueness guard is gone). Adding a column never needs a rebuild (op.add_column emits a
native ALTER ... ADD COLUMN on both SQLite and Postgres), and SQLite >= 3.35 supports a
native ALTER ... DROP COLUMN, so BOTH directions run without a rebuild and the expression
index is preserved (verify_project_sources test_g downgrade round-trip is the guard). The
SAME columns are added on the SQLite init_db/_ensure_columns path via
app/db._ADDED_COLUMNS["project"]/["workflow"], so all three build paths agree.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a98273c3c99'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain add_column (no batch): a native ALTER ... ADD COLUMN on SQLite + Postgres,
    # so the project table is never rebuilt and ux_project_inbox_key survives.
    op.add_column('project', sa.Column('archived_at', sa.DateTime(), nullable=True))
    op.add_column('workflow', sa.Column('archived_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        # Native DROP COLUMN (SQLite >= 3.35) rather than a batch rebuild, which would
        # silently drop project's expression-based ux_project_inbox_key index.
        op.execute('ALTER TABLE workflow DROP COLUMN archived_at')
        op.execute('ALTER TABLE project DROP COLUMN archived_at')
    else:
        op.drop_column('workflow', 'archived_at')
        op.drop_column('project', 'archived_at')
