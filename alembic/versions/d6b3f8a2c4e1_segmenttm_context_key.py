"""segmenttm.context_key: the in-context (101 %) key of a TM unit (ADR 0102)

Revision ID: d6b3f8a2c4e1
Revises: d7b3f1a9c2e4
Create Date: 2026-10-24 12:00:00.000000

One additive nullable column (no new table, no new index), and three dead WorkflowStep columns
dropped (tm_fill_threshold, tm_fuzzy_lock_mode, tm_context_floor; fuzzy fill removed, ADR 0102):

  segmenttm.context_key   VARCHAR NULL: "ctx:" + sha256(normalize(previous source) + US +
                          normalize(next source)) for a document segment, or "key:" + sha256(the
                          string key's full name) for a key (software) segment. NULL = no context
                          known (the unit can only be a 100 % match). Holds the context of the
                          LAST write for the unit's (project, source hash, locale pair) row.

The upgrade also runs a ONE-TIME BACKFILL (plain SQL plus hashlib; migrations never import app
code, so the key recipe below is a copy of tm_common.context_key / key_context_key and
tm_context.key_full_name, pinned by tests/verify_tm_exact_only.py). For every APPROVED segment
(the only state tm.write_back is called for), newest first, it finds the segmenttm row of the
same project, same locale pair, same source hash AND the same target text whose context_key is
still NULL, and sets the key from the segment's neighbours (previous and next source by
order_index in the same document and target locale) or, for a key segment, from its key name.
Rows with no such segment stay NULL. The pass is chunked (CHUNK segments at a time, so a
2,000,000-row TM never loads at once), idempotent (it only ever fills NULLs, so a second run
changes nothing), and a no-op on an install with no segments or no TM. Core constructs only, so it
runs on SQLite and Postgres.

Mirrors models.SegmentTM.context_key and the app/db.py _ADDED_COLUMNS shim (the shim path adds
the column but cannot backfill; a legacy install that boots through it simply starts with NULLs,
which only means "100 % until the next approval").

Downgrade: native ALTER TABLE ... DROP COLUMN on SQLite (a batch rebuild would drop expression
indexes; the 2a98273c3c99 precedent).
"""
from typing import Sequence, Union

import bisect
import hashlib

import sqlalchemy as sa
from alembic import op

revision: str = "d6b3f8a2c4e1"
down_revision: Union[str, None] = "d7b3f1a9c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHUNK = 2000
_US = "\x1f"
_APPROVED = "approved"


def _norm(text) -> str:
    return " ".join((text or "").split())


def _source_hash(text, source_locale, target_locale) -> str:
    key = f"{source_locale}{_US}{target_locale}{_US}{_norm(text)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _ctx_key(prev_source, next_source) -> str:
    raw = f"{_norm(prev_source)}{_US}{_norm(next_source)}"
    return "ctx:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key_ctx_key(namespace, name) -> str:
    full = f"{namespace or ''}{_US}{name or ''}"
    return "key:" + hashlib.sha256(full.encode("utf-8")).hexdigest()


def backfill_context_keys(conn, chunk: int = CHUNK) -> int:
    """Fill segmenttm.context_key from approved segments (see the module docstring).
    Returns the number of rows set. Safe to call on any schema that has the column."""
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    if not {"segmenttm", "segment", "document", "project"} <= tables:
        return 0
    seg = sa.table(
        "segment",
        sa.column("id"),
        sa.column("document_id"),
        sa.column("target_locale"),
        sa.column("order_index"),
        sa.column("source_text"),
        sa.column("target_text"),
        sa.column("status"),
        sa.column("key_id"),
    )
    doc = sa.table("document", sa.column("id"), sa.column("project_id"))
    proj = sa.table("project", sa.column("id"), sa.column("source_locale"))
    tmt = sa.table(
        "segmenttm",
        sa.column("id"),
        sa.column("project_id"),
        sa.column("source_locale"),
        sa.column("target_locale"),
        sa.column("source_hash"),
        sa.column("target_text"),
        sa.column("context_key"),
    )
    has_keys = "stringkey" in tables
    skey = sa.table("stringkey", sa.column("id"), sa.column("namespace"), sa.column("name"))

    # Nothing to do on an install with no TM rows lacking a key.
    if conn.execute(sa.select(tmt.c.id).where(tmt.c.context_key.is_(None)).limit(1)).first() is None:
        return 0

    def _neighbours(pairs):
        """{(document_id, locale): (order_indexes, sources)} sorted by order_index: ONE read per
        document and target locale (not two per segment)."""
        out: dict[tuple, tuple] = {}
        for doc_id, locale in pairs:
            rows = conn.execute(
                sa.select(seg.c.order_index, seg.c.source_text)
                .where(seg.c.document_id == doc_id, seg.c.target_locale == locale)
                .order_by(seg.c.order_index)
            ).all()
            out[(doc_id, locale)] = ([r[0] for r in rows], [r[1] for r in rows])
        return out

    def _keys(key_ids):
        """{stringkey id: key context key}, batched."""
        ids = sorted(set(key_ids))
        out: dict = {}
        for i in range(0, len(ids), 400):
            for kid, ns, name in conn.execute(
                sa.select(skey.c.id, skey.c.namespace, skey.c.name).where(
                    skey.c.id.in_(ids[i : i + 400])
                )
            ):
                out[kid] = _key_ctx_key(ns, name)
        return out

    total = 0
    last_id = None
    while True:
        q = (
            sa.select(
                seg.c.id,
                seg.c.document_id,
                seg.c.target_locale,
                seg.c.order_index,
                seg.c.source_text,
                seg.c.target_text,
                seg.c.key_id,
                doc.c.project_id,
                proj.c.source_locale,
            )
            .select_from(
                seg.join(doc, doc.c.id == seg.c.document_id).join(
                    proj, proj.c.id == doc.c.project_id
                )
            )
            .where(seg.c.status == _APPROVED, seg.c.target_text != "")
            .order_by(seg.c.id.desc())
            .limit(chunk)
        )
        if last_id is not None:
            q = q.where(seg.c.id < last_id)
        rows = conn.execute(q).all()
        if not rows:
            break
        last_id = rows[-1][0]

        wanted: dict[tuple, list] = {}
        for r in rows:
            h = _source_hash(r[4], r[8], r[2])
            wanted.setdefault((r[7], r[2], h), []).append(r)
        hashes = sorted({k[2] for k in wanted})
        projects = sorted({k[0] for k in wanted})
        stored: dict[tuple, tuple] = {}
        for i in range(0, len(hashes), 400):
            for tid, pid, tl, h, tgt in conn.execute(
                sa.select(
                    tmt.c.id, tmt.c.project_id, tmt.c.target_locale, tmt.c.source_hash,
                    tmt.c.target_text,
                ).where(
                    tmt.c.source_hash.in_(hashes[i : i + 400]),
                    tmt.c.project_id.in_(projects),
                    tmt.c.context_key.is_(None),
                )
            ):
                stored[(pid, tl, h)] = (tid, tgt)
        # Pick, per stored row, the newest approved segment holding the same target; then read
        # the neighbours of those segments (one read per document and locale) and their key
        # names (batched) for the whole chunk at once.
        picked: list[tuple] = []  # (tm row id, segment row)
        for k, segs in wanted.items():
            hit = stored.get(k)
            if hit is None:
                continue
            for r in segs:  # newest segment first: the first match wins
                if hit[1] == r[5]:
                    picked.append((hit[0], r))
                    break
        keyed = has_keys and any(r[6] is not None for _tid, r in picked)
        key_ctx = _keys(r[6] for _tid, r in picked if r[6] is not None) if keyed else {}
        nb = _neighbours({(r[1], r[2]) for _tid, r in picked if r[6] is None or not has_keys})
        updates: list[dict] = []
        for tid, r in picked:
            if r[6] is not None and has_keys:
                ck = key_ctx.get(r[6])
                if ck is None:  # the key row is gone: leave the unit context-free
                    continue
            else:
                orders, sources = nb[(r[1], r[2])]
                lo = bisect.bisect_left(orders, r[3])  # first index with order_index >= this one
                hi = bisect.bisect_right(orders, r[3])  # first index with order_index > this one
                ck = _ctx_key(sources[lo - 1] if lo > 0 else "", sources[hi] if hi < len(orders) else "")
            updates.append({"b_id": tid, "b_ck": ck})
        if updates:
            conn.execute(
                tmt.update()
                .where(tmt.c.id == sa.bindparam("b_id"), tmt.c.context_key.is_(None))
                .values(context_key=sa.bindparam("b_ck")),
                updates,
            )
            total += len(updates)
        if len(rows) < chunk:
            break
    return total


# ADR 0102: fuzzy TM fill was removed, so these three per-step overrides are dead. Dropped with a
# native DROP COLUMN (no batch rebuild, so no index is lost); each guarded so an install that never
# had one (or a pre-alembic shim install) upgrades cleanly. The downgrade re-adds them as NULL.
_DEAD_STEP_COLUMNS = (
    ("tm_fill_threshold", sa.Integer()),
    ("tm_fuzzy_lock_mode", sa.String()),
    ("tm_context_floor", sa.Integer()),
)


def upgrade() -> None:
    op.add_column("segmenttm", sa.Column("context_key", sa.String(), nullable=True))
    backfill_context_keys(op.get_bind())
    bind = op.get_bind()
    have = {c["name"] for c in sa.inspect(bind).get_columns("workflowstep")}
    # Literal statements (not a loop over the tuple): verify_migrations scans upgrade bodies for
    # "ALTER TABLE <t> DROP COLUMN <c>" to know which shim-registered columns were retired.
    if "tm_fill_threshold" in have:
        op.execute("ALTER TABLE workflowstep DROP COLUMN tm_fill_threshold")
    if "tm_fuzzy_lock_mode" in have:
        op.execute("ALTER TABLE workflowstep DROP COLUMN tm_fuzzy_lock_mode")
    if "tm_context_floor" in have:
        op.execute("ALTER TABLE workflowstep DROP COLUMN tm_context_floor")


def downgrade() -> None:
    bind = op.get_bind()
    have = {c["name"] for c in sa.inspect(bind).get_columns("workflowstep")}
    for name, type_ in _DEAD_STEP_COLUMNS:
        if name not in have:
            op.add_column("workflowstep", sa.Column(name, type_, nullable=True))
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE segmenttm DROP COLUMN context_key")
    else:
        op.drop_column("segmenttm", "context_key")
