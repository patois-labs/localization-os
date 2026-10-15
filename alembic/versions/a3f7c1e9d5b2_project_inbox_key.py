"""project inbox re-key — stable inbox_key so renaming an inbox never forks a dup

Revision ID: a3f7c1e9d5b2
Revises: f2a9d4c1b7e8
Create Date: 2026-07-09 12:00:00.000000

SPEC-rename-delete (prereq for Project rename): the ONE name-based lookup in the app
was routing.ensure_department_inbox matching inbox projects by ``Project.name ==`` — so
renaming an inbox forked a duplicate on the next inbound document. This revision adds a
stable key the lookup keys on instead, making the display name safe to change.

  project.inbox_key              — nullable stable key: NULL for every ordinary project
                                   (the vast majority); "dept:<lower>" for a department
                                   inbox, "actor:<actor>" for a per-actor inbox. The
                                   backfill below reproduces routing._inbox_key for
                                   pre-existing inbox projects (matched by their
                                   historical name / created_by) so the re-keyed lookup
                                   finds them rather than forking a fresh inbox.

  ux_project_inbox_key           — FUNCTIONAL unique index over
                                   (coalesce(org_id,-1), inbox_key): at most ONE inbox
                                   per (org bucket, key). coalesce collapses the NULL
                                   org so gate-off inboxes share one bucket; ordinary
                                   projects carry inbox_key NULL, which SQL keeps
                                   DISTINCT so they never collide. Mirrors
                                   ux_contenttype_scope_name — enforced on BOTH SQLite
                                   (expression indexes) and Postgres.

Backfill dedup: only the EARLIEST id per (org bucket, key) is keyed, so the unique
index can always be built even if legacy data holds two same-key inboxes (a pre-fix
rename-fork) — the loser keeps inbox_key NULL (an ordinary project), matching the old
``.first()`` semantics that returned one arbitrary row.

Additive: every existing ordinary Project keeps inbox_key NULL (byte-identical). The
SAME two backfill statements run in app/db._backfill_inbox_keys on the SQLite
init_db/_ensure_columns path. Hand-written off the head f2a9d4c1b7e8; the inbox_key
column is a PLAIN nullable column (no FK) like the other additive nullable columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7c1e9d5b2'
down_revision: Union[str, None] = 'f2a9d4c1b7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('inbox_key', sa.String(), nullable=True))
    # Backfill pre-existing inbox projects (SQLite- and Postgres-portable SQL; kept
    # byte-identical to app/db._backfill_inbox_keys). "Inbox: " is 7 chars.
    op.execute(
        "UPDATE project SET inbox_key = 'dept:' || lower(substr(name, 8)) "
        "WHERE name LIKE 'Inbox: %' AND inbox_key IS NULL AND id IN ("
        "  SELECT MIN(id) FROM project WHERE name LIKE 'Inbox: %' "
        "  GROUP BY coalesce(org_id, -1), lower(substr(name, 8)))")
    op.execute(
        "UPDATE project SET inbox_key = 'actor:' || created_by "
        "WHERE name LIKE 'MCP Inbox (%)' AND inbox_key IS NULL AND id IN ("
        "  SELECT MIN(id) FROM project WHERE name LIKE 'MCP Inbox (%)' "
        "  GROUP BY coalesce(org_id, -1), created_by)")
    op.create_index('ux_project_inbox_key', 'project',
                    [sa.text('coalesce(org_id, -1)'), sa.text('inbox_key')],
                    unique=True)


def downgrade() -> None:
    # IF EXISTS, because this index may already be gone (#158, same fix as a1f4c8b2d6e9). It
    # is EXPRESSION-BASED (coalesce(org_id, -1), inbox_key), and SQLAlchemy cannot reflect an
    # expression index -- so any `batch_alter_table('project')` LATER in the chain (a full
    # table rebuild on SQLite) silently drops it and does not put it back: b6f1d0a7c34e
    # (project.project_type) is that op today. Downgrading past it rebuilds `project`, the
    # index goes with the old table, and an unconditional drop then failed the whole chain
    # with "no such index". Both SQLite and Postgres support IF EXISTS on DROP INDEX.
    op.execute("DROP INDEX IF EXISTS ux_project_inbox_key")
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('inbox_key')
