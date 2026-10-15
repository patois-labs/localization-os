"""keys: part 2 (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import (
    KeyEntity,
    KeyOp,
    Segment,
    StringKey,
)
from app.services.keys_common import content_hash


def _enrich_value_entries(session: Session, entries: list[dict]) -> None:
    """Attach `text` + `content_hash` to VALUE entries that name a live segment (AP-1),
    IN PLACE. `key` entries and DELETED tombstones are left byte-identical (a tombstone
    keeps its shape; a metadata/key op carries no per-locale text). The text/hash
    reflect the CURRENT segment for (key_id, target_locale, plural_category) at read
    time -- not the state at the logged cursor -- so a consumer that reads the newest
    entry for a key always gets its newest value, and a value that has since been
    removed simply carries no text/hash (a later tombstone entry covers the removal).
    One batched query, so enrichment never N+1s the feed it exists to de-N+1.

    A CURRENTLY SOFT-DELETED key is NOT enriched (gate L4): its value rows survive in
    the trash document so `restore_key` is lossless, but a historical `updated` entry
    from before the delete must not leak that parked text. Such an entry keeps the base
    tombstone-less shape (no text/hash); the key's own `deleted` tombstone entry, which
    a since-cursor consumer also receives, is the authoritative signal."""
    keyed: dict[tuple, list[dict]] = {}
    for e in entries:
        if e.get("entity") != KeyEntity.VALUE or e.get("op") == KeyOp.DELETED:
            continue
        k = (e["key_id"], e.get("target_locale"), e.get("plural_category") or "")
        keyed.setdefault(k, []).append(e)
    if not keyed:
        return
    key_ids = {k[0] for k in keyed}
    # L4: which of these keys are currently soft-deleted -- skip enriching their entries.
    deleted_keys = set(
        session.exec(
            select(StringKey.id).where(StringKey.id.in_(key_ids), StringKey.deleted_at.is_not(None))
        ).all()
    )
    rows = session.exec(select(Segment).where(Segment.key_id.in_(key_ids))).all()
    by_tuple: dict[tuple, Segment] = {}
    for seg in rows:
        by_tuple[(seg.key_id, seg.target_locale, seg.plural_category or "")] = seg
    for k, es in keyed.items():
        if k[0] in deleted_keys:
            continue
        seg = by_tuple.get(k)
        if seg is None:
            continue
        text = seg.target_text or ""
        h = content_hash(text)
        for e in es:
            e["text"] = text
            e["content_hash"] = h
