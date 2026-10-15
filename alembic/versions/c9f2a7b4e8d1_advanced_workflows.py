"""advanced workflows harness (P0 skeleton)

Revision ID: c9f2a7b4e8d1
Revises: d7f4b2c9e6a1
Create Date: 2026-09-01 09:00:00.000000

Adds the SPEC-advanced-workflows P0 skeleton -- a SECOND workflow tier that lives
entirely BESIDE the classic step-list engine (the HARD RULE: the classic engine is
untouched). Four NEW tables, all additive (empty on upgrade):

  advancedmission          -- a reusable mission: brief, tool allowlist, model roster,
                              budget, fences, project/customer binding.
  advancedrun              -- one execution of a mission through the deterministic harness
                              loop: status (incl. PAUSED/FAILED_BUDGET/CANCELLED), the
                              cooperative cancel flag, live spend/fence actuals, LLM Ledger
                              correlation ids.
  advancedtranscriptentry  -- the durable, auditable transcript rows (plan / tool_call /
                              observation / delegate_result / adjudication / delivery_manifest
                              / note), write-coalesced by the harness.
  advancedstagedtarget     -- candidate targets the harness proposes; they live HERE and
                              never touch a classic content table (P0 delivery is a stub).

Mirrors the models (models.AdvancedMission / AdvancedRun / AdvancedTranscriptEntry /
AdvancedStagedTarget) + the create_all() new-table path in app/db.py: the nullability-parity
CI guard (tests/verify_migrations.py) asserts this revision's column set + nullability equal
the models'. Runs clean on BOTH SQLite (op.create_table) and Postgres. Hand-written (no
autogenerate) off the head d7f4b2c9e6a1, same convention as the sibling revisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c9f2a7b4e8d1"
down_revision: Union[str, None] = "d7f4b2c9e6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "advancedmission",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("brief", sa.String(), nullable=False, server_default=""),
        sa.Column("tool_allowlist", sa.JSON(), nullable=False),
        sa.Column("model_roster", sa.JSON(), nullable=False),
        sa.Column("scripted_actions", sa.JSON(), nullable=False),
        sa.Column("budget_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "max_provider_calls_per_unit", sa.Integer(), nullable=False, server_default="6"
        ),
        sa.Column("wall_clock_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organization.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "advancedrun",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mission_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("action_id", sa.String(), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("spent_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["mission_id"], ["advancedmission.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organization.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "advancedtranscriptentry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
        sa.Column("tool", sa.String(), nullable=True),
        sa.Column("tier", sa.String(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["advancedrun.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "advancedstagedtarget",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("unit_key", sa.String(), nullable=False, server_default=""),
        sa.Column("source_text", sa.String(), nullable=False, server_default=""),
        sa.Column("target_text", sa.String(), nullable=False, server_default=""),
        sa.Column("provider_used", sa.String(), nullable=True),
        sa.Column("model_used", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["advancedrun.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("advancedstagedtarget")
    op.drop_table("advancedtranscriptentry")
    op.drop_table("advancedrun")
    op.drop_table("advancedmission")
