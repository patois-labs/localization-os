"""wave2.qa_pe — LQAResult attribution columns + SegmentPostEdit episode table

Revision ID: e7a1b2c3d4e5
Revises: b3e8f1c92d47
Create Date: 2026-08-19 09:00:00.000000

SPEC-wave2-qa-pe-reporting Train A. Two additive changes, both purely forward:

  1. SIX NULLABLE columns on the EXISTING `lqaresult` table (§3.1):
       target_locale, finding_source, judge, scoring_id  (VARCHAR)
       vendor_id, dispatch_id                             (INTEGER)
     All NULLABLE, NO server default — NULL is the honest legacy value ("written before
     Wave 2") and every report buckets it as `(untracked ...)`. Plain op.add_column emits
     a native ALTER ... ADD COLUMN on SQLite + Postgres (no table rebuild, so lqaresult's
     indexes survive). The SAME columns are added on the non-alembic SQLite init_db path
     via app/db._ADDED_COLUMNS["lqaresult"] so all three build paths agree — the
     nullability-parity CI guard (verify_alembic_authority) compares them.

     FK ASYMMETRY (accepted, §2.8): vendor_id/dispatch_id are declared foreign_key= on the
     MODEL, so a fresh create_all() gives them a REFERENCES clause, but SQLite cannot add a
     REFERENCES clause to an existing table without a full rebuild — so this ALTER path and
     the _ADDED_COLUMNS shim add a bare INTEGER. Pre-existing across the schema (e.g.
     project.org_id); verify_alembic_authority compares column-set + nullability only and
     will not see it. Do NOT "fix" it with a batch rebuild — the no-rebuild convention is
     load-bearing (it preserves lqaresult's indexes).

  2. A NEW TABLE `segmentpostedit` (§3.2.3) — episode-scoped post-edit capture, one row
     per (segment, editing episode), UNIQUE on (segment_id, episode_seq). On the
     non-alembic SQLite path create_all() makes it (a NEW TABLE needs no _ADDED_COLUMNS
     entry — the LLMemory / SegmentEmbedding precedent).

Hand-written (no autogenerate) off the head b3e8f1c92d47, same additive convention as the
sibling revisions. DIALECT-BRANCHED downgrade (native SQLite DROP COLUMN vs op.drop_column),
matching alembic/versions/d4e5f6a7b8c9's shape.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a1b2c3d4e5'
down_revision: Union[str, None] = 'b3e8f1c92d47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The six LQAResult attribution columns (name, type). Bare types (no FK) — see the
# module docstring's FK-asymmetry note.
_LQARESULT_COLS = [
    ('target_locale', sa.String()),
    ('finding_source', sa.String()),
    ('judge', sa.String()),
    ('scoring_id', sa.String()),
    ('vendor_id', sa.Integer()),
    ('dispatch_id', sa.Integer()),
]


def upgrade() -> None:
    # 1. the new episode table. NOT NULL on the value columns (the table is empty at
    #    creation, so no server default is needed); nullable on the metric columns and
    #    the vendor/dispatch FKs (NULL = un-attributed).
    op.create_table(
        'segmentpostedit',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('segment_id', sa.Integer(), sa.ForeignKey('segment.id'), nullable=False),
        sa.Column('document_id', sa.Integer(), sa.ForeignKey('document.id'), nullable=False),
        sa.Column('target_locale', sa.String(), nullable=False),
        sa.Column('episode_seq', sa.Integer(), nullable=False),
        sa.Column('unit_kind', sa.String(), nullable=False),
        sa.Column('final_sha', sa.String(), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('actor_kind', sa.String(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), sa.ForeignKey('vendor.id'), nullable=True),
        sa.Column('dispatch_id', sa.Integer(), sa.ForeignKey('vendordispatch.id'),
                  nullable=True),
        sa.Column('baseline_text', sa.String(), nullable=False),
        sa.Column('baseline_kind', sa.String(), nullable=False),
        sa.Column('baseline_chars', sa.Integer(), nullable=False),
        sa.Column('final_chars', sa.Integer(), nullable=False),
        sa.Column('source_words', sa.Integer(), nullable=False),
        sa.Column('distance_char', sa.Integer(), nullable=True),
        sa.Column('distance_word', sa.Integer(), nullable=True),
        sa.Column('distance_pct', sa.Float(), nullable=True),
        sa.Column('cum_distance_char', sa.Integer(), nullable=True),
        sa.Column('cum_distance_word', sa.Integer(), nullable=True),
        sa.Column('cum_distance_pct', sa.Float(), nullable=True),
        sa.Column('metric_version', sa.String(), nullable=False),
        sa.Column('edit_count', sa.Integer(), nullable=False),
        sa.Column('first_edited_at', sa.DateTime(), nullable=False),
        sa.Column('last_edited_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('segment_id', 'episode_seq', name='ux_segment_post_edit_seq'),
    )
    op.create_index('ix_segmentpostedit_segment_id', 'segmentpostedit', ['segment_id'])
    op.create_index('ix_segmentpostedit_document_id', 'segmentpostedit', ['document_id'])
    op.create_index('ix_segmentpostedit_last_edited_at', 'segmentpostedit', ['last_edited_at'])

    # 2. the six additive nullable columns on lqaresult (native ALTER ADD COLUMN).
    for name, type_ in _LQARESULT_COLS:
        op.add_column('lqaresult', sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'
    # Drop the lqaresult columns first (reverse of add). Dialect-branched: native SQLite
    # DROP COLUMN (>= 3.35) rather than a batch rebuild, matching d4e5f6a7b8c9.
    for name, _type in reversed(_LQARESULT_COLS):
        if is_sqlite:
            op.execute('ALTER TABLE lqaresult DROP COLUMN %s' % name)
        else:
            op.drop_column('lqaresult', name)
    # Drop the episode table (its indexes + unique constraint go with it).
    op.drop_table('segmentpostedit')
