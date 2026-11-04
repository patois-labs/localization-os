"""live event bus table (engine redesign S3, cross-process events, ADR 0110)

Revision ID: b8d4f2a6c1e9
Revises: a7e3c9d1f4b2
Create Date: 2026-10-22 23:30:00.000000

Adds one table for app/services/events.py:

  liveevent (new table) - transient cross-process live events. On SQLite every published
      event is a row that each process polls with an id cursor; on Postgres only payloads
      too big for one NOTIFY are stored here and the NOTIFY carries the row id. Rows are
      deleted by the events retention sweep after a few minutes. Columns: id (AUTOINCREMENT
      on SQLite so ids never go backwards after a sweep), channel, payload (JSON text),
      created_at (indexed for the sweep).

All additive: the new table is empty on upgrade and no existing table is touched. Mirrors
models.LiveEvent (the column-completeness guard in tests/verify_migrations.py pins the set).
Classified EXPLICIT_EXCLUSIONS in app/services/data_export_part2.py. Hand-written off the head
a7e3c9d1f4b2.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8d4f2a6c1e9"
down_revision: Union[str, None] = "a7e3c9d1f4b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "liveevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_liveevent_created_at", "liveevent", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_liveevent_created_at", table_name="liveevent")
    op.drop_table("liveevent")
