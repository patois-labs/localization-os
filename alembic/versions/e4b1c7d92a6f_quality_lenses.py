"""quality lenses substrate (P2 T1)

Revision ID: e4b1c7d92a6f
Revises: b4e1d7c2a9f3
Create Date: 2026-09-01 12:00:00.000000

Adds the SPEC-quality-lenses-p2 T1 substrate -- five NEW tables, all additive (empty on
upgrade). NO column is added to any EXISTING table by this revision, so it needs no
`app/db.py::_ADDED_COLUMNS` twin (the mission/staged-target columns land with T3, each with
its own twin in the same commit).

  qualitylens         -- the named lens: taxonomy, severity vocabulary, weight table, bar.
  qualitylensexample  -- curated few-shot SPAN examples (spans only, never scores).
  qualityfinding      -- THE SUBSTRATE: one span/omission finding under one lens.
  qualitygolditem     -- a human-labelled gold item with an IMMUTABLE stored hypothesis.
  qualitygoldlabel    -- one human span/omission label on a gold item.

Mirrors the models (models.QualityLens / QualityLensExample / QualityFinding /
QualityGoldItem / QualityGoldLabel) + the create_all() new-table path in app/db.py: the
nullability-parity CI guard (tests/verify_migrations.py) asserts this revision's column set
+ nullability equal the models'. Runs clean on BOTH SQLite (op.create_table) and Postgres.
Hand-written (no autogenerate) off the head b4e1d7c2a9f3, same convention as the siblings.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e4b1c7d92a6f"
down_revision: Union[str, None] = "b4e1d7c2a9f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qualitylens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("slug", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(), nullable=False, server_default="judge"),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column("severities", sa.JSON(), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("block_severity", sa.String(), nullable=False, server_default="major"),
        sa.Column("threshold", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("max_findings", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("gate_policy", sa.String(), nullable=False, server_default="block"),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("blocking_checks", sa.JSON(), nullable=False),
        sa.Column("adjudication", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("normalize_per_words", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assertions", sa.JSON(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organization.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_qualitylens_slug", "qualitylens", ["slug"], unique=False)
    op.create_table(
        "qualitylensexample",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lens_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("source_locale", sa.String(), nullable=False, server_default=""),
        sa.Column("target_locale", sa.String(), nullable=False, server_default=""),
        sa.Column("source_text", sa.String(), nullable=False, server_default=""),
        sa.Column("target_text", sa.String(), nullable=False, server_default=""),
        sa.Column("span_start", sa.Integer(), nullable=True),
        sa.Column("span_end", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(), nullable=False, server_default=""),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lens_id"], ["qualitylens.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organization.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_qualitylensexample_lens_id", "qualitylensexample", ["lens_id"], unique=False
    )
    op.create_table(
        "qualityfinding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evaluation_id", sa.String(), nullable=False, server_default=""),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("segment_id", sa.Integer(), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("staged_target_id", sa.Integer(), nullable=True),
        sa.Column("unit_key", sa.String(), nullable=False, server_default=""),
        sa.Column("lens_id", sa.Integer(), nullable=True),
        sa.Column("lens_slug", sa.String(), nullable=False, server_default=""),
        sa.Column("origin", sa.String(), nullable=False, server_default="judge"),
        sa.Column("kind", sa.String(), nullable=False, server_default="span"),
        sa.Column("check_id", sa.String(), nullable=True),
        sa.Column("severity", sa.String(), nullable=False, server_default=""),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("span_start", sa.Integer(), nullable=True),
        sa.Column("span_end", sa.Integer(), nullable=True),
        sa.Column("span_text", sa.String(), nullable=False, server_default=""),
        sa.Column("source_start", sa.Integer(), nullable=True),
        sa.Column("source_end", sa.Integer(), nullable=True),
        sa.Column("source_span_text", sa.String(), nullable=False, server_default=""),
        sa.Column("note", sa.String(), nullable=False, server_default=""),
        sa.Column("engine_ref", sa.String(), nullable=True),
        sa.Column("engine_model", sa.String(), nullable=True),
        sa.Column("engine_family", sa.String(), nullable=True),
        sa.Column("translator_ref", sa.String(), nullable=True),
        sa.Column("translator_model", sa.String(), nullable=True),
        sa.Column("translator_family", sa.String(), nullable=True),
        sa.Column("family_separation", sa.Boolean(), nullable=True),
        sa.Column("source_locale", sa.String(), nullable=False, server_default=""),
        sa.Column("target_locale", sa.String(), nullable=False, server_default=""),
        sa.Column("content_type_id", sa.Integer(), nullable=True),
        sa.Column("adjudication_outcome", sa.String(), nullable=True),
        sa.Column("post_edit_fate", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["lens_id"], ["qualitylens.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organization.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["advancedrun.id"]),
        sa.ForeignKeyConstraint(["segment_id"], ["segment.id"]),
        sa.ForeignKeyConstraint(["staged_target_id"], ["advancedstagedtarget.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_qualityfinding_evaluation_id", "qualityfinding", ["evaluation_id"], unique=False
    )
    op.create_table(
        "qualitygolditem",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_item_id", sa.Integer(), nullable=False),
        sa.Column("hypothesis_text", sa.String(), nullable=False, server_default=""),
        sa.Column("hypothesis_sha", sa.String(), nullable=False, server_default=""),
        sa.Column("gold_score", sa.Integer(), nullable=True),
        sa.Column("labeler", sa.String(), nullable=False, server_default=""),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_item_id"], ["evaldatasetitem.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_qualitygolditem_dataset_item_id", "qualitygolditem", ["dataset_item_id"], unique=False
    )
    op.create_table(
        "qualitygoldlabel",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gold_item_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="span"),
        sa.Column("span_start", sa.Integer(), nullable=True),
        sa.Column("span_end", sa.Integer(), nullable=True),
        sa.Column("span_text", sa.String(), nullable=False, server_default=""),
        sa.Column("source_start", sa.Integer(), nullable=True),
        sa.Column("source_end", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(), nullable=False, server_default=""),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=False, server_default=""),
        sa.Column("labeler", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["gold_item_id"], ["qualitygolditem.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_qualitygoldlabel_gold_item_id", "qualitygoldlabel", ["gold_item_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_qualitygoldlabel_gold_item_id", table_name="qualitygoldlabel")
    op.drop_table("qualitygoldlabel")
    op.drop_index("ix_qualitygolditem_dataset_item_id", table_name="qualitygolditem")
    op.drop_table("qualitygolditem")
    op.drop_index("ix_qualityfinding_evaluation_id", table_name="qualityfinding")
    op.drop_table("qualityfinding")
    op.drop_index("ix_qualitylensexample_lens_id", table_name="qualitylensexample")
    op.drop_table("qualitylensexample")
    op.drop_index("ix_qualitylens_slug", table_name="qualitylens")
    op.drop_table("qualitylens")
