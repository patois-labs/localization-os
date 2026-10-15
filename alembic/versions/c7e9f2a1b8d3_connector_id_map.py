"""connectoridmap — the T3 (document-per-locale) source->target id map

Revision ID: c7e9f2a1b8d3
Revises: b6d1f4a9c2e7
Create Date: 2026-07-07 12:00:00.000000

Adds the `connectoridmap` table (SPEC-t3-notion-helpscout): the per-connection
source->target document/page id map that a T3 connector OWNS. T3 systems (Notion,
Help Scout Docs, GitBook, WordPress, AEM) have no native per-locale sub-resource,
so a "translation" is a SEPARATE page the connector creates and must persist the
(source, target_locale) -> target_id relationship for. This is framework code that
outlives any single connector.

The `status` column carries the reserve-then-fill + reconciliation lifecycle
(building | done | orphaned | needs_link); the UNIQUE (connection_id, source_id,
target_locale) index guarantees exactly one row per localized document, so a
resumable multi-batch build can never create two targets and a concurrent
double-run's second INSERT hits IntegrityError (the cross-process backstop to the
in-process reserve lock).

Hand-written (no autogenerate) off the D4 head b6d1f4a9c2e7, same convention as the
sibling webhook revisions. On the SQLite default path this table + index are built
by create_all()/_ensure_indexes in app/db.py; on Postgres Alembic is the authority,
so this revision is what makes the table exist there.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e9f2a1b8d3'
down_revision: Union[str, None] = 'b6d1f4a9c2e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'connectoridmap',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.String(), nullable=False),
        sa.Column('target_locale', sa.String(), nullable=False),
        sa.Column('target_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connection.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_connectoridmap_connection_id', 'connectoridmap',
                    ['connection_id'], unique=False)
    op.create_index('ix_connectoridmap_source_id', 'connectoridmap',
                    ['source_id'], unique=False)
    op.create_index('ux_connectoridmap_conn_src_loc', 'connectoridmap',
                    ['connection_id', 'source_id', 'target_locale'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_connectoridmap_conn_src_loc', table_name='connectoridmap')
    op.drop_index('ix_connectoridmap_source_id', table_name='connectoridmap')
    op.drop_index('ix_connectoridmap_connection_id', table_name='connectoridmap')
    op.drop_table('connectoridmap')
