"""Prompt Tap run envelopes Wave A - the tapaction + taprun tables

Revision ID: e1f4a9c72b60
Revises: d5a3c8b1e470
Create Date: 2026-08-06 12:00:00.000000

Adds the two run-envelope tables (PROPOSAL-prompt-tap-run-envelopes, Wave A "schema +
capture spine"). Additive only - both are EMPTY on upgrade and only
app/services/tap_envelope.py writes them, so every existing code path is byte-identical
after this migration.

  tapaction (new) - one translate action (a workflow run, a direct-MT pass, or an
                    ad-hoc translate_text call). ULID string primary key.
  taprun (new)    - one language-pair leg of an action; `workflow_run_id` joins back
                    to the production WorkflowRun (NULLABLE - direct MT has none).

DECISIONS WORTH DEFENDING IN A MIGRATION FILE
---------------------------------------------
1. NO FOREIGN KEYS on the PRODUCTION-ENTITY columns (tapaction.org_id/customer_id/
   project_id/document_id/workflow_id/batch_id and taprun.workflow_run_id) -- the same
   decision as EvalDataset.source_document_id, and for the same reason. An envelope is a
   passive, append-only OBSERVER of a run; it records WHICH entity a run was against but
   must never be able to BLOCK or CASCADE the deletion of one. A real FK would refuse a
   production delete (a WorkflowRun / Project / Document) whenever an envelope pointed at
   it -- verify_cli / verify_jobs_history delete runs a translation had captured. So
   those are bare provenance integers (indexed where section 6 requires). The ONE FK
   kept is taprun.action_id -> tapaction.id: INTERNAL (envelope -> envelope), a parent
   created immediately before the child and deleted only by the Wave C purge.

2. STRING (ULID) PRIMARY KEYS, not autoincrement integers (proposal section 4). ULIDs
   are time-sortable + index-friendly AND double as the per-action call FILE name
   (prompt_tap/actions/<id>.jsonl), so a stable string id is load-bearing, not
   cosmetic.

3. `tapaction.document_id` and `taprun.workflow_run_id` are NULLABLE ON PURPOSE. A
   key-set project run has no single document (section 11.1, "nullable doc/key
   fields"); a direct-MT leg has no WorkflowRun. Nullability is the model, not an edge
   case bolted on later - a reader who greps for a missing NOT NULL should find this
   paragraph.

4. `manifest` / `totals` (on tapaction) and `detail` (on taprun) are NULLABLE JSON
   columns RESERVED FOR WAVE B (asset manifest + roll-up totals + per-leg enrichment).
   They are NULL in Wave A. Putting the extensible payload in JSON now is what lets
   Wave B enrichment land with zero schema churn.

WHY NO app/db.py `_ADDED_COLUMNS` ENTRY: that shim is for new COLUMNS on EXISTING
tables, which create_all() will not add. These are new TABLES, which create_all()
DOES create on a non-alembic SQLite boot - the same posture as the six eval* tables
(c5a1f8e3b724 states it in full). tests/verify_migrations.py compares this migration's
output against the models column-for-column INCLUDING nullability, keeping the alembic
path and the create_all path honest.

PORTABLE DDL. Only sa.Integer/String/Float/Boolean/DateTime/JSON are used, all of which
render natively on SQLite and Postgres. Every NOT NULL column that a hand-written
INSERT might omit carries a server_default so the CREATE is safe against an upgraded
database; timestamp/JSON columns follow the eval-table convention (the app always
supplies them via the model defaults, and the tables are empty on create).

INDEXES mirror the models' `index=True` fields exactly and proposal section 6 ("indexed
on started_at, project, document, batch_id, status"): tapaction gets started_at,
project_id, document_id, batch_id, status; taprun gets started_at, action_id,
workflow_run_id, status. No unique indexes - an action legitimately has several runs,
and a (workflow_run_id) is reused across a resume, never duplicated by design but not
enforced unique here (the service is the single writer and keys on it).

Hand-written (no autogenerate) off the head d5a3c8b1e470. Runs clean on SQLite and
Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f4a9c72b60'
down_revision: Union[str, None] = 'd5a3c8b1e470'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tapaction',
        # ULID string PK - see the module docstring, decision 1.
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('kind', sa.String(), nullable=False, server_default='direct'),
        sa.Column('initiator', sa.String(), nullable=False, server_default=''),
        sa.Column('surface', sa.String(), nullable=False, server_default=''),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        # NULLABLE - a key-set run has no single document (decision 2).
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('source_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('requested_locales', sa.JSON(), nullable=False),
        sa.Column('workflow_id', sa.Integer(), nullable=True),
        sa.Column('workflow_name', sa.String(), nullable=False, server_default=''),
        sa.Column('step_fingerprint', sa.String(), nullable=False, server_default=''),
        sa.Column('batch_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='running'),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        # DISK-BUDGET BACKSTOP flag (proposal section 7): True when per-call capture was
        # withheld because prompt_tap/actions/ hit prompt_tap_actions_max_bytes.
        sa.Column('capture_withheld', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        # WAVE B (reserved, NULL in Wave A) - see decision 4. none_as_null so a Python
        # None persists as SQL NULL (Wave B keys on IS NULL), not the string 'null'.
        sa.Column('manifest', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('totals', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        # NO foreign keys on the production-entity columns (org/customer/project/
        # document/workflow/batch): an envelope is a passive observer and must never
        # block or cascade a production delete -- the same decision as
        # EvalDataset.source_document_id (see decision 1 in the module docstring, and
        # the TapAction model block). They are bare provenance integers.
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tapaction_started_at', 'tapaction', ['started_at'], unique=False)
    op.create_index('ix_tapaction_project_id', 'tapaction', ['project_id'], unique=False)
    op.create_index('ix_tapaction_document_id', 'tapaction', ['document_id'], unique=False)
    op.create_index('ix_tapaction_batch_id', 'tapaction', ['batch_id'], unique=False)
    op.create_index('ix_tapaction_status', 'tapaction', ['status'], unique=False)

    op.create_table(
        'taprun',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('action_id', sa.String(), nullable=False),
        # NULLABLE join to the production run (workflow legs only).
        sa.Column('workflow_run_id', sa.Integer(), nullable=True),
        sa.Column('target_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('source_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('status', sa.String(), nullable=False, server_default='running'),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        # WAVE B (reserved, NULL in Wave A). none_as_null so None persists as SQL NULL.
        sa.Column('detail', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        # action_id IS a real FK (internal envelope->envelope). workflow_run_id is a bare
        # provenance integer with NO FK, so a production run delete is never blocked by
        # an envelope pointing at it (see the module docstring, decision 1).
        sa.ForeignKeyConstraint(['action_id'], ['tapaction.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_taprun_started_at', 'taprun', ['started_at'], unique=False)
    op.create_index('ix_taprun_action_id', 'taprun', ['action_id'], unique=False)
    op.create_index('ix_taprun_workflow_run_id', 'taprun', ['workflow_run_id'],
                    unique=False)
    op.create_index('ix_taprun_status', 'taprun', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_taprun_status', table_name='taprun')
    op.drop_index('ix_taprun_workflow_run_id', table_name='taprun')
    op.drop_index('ix_taprun_action_id', table_name='taprun')
    op.drop_index('ix_taprun_started_at', table_name='taprun')
    op.drop_table('taprun')
    op.drop_index('ix_tapaction_status', table_name='tapaction')
    op.drop_index('ix_tapaction_batch_id', table_name='tapaction')
    op.drop_index('ix_tapaction_document_id', table_name='tapaction')
    op.drop_index('ix_tapaction_project_id', table_name='tapaction')
    op.drop_index('ix_tapaction_started_at', table_name='tapaction')
    op.drop_table('tapaction')
