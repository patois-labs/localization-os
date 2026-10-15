"""Per-workflow system prompt overrides (#356 phase 2b): the history table and a scope stamp.

Revision ID: c3d5e7f9a1b2
Revises: b2c4d6e8f0a1

Two additive pieces, nothing destructive:

  * `workflowsystempromptoverride`: the append-only version history of ONE workflow's
    override of an editable pipeline system prompt head (`step_head`, `mt_head`). One row
    per save / restore / reset; the highest version per (workflow, prompt_key) is the live
    state, and a reset row means the workflow inherits the org override. UNIQUE
    (workflow_id, prompt_key, version) keeps versions unique per workflow. A plain FK onto
    workflow.id (no ondelete): `workflows.delete_workflow` reaps these rows first.
  * `steprun.system_prompt_scope`: one NULLABLE column recording which scope's head an AI step
    visit ran with (workflow, org or builtin). NULL on every existing row and on every row
    written while the feature flag is off (no backfill).

The SQLite _ADDED_COLUMNS["steprun"] shim in app/db.py carries the same column.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d5e7f9a1b2"
down_revision: str | None = "b2c4d6e8f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflowsystempromptoverride",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("prompt_key", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("restored_from", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id", "prompt_key", "version", name="ux_wfsystempromptoverride_version"
        ),
    )
    op.create_index(
        op.f("ix_workflowsystempromptoverride_workflow_id"),
        "workflowsystempromptoverride",
        ["workflow_id"],
        unique=False,
    )
    op.add_column("steprun", sa.Column("system_prompt_scope", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("steprun", "system_prompt_scope")
    op.drop_index(
        op.f("ix_workflowsystempromptoverride_workflow_id"),
        table_name="workflowsystempromptoverride",
    )
    op.drop_table("workflowsystempromptoverride")
