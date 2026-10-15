"""delivery empty approval table (Batch 15 T2)

Revision ID: d7f4b2c9e6a1
Revises: c5e8a1f9d3b7
Create Date: 2026-08-30 14:00:00.000000

SPEC-tm-match-step-config §5.2 / §17 (Batch 15 Train 2). Additive only.

ADD TABLE deliveryemptyapproval -- the human APPROVAL record for delivering a
tm_match-provenanced deliverable that still carries EMPTY (untranslated) targets. The
held state itself is DERIVED LIVE at delivery (tm_delivery.empty_gate_decision); this
table stores ONLY the approval. DUAL-MODE (N1): document_id set = DOCUMENT mode; NULL =
KEY/project mode.

TWO PARTIAL UNIQUE INDEXES keep "one live approval per delivery target" per mode without
a NULL-defeated composite unique (P4):
  ux_dea_doc  ON (document_id, locale) WHERE document_id IS NOT NULL
  ux_dea_keys ON (project_id, locale)  WHERE document_id IS NULL
Both must ALSO ride the model __table_args__ with both dialect WHEREs (so create_all
emits them); neither verify_alembic_authority (no index diff) nor verify_migrations (only
the four db.py hot-path indexes) compares them, so the three-path index+WHERE probe lives
in verify_tm_empty_delivery.py.

Chained off the Batch 15 T1 head (c5e8a1f9d3b7). A NEW table needs NO app/db.py
_ADDED_COLUMNS shim -- create_all() makes it on the SQLite path (the KeyImage / LLMemory
precedent). Hand-written (no autogenerate).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d7f4b2c9e6a1"
down_revision: Union[str, None] = "c5e8a1f9d3b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deliveryemptyapproval",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("locale", sa.String(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("covered", sa.JSON(), nullable=True),
        sa.Column("covered_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["workflowrun.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deliveryemptyapproval_project_id", "deliveryemptyapproval", ["project_id"])
    op.create_index(
        "ix_deliveryemptyapproval_document_id", "deliveryemptyapproval", ["document_id"]
    )
    op.create_index("ix_deliveryemptyapproval_locale", "deliveryemptyapproval", ["locale"])
    # The two partial unique indexes (P4). SQLite honors the sqlite_where; Postgres the
    # postgresql_where. batch_op is unnecessary for a fresh table.
    op.create_index(
        "ux_dea_doc",
        "deliveryemptyapproval",
        ["document_id", "locale"],
        unique=True,
        sqlite_where=sa.text("document_id IS NOT NULL"),
        postgresql_where=sa.text("document_id IS NOT NULL"),
    )
    op.create_index(
        "ux_dea_keys",
        "deliveryemptyapproval",
        ["project_id", "locale"],
        unique=True,
        sqlite_where=sa.text("document_id IS NULL"),
        postgresql_where=sa.text("document_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_dea_keys", table_name="deliveryemptyapproval")
    op.drop_index("ux_dea_doc", table_name="deliveryemptyapproval")
    op.drop_index("ix_deliveryemptyapproval_locale", table_name="deliveryemptyapproval")
    op.drop_index("ix_deliveryemptyapproval_document_id", table_name="deliveryemptyapproval")
    op.drop_index("ix_deliveryemptyapproval_project_id", table_name="deliveryemptyapproval")
    op.drop_table("deliveryemptyapproval")
