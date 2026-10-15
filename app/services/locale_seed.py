"""Seed a new target locale on an already-ingested document (#576).

Documented in: docs/architecture/translation-pipeline.md

Two callers shared this logic by copy: the translate-time seeder (app/web/api_discovery.py
`_ensure_doc_locale_segments`) and the add-a-language seeder (content_project_rename_delete_sp.py
`_seed_locale_segments`). Both clone the source-locale rows (source_text, order_index,
inline_tags) into the new locale. They now call `seed_locale` here.

Two guards from #571 reach this path:
  * the total row cap: a document that would pass MAX_DOCUMENT_ROWS after the clone is not
    seeded. `strict=True` (the translate door) raises InputLimitError so the caller answers
    413 before any engine call; `strict=False` (the add-a-language door) logs and skips, so
    adding a language to a project never fails half-way; the locale is seeded, or refused
    with the same message, the moment someone starts a translation for it;
  * chunked, resumable writes: a document of more than SEED_CHUNK source rows is cloned in
    committed chunks (the write lock is held for milliseconds). "Has any row for the locale"
    still means "seeded", so a big clone first records a marker on Document.skeleton
    (`_seeding`), writes the chunks, then clears it. A crash part-way leaves the marker, and
    the next call finds the marker and writes only the missing rows. A small document is one
    atomic transaction, exactly as before.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import String
from sqlmodel import Session, func, select

from app import config
from app.filters.base import InputLimitError
from app.models import Document, Segment
from app.services import seed_caps

log = logging.getLogger("loc_tms")

MARKER = "_seeding"

# One seed per (document, locale) at a time in this process. Without it a second caller
# (translate clicked while an add-a-language clone is still running) sees the marker,
# takes it for a crashed run and "resumes" next to the live one: duplicate rows.
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[int, str], threading.Lock] = {}


def _lock_for(document_id: int, locale: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((document_id, locale), threading.Lock())


def in_progress(doc: Document, locale: str) -> bool:
    """True while `locale` is only partly cloned onto `doc` (a chunked seed is running, or
    it crashed and nobody has resumed it). Delivery surfaces refuse such a locale."""
    return _marker(doc, locale) is not None


def seeding_pairs(session: Session, doc_ids) -> list[tuple[int, str]]:
    """(document_id, locale) for every locale still being added (#583). A cheap LIKE on the
    skeleton text picks the few candidate documents; only those are loaded."""
    ids = list(doc_ids)
    out: list[tuple[int, str]] = []
    for i in range(0, len(ids), 150):
        batch = ids[i : i + 150]
        stmt = select(Document).where(
            Document.id.in_(batch), func.cast(Document.skeleton, String).like(f'%"{MARKER}"%')
        )
        for doc in session.exec(stmt).all():
            sk = doc.skeleton if isinstance(doc.skeleton, dict) else {}
            out.extend((doc.id, loc) for loc in (sk.get(MARKER) or {}))
    return out


def seeding_status_counts(session: Session, doc_ids) -> dict:
    """{Segment.status: n} for the rows of every still-being-added locale, so a count
    surface can subtract them (#583: a half-cloned locale is not counted)."""
    out: dict = {}
    for doc_id, loc in seeding_pairs(session, doc_ids):
        stmt = (
            select(Segment.status, func.count())
            .where(Segment.document_id == doc_id, Segment.target_locale == loc)
            .group_by(Segment.status)
        )
        for key, n in session.exec(stmt).all():
            out[key] = out.get(key, 0) + int(n)
    return out


def _note_skipped(session: Session, doc: Document, msg: str) -> None:
    """Tell the user: a skipped language is shown as a notice on the document page and in
    `doc show` (the existing `file_warnings` channel), not only in the server log."""
    sk = dict(doc.skeleton) if isinstance(doc.skeleton, dict) else {}
    warnings = list(sk.get("file_warnings") or [])
    if any(isinstance(w, dict) and w.get("message") == msg for w in warnings):
        return
    warnings.append({"message": msg, "part": "add language"})
    sk["file_warnings"] = warnings
    doc.skeleton = sk
    session.add(doc)
    session.commit()


def _marker(doc: Document, locale: str):
    sk = doc.skeleton if isinstance(doc.skeleton, dict) else {}
    return (sk.get(MARKER) or {}).get(locale)


def _set_marker(session: Session, doc: Document, locale: str, expected: int | None) -> None:
    """Record (expected int) or clear (None) the in-progress marker. The dict is rebuilt, so
    SQLAlchemy sees the JSON column change."""
    sk = dict(doc.skeleton) if isinstance(doc.skeleton, dict) else {}
    marks = dict(sk.get(MARKER) or {})
    if expected is None:
        marks.pop(locale, None)
    else:
        marks[locale] = expected
    if marks:
        sk[MARKER] = marks
    else:
        sk.pop(MARKER, None)
    doc.skeleton = sk or None
    session.add(doc)
    session.commit()


def seed_locale(session: Session, document_id: int, locale: str, *, strict: bool) -> int:
    """Clone the document's source rows into `locale` if it has none yet (or finish an
    interrupted clone). Returns the number of rows written. One seed per (document,
    locale) runs at a time; a second caller waits, then finds the work done."""
    with _lock_for(document_id, locale):
        return _seed_locale(session, document_id, locale, strict=strict)


def _seed_locale(session: Session, document_id: int, locale: str, *, strict: bool) -> int:
    doc = session.get(Document, document_id)
    if doc is None:
        return 0
    session.refresh(doc)  # the marker may have been written or cleared while we waited
    # #687: a bilingual vendor file is never seeded for a language it does not declare
    # (its export would put that language under the file's own target-language).
    from app.services import bilingual_target as bt

    if not bt.allows(doc.skeleton, locale):
        msg = bt.locale_refusal(doc.skeleton[bt.SKELETON_KEY], locale)
        if strict:
            raise bt.FileTargetMismatch(msg)
        _note_skipped(session, doc, msg)
        return 0
    have = session.exec(
        select(Segment.id).where(
            Segment.document_id == document_id, Segment.target_locale == locale
        )
    ).first()
    resuming = bool(have) and _marker(doc, locale) is not None
    if have and not resuming:
        return 0
    first = session.exec(
        select(Segment.target_locale)
        .where(Segment.document_id == document_id)
        .order_by(Segment.order_index, Segment.id)
        .limit(1)
    ).first()
    if first is None:
        return 0
    src_locale = first
    n_src = session.exec(
        select(func.count())
        .select_from(Segment)
        .where(Segment.document_id == document_id, Segment.target_locale == src_locale)
    ).one()
    if not resuming:
        total = session.exec(
            select(func.count()).select_from(Segment).where(Segment.document_id == document_id)
        ).one()
        if total + n_src > config.MAX_DOCUMENT_ROWS:
            msg = (
                f"Adding {locale} would make this document {total + n_src} segment rows "
                f"({total} now, {n_src} more), over the limit of {config.MAX_DOCUMENT_ROWS} "
                "(MAX_DOCUMENT_ROWS). Nothing was seeded and no translation was started. "
                "Remove a target language or split the document."
            )
            if strict:
                raise InputLimitError(msg)
            log.warning("document %s: %s", document_id, msg)
            _note_skipped(session, doc, msg)
            return 0
    rows_q = (
        select(Segment)
        .where(Segment.document_id == document_id, Segment.target_locale == src_locale)
        .order_by(Segment.order_index, Segment.id)
    )
    if n_src <= seed_caps.SEED_CHUNK and not resuming:
        # Small document: one atomic transaction, as before.
        written = 0
        for sg in session.exec(rows_q).all():
            session.add(_clone(document_id, locale, sg))
            written += 1
        session.commit()
        return written
    # Large document: marker, committed chunks, clear the marker.
    if not resuming:
        _set_marker(session, doc, locale, n_src)
    written = _page_clone(session, document_id, locale, src_locale)
    _set_marker(session, doc, locale, None)
    return written


def _clone(document_id: int, locale: str, sg: Segment) -> Segment:
    return Segment(
        document_id=document_id,
        target_locale=locale,
        order_index=sg.order_index,
        source_text=sg.source_text,
        inline_tags=sg.inline_tags,
    )


def _page_clone(session: Session, document_id: int, locale: str, src_locale: str) -> int:
    """Keyset-page the source rows (order_index, id) and write each page as one committed
    chunk. Returns rows written. Rows whose order_index the new locale already has (a resumed
    clone) are skipped, checked per page."""
    written = 0
    last = None
    while True:
        q = select(Segment).where(
            Segment.document_id == document_id, Segment.target_locale == src_locale
        )
        if last is not None:
            q = q.where(
                (Segment.order_index > last[0])
                | ((Segment.order_index == last[0]) & (Segment.id > last[1]))
            )
        page = session.exec(
            q.order_by(Segment.order_index, Segment.id).limit(seed_caps.SEED_CHUNK)
        ).all()
        if not page:
            break
        last = (page[-1].order_index, page[-1].id)
        have_idx = set(
            session.exec(
                select(Segment.order_index).where(
                    Segment.document_id == document_id,
                    Segment.target_locale == locale,
                    Segment.order_index >= page[0].order_index,
                    Segment.order_index <= page[-1].order_index,
                )
            ).all()
        )
        batch = [_clone(document_id, locale, sg) for sg in page if sg.order_index not in have_idx]
        # Same helper as every other seeder: add, commit, yield the write lock briefly.
        written += seed_caps.add_in_chunks(session, batch, chunk=seed_caps.SEED_CHUNK)
        if len(page) < seed_caps.SEED_CHUNK:
            break
    return written
