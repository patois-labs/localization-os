"""train.I.scim — user.external_id column + usergroup/usergroupmember tables

Revision ID: c7d2e9f4a1b8
Revises: b4d9f2c7e1a3
Create Date: 2026-08-24 09:00:00.000000

SPRINT-2026-08-24 Train I (SCIM 2.0 + SAML 2.0). Purely additive, off the wave3
collaboration head b4d9f2c7e1a3 — verified single head at authoring time. Three
additive changes:

  1. ONE NULLABLE column on the EXISTING `user` table: `external_id` (the SCIM IdP's
     stable opaque id, NULL on every non-SCIM account). NO server default — NULL is
     the honest value. Native ALTER ... ADD COLUMN on SQLite + Postgres (no table
     rebuild, so user's indexes survive). The SAME column lands on the non-alembic
     SQLite path via app/db._ADDED_COLUMNS["user"], and the ix_user_external_id index
     via app/db._INDEXES + the model Field(index=True), so all three build paths agree
     and verify_alembic_authority stays at zero diff.

  2. TWO NEW TABLES — `usergroup` and `usergroupmember` (SCIM directory metadata).
     On the non-alembic SQLite init_db path create_all() makes them (a NEW TABLE needs
     NO _ADDED_COLUMNS entry — the querythread/SegmentPostEdit precedent). `usergroup`
     carries a FUNCTIONAL unique index ux_usergroup_org_name over (coalesce(org_id,-1),
     name) — the ux_project_inbox_key precedent (coalesce collapses NULL org so gate-off
     groups share one bucket). `usergroupmember` carries ux_usergroupmember_group_user so
     a repeated SCIM `add` PATCH is idempotent.

  3. The externalId lookup indexes (ix_user_external_id, ix_usergroup_external_id) —
     names match SQLAlchemy's index=True default so create_all() agrees.

Hand-written (no autogenerate) off the head b4d9f2c7e1a3, same additive convention as
the sibling revisions. DIALECT-BRANCHED downgrade (native SQLite DROP COLUMN vs
op.drop_column for the user column), matching b4d9f2c7e1a3's shape.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2e9f4a1b8'
down_revision: Union[str, None] = 'b4d9f2c7e1a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. user.external_id (native ALTER ADD COLUMN; nullable, no server default).
    op.add_column('user', sa.Column('external_id', sa.String(), nullable=True))
    op.create_index('ix_user_external_id', 'user', ['external_id'])

    # 2. usergroup — a SCIM directory group (directory metadata only; zero authz).
    op.create_table(
        'usergroup',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organization.id'), nullable=True),
        sa.Column('external_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_usergroup_name', 'usergroup', ['name'])
    op.create_index('ix_usergroup_external_id', 'usergroup', ['external_id'])
    # FUNCTIONAL unique index (the ux_project_inbox_key precedent): one group per
    # (org bucket, name); coalesce collapses NULL org so gate-off groups share a bucket.
    op.create_index('ux_usergroup_org_name', 'usergroup',
                    [sa.text('coalesce(org_id, -1)'), sa.text('name')],
                    unique=True)

    # 3. usergroupmember — one (group, user) edge; unique so a repeated add is idempotent.
    op.create_table(
        'usergroupmember',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('usergroup.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('added_by', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('group_id', 'user_id', name='ux_usergroupmember_group_user'),
    )
    op.create_index('ix_usergroupmember_group_id', 'usergroupmember', ['group_id'])
    op.create_index('ix_usergroupmember_user_id', 'usergroupmember', ['user_id'])


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'
    # 3/2 reverse: drop the two new tables (their indexes + unique constraints go with them).
    op.drop_table('usergroupmember')
    op.drop_table('usergroup')
    # 1 reverse: drop the externalId index + user column.
    op.drop_index('ix_user_external_id', table_name='user')
    if is_sqlite:
        op.execute('ALTER TABLE "user" DROP COLUMN external_id')
    else:
        op.drop_column('user', 'external_id')
