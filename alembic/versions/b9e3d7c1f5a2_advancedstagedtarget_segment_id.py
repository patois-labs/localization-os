"""advancedstagedtarget segment_id + source_hash (Advanced Workflows P3 T2, SPEC §8)

Revision ID: b9e3d7c1f5a2
Revises: d5a2f8c19e4b
Create Date: 2026-09-04 16:00:00.000000

Adds two columns to the EXISTING advancedstagedtarget table so a conductor run can deliver a
SEGMENTED DOCUMENT project's segment values (P3 T2):

  - segment_id (INTEGER, nullable, INDEXED): a document unit's real identity, a SOFT reference
    (a plain nullable integer, NO ForeignKey and NO named constraint). app/db.py sets
    PRAGMA foreign_keys=ON, so a real FK would materialize an ENFORCED constraint and raise
    IntegrityError in the three classic segment-delete sites (retention.delete_document,
    content.delete_project step 4, the locale-removal Segment delete) -- the run's staged table
    must sit OUTSIDE every content cascade, exactly as unit_key references a StringKey by NAME
    with no FK. NULL = a keys unit or a pre-T2 row (byte-identical).
  - source_hash (VARCHAR, nullable): sha256 of the source the delegate translated, so _deliver
    re-checks the binding against the CURRENT segment (a recycled id or a human edit between drive
    and deliver skips fail-closed). NULL = a keys unit or a pre-T2 row.

A BARE FK-less column is deliberate: batch_alter_table rebuilds the table on SQLite and a real FK
would materialize an ENFORCED constraint the app/db.py ALTER TABLE ... ADD COLUMN boot shim can
never reproduce, so the two boot paths would diverge in exactly the FK-enforcement property that
decides whether the deletes above raise. The index is created EXPLICITLY (op.create_index) so all
three schema paths agree: create_all() from Field(index=True), this revision from op.create_index,
and the app/db.py _INDEXES shim (ix_advancedstagedtarget_segment_id). Chained off d5a2f8c19e4b
(the Advanced fix bundle), re-parented at the merge from the original a3f7c1e9d2b8 so LAW-P3-12's
single-head requirement stays satisfied. Hand-written (no autogenerate), same convention as
a3f7c1e9d2b8_workflowstep_concept_glossary.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9e3d7c1f5a2'
down_revision: Union[str, None] = 'd5a2f8c19e4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.add_column(sa.Column('segment_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('source_hash', sa.String(), nullable=True))
    op.create_index(
        'ix_advancedstagedtarget_segment_id', 'advancedstagedtarget', ['segment_id']
    )


def downgrade() -> None:
    op.drop_index('ix_advancedstagedtarget_segment_id', table_name='advancedstagedtarget')
    with op.batch_alter_table('advancedstagedtarget', schema=None) as batch_op:
        batch_op.drop_column('source_hash')
        batch_op.drop_column('segment_id')
