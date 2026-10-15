"""System prompt overrides (#356 phase 2a): the history table and the step-run stamp.

Revision ID: b2c4d6e8f0a1
Revises: a276b2c3d4e5

Two additive pieces, nothing destructive:

  * `systempromptoverride`: the append-only version history of an org's override of an
    editable pipeline system prompt head (the AI-step head and the machine-translation
    system message). One row per save / restore / reset; the highest version per
    (org, prompt_key) is the live state. A functional UNIQUE index over
    (coalesce(org_id, -1), prompt_key, version) keeps versions unique per org even where
    org_id is NULL (single-tenant), the same idiom as ux_project_inbox_key.
  * `steprun.system_prompt_key / system_prompt_version / system_prompt_sha256`: three
    NULLABLE columns recording which head an AI step visit ran with. NULL on every existing
    row and on every row written while the feature flag is off (no backfill).

op.add_column (not batch_alter_table) so a SQLite rebuild never touches an expression index.
The SQLite _ADDED_COLUMNS["steprun"] shim in app/db.py carries the same three columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c4d6e8f0a1"
down_revision: str | None = "a276b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "systempromptoverride",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("prompt_key", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("restored_from", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organization.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_systempromptoverride_version",
        "systempromptoverride",
        [sa.text("coalesce(org_id, -1)"), sa.text("prompt_key"), sa.text("version")],
        unique=True,
    )
    op.add_column("steprun", sa.Column("system_prompt_key", sa.String(), nullable=True))
    op.add_column("steprun", sa.Column("system_prompt_version", sa.Integer(), nullable=True))
    op.add_column("steprun", sa.Column("system_prompt_sha256", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("steprun", "system_prompt_sha256")
    op.drop_column("steprun", "system_prompt_version")
    op.drop_column("steprun", "system_prompt_key")
    op.drop_index("ux_systempromptoverride_version", table_name="systempromptoverride")
    op.drop_table("systempromptoverride")
