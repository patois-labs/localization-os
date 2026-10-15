"""glossaryentry.project_id nullable for module-owned entries (Postgres FK fix)

Revision ID: d2e6b8c4a1f7
Revises: c4f9a1b7e2d8
Create Date: 2026-07-06 13:00:00.000000

Phase 5-B FK fix. A dedicated-module glossary entry (glossary_id set) is NOT
project-bound, but it was stored with a project_id=0 SENTINEL. glossaryentry has a
real FK glossaryentry.project_id -> project.id (baseline fcc52f1978bf), and no
project row has id 0, so on Postgres the first module add_term / import_csv / LLM
glossary_build hit an IntegrityError. (SQLite only passed because db.py never
enables FK enforcement, and CI Postgres only ran a schema smoke.)

Fix: make glossaryentry.project_id NULLABLE and store NULL for module-owned entries
(glossary_id IS NULL still discriminates legacy rows; every legacy filter is
`glossary_id IS NULL AND project_id == pid`, so a NULL-project module row can never
leak into a project's legacy view). The application code now writes NULL, not 0.

Migration is safe if any legacy 0-sentinel rows exist (there should be none in
prod): after widening the column, every module-owned entry (glossary_id NOT NULL)
whose project_id is the 0 sentinel is rewritten to NULL.

Runs clean on BOTH SQLite (batch_alter_table rebuilds the table to change the
column's nullability) and Postgres (native ALTER COLUMN DROP NOT NULL).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e6b8c4a1f7'
down_revision: Union[str, None] = 'c4f9a1b7e2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widen the column to nullable. batch_alter_table rebuilds the table on SQLite
    # (which cannot ALTER COLUMN in place); native ALTER on Postgres.
    with op.batch_alter_table('glossaryentry', schema=None) as batch_op:
        batch_op.alter_column('project_id', existing_type=sa.Integer(), nullable=True)
    # Migrate any pre-fix module rows off the 0 sentinel to NULL (idempotent; a
    # no-op when — as expected — no such rows exist).
    op.execute(
        "UPDATE glossaryentry SET project_id = NULL "
        "WHERE glossary_id IS NOT NULL AND project_id = 0"
    )


def downgrade() -> None:
    # Restore module rows to the 0 sentinel before re-imposing NOT NULL, so the
    # narrowing can't fail on a NULL project_id.
    op.execute(
        "UPDATE glossaryentry SET project_id = 0 "
        "WHERE glossary_id IS NOT NULL AND project_id IS NULL"
    )
    with op.batch_alter_table('glossaryentry', schema=None) as batch_op:
        batch_op.alter_column('project_id', existing_type=sa.Integer(), nullable=False)
