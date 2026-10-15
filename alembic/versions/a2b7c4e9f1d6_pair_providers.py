"""per-language-pair provider selection (Phase 2 / Intento-aligned)

Revision ID: a2b7c4e9f1d6
Revises: f1a2b3c4d5e6
Create Date: 2026-07-06 09:00:00.000000

Adds the Phase 2 per-pair provider schema (app/services/pair_providers.py resolver
+ the new-project popout mode toggle/matrix + `project create --pair-provider` CLI):

  project.provider_mode              — 'uniform' (default) | 'per_pair'
  projectpairprovider (new table)    — one row per (project_id, source_locale,
                                        target_locale) -> provider name / engine:<id>

All additive: project.provider_mode ships NOT NULL server_default 'uniform', so
every existing project backfills to UNIFORM = byte-identical to the pre-Phase-2
behavior (default_provider drives every pair; the resolver short-circuits on
UNIFORM). The projectpairprovider table is empty on upgrade (no rows until a
project opts into PER_PAIR). Mirrors the SQLite _ensure_columns() additive column
+ create_all() new-table pattern in app/db.py.

Hand-written (no autogenerate) to avoid spurious drop_index noise, matching
b2e4d7a9c3f1_workflow_groups.py. Runs clean on BOTH SQLite (batch_alter_table)
and Postgres (native ALTER).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b7c4e9f1d6'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provider_mode', sa.String(), nullable=False,
                                      server_default='uniform'))
    op.create_table(
        'projectpairprovider',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('source_locale', sa.String(), nullable=False),
        sa.Column('target_locale', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_projectpairprovider_project_id', 'projectpairprovider',
                    ['project_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_projectpairprovider_project_id',
                  table_name='projectpairprovider')
    op.drop_table('projectpairprovider')
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('provider_mode')
