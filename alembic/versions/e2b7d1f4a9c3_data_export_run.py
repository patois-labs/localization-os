"""data export run (full-dump export audit/retention table, Batch 13 Train A)

Revision ID: e2b7d1f4a9c3
Revises: c7f1a2b93d84
Create Date: 2026-08-26 12:00:00.000000

Adds the SPEC-data-export Train-A audit/retention table:

  dataexportrun (new table) — one row per full-object-graph export: run_uuid (immutable,
      globally unique; the staged artifact key derives from it), the resolved scope
      (org_id/customer_id/scope_kind/scope_ref), the job options (history/fmt/emitters/
      include_audit/include_embeddings), status (queued|running|ok|partial|failed|
      expired|purged — `partial` first-class), job_id (no FK — the JobQueue idiom), the
      JSON bookkeeping (entity_counts, dropped_counts), the staged-artifact fields
      (artifact_key, artifact_bytes, manifest_sha256, chain_verify), the retention
      fields (expires_at, last_downloaded_at), error (secrets_scrub'd), and
      started_at/finished_at.

All additive: the new table is empty on upgrade and no existing table is touched — the
walker is strictly read-only against every SOURCE table and writes only these audit
rows. Mirrors the SQLite create_all() new-table + Postgres-alembic-authority pattern
(app/db.py) and models.DataExportRun; the column-completeness CI guard
(tests/verify_migrations.py) asserts this revision's column set equals the model's.
Hand-written (no autogenerate) off the head c7f1a2b93d84, same convention as the sibling
additive revisions (b3e8f1c92d47_databricks_export_run). Server_defaults on the NOT-NULL
columns mirror the model's sa_column_kwargs so create_all and the migration agree (the
Project.project_type lesson).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b7d1f4a9c3'
down_revision: Union[str, None] = 'c7f1a2b93d84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dataexportrun',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_uuid', sa.String(), nullable=False, server_default=''),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('actor', sa.String(), nullable=True),
        sa.Column('scope_kind', sa.String(), nullable=False, server_default=''),
        sa.Column('scope_ref', sa.Integer(), nullable=True),
        sa.Column('history', sa.String(), nullable=False, server_default='current'),
        sa.Column('fmt', sa.String(), nullable=False, server_default='jsonl'),
        sa.Column('emitters', sa.JSON(), nullable=True),
        sa.Column('include_audit', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('include_embeddings', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('include_eval', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('trusted_operator', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(), nullable=False, server_default='queued'),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('entity_counts', sa.JSON(), nullable=True),
        sa.Column('dropped_counts', sa.JSON(), nullable=True),
        sa.Column('artifact_key', sa.String(), nullable=False, server_default=''),
        sa.Column('artifact_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('manifest_sha256', sa.String(), nullable=False, server_default=''),
        sa.Column('chain_verify', sa.String(), nullable=False, server_default=''),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_downloaded_at', sa.DateTime(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_dataexportrun_run_uuid', 'dataexportrun',
                    ['run_uuid'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_dataexportrun_run_uuid', table_name='dataexportrun')
    op.drop_table('dataexportrun')
