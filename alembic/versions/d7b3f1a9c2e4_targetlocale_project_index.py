"""index targetlocale.project_id (S7b round 2, ADR 0103)

Revision ID: d7b3f1a9c2e4
Revises: c5a1e9d3b7f2
Create Date: 2026-10-24 04:00:00.000000

Every per-project language read (`TargetLocale.project_id == :id`: the translate door's
spellings, the lifecycle check after each translate job, the language lists) scanned the whole
table: under the load harness on a test Postgres, 11,527 sequential scans read 12 million rows
and no index was ever used, and the table grows by one row per project language. One plain
b-tree index on project_id; mirrors `models.TargetLocale.project_id` (index=True). Additive, no
data change, hand-written off the head c5a1e9d3b7f2.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d7b3f1a9c2e4"
down_revision: Union[str, None] = "c5a1e9d3b7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_targetlocale_project_id", "targetlocale", ["project_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_targetlocale_project_id", table_name="targetlocale")
