"""Project delivery / download (P1, item 3) — assemble a project's translated files
per locale, shared by the web download routes and the CLI `project export`.

Reuses the per-document round-trip (app/services/export.build_translated_file), so
format fidelity (DOCX/XLIFF inline tags, PDF, HTML, …) is exactly what a single
`doc export` produces. Two shapes:

  * export_project(..., zip=True)  → a ZIP with **subfolders per locale** and each
    file named `<stem>_<locale>.<ext>` (digest §item-3: Smartling/Lokalise
    subfolders-per-language + locale-in-filename; the locale is forced into BOTH
    the path AND the name so files can never overwrite — Lokalise's %LANG_ISO%
    footgun designed out).
  * export_project(..., zip=False) with exactly one locale → the single translated
    file's bytes + its `<stem>_<locale>.<ext>` name (the single-file fast path).

`locales=None` means every target locale on the project. A requested locale the
project doesn't have is simply skipped (never a hard error); a document with no
segments for a locale falls back to source text via export.build_translated_file.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import re
import zipfile

from sqlmodel import Session, select

from app.models import Document, Project, ProjectType, TargetLocale
from app.services import export, tm_delivery


def _safe(part: str) -> str:
    """Filesystem-safe path component: keep word chars, dot, dash; collapse the
    rest to underscore. Guards against a hostile filename/locale escaping the ZIP
    subfolder (path traversal) and keeps the archive portable."""
    return re.sub(r"[^\w.\-]+", "_", (part or "").strip()) or "file"


def _stem_ext(filename: str, fmt: str) -> tuple[str, str]:
    """(stem, ext) for a document, preferring the real filename extension and
    falling back to the stored fmt."""
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
    else:
        stem, ext = filename, (fmt or "bin")
    return stem or "document", (ext or fmt or "bin")


def file_name_for(filename: str, fmt: str, locale: str) -> str:
    """`<stem>_<locale>.<ext>` — the delivered filename for one document/locale."""
    from app.services.mime import export_ext_for

    stem, ext = _stem_ext(filename, fmt)
    ext = export_ext_for(ext)
    return f"{_safe(stem)}_{_safe(locale)}.{_safe(ext)}"


def _batch_info(doc) -> dict:
    """The ZIP batch stamp on a document's skeleton ({id, archive, path}) or {}."""
    info = (doc.skeleton or {}).get("zip_batch") if isinstance(doc.skeleton, dict) else None
    return info if isinstance(info, dict) else {}


def _batch_arc_path(doc) -> str:
    """The member's original relative path, re-validated (a stored path can never climb
    out of its locale folder); a path that fails falls back to the safe flat name."""
    from app.filters.zip_batch import safe_member_path

    path = safe_member_path(_batch_info(doc).get("path") or "")
    if path is None:
        stem, ext = _stem_ext(doc.filename, doc.fmt)
        return f"{_safe(stem)}.{_safe(ext)}"
    return path


def zip_batches(session: Session, project_id: int, documents=None) -> list[dict]:
    """The ZIP batch uploads in a project, newest first:
    `[{id, archive, documents, created_at}]`. Pass `documents` when the caller already
    holds the project's Document rows (the project page, the document lists)."""
    if documents is None:
        documents = session.exec(select(Document).where(Document.project_id == project_id)).all()
    rows = [(d.id, d.created_at, _batch_info(d)) for d in documents]
    out: dict[str, dict] = {}
    for _doc_id, created, info in rows:
        if not isinstance(info, dict) or not isinstance(info.get("id"), str):
            continue
        b = out.setdefault(
            info["id"],
            {
                "id": info["id"],
                "archive": str(info.get("archive") or ""),
                "documents": 0,
                "created_at": created,
            },
        )
        b["documents"] += 1
        if created and (b["created_at"] is None or created < b["created_at"]):
            b["created_at"] = created
    batches = sorted(out.values(), key=lambda b: str(b["created_at"] or ""), reverse=True)
    for b in batches:
        b["created_at"] = b["created_at"].isoformat() if b["created_at"] else None
    return batches


def project_locales(session: Session, project_id: int) -> list[str]:
    """Every target locale configured on the project, in insertion order."""
    return [
        tl.locale
        for tl in session.exec(
            select(TargetLocale).where(TargetLocale.project_id == project_id)
        ).all()
    ]


def _resolve_locales(session: Session, project_id: int, locales) -> list[str]:
    """The locales to actually export: the project's own set when `locales` is
    None, else the requested subset intersected with the project's set (order
    preserved, duplicates dropped)."""
    have = project_locales(session, project_id)
    if locales is None:
        return have
    have_set = set(have)
    out, seen = [], set()
    for loc in locales:
        loc = (loc or "").strip()
        if loc and loc in have_set and loc not in seen:
            seen.add(loc)
            out.append(loc)
    return out


def export_project(
    session: Session,
    project_id: int,
    locales=None,
    zip: bool = True,
    report: list | None = None,
    allow_held: bool = False,
    batch: str | None = None,
):
    """Build the delivery payload for a project.

    `batch` (Batch 13, ZIP batch upload): only the documents created from that uploaded
    .zip, always as a ZIP laid out `<locale>/<original path inside the archive>`, so each
    target language gets the same folder structure the upload had.

    `allow_held` (review M1): the single-file fast path refuses a HELD locale
    (ExportHeldLocale) by default; pass True to ship the source-filled file anyway (the
    explicit per-export override, mirroring the document surfaces' `allow_held`). The ZIP
    path already skip-and-reports held docs (naming the override), so the flag only
    affects the single-file path.

    Returns (data: bytes, filename: str, ctype: str, skipped: list). With zip=True (or
    more than one locale) a ZIP is produced: `<locale>/<stem>_<locale>.<ext>` per
    document. With zip=False and exactly one resolved locale, the single translated
    file is returned directly (its own MIME). Raises ValueError when the project is
    missing or resolves to no exportable locale/document.

    `skipped` (AI Tagging 1.6 / M1) lists any `(locale, document)` pairs the export
    QUARANTINED for inline-tag desync — SKIP-AND-REPORT (mirroring jira.py's per-
    attachment pattern): a quarantined doc is EXCLUDED from the ZIP rather than
    aborting the whole bundle, and the caller surfaces the list honestly. Each entry
    is `{doc_id, filename, locale, reason}`. In the single-file fast path a quarantine
    has nothing to return, so it propagates `TagExportQuarantine` for the caller to
    surface cleanly. Empty on a clean export (byte-identical to the pre-1.6 3-tuple
    plus a trailing []).

    `report` (BUG-04) is the SUBSTITUTION channel's bundle twin, an OUT-PARAMETER for
    the same reason `export.build_translated_file`'s is: the bytes of a degraded bundle
    look perfectly well-formed, so the fact has to ride alongside them. A quarantine is
    LOUD (a doc goes missing from the ZIP); a `source`-policy substitution is SILENT —
    the file is there, well-formed, and one segment is quietly English. `doc export`
    reported that on all three surfaces from #41 onwards, but the BUNDLE — the surface
    that actually delivers a project — dropped the report on the floor by passing no
    channel at all, and `docs/CLI.md` has documented a `_SUBSTITUTED.txt` manifest that
    did not exist. Extended in place with ONE record per affected (document, locale):
    `{doc_id, filename, locale, count, detail}`, where `detail` is
    `export.substitution_detail()` over that document's own entries — per-document
    because a flat index list means nothing once several documents share it. Unaffected
    documents contribute nothing, so a clean bundle leaves the list empty and the ZIP
    byte-identical."""
    from app.services.export import TagExportQuarantine
    from app.services.mime import mime_for_fmt

    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"no such project: {project_id}")
    targets = _resolve_locales(session, project_id, locales)
    if not targets:
        raise ValueError("no target locales to export for this project")
    # SPEC-key-based-projects 2.5: internal key documents have no original file to
    # rebuild, so they are never part of a delivery bundle. A key project's
    # deliverable is the per-locale key file (`key_files.export_locale`), offered on
    # the key grid and `keys export` -- the universal-export merge is deferred (9).
    docs = [
        d
        for d in session.exec(select(Document).where(Document.project_id == project_id)).all()
        if d.fmt != "keys"
    ]
    if batch is not None:
        docs = [d for d in docs if _batch_info(d).get("id") == batch]
        if not docs:
            raise ValueError(f"no documents from ZIP upload {batch!r} in this project")
        zip = True
    if not docs:
        # NAME THE RIGHT DOOR (review B13). A key project has no listable documents by
        # construction, so "project has no documents to export" is true, useless, and
        # reads as a fault; its deliverable is the per-locale key file.
        if getattr(project, "project_type", None) == ProjectType.KEY_BASED:
            raise ValueError(
                "this is a key-based project: its deliverable is one key file per "
                "locale, not a document bundle. Use Export on the key grid, or "
                f"`keys export --project {project_id} --locale <locale>`."
            )
        raise ValueError("project has no documents to export")

    proj_slug = _safe(project.name)
    skipped: list[dict] = []

    def _record(doc, loc, entries) -> None:
        """One per-document substitution record into `report` (no-op when clean)."""
        if report is None or not entries:
            return
        report.append(
            {
                "doc_id": doc.id,
                "filename": doc.filename,
                "locale": loc,
                "count": len(entries),
                "detail": export.substitution_detail(entries),
            }
        )

    # Single-file fast path: one locale, caller asked for no zip, one document. A
    # quarantine here has no clean file to return, so it propagates for the caller.
    if not zip and len(targets) == 1 and len(docs) == 1:
        loc = targets[0]
        doc = docs[0]
        # chip task_9c665111: a held locale (no committed translation) has no clean file
        # to return, so it propagates for the caller to surface (a 409), never a
        # source-only download. Mirrors the TagExportQuarantine single-file contract.
        # Batch 15 T2 (§9.1/LAW-TM-14): the TM-only empty gate COMPANION. HOLD refuses
        # (ExportEmptyTargetsHeld, NOT bypassable by allow_held); RELEASE_EMPTIES delivers
        # with empty strings (build emits them, classic held check skipped); PASS falls
        # through to the unchanged assert_deliverable / allow_held logic.
        # ISSUE #95 (gate C round 1, H1): the QUALITY-GATE hold COMPANION, consulted
        # first and NOT bypassable by allow_held. Raised as the same ExportHeldLocale the
        # classic hold uses so every caller's existing 409 path carries it unchanged.
        _qgate = export.gate_held_reason(session, doc, loc)
        if _qgate is not None:
            raise export.ExportHeldLocale(_qgate, doc_id=doc.id, locale=loc)
        _gate = tm_delivery.empty_gate_decision(session, doc=doc, locale=loc)
        if _gate == tm_delivery.HOLD:
            empties = tm_delivery.doc_empties(session, doc, loc)
            raise export.ExportEmptyTargetsHeld(
                # #105: shared diagnosis seam + this site's way-out.
                tm_delivery.held_message(
                    session,
                    doc=doc,
                    locale=loc,
                    count=len(empties),
                    unit="segment",
                    way_out=("Use doc deliver-approve; allow_held cannot bypass it."),
                ),
                doc_id=doc.id,
                locale=loc,
                empties=empties,
            )
        # M1: allow_held ships the source-filled file explicitly (the way out).
        if _gate != tm_delivery.RELEASE_EMPTIES and not allow_held:
            export.assert_deliverable(session, doc, loc)
        entries: list = []
        data = export.build_translated_file(session, doc, loc, report=entries)
        _record(doc, loc, entries)
        return data, file_name_for(doc.filename, doc.fmt, loc), mime_for_fmt(doc.fmt), skipped

    buf = io.BytesIO()
    # Batch 15 T2 (N8): memoize the durable used_tm_match marker per document across the
    # whole bundle (~docs x locales evaluations) -- one indexed read per document, not per
    # (doc, locale).
    _marker_cache: dict = {}
    # files-04: two documents whose names are equal or sanitise to the same string would
    # otherwise write DUPLICATE members, and an unzip keeps only the last one (a deliverable
    # silently dropped). Track used names case-insensitively (Windows/macOS unzip) and give a
    # colliding member a `_<doc_id>` suffix before its extension, listed in `_RENAMED.txt`.
    used_arcs: set = set()
    renamed: list = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for loc in targets:
            for doc in docs:
                entries = []
                # #687: a bilingual vendor file is delivered only in its own target language.
                other_target = export.file_target_refusal(doc, loc)
                if other_target:
                    skipped.append(
                        {
                            "doc_id": doc.id,
                            "filename": doc.filename,
                            "locale": loc,
                            "reason": other_target,
                        }
                    )
                    continue
                # Batch 15 T2 (§9.1/§9.2): the TM-only empty gate COMPANION. A HOLD is
                # SKIP-AND-REPORTED like a held/quarantined doc, naming the approval way-out
                # (NOT allow_held); a RELEASE_EMPTIES delivers with empty strings (build
                # emits them, classic held check skipped); a PASS falls through to the
                # unchanged classic held skip-and-report.
                # ISSUE #95 (gate C round 1, H1): a gate-held (doc, locale) is
                # SKIP-AND-REPORTED like a held/quarantined doc -- unreviewed failing text
                # never rides into a bundle as if it were delivered.
                _qgate = export.gate_held_reason(session, doc, loc)
                if _qgate is not None:
                    skipped.append(
                        {
                            "doc_id": doc.id,
                            "filename": doc.filename,
                            "locale": loc,
                            "reason": _qgate,
                        }
                    )
                    continue
                _gate = tm_delivery.empty_gate_decision(
                    session, doc=doc, locale=loc, _marker_cache=_marker_cache
                )
                if _gate == tm_delivery.HOLD:
                    n = len(tm_delivery.doc_empties(session, doc, loc))
                    skipped.append(
                        {
                            "doc_id": doc.id,
                            "filename": doc.filename,
                            "locale": loc,
                            # #105: shared diagnosis seam + the bundle's per-document way-out.
                            # Round-1 LOW-4: the SAME memo the decision above used, so a held
                            # document costs ONE in-flight read for the pair, not two.
                            "reason": tm_delivery.held_message(
                                session,
                                doc=doc,
                                locale=loc,
                                count=n,
                                unit="segment",
                                way_out=(
                                    "Approve it individually (doc deliver-approve / "
                                    f"POST /api/documents/{doc.id}/deliver-approve?locale={loc})."
                                ),
                                cache=_marker_cache,
                            ),
                        }
                    )
                    continue
                # chip task_9c665111: a held locale for this doc (no committed
                # translation) is SKIP-AND-REPORTED, exactly like a quarantine -- never
                # source-echoed into the bundle as if delivered. M1: the reason names the
                # per-document way-out (the bundle has no source-anyway mode of its own).
                held = (
                    None
                    if _gate == tm_delivery.RELEASE_EMPTIES
                    else export.held_locale_reason(session, doc, loc)
                )
                if held is not None:
                    skipped.append(
                        {
                            "doc_id": doc.id,
                            "filename": doc.filename,
                            "locale": loc,
                            "reason": held
                            + (
                                " To ship it as source anyway, export this "
                                "document individually with the allow-held "
                                "override (doc export --allow-held / "
                                "?allow_held=1)."
                            ),
                        }
                    )
                    continue
                try:
                    data = export.build_translated_file(
                        session, doc, loc, report=entries, _marker_cache=_marker_cache
                    )
                except (TagExportQuarantine, export.ExportFileTargetMismatch) as exc:
                    # SKIP-AND-REPORT: exclude the quarantined doc, keep building the
                    # rest of the bundle, and record it for an honest manifest + the
                    # caller's response.
                    skipped.append(
                        {
                            "doc_id": doc.id,
                            "filename": doc.filename,
                            "locale": loc,
                            "reason": str(exc),
                        }
                    )
                    continue
                _record(doc, loc, entries)
                leaf = (
                    _batch_arc_path(doc)
                    if batch is not None
                    else file_name_for(doc.filename, doc.fmt, loc)
                )
                arc = f"{_safe(loc)}/{leaf}"
                if arc.casefold() in used_arcs:
                    base, dot, ext = arc.rpartition(".")
                    if not dot:
                        base, ext = arc, ""
                    n, cand = 0, f"{base}_{doc.id}{dot}{ext}"
                    while cand.casefold() in used_arcs:
                        n += 1
                        cand = f"{base}_{doc.id}_{n}{dot}{ext}"
                    renamed.append((doc.filename, loc, arc, cand))
                    arc = cand
                used_arcs.add(arc.casefold())
                zf.writestr(arc, data)
        if report:
            # The SILENT half of the honesty story (BUG-04). `_QUARANTINED.txt` names
            # what is MISSING from the bundle; this names what is PRESENT but partly
            # untranslated, which nothing in the archive could otherwise reveal.
            lines = [
                "Documents delivered with SUBSTITUTED segments",
                "(inline-formatting tag or placeholder damage -> the SOURCE text was exported for",
                "those units, so the file is well-formed but they are UNTRANSLATED):",
                "",
            ]
            lines += [f"  - {r['filename']} [{r['locale']}]: {r['detail']}" for r in report]
            zf.writestr("_SUBSTITUTED.txt", "\n".join(lines) + "\n")
        if skipped:
            # An in-ZIP manifest so a downloaded bundle is self-documenting about what
            # was withheld and why (no silent omission).
            lines = [
                "Documents QUARANTINED for review and EXCLUDED from this bundle",
                "(inline-formatting tag integrity could not be verified):",
                "",
            ]
            lines += [f"  - {s['filename']} [{s['locale']}]: {s['reason']}" for s in skipped]
            zf.writestr("_QUARANTINED.txt", "\n".join(lines) + "\n")
        if renamed:
            lines = [
                "Documents RENAMED in this bundle (another document already used the name):",
                "",
            ]
            lines += [f"  - {fn} [{loc}]: {old} -> {new}" for fn, loc, old, new in renamed]
            zf.writestr("_RENAMED.txt", "\n".join(lines) + "\n")
    zip_name = f"{proj_slug}_translated.zip"
    if batch is not None:
        stem = (_batch_info(docs[0]).get("archive") or "batch.zip").rsplit(".", 1)[0]
        zip_name = f"{_safe(stem)}_translated.zip"
    return buf.getvalue(), zip_name, "application/zip", skipped


def export_sdl_return_package(session: Session, project_id: int, package_guid=None, **kw):
    """One Trados return package (.sdlrpx) for a project's package documents
    (app/services/sdl_package.py; reached through this module so the kernel facade
    surface does not grow). Returns (bytes, filename, skipped)."""
    from app.services import sdl_package

    return sdl_package.build_return_package(session, project_id, package_guid, **kw)
