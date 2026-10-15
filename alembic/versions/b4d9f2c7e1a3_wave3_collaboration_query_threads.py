"""wave3.collaboration — QueryThread/QueryPost tables + LQAResult human-finding columns + worklist indexes

Revision ID: b4d9f2c7e1a3
Revises: f7c2a9e4b1d8
Create Date: 2026-08-19 12:00:00.000000

SPEC-wave3-collaboration Train A. Purely additive, off the POST-BUDGETS head
f7c2a9e4b1d8 (the project-budgets train's revision) — verified single head at
authoring time. Three additive changes:

  1. TWO NEW TABLES — `querythread` and `querypost` (§3.1). QueryThread carries a
     thread's identity/scope/lifecycle/assignment once; QueryPost is its append-only
     message log. On the non-alembic SQLite init_db path create_all() makes them (a
     NEW TABLE needs NO app/db._ADDED_COLUMNS entry — the SegmentPostEdit/LLMemory
     precedent), so the two build paths agree and verify_alembic_authority.test_a
     stays at zero diff. Both carry the vendor-idempotency UNIQUE constraints of
     §3.1/MED-4 (NULLs-are-distinct leaves every human row unconstrained).

  2. TWO NULLABLE columns on the EXISTING `lqaresult` table (§6.1): `actor` (the
     logging reviewer's email — the table's first PII column, NULL on every machine
     row) and `withdrawn_at` (soft-delete timestamp, §6.7). NO server default — NULL
     is the honest value. Native ALTER ... ADD COLUMN on SQLite + Postgres (no table
     rebuild, so lqaresult's indexes survive). The SAME columns land on the non-alembic
     SQLite path via app/db._ADDED_COLUMNS["lqaresult"] so all three build paths agree.
     Bare types (no FK) — same convention as the Wave 2 revision e7a1b2c3d4e5.

  3. THREE additive indexes the reviewer worklist join needs (§8.5), on EXISTING
     tables that lacked them: workflowstep.assignee_email, steprun.status (StepRun had
     no index at all), document.project_id (a bare FK while its siblings carry one).
     Added here AND as index=True on the model fields, so create_all() makes the same
     indexes and the alembic-vs-create_all diff stays zero.

Hand-written (no autogenerate) off the head f7c2a9e4b1d8, same additive convention as
the sibling revisions. DIALECT-BRANCHED downgrade (native SQLite DROP COLUMN vs
op.drop_column for the lqaresult columns), matching e7a1b2c3d4e5's shape.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d9f2c7e1a3'
down_revision: Union[str, None] = 'f7c2a9e4b1d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The two additive LQAResult columns (name, type). Bare types (no FK) — the Wave 2
# FK-asymmetry note applies unchanged.
_LQARESULT_COLS = [
    ('actor', sa.String()),
    ('withdrawn_at', sa.DateTime()),
]

# The three worklist-join indexes on existing tables (name, table, column). Names match
# SQLAlchemy's index=True default (ix_<table>_<column>) so create_all() agrees.
_WORKLIST_INDEXES = [
    ('ix_workflowstep_assignee_email', 'workflowstep', 'assignee_email'),
    ('ix_steprun_status', 'steprun', 'status'),
    ('ix_document_project_id', 'document', 'project_id'),
]


def upgrade() -> None:
    # 1. querythread — the thread head. NOT NULL on the always-present scalar columns
    #    (empty-string / 0 / now defaults on the model), nullable on the optional FKs
    #    and the vendor/close columns. UNIQUE (dispatch_id, external_id) is the
    #    thread-CREATING vendor branch's idempotency backstop (§7.3/MED-4).
    op.create_table(
        'querythread',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id'), nullable=False),
        sa.Column('document_id', sa.Integer(), sa.ForeignKey('document.id'), nullable=True),
        sa.Column('segment_id', sa.Integer(), sa.ForeignKey('segment.id'), nullable=True),
        sa.Column('key_id', sa.Integer(), sa.ForeignKey('stringkey.id'), nullable=True),
        sa.Column('target_locale', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('author', sa.String(), nullable=False),
        sa.Column('author_kind', sa.String(), nullable=False),
        sa.Column('assignee_email', sa.String(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), sa.ForeignKey('vendor.id'), nullable=True),
        sa.Column('dispatch_id', sa.Integer(), sa.ForeignKey('vendordispatch.id'),
                  nullable=True),
        sa.Column('external_id', sa.String(), nullable=True),
        sa.Column('post_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('closed_by', sa.String(), nullable=False),
        sa.UniqueConstraint('dispatch_id', 'external_id',
                            name='ux_querythread_vendor_external'),
    )
    op.create_index('ix_querythread_project_id', 'querythread', ['project_id'])
    op.create_index('ix_querythread_document_id', 'querythread', ['document_id'])
    op.create_index('ix_querythread_segment_id', 'querythread', ['segment_id'])
    op.create_index('ix_querythread_key_id', 'querythread', ['key_id'])
    op.create_index('ix_querythread_updated_at', 'querythread', ['updated_at'])

    # 2. querypost — the append-only message log. UNIQUE (thread_id, external_id) dedups
    #    a re-admitted vendor post (§3.1); NULL external_id leaves human posts unconstrained.
    op.create_table(
        'querypost',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('thread_id', sa.Integer(), sa.ForeignKey('querythread.id'),
                  nullable=False),
        sa.Column('body', sa.String(), nullable=False),
        sa.Column('author', sa.String(), nullable=False),
        sa.Column('author_kind', sa.String(), nullable=False),
        sa.Column('author_label', sa.String(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), sa.ForeignKey('vendor.id'), nullable=True),
        sa.Column('dispatch_id', sa.Integer(), sa.ForeignKey('vendordispatch.id'),
                  nullable=True),
        sa.Column('external_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('thread_id', 'external_id', name='ux_querypost_external'),
    )
    op.create_index('ix_querypost_thread_id', 'querypost', ['thread_id'])
    op.create_index('ix_querypost_created_at', 'querypost', ['created_at'])

    # 3. the two additive nullable columns on lqaresult (native ALTER ADD COLUMN).
    for name, type_ in _LQARESULT_COLS:
        op.add_column('lqaresult', sa.Column(name, type_, nullable=True))

    # 4. the three worklist-join indexes on existing tables (§8.5).
    for name, table, col in _WORKLIST_INDEXES:
        op.create_index(name, table, [col])


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'
    # 4/3 reverse: drop the worklist indexes, then the lqaresult columns.
    for name, table, _col in reversed(_WORKLIST_INDEXES):
        op.drop_index(name, table_name=table)
    for name, _type in reversed(_LQARESULT_COLS):
        if is_sqlite:
            op.execute('ALTER TABLE lqaresult DROP COLUMN %s' % name)
        else:
            op.drop_column('lqaresult', name)
    # 2/1 reverse: drop the two new tables (their indexes + unique constraints go with them).
    op.drop_table('querypost')
    op.drop_table('querythread')
