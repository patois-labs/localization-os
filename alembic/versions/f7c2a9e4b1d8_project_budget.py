"""projectbudget — per-project monthly USD spend cap (CV-2)

Revision ID: f7c2a9e4b1d8
Revises: e7a1b2c3d4e5
Create Date: 2026-08-19 12:00:00.000000

ONE new table `projectbudget` (requirement CV-2): a per-PROJECT monthly spend cap in
money, the sibling of `departmentbudget` (per-department TOKEN cap). Columns mirror the
ProjectBudget model exactly (verify_alembic_authority compares alembic-head vs
create_all(models) for column-set + nullability — they MUST agree):

    id                 INTEGER  PK, NOT NULL
    project_id         INTEGER  NOT NULL, UNIQUE  (one budget per project)
    monthly_usd_limit  FLOAT    NOT NULL          (<= 0 = unlimited)
    org_id             INTEGER  NULL              (multi-org scoping, like departmentbudget)
    notified_period    VARCHAR  NULL              ("YYYY-MM" the notified_pct applies to)
    notified_pct       INTEGER  NOT NULL          (highest threshold band alerted: 0/80/100)
    created_by         VARCHAR  NOT NULL
    created_at         DATETIME NOT NULL
    updated_at         DATETIME NOT NULL

A NEW TABLE needs NO app/db.py `_ADDED_COLUMNS` entry — create_all() builds it on the
non-alembic SQLite init_db path (the departmentbudget / segmentpostedit / LLMemory
precedent; _ADDED_COLUMNS exists only for new COLUMNS on EXISTING tables, which
create_all never adds). This revision is the Postgres/alembic authority for the same
table. Hand-written (no autogenerate) off the head e7a1b2c3d4e5, same additive convention
as the sibling revisions; the create/drop symmetry follows d4e5f6a7b8c9 / e7a1b2c3d4e5
(a plain drop_table needs no dialect branch — no column is dropped from an existing table).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7c2a9e4b1d8'
down_revision: Union[str, None] = 'e7a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'projectbudget',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id'), nullable=False),
        sa.Column('monthly_usd_limit', sa.Float(), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organization.id'), nullable=True),
        sa.Column('notified_period', sa.String(), nullable=True),
        sa.Column('notified_pct', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('project_id', name='ux_projectbudget_project_id'),
    )
    op.create_index('ix_projectbudget_project_id', 'projectbudget', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_projectbudget_project_id', table_name='projectbudget')
    op.drop_table('projectbudget')
