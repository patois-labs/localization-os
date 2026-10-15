"""tm: part 2 (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

from datetime import datetime

from sqlmodel import Session, select

from app.models import FullTextTM, SegmentTM
from app.services.tm_common import (
    _own_first,
    _project_content_type_id,
    _source_fallback_blocked,
    _tm_read_scope,
    hash_text,
    resolve_export_locales,
)


def _export_entries(session, project_id, source_locale, target_locale):
    """(effective_source, effective_target, [SegmentTM rows]) for an export -- the ONE
    query every export caller funnels through, so the TMX bytes, the /api + CLI `count`,
    and the effective-locale echo all describe the SAME rows (review H2: counting
    `<tu>` in the serialized bytes missed a meta-stamped `<tu creationtool=...>` -- i.e.
    every human_approved write_back row -- and reported 0 for a populated export)."""
    src, tgt = resolve_export_locales(session, project_id, source_locale, target_locale)
    entries = session.exec(
        select(SegmentTM).where(
            SegmentTM.project_id == project_id,
            SegmentTM.source_locale == src,
            SegmentTM.target_locale == tgt,
        )
    ).all()
    return src, tgt, entries


# --- Full-Text TM --------------------------------------------------------------
def ft_lookup(
    session: Session,
    project_id: int | None,
    source_text: str,
    source_locale: str,
    target_locale: str,
    *,
    accepted: bool = False,
) -> str | None:
    h = hash_text(source_text, source_locale, target_locale)
    row = session.exec(
        select(FullTextTM)
        .where(
            FullTextTM.source_hash == h,
            FullTextTM.source_locale == source_locale,
            FullTextTM.target_locale == target_locale,
            _tm_read_scope(FullTextTM, project_id, session),
            # OWN PROJECT FIRST, then lowest id -- the same rule the SegmentTM exact paths
            # use, and needed here for the same reason plus a sharper one: a FULL_TEXT hit
            # short-circuits the whole document (translate.py's cache check), so a borrowed
            # body would be served wholesale over the project's own corrected one, and a
            # re-approval (a NEW own row, higher id) could never displace it.
        )
        .order_by(_own_first(FullTextTM, project_id), FullTextTM.id)
    ).first()
    if row is not None and accepted:
        row.activity_at = datetime.utcnow()
        session.add(row)
    return row.target_text if row else None


def ft_store(
    session: Session,
    project_id: int | None,
    source_text: str,
    target_text: str,
    source_locale: str,
    target_locale: str,
    segment=None,
) -> FullTextTM | None:
    """Upsert a whole-document target into the full-text TM.

    `segment` (optional, GitHub #42) is the Segment the body came from; a
    `source_fallback` qa_flag REFUSES the write (see `_source_fallback_blocked`) — a
    FULL_TEXT body with a source-reverted paragraph must never seed shared TM. Passing
    nothing is byte-identical to the pre-#42 behaviour."""
    if _source_fallback_blocked(segment):
        return None
    # Issue #567: the approve-path integrity gate (#562) at the full-text writer too. A body
    # that drops or adds a placeholder, or whose inline tokens diverge, would be re-served as
    # a 100% hit; machine output has no operator to acknowledge, so it is simply not stored.
    from app.services import quality

    if quality.placeholder_violation(
        source_text, target_text, segment=segment
    ) or quality.tag_damage(source_text, target_text):
        return None
    h = hash_text(source_text, source_locale, target_locale)
    existing = session.exec(
        select(FullTextTM).where(
            FullTextTM.source_hash == h,
            FullTextTM.source_locale == source_locale,
            FullTextTM.target_locale == target_locale,
            FullTextTM.project_id == project_id,
        )
    ).first()
    if existing:
        existing.activity_at = datetime.utcnow()
        existing.target_text = target_text
        session.add(existing)
        session.commit()
        return existing
    row = FullTextTM(
        project_id=project_id,
        source_locale=source_locale,
        target_locale=target_locale,
        source_hash=h,
        activity_at=datetime.utcnow(),
        source_text=source_text,
        target_text=target_text,
        # SPEC-content-types phase 3: stamp the project's content type on the new row
        # (insert only — an existing row is not retro-restamped, §4.5).
        content_type_id=_project_content_type_id(session, project_id),
    )
    session.add(row)
    session.commit()
    return row
