"""quality lenses T3: mission lens_id + staged-target gate columns

Revision ID: f2c8a4e6b1d9
Revises: d7a3c1b9e4f2
Create Date: 2026-09-02 12:00:00.000000

Three ADDITIVE columns wiring the Quality Lens gate into the Advanced tier's OWN tables (the
classic engine is untouched -- SPEC-advanced-workflows §2 HARD RULE):

  advancedmission.lens_id                 -- the lens that gates this mission's deliveries.
                                             NULL = NO lens = today's behavior (P2-D14): a
                                             pre-T3 mission runs UNGATED. No FK (the
                                             customer_id idiom), so a forked/re-homed lens does
                                             not block the write.
  advancedstagedtarget.quality_evaluation_id -- the uuid4 hex of the evaluation that judged
                                             this staged row (H7). NULL = never judged.
  advancedstagedtarget.quality_blocked    -- True once quality.judge held this row; deliver
                                             SKIPS it and names the blocking check(s). NOT NULL
                                             DEFAULT 0 (the qe_gate bool shape), so every
                                             existing row backfills False = byte-identical.

All three are nullable / neutral-defaulted, so the upgrade is a no-op for existing rows. Mirrors
models.AdvancedMission / models.AdvancedStagedTarget + the create_all() shim in app/db.py
(_ADDED_COLUMNS), so the nullability-parity guard (tests/verify_migrations.py) holds on BOTH
SQLite and Postgres. Chained off the single head d7a3c1b9e4f2 (alembic heads == 1).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c8a4e6b1d9"
down_revision: Union[str, None] = "d7a3c1b9e4f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("advancedmission", sa.Column("lens_id", sa.Integer(), nullable=True))
    op.add_column(
        "advancedstagedtarget",
        sa.Column("quality_evaluation_id", sa.String(), nullable=True),
    )
    op.add_column(
        "advancedstagedtarget",
        sa.Column("quality_blocked", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("advancedstagedtarget", "quality_blocked")
    op.drop_column("advancedstagedtarget", "quality_evaluation_id")
    op.drop_column("advancedmission", "lens_id")
