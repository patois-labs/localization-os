"""databricks export run (analytical exporter audit/resume table, phase 2 Train A)

Revision ID: b3e8f1c92d47
Revises: c2f1a7d4e9b6
Create Date: 2026-08-16 12:00:00.000000

Adds the SPEC-databricks-exporter Train-A audit/resume table:

  databricksexportrun (new table) — one row per export BATCH: run_uuid (immutable,
      globally-unique, invariant I1), started_at/finished_at, target (local|databricks),
      status (running|ok|partial|failed — `partial` first-class), format (ndjson|parquet),
      and the JSON bookkeeping columns (streams, row_counts, dropped_counts, high_water,
      watermark_lag), deletions_not_tracked, actor_hash_key_version, error (redacted),
      actor, org_id.

All additive: the new table is empty on upgrade and no existing table is touched — the
exporter is strictly read-only against every SOURCE table and writes only these audit
rows. Mirrors the SQLite create_all() new-table + Postgres-alembic-authority pattern
(app/db.py) and models.DatabricksExportRun; the nullability-parity CI guard
(tests/verify_migrations.py) asserts this revision's column set + nullability equal the
model's. Hand-written (no autogenerate) off the head c2f1a7d4e9b6, same convention as the
sibling additive revisions. Server_defaults on the NOT-NULL columns mirror the model's
sa_column_kwargs so create_all and the migration agree (the Project.project_type lesson).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e8f1c92d47'
down_revision: Union[str, None] = 'c2f1a7d4e9b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'databricksexportrun',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_uuid', sa.String(), nullable=False, server_default=''),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('target', sa.String(), nullable=False, server_default='local'),
        sa.Column('status', sa.String(), nullable=False, server_default='running'),
        sa.Column('format', sa.String(), nullable=False, server_default='ndjson'),
        sa.Column('streams', sa.JSON(), nullable=True),
        sa.Column('row_counts', sa.JSON(), nullable=True),
        sa.Column('dropped_counts', sa.JSON(), nullable=True),
        sa.Column('high_water', sa.JSON(), nullable=True),
        sa.Column('watermark_lag', sa.JSON(), nullable=True),
        sa.Column('deletions_not_tracked', sa.Boolean(), nullable=False,
                  server_default='0'),
        sa.Column('actor_hash_key_version', sa.String(), nullable=False,
                  server_default=''),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('actor', sa.String(), nullable=True),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_databricksexportrun_run_uuid', 'databricksexportrun',
                    ['run_uuid'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_databricksexportrun_run_uuid',
                  table_name='databricksexportrun')
    op.drop_table('databricksexportrun')
