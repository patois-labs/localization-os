"""auditlog.org_id (#487: audit rows are confined by the org they were written in)

Revision ID: d7b3e9f1a5c2
Revises: c5e2a8d4f1b7
Create Date: 2026-10-05 21:00:00.000000

Adds `auditlog.org_id` (nullable INTEGER, indexed ix_auditlog_org_id): the organization the
ACTOR belonged to when the row was written, stamped at the audit seam. Multi-org audit
views (customer /audit, the subject-access export, the workspace export) used to filter on
the actor's CURRENT org, so a user moved to another org carried the old org's history
along. Like customer_id this is a view/filter column OUTSIDE the HMAC-chained payload, so
no existing row_hash changes and the chain keeps verifying.

Backfill (best effort, one time): every row is stamped with its actor's current org, then
rows written before a recorded `user.move_org` are re-stamped with the org the user was
moved out of (the move row's detail starts `org <old>-><new>`). Rows whose actor has no
account (system, unauthenticated) stay NULL and are never shown to an org. The SQLite
non-alembic boot path carries the same column in db._ADDED_COLUMNS, the index in
db._INDEXES and the same backfill in db._backfill_auditlog_org.

Hand-written off the head c5e2a8d4f1b7. Native ALTER ADD COLUMN (no batch_alter_table).
"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7b3e9f1a5c2'
down_revision: Union[str, None] = 'c5e2a8d4f1b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('auditlog', sa.Column('org_id', sa.Integer(), nullable=True))
    op.create_index('ix_auditlog_org_id', 'auditlog', ['org_id'])
    conn = op.get_bind()
    conn.execute(
        sa.text(
            'UPDATE auditlog SET org_id = (SELECT u.org_id FROM "user" u '
            'WHERE lower(u.email) = lower(auditlog.actor) LIMIT 1) WHERE org_id IS NULL'
        )
    )
    moves = conn.execute(
        sa.text("SELECT id, target, detail FROM auditlog WHERE action = 'user.move_org' ORDER BY id")
    ).fetchall()
    previous = {}
    for move_id, target, detail in moves:
        who = (target or '').lower()
        hit = re.match(r'org (\d+|None)->', detail or '')
        lower = previous.get(who, 0)
        previous[who] = move_id
        if not who or not hit:
            continue
        old_org = None if hit.group(1) == 'None' else int(hit.group(1))
        conn.execute(
            sa.text(
                'UPDATE auditlog SET org_id = :org WHERE lower(actor) = :who '
                'AND id > :lo AND id <= :hi'
            ),
            {'org': old_org, 'who': who, 'lo': lower, 'hi': move_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index('ix_auditlog_org_id', table_name='auditlog')
    if bind.dialect.name == 'sqlite':
        op.execute('ALTER TABLE auditlog DROP COLUMN org_id')
    else:
        op.drop_column('auditlog', 'org_id')
