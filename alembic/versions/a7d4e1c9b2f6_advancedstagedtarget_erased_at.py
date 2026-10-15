"""advanced retention reap round 3 (L2): advancedstagedtarget.erased_at

Revision ID: a7d4e1c9b2f6
Revises: c4d8e2a1f7b6
Create Date: 2026-09-05 12:00:00.000000

ONE additive, nullable column for the Advanced document-erasure reap (round-3 L2):

  advancedstagedtarget.erased_at (DATETIME, nullable): the instant a document/key erasure
    SEVERED this row's content (reap_document_content arm (c) NULLs source/target/hash/
    segment_id/key_id). `deliver` skips any row carrying this stamp, so a severed KEYS row can
    never resolve AGAIN by a namespace::name lookup after a delete+restore and deliver an EMPTY
    target (the lens `empty` check ran BEFORE the erasure). NULL = never erased -> byte-identical
    to a pre-fix row; the DOCUMENT arm was already fail-closed by its segment_id recheck.

Chained off the current single head (c4d8e2a1f7b6, terminology T1b). Mirrors the SQLite
_ensure_columns() additive shim in app/db.py + the model (nullability-parity CI guard).
Hand-written (no autogenerate), same convention as d5a2f8c19e4b.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d4e1c9b2f6'
down_revision: Union[str, None] = 'c4d8e2a1f7b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.add_column(sa.Column('erased_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.drop_column('erased_at')
