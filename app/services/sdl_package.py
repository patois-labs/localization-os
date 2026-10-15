"""Trados Studio packages at the project level: import a project package (.sdlppx) as one
document per target SDLXLIFF, create a project from one, and build one return package
(.sdlrpx) for every document of a package.

The format work (zip reading, .sdlproj parsing, the per-document member blob, the
return-package build) is `app/filters/sdl_package.py`; this module only talks to the
database. Every surface calls these functions: the web upload, the new-project form's
Trados package field and the project page's return-package link; `POST /api/documents`
(with `sdl_new_project=1` to create the project) and
`GET /api/projects/{id}/export?sdl_return_package=1`; and the CLI `doc upload`,
`project create --from-sdl-package` and `project export --sdl-return-package`.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import logging
import os
from uuid import uuid4

from sqlmodel import Session, select

from app.filters import sdl_package as pkg
from app.filters.base import InputLimitError
from app.models import Document, TargetLocale

_LOG = logging.getLogger("loc_tms.sdl_package")

FMT = "sdlrpx"


def is_package_upload(filename: str, data: bytes) -> bool:
    """A raw Trados package (not one of our stored member blobs) by name and content."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in pkg.PACKAGE_EXTS and pkg.member_meta(data) is None


def inspect(data: bytes, filename: str = "") -> dict:
    """The package summary every surface shows before importing (language pair, the
    documents it would create, the skipped files)."""
    return pkg.inspect(data, os.path.basename(filename or "")).as_dict()


def _doc_filename(path: str, locale: str, multi: bool) -> str:
    base = pkg._norm(path).rsplit("/", 1)[-1]
    for ext in (".sdlxliff", ".sdlxlf"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
    return f"{locale}_{base}.{FMT}" if multi else f"{base}.{FMT}"


def ingest_package(
    session: Session,
    project,
    filename: str,
    data: bytes,
    mode: str,
    created_by: str = "",
    context: dict | None = None,
) -> Document:
    """Create one document per target SDLXLIFF of the package that the project translates
    into. Returns the FIRST document; `import_summary(session, doc)` lists them all plus
    the skipped files. All or nothing: a refused file removes the documents already made.
    """
    from app.services import content
    from app.services.ingest import ingest_document

    info = pkg.inspect(data, filename)
    project_targets = {
        (tl.locale or "").lower(): tl.locale
        for tl in session.exec(select(TargetLocale).where(TargetLocale.project_id == project.id))
    }
    usable = [t for t in info.targets if t["locale"].lower() in project_targets]
    not_here = sorted({t["locale"] for t in info.targets} - {t["locale"] for t in usable})
    if not usable:
        raise InputLimitError(
            "This Trados package translates into "
            f"{', '.join(info.target_locales)}, but this project's target languages are "
            f"{', '.join(sorted(project_targets.values())) or 'not set'}. Add those languages "
            "to the project, or create a new project from the package."
        )
    skipped = list(info.skipped)
    for t in info.targets:
        if t["locale"] in not_here and len(skipped) < pkg.MAX_LISTED:
            skipped.append(
                {
                    "path": t["path"],
                    "reason": f"{t['locale']} is not a target language of this project",
                }
            )
    warnings = []
    if (project.source_locale or "").lower() != info.source_locale.lower():
        warnings.append(
            {
                "kind": "sdl_package_source",
                "message": (
                    f"The package's source language is {info.source_locale}; this project's "
                    f"is {project.source_locale}. Segments follow the package's SDLXLIFF."
                ),
            }
        )
    batch = uuid4().hex
    multi = len(info.target_locales) > 1
    docs: list[Document] = []
    zf = pkg.open_zip(data)
    try:
        for t in usable:
            blob = pkg.member_blob(data, info, t, zf=zf)
            doc = ingest_document(
                session,
                project,
                _doc_filename(t["path"], t["locale"], multi),
                blob,
                mode,
                created_by=created_by,
                context=context,
            )
            skel = dict(doc.skeleton or {})
            meta = dict(skel.get("sdl_package") or {})
            meta.update(
                {
                    "batch": batch,
                    "locale": project_targets[t["locale"].lower()],
                    "project_name": info.project_name,
                    "source_locale": info.source_locale,
                }
            )
            if not docs:  # the import's listing rides on the first document only
                meta["skipped"] = skipped
                meta["skipped_total"] = info.skipped_total + (len(info.targets) - len(usable))
            skel["sdl_package"] = meta
            if warnings:
                skel["file_warnings"] = list(skel.get("file_warnings") or []) + warnings
            doc.skeleton = skel
            session.add(doc)
            session.commit()
            session.refresh(doc)
            docs.append(doc)
    except Exception:
        session.rollback()
        for d in docs:
            try:
                content.delete_document(
                    session, d.id, actor=created_by or "ingest", purge_exports=False
                )
            except Exception:  # cleanup is best effort; the original error is what matters
                _LOG.warning("could not remove package document %s", d.id, exc_info=True)
        raise
    finally:
        zf.close()
    return docs[0]


def import_summary(session: Session, doc: Document) -> dict | None:
    """The documents and skipped files of the package import `doc` came from, or None
    for any other document."""
    meta = (doc.skeleton or {}).get("sdl_package") or {}
    batch = meta.get("batch")
    if not batch:
        return None
    docs = [
        d
        for d in session.exec(
            select(Document).where(Document.project_id == doc.project_id, Document.fmt == FMT)
        ).all()
        if ((d.skeleton or {}).get("sdl_package") or {}).get("batch") == batch
    ]
    docs.sort(key=lambda d: d.id)
    first = (docs[0].skeleton or {}).get("sdl_package", {}) if docs else meta
    return {
        "package": first.get("package", ""),
        "package_guid": first.get("package_guid", ""),
        "source_locale": first.get("source_locale", ""),
        "documents": [
            {
                "id": d.id,
                "filename": d.filename,
                "locale": ((d.skeleton or {}).get("sdl_package") or {}).get("locale", ""),
                "path": ((d.skeleton or {}).get("sdl_package") or {}).get("member", ""),
            }
            for d in docs
        ],
        "skipped": list(first.get("skipped") or []),
        "skipped_total": int(first.get("skipped_total") or 0),
    }


def create_project_from_package(
    session: Session,
    data: bytes,
    filename: str,
    *,
    name: str = "",
    mode: str = "segmented",
    created_by: str = "",
    **create_kwargs,
):
    """A new project with the package's source and target languages, then the package
    imported into it. Returns (project, first document). `create_kwargs` pass through to
    `content.project_create` (customer_id, creator, bound_customer_id, org_id, ...). A
    refused import deletes the project again, so nothing is left behind."""
    from app.services import content

    info = pkg.inspect(data, filename)
    project = content.project_create(
        session,
        (name or "").strip()
        or info.project_name
        or os.path.splitext(filename)[0]
        or "Trados package",
        source_locale=info.source_locale,
        target_locales=" ".join(info.target_locales),
        created_by=created_by,
        **create_kwargs,
    )
    if project is None:
        raise ValueError("project name is required")
    try:
        doc = ingest_package(session, project, filename, data, mode, created_by=created_by)
    except Exception:
        session.rollback()
        try:
            content.delete_project(session, project.id, actor=created_by or "ingest")
        except Exception:
            _LOG.warning("could not remove project %s after a failed import", project.id)
        raise
    return project, doc


def package_documents(session: Session, project_id: int) -> dict:
    """{package_guid: [documents]} for the project's Trados package documents."""
    out: dict = {}
    for d in session.exec(
        select(Document).where(Document.project_id == project_id, Document.fmt == FMT)
    ).all():
        meta = (d.skeleton or {}).get("sdl_package") or {}
        out.setdefault(meta.get("package_guid") or "", []).append(d)
    for docs in out.values():
        docs.sort(key=lambda d: d.id)
    return out


def build_return_package(
    session: Session,
    project_id: int,
    package_guid: str | None = None,
    *,
    allow_held: bool = False,
) -> tuple[bytes, str, list]:
    """One .sdlrpx for every document of one package in the project: the original
    .sdlproj (PackageType switched to ReturnPackage) plus each document's rebuilt target
    SDLXLIFF under its original path, in the package's own member order. A document that
    is held, gated or quarantined is left out and reported in `skipped`, exactly as the
    project ZIP export does. Returns (bytes, filename, skipped). ValueError when the
    project has no package documents, several packages and none named, or nothing to
    deliver."""
    from app.services import delivery, export, tm_delivery
    from app.services.export import TagExportQuarantine

    groups = package_documents(session, project_id)
    if not groups:
        raise ValueError("this project has no documents imported from a Trados package")
    if package_guid:
        docs = groups.get(package_guid)
        if not docs:
            raise ValueError("that Trados package is not in this project")
    elif len(groups) == 1:
        package_guid, docs = next(iter(groups.items()))
    else:
        raise ValueError(
            "this project holds documents from more than one Trados package; name one: "
            + ", ".join(sorted(g or "(no GUID)" for g in groups))
        )
    proj_part = None
    members: dict = {}
    comment = ""
    skipped: list = []
    package_name = ""
    for doc in docs:
        meta = (doc.skeleton or {}).get("sdl_package") or {}
        loc = meta.get("locale") or ""
        package_name = package_name or meta.get("package") or ""
        # The same three guards, in the same order, as the project ZIP export.
        reason = export.gate_held_reason(session, doc, loc)
        gate = tm_delivery.empty_gate_decision(session, doc=doc, locale=loc)
        if reason is None and gate == tm_delivery.HOLD:
            reason = "empty targets are held for approval (doc deliver-approve)"
        if reason is None and not allow_held and gate != tm_delivery.RELEASE_EMPTIES:
            reason = export.held_locale_reason(session, doc, loc)
        if reason is not None:
            skipped.append(
                {"doc_id": doc.id, "filename": doc.filename, "locale": loc, "reason": reason}
            )
            continue
        try:
            built = export.build_translated_file(session, doc, loc)
        except TagExportQuarantine as exc:
            skipped.append(
                {"doc_id": doc.id, "filename": doc.filename, "locale": loc, "reason": str(exc)}
            )
            continue
        proj, rest, built_comment = pkg.return_package_parts(built)
        if proj is None:
            raise ValueError(f"document {doc.id} did not rebuild as a Trados return package")
        if proj_part is None:
            proj_part, comment = proj, built_comment
        for m_info, m_bytes in rest:
            members[m_info.filename] = (m_info, m_bytes)
    if proj_part is None:
        raise ValueError(
            "nothing to return: every document of this package is held or quarantined ("
            + "; ".join(f"{s['filename']}: {s['reason']}" for s in skipped)
            + ")"
        )
    parts = [proj_part] + [members[k] for k in sorted(members, key=lambda n: n.lower())]
    data = pkg.write_package(parts, comment)
    stem = os.path.splitext(package_name)[0] if package_name else f"project-{project_id}"
    return data, f"{delivery._safe(stem)}.sdlrpx", skipped
