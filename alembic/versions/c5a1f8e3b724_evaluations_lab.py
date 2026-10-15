"""Evaluations Lab Phase 1 - the six eval* tables (PROPOSAL-evaluations-lab section 4)

Revision ID: c5a1f8e3b724
Revises: f2a9c4d81b73
Create Date: 2026-08-01 09:00:00.000000

Adds the six Evaluations Lab tables. Additive only - all six are EMPTY on upgrade and
NOTHING outside app/services/eval_*.py and app/services/evaluations.py reads them, so
every existing code path (prompts, orchestrator, translate, LQA, QE, CLI, web) is
byte-identical after this migration.

  evaldataset (new)     - a named corpus of source (+ optional reference) segments.
  evaldatasetitem (new) - one evaluated unit. `reference_text` is NULLABLE.
  evalcandidate (new)   - the config under test; `locked` freezes it once run against.
  evalrun (new)         - one (dataset x candidate x repeat) execution.
  evalrunitem (new)     - one produced output. The ONLY durable home of a Lab output.
  evalscore (new)       - one scorer verdict, carrying `state` + a NULLABLE `value`.

THE TWO DECISIONS WORTH DEFENDING IN A MIGRATION FILE
-----------------------------------------------------
1. `evaldataset.source_document_id` HAS NO FOREIGN KEY, on purpose. Copy-in from a
   production Document is a SNAPSHOT (proposal section 3, rule 3): the text is copied
   into evaldatasetitem rows and the integer only records where it came from. An FK
   here would make the Lab live-coupled to production - a production DELETE would
   either cascade into the Lab or be blocked by a Lab row, and either way the dataset
   would stop being reproducible. A reader who greps this file for a missing FK should
   find this paragraph, not add one.

2. `evalscore.value` IS NULLABLE AND `evalscore.state` IS NOT NULL. That pair is
   proposal section 6 (the reference-availability rule) expressed in DDL: a scorer that
   needs a reference and has none writes state='not_applicable' with value NULL, which
   an AVG() correctly ignores. Storing 0.0 there would silently poison every aggregate
   with "the reference was missing" scored as "the translation was terrible". The
   column is nullable so the wrong thing is not even representable.

WHY NO app/db.py `_ADDED_COLUMNS` ENTRY: that shim exists for new COLUMNS on EXISTING
tables, which create_all() will never add. These are new TABLES, which create_all()
DOES create on a non-alembic SQLite boot - the same posture as workflowgroup,
projectpairprovider, contenttype, glossary/glossarypair, workflowbatch, styleguide and
the three LLMemory tables (b3e7c1d94a26 states it in full). Adding a shim entry would
be a no-op at best and a schema fork at worst. tests/verify_migrations.py compares this
migration's output against the models column-for-column INCLUDING nullability, which is
what keeps the alembic path and the create_all path honest.

SELF-FK ON SQLITE: evalcandidate.derived_from_id references evalcandidate.id inside the
same CREATE TABLE (the fork chain: an edit to a locked candidate writes a new row
pointing back at the old). Legal on SQLite and Postgres alike - the column is nullable
and is only ever set at INSERT time to an id that already exists.

PORTABLE DDL. Only sa.Integer/String/Float/Boolean/DateTime/JSON are used, all of which
render natively on both SQLite and Postgres (sa.JSON becomes SQLite TEXT-with-JSON-
serialization and Postgres JSON, exactly as the existing workflow/step JSON columns do).
Every NOT NULL column carries a server_default so the CREATE TABLE is safe to run
against a database that is being upgraded rather than built, and so a hand-written
INSERT that omits a column behaves like the model's Python default.

INDEXES mirror the models' `index=True` fields exactly and nothing more: the FK columns
every read filters on (evaldatasetitem.dataset_id, evalrun.dataset_id/candidate_id,
evalrunitem.run_id/dataset_item_id, evalscore.run_id/run_item_id), the customer scoping
columns (evaldataset.customer_id, evalcandidate.customer_id), and evalscore.scorer
(the comparison view slices every score in a run by metric id). No unique indexes: a
dataset x candidate pair legitimately has SEVERAL runs (that is repeat-N), and one run
item legitimately has several scores (one per scorer).

Hand-written (no autogenerate) off the head f2a9c4d81b73, same convention as
b3e7c1d94a26_llmemory_phase1.py. Runs clean on BOTH SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5a1f8e3b724'
down_revision: Union[str, None] = 'f2a9c4d81b73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'evaldataset',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False, server_default=''),
        sa.Column('description', sa.String(), nullable=False, server_default=''),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('source_locale', sa.String(), nullable=False, server_default='en-US'),
        sa.Column('target_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('origin', sa.String(), nullable=False, server_default='upload'),
        # PROVENANCE INTEGER, NOT AN FK - see the module docstring, decision 1.
        sa.Column('source_document_id', sa.Integer(), nullable=True),
        sa.Column('item_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_evaldataset_customer_id', 'evaldataset',
                    ['customer_id'], unique=False)

    op.create_table(
        'evaldatasetitem',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dataset_id', sa.Integer(), nullable=False),
        sa.Column('item_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('source_text', sa.String(), nullable=False, server_default=''),
        # NULLABLE ON PURPOSE (proposal section 6): NULL = "no reference", which is a
        # different state from an empty reference and from a bad score.
        sa.Column('reference_text', sa.String(), nullable=True),
        sa.Column('meta', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['dataset_id'], ['evaldataset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_evaldatasetitem_dataset_id', 'evaldatasetitem',
                    ['dataset_id'], unique=False)

    op.create_table(
        'evalcandidate',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False, server_default=''),
        sa.Column('description', sa.String(), nullable=False, server_default=''),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('workflow_id', sa.Integer(), nullable=True),
        sa.Column('config', sa.JSON(), nullable=True),
        sa.Column('locked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('derived_from_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflow.id']),
        sa.ForeignKeyConstraint(['derived_from_id'], ['evalcandidate.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_evalcandidate_customer_id', 'evalcandidate',
                    ['customer_id'], unique=False)

    op.create_table(
        'evalrun',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dataset_id', sa.Integer(), nullable=False),
        sa.Column('candidate_id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('repeat_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('scorers', sa.JSON(), nullable=True),
        sa.Column('metric_signature', sa.JSON(), nullable=True),
        sa.Column('provider', sa.String(), nullable=False, server_default=''),
        sa.Column('model', sa.String(), nullable=False, server_default=''),
        sa.Column('item_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('budget_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('error', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['dataset_id'], ['evaldataset.id']),
        sa.ForeignKeyConstraint(['candidate_id'], ['evalcandidate.id']),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_evalrun_dataset_id', 'evalrun', ['dataset_id'], unique=False)
    op.create_index('ix_evalrun_candidate_id', 'evalrun', ['candidate_id'], unique=False)

    op.create_table(
        'evalrunitem',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('dataset_item_id', sa.Integer(), nullable=False),
        sa.Column('output_text', sa.String(), nullable=False, server_default=''),
        sa.Column('latency_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['evalrun.id']),
        sa.ForeignKeyConstraint(['dataset_item_id'], ['evaldatasetitem.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_evalrunitem_run_id', 'evalrunitem', ['run_id'], unique=False)
    op.create_index('ix_evalrunitem_dataset_item_id', 'evalrunitem',
                    ['dataset_item_id'], unique=False)

    op.create_table(
        'evalscore',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        # NULL = a run-level AGGREGATE score rather than a per-item score.
        sa.Column('run_item_id', sa.Integer(), nullable=True),
        sa.Column('scorer', sa.String(), nullable=False, server_default=''),
        sa.Column('state', sa.String(), nullable=False, server_default='ok'),
        # NULLABLE ON PURPOSE - see the module docstring, decision 2.
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['evalrun.id']),
        sa.ForeignKeyConstraint(['run_item_id'], ['evalrunitem.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_evalscore_run_id', 'evalscore', ['run_id'], unique=False)
    op.create_index('ix_evalscore_run_item_id', 'evalscore', ['run_item_id'], unique=False)
    op.create_index('ix_evalscore_scorer', 'evalscore', ['scorer'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_evalscore_scorer', table_name='evalscore')
    op.drop_index('ix_evalscore_run_item_id', table_name='evalscore')
    op.drop_index('ix_evalscore_run_id', table_name='evalscore')
    op.drop_table('evalscore')
    op.drop_index('ix_evalrunitem_dataset_item_id', table_name='evalrunitem')
    op.drop_index('ix_evalrunitem_run_id', table_name='evalrunitem')
    op.drop_table('evalrunitem')
    op.drop_index('ix_evalrun_candidate_id', table_name='evalrun')
    op.drop_index('ix_evalrun_dataset_id', table_name='evalrun')
    op.drop_table('evalrun')
    op.drop_index('ix_evalcandidate_customer_id', table_name='evalcandidate')
    op.drop_table('evalcandidate')
    op.drop_index('ix_evaldatasetitem_dataset_id', table_name='evaldatasetitem')
    op.drop_table('evaldatasetitem')
    op.drop_index('ix_evaldataset_customer_id', table_name='evaldataset')
    op.drop_table('evaldataset')
