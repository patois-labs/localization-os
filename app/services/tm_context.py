"""TM in-context (101 %) keys for live segments (ADR 0102).

A TM unit carries the context key of its last write (SegmentTM.context_key); a lookup carries
the context key of the segment being filled; equal keys promote a 100 % hit to 101 %. The pure
recipes live in tm_common (`context_key`, `key_context_key`); this module turns Segment rows
into those keys:

  * a document segment: the previous and next SOURCE segment of the same document and target
    locale, by order_index (a missing neighbour at a document edge is the empty string);
  * a key (software) segment, `Segment.key_id` set: the string key's full name
    (`key_full_name`: namespace and name).

The alembic revision d6b3f8a2c4e1 holds a copy of the recipes for its one-time backfill (a
migration never imports app code); tests/verify_tm_exact_only.py pins that the copy and these
functions agree.

Documented in: docs/architecture/tm-and-glossary.md
"""

from sqlmodel import Session, select

from app.models import Segment, StringKey
from app.services.tm_common import context_key, key_context_key

_US = "\x1f"


def key_full_name(namespace: str | None, name: str | None) -> str:
    """The text a key context key hashes: namespace, U+001F, name (so ns "a" + name "b" can
    never collide with ns "" + name "a::b")."""
    return f"{namespace or ''}{_US}{name or ''}"


def _key_keys(session: Session, key_ids) -> dict[int, str]:
    """{key_id: key context key} for the given StringKey ids (one query per 500)."""
    ids = sorted({k for k in key_ids if k is not None})
    out: dict[int, str] = {}
    for i in range(0, len(ids), 500):
        for k in session.exec(select(StringKey).where(StringKey.id.in_(ids[i : i + 500]))).all():
            out[k.id] = key_context_key(key_full_name(k.namespace, k.name))
    return out


def key_context_keys(session: Session, key_ids) -> dict[int, str]:
    """{key_id: key context key} for many StringKey ids at once (bulk read for
    `segment_context_keys(..., keyed=...)`)."""
    return _key_keys(session, key_ids)


def segment_context_keys(session: Session, segments, keyed=None) -> list[str | None]:
    """Per-segment context keys for an ORDERED list of segment-like objects (attributes
    `source_text` and optionally `key_id`) of ONE document and ONE target locale.

    The neighbours are the list's own previous and next items, so the caller passes the
    document's segments in order_index order (the gaps a filtered list would leave are the
    caller's to avoid: pass the whole document, then pick). A key segment (key_id set) gets
    its string-key key. A key id that no longer resolves gives None (100 % only).

    `keyed` ({key_id: key context key}, from `key_context_keys`) lets a caller that handles
    many documents resolve all their string keys in one read instead of one per call."""
    segs = list(segments)
    if keyed is None:
        keyed = _key_keys(session, (getattr(s, "key_id", None) for s in segs))
    out: list[str | None] = []
    for i, s in enumerate(segs):
        kid = getattr(s, "key_id", None)
        if kid is not None:
            out.append(keyed.get(kid))
            continue
        prev_src = segs[i - 1].source_text if i > 0 else ""
        next_src = segs[i + 1].source_text if i + 1 < len(segs) else ""
        out.append(context_key(prev_src, next_src))
    return out


def context_key_for_segment(session: Session, seg) -> str | None:
    """The context key of one Segment row, reading its neighbours from the database (the
    approve write-back path). None when the segment is not a Segment row we can place."""
    kid = getattr(seg, "key_id", None)
    if kid is not None:
        return _key_keys(session, [kid]).get(kid)
    doc_id = getattr(seg, "document_id", None)
    order = getattr(seg, "order_index", None)
    locale = getattr(seg, "target_locale", None)
    if doc_id is None or order is None:
        return None
    base = select(Segment.source_text).where(
        Segment.document_id == doc_id, Segment.target_locale == locale
    )
    prev_row = session.exec(
        base.where(Segment.order_index < order).order_by(Segment.order_index.desc()).limit(1)
    ).first()
    next_row = session.exec(
        base.where(Segment.order_index > order).order_by(Segment.order_index.asc()).limit(1)
    ).first()
    return context_key(prev_row or "", next_row or "")


def document_context_keys(
    session: Session, document_id: int, target_locale: str
) -> dict[int, str | None]:
    """{segment id: context key} for every segment of a document in one target locale."""
    segs = session.exec(
        select(Segment)
        .where(Segment.document_id == document_id, Segment.target_locale == target_locale)
        .order_by(Segment.order_index)
    ).all()
    return dict(zip((s.id for s in segs), segment_context_keys(session, segs), strict=True))
