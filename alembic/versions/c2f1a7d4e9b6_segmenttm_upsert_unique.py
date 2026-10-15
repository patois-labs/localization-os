"""segmenttm upsert uniqueness — UNIQUE(project_id, source_hash, source_locale, target_locale)

Revision ID: c2f1a7d4e9b6
Revises: d4e5f6a7b8c9
Create Date: 2026-08-11 12:00:00.000000

QA Part-2 finding P-C2 (laneCD C2-M1). `tm.write_back` (app/services/tm.py) upserts an
approved segment into SegmentTM with a read-check-then-INSERT (SELECT .first() -> if None
INSERT else UPDATE) and NOTHING backed the uniqueness: SegmentTM had only the NON-unique
ix_segmenttm_lookup. Two writers that both pass the SELECT before either commits both
INSERT -> duplicate rows for one source. The GIL serialized this away in-process (held at
96 concurrent), but a forced read->insert window produced 2 duplicate rows, and the window
opens for real under MULTI-PROCESS writers (the long-lived server AND a CLI approving the
same source) or a slow disk. Duplicate rows then make tm.lookup nondeterministic (two rows
for one source, arbitrary winner).

This revision closes it structurally:

  ux_segmenttm_upsert_key  — UNIQUE (project_id, source_hash, source_locale, target_locale),
                             the exact tuple write_back's read-check keys on (and the tuple
                             import_tmx's existing_by_hash preload is scoped by). source_hash
                             already encodes source_locale+target_locale+normalize(source),
                             so the two locale columns are redundant but kept to mirror both
                             read-checks byte-for-byte. content_type_id is intentionally NOT
                             in the key: neither write path ever creates two rows for one
                             (project, source) differing only by content type (a re-write
                             finds the existing row and UPDATEs it, no retro-restamp -- §4.5),
                             and the content-type "general bucket" rule (_content_type_scope)
                             operates ACROSS project_ids, which a per-project unique index
                             permits. So the index enforces an invariant the code already
                             assumes; it never merges legitimately-distinct rows.

  The app-side upsert is made IntegrityError-tolerant in the same change (tm.write_back
  catches the constraint violation, rolls back, re-reads the winner and takes the UPDATE
  path) so the loser of a real race converges to last-writer-wins instead of duplicating.

DEDUP PREFLIGHT (idempotent, runs before the index add): collapse any EXISTING duplicate
groups that would block the CREATE UNIQUE INDEX -- keep the NEWEST row (MAX(id) == last
write == what a correct upsert would have left) per group, delete the older rows, and
delete the losing rows' SegmentEmbedding vectors FIRST (SegmentEmbedding.segment_tm_id ->
segmenttm.id; children before parent, the retention._delete_project_tm ordering law). Scoped
to project_id IS NOT NULL: those are the ONLY groups a standard UNIQUE index would reject
(SQL keeps NULLs DISTINCT, so NULL-project/global rows never block the index and are left
untouched -- write_back is always called with a real project_id). SQLite- and
Postgres-portable SQL (plain GROUP BY / NOT IN over a PK-only subquery, no NULLs in the
NOT IN list). The SAME index rides app/db._UNIQUE_INDEXES for the create_all / legacy-shim
SQLite path (which SKIPS-with-warning on pre-existing dups, the segment/segmentembedding
house style) so a non-alembic boot builds the identical named index.

downgrade drops only the index -- the dedup DELETEs are data and are not reversed (standard
for a data migration; the collapsed duplicates were redundant by construction).

BLAST RADIUS NOTE: the index binds EVERY SegmentTM inserter, not just write_back. A
single-process import_tmx cannot hit it (its existing_by_hash preload + in-batch pending_rows
dedup by hash, so a repeated source is always an UPDATE, never a second INSERT). A rare
CROSS-process import racing another writer on the same key now rolls the whole import back
atomically (its `except BaseException` savepoint) instead of silently duplicating -- more
correct, and safe (segments stay untouched), but a new, honest failure mode worth knowing.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c2f1a7d4e9b6'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The winners-per-group subquery, reused by both DELETEs (embeddings then the rows).
# MAX(id) keeps the newest row of each duplicate group; project_id IS NOT NULL scopes to
# exactly the groups the UNIQUE index would reject.
_LOSERS = (
    "SELECT id FROM segmenttm WHERE project_id IS NOT NULL AND id NOT IN ("
    "  SELECT MAX(id) FROM segmenttm WHERE project_id IS NOT NULL"
    "  GROUP BY project_id, source_hash, source_locale, target_locale)"
)


def upgrade() -> None:
    # 1) Child embeddings of the losing (non-newest) duplicate rows FIRST (FK order:
    #    SegmentEmbedding.segment_tm_id -> segmenttm.id).
    op.execute(
        "DELETE FROM segmentembedding WHERE segment_tm_id IN (%s)" % _LOSERS)
    # 2) The losing duplicate rows themselves (keep MAX(id) per group).
    op.execute(
        "DELETE FROM segmenttm WHERE project_id IS NOT NULL AND id NOT IN ("
        "  SELECT MAX(id) FROM segmenttm WHERE project_id IS NOT NULL"
        "  GROUP BY project_id, source_hash, source_locale, target_locale)")
    # 3) Now the table has at most one row per key -- add the UNIQUE index.
    op.create_index(
        'ux_segmenttm_upsert_key', 'segmenttm',
        ['project_id', 'source_hash', 'source_locale', 'target_locale'],
        unique=True)


def downgrade() -> None:
    op.drop_index('ux_segmenttm_upsert_key', table_name='segmenttm')
