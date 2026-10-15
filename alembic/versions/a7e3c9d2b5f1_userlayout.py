"""userlayout: per-user saved page layouts (SPEC-project-layout-editor, train L1)

Revision ID: a7e3c9d2b5f1
Revises: d8c2a6f1e4b9
Create Date: 2026-09-24 12:00:00.000000

ONE new table `userlayout`: a person's saved card layout for one page TYPE (only
`project` in L1). Columns mirror the UserLayout model exactly (verify_alembic_authority
compares alembic-head vs create_all(models) for column-set + nullability):

    id           INTEGER  PK, NOT NULL
    owner        VARCHAR  NOT NULL   ("user:<id>", or "local" with sign-in off)
    user_id      INTEGER  NULL, FK user.id  (NULL for the auth-off "local" owner)
    page_key     VARCHAR  NOT NULL   (a key of app/services/page_layouts.py REGISTRY)
    layout_json  VARCHAR  NOT NULL   (validated {"v":1,"rows":[[...]]}, size-bounded)
    updated_at   DATETIME NOT NULL

UNIQUE (owner, page_key) as the named constraint ux_userlayout_owner_page, plus the two
non-unique indexes the model's `index=True` fields emit (ix_userlayout_owner,
ix_userlayout_user_id), so the alembic and create_all index sets agree.

Purely additive: the table starts empty, and no row = the server default layout, so an
upgraded install renders every page exactly as before. A NEW TABLE needs no app/db.py
`_ADDED_COLUMNS` entry (create_all builds it on the non-alembic SQLite path; the
projectbudget precedent). Hand-written off the head d8c2a6f1e4b9; a plain drop_table
downgrade needs no dialect branch.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7e3c9d2b5f1'
down_revision: Union[str, None] = 'd8c2a6f1e4b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'userlayout',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        sa.Column('page_key', sa.String(), nullable=False),
        sa.Column('layout_json', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('owner', 'page_key', name='ux_userlayout_owner_page'),
    )
    op.create_index('ix_userlayout_owner', 'userlayout', ['owner'])
    op.create_index('ix_userlayout_user_id', 'userlayout', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_userlayout_user_id', table_name='userlayout')
    op.drop_index('ix_userlayout_owner', table_name='userlayout')
    op.drop_table('userlayout')
