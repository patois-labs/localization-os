"""Turn an uploaded file into a Document + Segments.

Segmented mode  -> one Segment per sentence (TM-aligned).
Full-text mode  -> a single Segment holding the whole document body (sent to the
                   LLM as one unit; no parse/concat/reparse cycle).

Either way we record a chunk plan (how the orchestrator would split the doc to
stay under a provider's request budget) and stash the original bytes so we can
rebuild a format-preserving file on export.

Documented in: docs/architecture/translation-pipeline.md
"""

import time
from uuid import uuid4

from sqlmodel import Session, select

from app import config
from app.db import engine
from app.filters import get_filter
from app.filters.base import (
    EmptyDocumentError,
    InputLimitError,
    check_segment_cap,
    check_unit_texts,
)
from app.models import DocStatus, Document, Segment, TargetLocale, TransMode
from app.services.segment import (
    LEGACY_ENGINE_V2,
    est_tokens,
    extract_inline_tags,
    plan_chunks,
    split_sentences,
    split_with_ruleset,
)

# Segment rows written per commit (see ingest_document, #571).
_INSERT_CHUNK = 1000


class UnsupportedFormat(Exception):
    pass


class NotADocumentProject(Exception):
    """A file was uploaded into a KEY-BASED project.

    SPEC-key-based-projects 2.1/2.5 (Wave B fix round, A-F4): the two project types
    are disjoint and the type is immutable, so a document inside a key project is a
    state the product has no answer for. Neither the web upload nor `/api` ingest
    checked, and a smuggled document was INVISIBLE in the UI (the key project page
    renders the key grid, never a document list) while showing up in `/api/projects/
    {id}/documents` — and it broke two claims the wave makes in writing: a key project
    never auto-completes, and a key project's delivery bundle is empty.

    Refused in `ingest_document` because it is the one seam both surfaces (and any
    future one) go through; a per-route check is a thing to forget."""

    def __init__(self, project_id):
        self.project_id = project_id
        super().__init__(
            f"project {project_id} is a key-based project: its content is string keys, not "
            "uploaded files. Import a key file on the key grid instead."
        )


def _split_for_ingest(text: str, locale: str, srx_on: bool, ruleset: bytes) -> list[str]:
    """Segment one paragraph at ingest under the RESOLVED segmentation profile (P6).

    When the SRX gate is on, the resolved ruleset bytes (the same bytes the
    seg_engine stamp binds) drive the split via segment.split_with_ruleset; a
    per-paragraph failure (oversized paragraph / unparseable ruleset) falls back
    to the legacy splitter for THAT paragraph — mirroring _split_via_srx's
    fallback, which is also what export's stamped reproduction does, so ingest
    and export stay aligned. Gate off = the legacy splitter, version 2 (abbreviation
    rules, decision 15), pinned to the "legacy:2" stamp ingest writes. engine="legacy"
    pins the fallback so a live srx_enabled flip mid-ingest can't re-enter SRX with
    DIFFERENT (active) bytes than the stamp."""
    if srx_on and ruleset:
        out = split_with_ruleset(text, locale, ruleset)
        if out is not None:
            return out
        return split_sentences(text, locale, engine="legacy")
    return split_sentences(text, locale, engine=LEGACY_ENGINE_V2)


def a11y_unit_note(style) -> str:
    """The Editor note for an accessibility unit's style ("" for any other unit)."""
    from app.filters import a11y

    return a11y.unit_note(style)


def _a11y_alt_flags(skeleton: dict, first_seg: dict, n_segments: int) -> list[dict]:
    """VPAT 504.3 prompt: the filter's missing/empty-alt findings (skeleton
    ["a11y_images"], present only for formats that carry images) resolved to segment
    order indices, as `{order_index, kind, where}`. FULL_TEXT (one body segment) and
    an image with no nearby unit both anchor on segment 0. [] when there is nothing to
    flag or no segment to hang it on."""
    info = skeleton.get("a11y_images") or {}
    out: list[dict] = []
    if not n_segments:
        return out
    for f in info.get("missing_alt") or []:
        order = first_seg.get(f.get("unit"), 0) if first_seg else 0
        order = min(max(0, int(order)), n_segments - 1)
        out.append({"order_index": order, "kind": f.get("kind"), "where": f.get("where", "")})
    return out


def _table_kwargs(session, project, filt, columns, stamp: dict | None = None) -> dict:
    """Batch 6: the extra parse arguments a tabular filter (CSV/TSV, XLSX column mode)
    takes: the user's column mapping and the project's locales (auto header detection
    matches `de`/`fr` columns to target languages). {} for every other filter, so their
    parse call is unchanged. `stamp` is a stored skeleton's table stamp (reingest)."""
    if not getattr(filt, "takes_columns", False):
        return {}
    if stamp is not None:
        columns = stamp.get("spec")
        if not columns and not stamp.get("plan"):
            return {}
    elif columns is None and filt.fmt not in ("csv", "tsv"):
        return {}
    targets = [
        tl.locale
        for tl in session.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == project.id, TargetLocale.stopped_at.is_(None)
            )
        ).all()
    ]
    return {
        "columns": columns,
        "source_locale": getattr(project, "source_locale", "") or "",
        "target_locales": targets,
    }


def _comment_kwargs(project, filt) -> dict:
    """#690: `comments=` for the Office filters that take the per-project "translate
    comments" option (services/comment_option); {} for every other filter, so their parse
    call is unchanged."""
    if not getattr(filt, "takes_comments", False):
        return {}
    from app.services import comment_option

    return {"comments": comment_option.enabled_for(project, filt.fmt)}


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _bilingual_locales(fmt: str, data: bytes, skeleton: dict, locales: list[str]) -> list[str]:
    """#687: the target locales a bilingual vendor file may be seeded for (see
    app/services/bilingual_target.py). Stamps the file's declared target(s) and the warning
    on `skeleton`; raises FileTargetMismatch (an InputLimitError) when none match."""
    from app.services import bilingual_target as bt

    if fmt not in bt.BILINGUAL_FMTS:
        return locales
    declared = bt.declared_targets(fmt, data)
    if not declared:
        return locales
    keep, warning = bt.plan_ingest(declared, locales)
    skeleton[bt.SKELETON_KEY] = declared
    warnings = [
        w
        for w in (skeleton.get("file_warnings") or [])
        if not (isinstance(w, dict) and w.get("kind") == bt.WARNING_KIND)
    ]
    if warning:
        warnings.append(warning)
    if warnings:
        skeleton["file_warnings"] = warnings
    else:
        skeleton.pop("file_warnings", None)
    return keep


def _htmlhelp_kwargs(
    session, project, fmt: str, hhp: bytes | None, *, source_fallback: bool = False
) -> dict:
    """Decision 21: a .hhc/.hhk with no declared code page is read in
    the code page of the HTML Help project's language (.hhp `Language=`): the .hhp from
    the same upload (`hhp`, a ZIP batch) or, failing that, one already in the project.
    {} otherwise, so the #509 rule (UTF-8, declared charset, Windows-1252) applies."""
    if fmt not in ("hhc", "hhk"):
        return {}
    from app.filters.htmlhelp_filter import hhp_code_page

    cp = hhp_code_page(hhp) if hhp else None
    if cp is None and getattr(project, "id", None) is not None:
        from app.ports import blobstore

        rows = session.exec(
            select(Document)
            .where(Document.project_id == project.id, Document.fmt == "hhp")
            .order_by(Document.id.desc())
        ).all()
        for d in rows:
            try:
                cp = hhp_code_page(blobstore.get().get(blobstore.document_key(d)))
            except Exception:  # an unreadable stored .hhp is no hint, never a failed upload
                cp = None
            if cp is not None:
                break
    if cp is None and source_fallback:
        # Decision 22: no .hhp either, so the code page of the project's SOURCE language
        # (a fresh upload only; a repair keeps what the import used).
        from app.filters.code_pages import target_code_page

        cp = target_code_page(getattr(project, "source_locale", "") or "")
        cp = None if cp == 1252 else cp
    return {"hhp_code_page": cp} if cp is not None else {}


def ingest_document(
    session: Session,
    project,
    filename: str,
    data: bytes,
    mode: str,
    created_by: str = "",
    context: dict | None = None,
    allow_empty: bool = False,
    columns: str | None = None,
    skeleton_extra: dict | None = None,
    hhp: bytes | None = None,
) -> Document:
    # A-F4: a key project takes no documents. Checked BEFORE the parse, so a hostile
    # upload into one is refused without ever being decoded.
    from app.services import keys as _keys

    if _keys.is_key_project(project):
        raise NotADocumentProject(getattr(project, "id", project))
    # Issue #142: the name arrives from a MACHINE (a multipart upload's filename, a CLI
    # path's basename, an image upload), so it is SANITISED here rather than refused —
    # a browser sending `C:\fakepath\x.md` is not the operator making a mistake. This is
    # THE ingest chokepoint: the web upload, /api upload, CLI upload and image upload all
    # funnel through it, so no surface can store a filename carrying a path. The
    # operator's own `rename` refuses such a name instead (validate_document_filename).
    from app.services.content_part2 import safe_document_filename

    filename = safe_document_filename(filename)
    fmt = _ext(filename)
    if fmt in ("sdlppx", "sdlrpx"):
        # A Trados package becomes one document per target SDLXLIFF (every surface).
        from app.services import sdl_package

        if sdl_package.is_package_upload(filename, data):
            return sdl_package.ingest_package(
                session, project, filename, data, mode, created_by=created_by, context=context
            )
    filt = get_filter(fmt)
    if filt is None:
        raise UnsupportedFormat(fmt or "unknown")

    # AI opt-out (DPAI-08): a parse may OCR page images through a vision model; it
    # runs in the project's AI scope, so an opted-out project's pages never leave.
    # Decision 22: an undeclared non-UTF-8 text file is read in the code
    # page of the project's SOURCE language, with a warning when its letters look wrong.
    from app.filters.base import apply_code_page_hint, source_code_page
    from app.services import ai_optout

    pid, src_locale = getattr(project, "id", None), getattr(project, "source_locale", "") or ""
    with ai_optout.scope(project_id=pid), source_code_page(src_locale) as cp_hint:
        parsed = filt.parse(
            data,
            provider=project.default_provider,
            **_table_kwargs(session, project, filt, columns),
            **_htmlhelp_kwargs(session, project, fmt, hhp, source_fallback=True),
            **_comment_kwargs(project, filt),
        )
    # Batch 4 gate: a macro-enabled Office package named .docx/.xlsx/.pptx (or a
    # template / show form) is refused; macros come in only under .docm/.xlsm/.pptm etc.
    from app.filters.zip_guard import MacroExtensionMismatch, refuse_disguised_macro

    try:
        refuse_disguised_macro(data, fmt)
    except MacroExtensionMismatch as exc:
        raise UnsupportedFormat(str(exc)) from exc
    skeleton = apply_code_page_hint(parsed.skeleton, cp_hint)
    unit_texts = [u["text"] for u in skeleton["units"]]
    # #571: every format answers to one segment cap and one line-length cap, BEFORE the
    # Document row or any Segment is written (a filter may already have stopped earlier).
    check_unit_texts(unit_texts)

    # #573: a file that parses cleanly but carries no text (empty, or only whitespace)
    # is refused here - THE ingest chokepoint every surface funnels through - BEFORE any
    # Document row, blob or segment is created. It must not silently land as a 0-segment
    # document. EmptyDocumentError subclasses InputLimitError, so the web page and CLI
    # already show the message; the API maps it to 422 (see api_part2).
    # allow_empty is for image ingest only: a raster with zero detected regions is a
    # supported state (the honest offline answer with no OCR engine; the image page and
    # CLI say how to enable one), so it must still land as a document.
    if not allow_empty and not any(t.strip() for t in unit_texts):
        raise EmptyDocumentError("The file has no text to translate.")

    mode = mode if mode in (TransMode.SEGMENTED, TransMode.FULL_TEXT) else TransMode.SEGMENTED
    if mode == TransMode.FULL_TEXT:
        # Accessibility units (image alt text, link titles; `a11y:*` style) stay OUT of a
        # full-text document: appended to the one body they would be context-free
        # trailing paragraphs a model may merge (tripping the export paragraph-count
        # guard). Dropped from the skeleton itself, so every FULL_TEXT reader (export,
        # paragraph view, Advanced) sees the same unit list and their alt text is left
        # as it is on export. Segmented mode keeps them as their own segments.
        # #733: page metadata units (`meta:*`) are appended the same way and stay out too.
        from app.filters.a11y import is_appended_style

        skeleton["units"] = [u for u in skeleton["units"] if not is_appended_style(u.get("style"))]
        unit_texts = [u["text"] for u in skeleton["units"]]
    skeleton["mode"] = mode
    if skeleton_extra:
        # ZIP batch (Batch 13): the member's archive path + batch id, stamped before the
        # Document row exists so the record is never half-written.
        skeleton.update(skeleton_extra)
    # Stamp the source locale used for segmentation so export re-splits with the
    # SAME locale (SRX maprule selection is locale-dependent) even if the project
    # locale or the srx_file setting changes later — the round-trip sentence count
    # MUST match what ingest produced, or export would misalign the target text.
    seg_locale = getattr(project, "source_locale", "") or ""
    skeleton["seg_locale"] = seg_locale
    # Resolve the SEGMENTATION PROFILE for this project (P6, item 8): the fallback
    # chain project → customer → system-default → built-in. The resolved ruleset
    # BYTES drive BOTH the segmentation below AND the seg_engine stamp, so they can
    # never diverge. srx_enabled OFF (the default) ignores the chain entirely —
    # the legacy splitter runs and the stamp is "legacy" (byte-identical to today).
    from app.services import segment as _segment
    from app.services import segmentation_profiles as _segprof
    from app.services import srx as _srx

    srx_on = _segment._srx_enabled()
    seg_ruleset = _segprof.resolve_ruleset(session, project) if srx_on else b""
    # Stamp the FULL segmentation identity at ingest, not just the locale (finding
    # F1). "legacy" when the SRX gate is off, else "srx:<hash-of-resolved-ruleset>".
    # Export re-splits with the STAMPED engine (see export.paragraph_texts_for) so
    # toggling srx_enabled or swapping the ruleset between ingest and export can never
    # re-split differently and scramble the target alignment.
    skeleton["seg_engine"] = (
        _srx.engine_identity_for_bytes(True, seg_ruleset) if srx_on else _srx.engine_identity(False)
    )
    skeleton["chunk_plan"] = plan_chunks(unit_texts, config.MAX_CHUNK_TOKENS)
    skeleton["est_tokens_total"] = sum(est_tokens(t) for t in unit_texts)

    # build the source segment texts for this mode. #571: done BEFORE the Document row is
    # committed, so a refused (over-cap) file leaves nothing behind.
    # `notes` runs parallel to `sources`: the Editor note for an accessibility unit
    # (image alt text, link title; app/filters/a11y.py), "" for every other segment.
    # `first_seg` maps a unit's p_index to its first segment's order_index, so a
    # missing-alt finding anchored to a unit lands on a real segment.
    notes: list[str] = []
    first_seg: dict = {}
    if mode == TransMode.FULL_TEXT:
        sources = ["\n\n".join(unit_texts)] if unit_texts else []
        notes = [""] * len(sources)
    else:
        sources = []
        # #511 gate: record which paragraphs fell back to the default SRX rules so
        # export replays the same decision (srx.replay_fallbacks).
        with _srx.record_fallbacks() as srx_fb:
            for u in skeleton["units"]:
                first_seg[u.get("p_index")] = len(sources)
                pieces = _split_for_ingest(u["text"], seg_locale, srx_on, seg_ruleset)
                sources.extend(pieces)
                check_segment_cap(len(sources))  # #571: refuse before any row exists
                # Batch 6: a tabular unit's key/context/max-length cells are its note.
                notes.extend([u.get("note") or a11y_unit_note(u.get("style"))] * len(pieces))
        if srx_fb:
            skeleton["srx_fallback"] = sorted(srx_fb)
    alt_flags = _a11y_alt_flags(skeleton, first_seg, len(sources))
    if alt_flags:
        skeleton["a11y_alt_flags"] = alt_flags

    # One Segment row is written per (segment x target locale), so the row total, not the
    # segment count, is what the database and the translate run pay for. Checked here, still
    # before the Document row exists, so a refused file stores nothing.
    locales = [
        tl.locale
        for tl in session.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == project.id, TargetLocale.stopped_at.is_(None)
            )
        ).all()
    ] or ["—"]
    # #687 (2026-10-11, decision 6): a bilingual vendor file (SDLXLIFF, Phrase/memoQ
    # XLIFF, Wordfast TXLF/TXML, TTX, a Trados package member) keeps its own target
    # language on export, so it is seeded only for the project target(s) it declares, with
    # a warning; none matching is refused here, before any row is written.
    locales = _bilingual_locales(fmt, data, skeleton, locales)
    if len(sources) * len(locales) > config.MAX_DOCUMENT_ROWS:
        raise InputLimitError(
            f"This file would create {len(sources) * len(locales)} segment rows "
            f"({len(sources)} segments x {len(locales)} target languages); the limit is "
            f"{config.MAX_DOCUMENT_ROWS} (MAX_DOCUMENT_ROWS). Remove target languages from the "
            "project or split the file."
        )

    doc = Document(
        project_id=project.id,
        upload_blob_key=f"upload_{uuid4().hex}.{fmt}",
        filename=filename,
        fmt=fmt,
        mode=mode,
        status=DocStatus.NEW,
        skeleton=skeleton,
        created_by=created_by,
        # SPEC-context-metadata phase 1: the request/doc-level TranslationContext
        # (already validated+sanitized by the caller via context_pkg). None =
        # byte-identical to today (no context stored).
        context=(context or None),
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)

    # The Document row is now COMMITTED, so a later failure (writing the upload
    # bytes, seeding segments) can no longer be undone by a session.rollback() in
    # the caller — it would leave an orphan doc row. Wrap the remaining work and,
    # on any failure, delete the just-created Document (and any seeded segments)
    # before re-raising, so both the API and UI upload paths stay clean.
    try:
        # keep the original so export can preserve formatting (encrypted at rest if enabled)
        from app.ports import blobstore

        blobstore.get().put(blobstore.document_key(doc), data)

        # #571: write in bounded chunks, committing each, so the SQLite write lock is held
        # for milliseconds at a time (not for one giant flush that starves every other
        # request) and memory stays flat. The brief sleep hands the interpreter lock back
        # to the event loop between chunks. A failure part-way is undone by the cleanup
        # below, exactly as before.
        doc_id = doc.id
        pending = 0
        for loc in locales:
            for k, src in enumerate(sources):
                tags = extract_inline_tags(src)
                session.add(
                    Segment(
                        document_id=doc_id,
                        target_locale=loc,
                        order_index=k,
                        source_text=src,
                        inline_tags=tags or None,
                        # Only accessibility units carry a note; every other segment
                        # keeps meta NULL exactly as before.
                        meta=({"notes": notes[k]} if notes[k] else None),
                    )
                )
                pending += 1
                if pending >= _INSERT_CHUNK:
                    session.commit()
                    time.sleep(0.002)
                    pending = 0
        session.commit()
    except Exception:
        # Undo the committed doc + any seeded segments so the failure leaves no
        # orphan. Roll back the uncommitted work first, then hard-delete on a
        # fresh transaction and re-raise for the caller to handle (415/422/…).
        doc_id = doc.id
        session.rollback()
        from app.services import content

        content.delete_document(session, doc_id, actor=created_by or "ingest", purge_exports=False)
        raise
    # Living-projects reopen (P1): a new document on a COMPLETED/CLOSED project
    # flips it back to IN_TRANSLATION (additive content re-opens it).
    from app.services import lifecycle

    lifecycle.reopen_if_terminal(session, project.id)
    return doc


def ingest_zip_batch(
    session: Session,
    project,
    filename: str,
    data: bytes,
    mode: str,
    created_by: str = "",
    context: dict | None = None,
) -> dict:
    """ZIP batch upload (Batch 13): one document per supported member of an uploaded .zip.

    Every member goes through `ingest_document` (the same chokepoint, caps and checks as a
    single upload) with its archive path stamped on the skeleton. Returns
    `{batch_id, archive, documents: [{id, filename, path, fmt, segments}],
    skipped: [{path, reason}], failed: [{path, error}]}`: unsupported or unsafe-to-open
    members are SKIPPED and listed; a supported member that would not ingest (empty, over a
    cap, unreadable) is listed under FAILED while the rest of the batch still lands.

    Raises NotADocumentProject for a key project, UnsupportedFormat when the bytes are not a
    ZIP, InputLimitError when the archive itself is over a limit or carries an unsafe path
    (nothing is stored in those cases)."""
    from app.filters import zip_batch
    from app.services import keys as _keys

    if _keys.is_key_project(project):
        raise NotADocumentProject(getattr(project, "id", project))
    from app.services.content_part2 import safe_document_filename

    archive = safe_document_filename(filename, fallback="upload.zip")
    try:
        batch = zip_batch.expand(data, archive=archive)
    except zip_batch.NotAZipArchive as exc:
        raise UnsupportedFormat("zip (not a readable ZIP archive)") from exc
    documents: list[dict] = []
    failed: list[dict] = []
    # Decision 21: a .hhc/.hhk is read in the code page of the .hhp in the same archive.
    hhp = next((m.data for m in batch.members if _ext(m.name) == "hhp"), None)
    for member in batch.members:
        try:
            doc = ingest_document(
                session,
                project,
                member.name,
                member.data,
                mode,
                created_by=created_by,
                context=context,
                skeleton_extra=zip_batch.stamp(batch, member),
                hhp=hhp,
            )
        except UnsupportedFormat as exc:
            session.rollback()
            failed.append({"path": member.path, "error": f"unsupported format: {exc}"})
            continue
        except InputLimitError as exc:
            session.rollback()
            failed.append({"path": member.path, "error": str(exc)})
            continue
        except Exception:
            session.rollback()
            failed.append(
                {"path": member.path, "error": "The file couldn't be read; it may be corrupted."}
            )
            continue
        n = session.exec(
            select(Segment.order_index).where(Segment.document_id == doc.id).distinct()
        ).all()
        documents.append(
            {
                "id": doc.id,
                "filename": doc.filename,
                "path": member.path,
                "fmt": doc.fmt,
                "segments": len(n),
            }
        )
    return {
        "batch_id": batch.batch_id,
        "archive": archive,
        "documents": documents,
        "skipped": batch.skipped,
        "failed": failed,
    }


def zip_batch_summary(result: dict, max_chars: int | None = None) -> str:
    """One plain line for a ZIP batch result (web flash, CLI line). `max_chars` caps it for
    a redirect URL: ten long non-Latin member paths percent-encode to tens of KB, past the
    server's request-line limit, so the web upload would answer an error page instead."""
    parts = [f"{len(result['documents'])} document(s) created from {result['archive']}"]
    if result["skipped"]:
        parts.append(
            f"{len(result['skipped'])} skipped ("
            + "; ".join(f"{s['path']}: {s['reason']}" for s in result["skipped"][:10])
            + ("; ..." if len(result["skipped"]) > 10 else "")
            + ")"
        )
    if result["failed"]:
        parts.append(
            f"{len(result['failed'])} failed ("
            + "; ".join(f"{f['path']}: {f['error']}" for f in result["failed"][:10])
            + ("; ..." if len(result["failed"]) > 10 else "")
            + ")"
        )
    line = ". ".join(parts) + "."
    if max_chars is not None and len(line) > max_chars:
        line = (
            line[: max(0, max_chars - 45)].rstrip() + " ... (the full list is on the API and CLI)"
        )
    return line


def _reingest_htmlhelp_kwargs(session, project, doc) -> dict:
    """Decision 21 on a repair: the code page the import used (stamped on the skeleton),
    else the project's .hhp, else nothing (the #509 rule)."""
    cp = (doc.skeleton or {}).get("hhp_code_page")
    if doc.fmt in ("hhc", "hhk") and isinstance(cp, int):
        return {"hhp_code_page": cp}
    return _htmlhelp_kwargs(session, project, doc.fmt, None)


def reingest_document(doc_id: int) -> bool:
    """Re-parse a document from its stored upload bytes and rebuild its source
    segments (operator remediation for a BROKEN ingest — a document whose parse
    produced no/garbled segments).

    SAFETY: this is a repair, not a re-run. It refuses to touch a document that
    carries POST-EDITED work — if ANY segment has non-empty target_text (a human
    or MT already wrote a translation), re-ingesting would clobber it, so we bail
    (returns False). It only rebuilds source segments when the document's targets
    are all empty (the true "broken/empty" case). Reuses the SAME parse + sentence
    split the upload path used, so the rebuilt segmentation is identical to a
    fresh upload. Returns True on success, False when it declines / can't.
    """
    from app.ports import blobstore

    with Session(engine) as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return False
        from app.models import Project

        project = session.get(Project, doc.project_id)
        if project is None:
            return False
        existing = session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        # Refuse to blow away any translated/edited content.
        if any((s.target_text or "").strip() for s in existing):
            return False

        try:
            data = blobstore.get().get(blobstore.document_key(doc))
        except OSError:
            return False
        filt = get_filter(doc.fmt)
        if filt is None:
            return False
        # AI opt-out (DPAI-08): a parse may OCR page images through a vision model; it
        # runs in the project's AI scope, so an opted-out project's pages never leave.
        from app.filters.base import apply_code_page_hint, document_code_page
        from app.services import ai_optout

        # Decision 22: re-read in the code page the import used (none stamped: the #509 rule).
        pid = getattr(project, "id", None)
        with ai_optout.scope(project_id=pid), document_code_page(doc.skeleton) as cp_hint:
            parsed = filt.parse(
                data,
                provider=project.default_provider,
                **_table_kwargs(
                    session, project, filt, None, stamp=(doc.skeleton or {}).get("table") or {}
                ),
                **_reingest_htmlhelp_kwargs(session, project, doc),
                **_comment_kwargs(project, filt),
            )
        if cp_hint is not None:
            apply_code_page_hint(parsed.skeleton, cp_hint)
        parsed_units = parsed.skeleton.get("units", [])
        check_unit_texts([u["text"] for u in parsed_units])  # #571: same caps as a fresh upload
        if doc.mode == TransMode.FULL_TEXT:
            # As ingest_document: accessibility units stay out of a full-text body (the
            # export paragraph-count guard quarantines a body that carries them).
            from app.filters.a11y import is_appended_style

            parsed_units = [u for u in parsed_units if not is_appended_style(u.get("style"))]
        unit_texts = [u["text"] for u in parsed_units]
        seg_locale = getattr(project, "source_locale", "") or ""
        # Resolve the SEGMENTATION PROFILE exactly as ingest_document does (P6): a
        # stamped SRX ruleset must drive the re-split, not the legacy splitter, so a
        # repair reproduces the SAME segmentation a fresh upload would (the docstring's
        # "identical to a fresh upload" promise). srx_enabled OFF (the default) resolves
        # to the legacy splitter, byte-identical to today.
        from app.services import segment as _segment
        from app.services import segmentation_profiles as _segprof
        from app.services import srx as _srx

        srx_on = _segment._srx_enabled()
        seg_ruleset = _segprof.resolve_ruleset(session, project) if srx_on else b""
        # Re-STAMP the segmentation identity + locale to match the re-split (reviewer
        # F1): export.paragraph_texts_for re-splits the source by the STAMPED engine,
        # so a reingest that re-split under a DIFFERENT engine than the old stamp
        # (settings/profile changed since the original ingest) would misalign every
        # target at export (e.g. stamp 'legacy' -> 1 paragraph vs 3 SRX segments).
        # Derived exactly as ingest_document derives them; the whole JSON column is
        # reassigned (never mutated in place) so SQLAlchemy tracks the change.
        skel = dict(doc.skeleton or {})
        skel["seg_locale"] = seg_locale
        skel["seg_engine"] = (
            _srx.engine_identity_for_bytes(True, seg_ruleset)
            if srx_on
            else _srx.engine_identity(False)
        )
        # GitHub #451 follow-up: the rebuilt segments come from THIS parse, so the stored
        # unit list must too, for EVERY format. It used to move only for a docx whose walk
        # changed, so any other format (and a first parse that found 0 units) kept the old
        # list while its segments came from the new one, and export rebuilt the file from
        # units that no longer matched the segments.
        skel["units"] = parsed_units
        if filt.fmt == "docx" and skel.get("docx_walk") != parsed.skeleton.get("docx_walk"):
            # #349: this repair re-walks with the current table walk, so the stored
            # unit numbering carries its stamp; otherwise export would rebuild the new
            # segments under the old numbering.
            skel["docx_walk"] = parsed.skeleton.get("docx_walk")
        # #368: what the parse reports about the FILE (the warnings the current filter
        # names, the image alt-text findings) is recomputed too, so a repaired document
        # shows what a fresh upload of the same bytes would, never the stale import's.
        # #731: md / mdx units are numbered by the current walk now, so the stamp moves too.
        for key in (
            "file_warnings",
            "a11y_images",
            "table",
            "md_walk",
            "md_fm_keys",
            "hhp_code_page",
            "text_code_page",
        ):
            if parsed.skeleton.get(key):
                skel[key] = parsed.skeleton[key]
            else:
                skel.pop(key, None)
        skel["chunk_plan"] = plan_chunks(unit_texts, config.MAX_CHUNK_TOKENS)
        skel["est_tokens_total"] = sum(est_tokens(t) for t in unit_texts)

        # The segments, their accessibility notes and the missing-alt flags exactly as
        # ingest_document builds them (the flags index the NEW segment order).
        notes: list[str] = []
        first_seg: dict = {}
        if doc.mode == TransMode.FULL_TEXT:
            sources = ["\n\n".join(unit_texts)] if unit_texts else []
            notes = [""] * len(sources)
        else:
            sources = []
            with _srx.record_fallbacks() as srx_fb:  # #511 gate, as ingest_document
                for u in parsed_units:
                    first_seg[u.get("p_index")] = len(sources)
                    pieces = _split_for_ingest(u["text"], seg_locale, srx_on, seg_ruleset)
                    sources.extend(pieces)
                    check_segment_cap(len(sources))  # #571: before the old rows are deleted
                    # Batch 6: a tabular unit's key/context/max-length cells are its note.
                    notes.extend([u.get("note") or a11y_unit_note(u.get("style"))] * len(pieces))
        if doc.mode != TransMode.FULL_TEXT and srx_fb:
            skel["srx_fallback"] = sorted(srx_fb)
        else:
            skel.pop("srx_fallback", None)
        alt_flags = _a11y_alt_flags(skel, first_seg, len(sources))
        if alt_flags:
            skel["a11y_alt_flags"] = alt_flags
        else:
            skel.pop("a11y_alt_flags", None)
        doc.skeleton = skel

        # Issue #451: the locale set is the project's TargetLocale rows (the same lookup
        # ingest_document seeds from) MERGED with whatever locales the broken segments
        # already carried, so a repair matches a fresh upload without dropping a locale
        # added after ingest. Taking them from the segments alone rebuilt a document whose
        # first parse produced 0 segments -- the case this repair exists for -- under the
        # placeholder locale only, so it still could not be translated or exported.
        project_locales = [
            tl.locale
            for tl in session.exec(
                select(TargetLocale).where(
                    TargetLocale.project_id == project.id, TargetLocale.stopped_at.is_(None)
                )
            ).all()
        ]
        locales = project_locales + [s.target_locale for s in existing]
        # de-dupe while preserving order
        seen = set()
        locales = [x for x in locales if not (x in seen or seen.add(x))]
        # The placeholder is only a stand-in for "no locale yet": once any real locale is
        # known, a document first ingested (or earlier repaired) under it must not keep a
        # placeholder segment set next to the real ones, matching a fresh upload.
        locales = [x for x in locales if x != "—"] or ["—"]
        # #687: a bilingual vendor file is rebuilt only for the target(s) it declares, as a
        # fresh upload is; a repair never seeds a language the file is not for.
        try:
            locales = _bilingual_locales(doc.fmt, data, skel, locales)
        except InputLimitError:
            return False

        # GitHub #451 follow-up: ONE transaction. The old segments used to be deleted and
        # COMMITTED before the new ones were built, so a failure in between (a bad parse
        # result, a DB error mid-insert) left the document with no segments at all. Now the
        # delete is flushed (so the unique order index is free for the inserts) but not
        # committed: any failure rolls the whole repair back and the old segments remain.
        for seg in existing:
            session.delete(seg)
        session.flush()

        for loc in locales:
            for k, src in enumerate(sources):
                tags = extract_inline_tags(src)
                session.add(
                    Segment(
                        document_id=doc.id,
                        target_locale=loc,
                        order_index=k,
                        source_text=src,
                        inline_tags=tags or None,
                        meta=({"notes": notes[k]} if notes[k] else None),
                    )
                )
        doc.status = DocStatus.NEW
        session.add(doc)
        session.commit()
    return True


# --- Trados packages (Batch 13): the shells reach app.services.sdl_package through this
# module so the kernel facade surface does not grow (verify_kernel_facade).
def inspect_sdl_package(data: bytes, filename: str = "") -> dict:
    from app.services import sdl_package

    return sdl_package.inspect(data, filename)


def ingest_sdl_package(session: Session, project, filename: str, data: bytes, mode: str, **kw):
    from app.services import sdl_package

    return sdl_package.ingest_package(session, project, filename, data, mode, **kw)


def sdl_package_summary(session: Session, doc) -> dict | None:
    from app.services import sdl_package

    return sdl_package.import_summary(session, doc)


def create_sdl_package_project(session: Session, data: bytes, filename: str, **kw):
    """A new project from a Trados project package, then the package imported into it
    (web new-project form, POST /api/documents sdl_new_project=1). Returns (project,
    first document); a refused import deletes the project again."""
    from app.services import sdl_package

    return sdl_package.create_project_from_package(session, data, filename, **kw)
