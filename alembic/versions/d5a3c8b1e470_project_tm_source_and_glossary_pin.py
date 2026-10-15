"""attach an established TM / glossary to a project (projecttmsource, projectglossary)

Revision ID: d5a3c8b1e470
Revises: c4b7e1f92a3d
Create Date: 2026-08-04 09:00:00.000000

The maintainer's feedback intake 2026-08-03, item 5: the project panel could only IMPORT a
TMX / glossary file; there was no way to attach memory or terminology that already
exists elsewhere in the install. Two link tables, both purely additive:

  * `projecttmsource` (project_id, source_project_id) -- a REFERENCE. It widens the
    read scope every TM lookup funnels through (tm._tm_read_scope); it copies no rows
    and changes no write path, so deleting a link restores the previous behaviour
    byte-identically. One level only (never transitive).
  * `projectglossary` (project_id, glossary_id) -- a project-level PIN of an existing
    module glossary, unioned into glossary_module.build_glossary_plan.

Both carry a UNIQUE index over the pair, so a duplicate attach is a no-op rather than
a second row. Neither is trusted at read time: the reading seams re-derive what a link
is ALLOWED to reach from the live Project/Glossary rows through the same access
predicate the write path enforces, so a stale or hostile link row reaches nothing.

Every column is NOT NULL except the pure-default strings/timestamps, matching the model
fields exactly for the nullability-parity CI guard (verify_alembic_authority /
verify_migrations). An install that never attaches anything holds zero rows in both
tables, which is what makes the whole change byte-identical to before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5a3c8b1e470'
down_revision: Union[str, None] = 'c4b7e1f92a3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. projecttmsource -------------------------------------------------------
    op.create_table(
        'projecttmsource',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('source_project_id', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ),
        sa.ForeignKeyConstraint(['source_project_id'], ['project.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_projecttmsource_project_id', 'projecttmsource',
                    ['project_id'], unique=False)
    op.create_index('ix_projecttmsource_source_project_id', 'projecttmsource',
                    ['source_project_id'], unique=False)
    # At most ONE link per (project, source) -- a duplicate attach is a no-op, and the
    # read-side de-dup can never be defeated by a second row.
    op.create_index('ux_projecttmsource_pair', 'projecttmsource',
                    ['project_id', 'source_project_id'], unique=True)

    # --- 2. projectglossary -------------------------------------------------------
    op.create_table(
        'projectglossary',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('glossary_id', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ),
        sa.ForeignKeyConstraint(['glossary_id'], ['glossary.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_projectglossary_project_id', 'projectglossary',
                    ['project_id'], unique=False)
    op.create_index('ix_projectglossary_glossary_id', 'projectglossary',
                    ['glossary_id'], unique=False)
    op.create_index('ux_projectglossary_pair', 'projectglossary',
                    ['project_id', 'glossary_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_projectglossary_pair', table_name='projectglossary')
    op.drop_index('ix_projectglossary_glossary_id', table_name='projectglossary')
    op.drop_index('ix_projectglossary_project_id', table_name='projectglossary')
    op.drop_table('projectglossary')
    op.drop_index('ux_projecttmsource_pair', table_name='projecttmsource')
    op.drop_index('ix_projecttmsource_source_project_id', table_name='projecttmsource')
    op.drop_index('ix_projecttmsource_project_id', table_name='projecttmsource')
    op.drop_table('projecttmsource')
