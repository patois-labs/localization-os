"""workflow batch (fan-out dispatch entity, Phase 5a)

Revision ID: f8c1a4d7e2b9
Revises: e7b2a9d4c1f8
Create Date: 2026-07-18 14:00:00.000000

Adds the SPEC-workflows-redesign Phase 5a batch layer — one action that runs a
workflow across a document's target locales as a single observable, spend-capped,
concurrency-bounded batch:

  workflowbatch (new table)   — one fan-out: the REQUESTED workflow_id (members
                                resolve a per-locale variant off it), document_id,
                                created_by/created_at, finished_at (the terminal
                                authority), requested_locales (JSON — the exact
                                targets of THIS batch), max_parallel, fail_fast,
                                status (running|completed|partial|failed) + notes
                                (nullable, spend-stop / fail-fast explanation).
  workflowrun.batch_id        — nullable FK -> workflowbatch.id. NULL on every
                                existing row = a plain single run, byte-identical to
                                every pre-5a path (nothing legacy sets it); non-NULL
                                links a member run to its batch.

All additive: the new table is empty on upgrade, and every existing WorkflowRun
backfills batch_id to NULL (a single run). Mirrors the SQLite _ensure_columns()
additive-column + create_all() new-table pattern in app/db.py and the model
(models.WorkflowBatch + WorkflowRun.batch_id) — the nullability-parity CI guard
(tests/verify_migrations.py) asserts this revision's column set + nullability equal
the models'. Runs clean on BOTH SQLite (batch_alter_table) and Postgres (native
ALTER). The workflowrun.batch_id ALTER is added as a PLAIN nullable column (no
ForeignKeyConstraint in the ALTER) exactly like the sibling additive-FK revisions
(content_types, glossary_module): the SQLModel class carries the real FK (create_all
+ a fresh Postgres build it; SQLite's PRAGMA foreign_keys=ON enforces it), while the
SQLite-safe ALTER stays a bare ADD COLUMN.

Hand-written (no autogenerate) off the head e7b2a9d4c1f8, same convention as the
sibling revisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8c1a4d7e2b9'
down_revision: Union[str, None] = 'e7b2a9d4c1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'workflowbatch',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('workflow_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('requested_locales', sa.JSON(), nullable=False),
        sa.Column('max_parallel', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('fail_fast', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(), nullable=False, server_default='running'),
        sa.Column('notes', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflow.id']),
        sa.ForeignKeyConstraint(['document_id'], ['document.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('batch_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowrun', schema=None) as batch_op:
        batch_op.drop_column('batch_id')
    op.drop_table('workflowbatch')
