"""user.onboarding_dismissed_at (onboarding checklist eligibility flag)

Revision ID: f9c2e7a4d1b8
Revises: f8c1a4d7e2b9
Create Date: 2026-07-20 09:00:00.000000

Adds the SPEC-onboarding-tour Phase 0 flag to `user`:

  user.onboarding_dismissed_at  — nullable DATETIME. NULL = ELIGIBLE (the first-run
                                  "Get started" setup checklist may auto-show once,
                                  admins/linguists only); a non-NULL timestamp =
                                  DISMISSED/complete (never auto-shows again).

THE CRITICAL back-compat detail (the live Fly instance has REAL users): a plain
nullable column defaults to NULL, which our own semantics read as ELIGIBLE — so
without a backfill every existing user would be shown the tour on their next
dashboard view. WRONG. The upgrade therefore BACKFILLS all pre-existing users to
dismissed:

  UPDATE "user" SET onboarding_dismissed_at =
      COALESCE(last_login, created_at, CURRENT_TIMESTAMP)
  WHERE onboarding_dismissed_at IS NULL

Only users created AFTER the migration land with NULL -> eligible. This mirrors the
established one-time-backfill-in-the-just-added-branch pattern in app/db.py
(inbox_key, webhookevent.processed_at) and the SQLite _ADDED_COLUMNS["user"] shim
carries the SAME backfill so a non-alembic SQLite boot backfills existing users
exactly once. The back-compat invariant a test asserts: after upgrade, ZERO
existing users are eligible; only accounts created afterward are.

Hand-written (no autogenerate) off the head f8c1a4d7e2b9, same convention as the
sibling additive-column revisions. Runs clean on SQLite (batch_alter_table) and
Postgres (native ALTER); the CURRENT_TIMESTAMP / COALESCE UPDATE is portable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9c2e7a4d1b8'
down_revision: Union[str, None] = 'f8c1a4d7e2b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('onboarding_dismissed_at', sa.DateTime(), nullable=True))
    # Backfill EVERY pre-existing user to dismissed so no live account is shown the
    # tour uninvited (see the module docstring). Only rows present at migration time
    # are stamped; accounts created afterward keep NULL = eligible.
    op.execute(
        'UPDATE "user" SET onboarding_dismissed_at = '
        'COALESCE(last_login, created_at, CURRENT_TIMESTAMP) '
        'WHERE onboarding_dismissed_at IS NULL')


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('onboarding_dismissed_at')
