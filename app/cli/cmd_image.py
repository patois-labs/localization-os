"""`image` command group: upload / list / show / translate / render / export.

The CLI half of the Images (Beta) module (SPEC-image-localization Phase 1) -
exact parity with the /images web surface, thin shells over
app.services.image_localization (the shared seam, so web and CLI cannot drift).

All in-process (require_local); `image translate` runs SYNCHRONOUSLY like
`doc translate`. Gated by the same `image_localization_enabled` kill switch as
the web routes - every verb refuses cleanly when the module is off.
ASCII-only output (Windows consoles).

Documented in: docs/architecture/image-localization.md
"""

import os

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    default_out_in_cwd,
    get_scoped_or_404,
    op_org_id,
    remote_mode,
    require_local,
    require_org_scope,
)


def register(subparsers) -> None:
    image = subparsers.add_parser(
        "image", help="Image localization: upload, detect, translate, render, export."
    )
    sub = image.add_subparsers(dest="image_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "upload",
        help=(
            "Upload an image (svg/png/jpg/jpeg/tif/tiff/bmp/gif/webp) into a project; "
            "detection runs at ingest."
        ),
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("file", help="path to the image file.")
    p.set_defaults(func=_image_upload)

    p = sub.add_parser("list", help="List a project's image documents.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_image_list)

    p = sub.add_parser("show", help="Show an image's regions, translations and flags per locale.")
    p.add_argument("doc_id", type=bounded_id, help="image document id.")
    p.add_argument("--locale", help="only this target locale (default: first).")
    p.set_defaults(func=_image_show)

    p = sub.add_parser(
        "translate",
        help="Translate an image's regions into a locale (synchronous; TM/glossary/refusal apply).",
    )
    p.add_argument("doc_id", type=bounded_id, help="image document id.")
    p.add_argument("--locale", required=True, help="target locale.")
    p.set_defaults(func=_image_translate)

    p = sub.add_parser(
        "render",
        help="Re-render the localized image and report "
        "per-region fit flags (writes nothing unless -o).",
    )
    p.add_argument("doc_id", type=bounded_id, help="image document id.")
    p.add_argument("--locale", required=True, help="target locale to render.")
    p.add_argument("-o", "--out", help="optionally save the rendered preview here.")
    p.add_argument("--force", action="store_true", help="overwrite -o if it exists.")
    p.set_defaults(func=_image_render)

    p = sub.add_parser("export", help="Export the localized image file.")
    p.add_argument("doc_id", type=bounded_id, help="image document id.")
    p.add_argument("--locale", required=True, help="target locale to export.")
    p.add_argument(
        "-o", "--out", help="destination path (default: <stem>.<locale>.<fmt> in the CWD)."
    )
    p.add_argument("--force", action="store_true", help="overwrite the file if it exists.")
    p.set_defaults(func=_image_export)


# --- helpers -----------------------------------------------------------------
def _flag_gate():
    """Layer-2 CLI kill switch (the message lives in the registry; see feature_gate)."""
    from ._common import feature_gate

    feature_gate("images")


def _get_image_doc(session, doc_id, org_id=None):
    from app.kernel import image_localization as il
    from app.models import Document, Project

    doc = session.get(Document, doc_id)
    if doc is None or not il.is_image_fmt(doc.fmt):
        raise CliError(f"no such image document: {doc_id}", EXIT_USAGE)
    if org_id is not None:
        project = session.get(Project, doc.project_id)
        if project is None or getattr(project, "org_id", None) != org_id:
            raise CliError(f"no such image document: {doc_id}", EXIT_USAGE)
    return doc


def _region_payload(rows):
    return [
        {
            "id": r["region"]["id"],
            "reading_order_index": r["region"]["reading_order_index"],
            "bbox": r["region"]["bbox_axis_aligned"],
            "confidence": r["confidence"],
            "source": r["region"].get("source", ""),
            "source_text": r["source_text"],
            "target_text": r["target_text"],
            "translated": r["translated"],
            "overflow": r["overflow"],
            # #125 (MED): the egress refusal on this region, surfaced to the CLI JSON the
            # same way it rides in the web review + /api/images row model. "" = no refusal.
            "vlm_error": r.get("vlm_error", ""),
            "segment_ids": [s.id for s in r["segments"]],
        }
        for r in rows
    ]


# --- verbs ---------------------------------------------------------------------
def _image_upload(args, emit) -> int:
    if remote_mode(args):
        # The server enforces the image_localization_enabled kill switch (404); the
        # remote arm must NOT call the local _flag_gate (it reads local settings).
        return _remote_impl.image_upload(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import image_localization as il
    from app.models import Project

    actor = getattr(args, "actor", None) or "cli"
    if not os.path.isfile(args.file):
        raise CliError(f"file not found: {args.file}", EXIT_USAGE)
    with open(args.file, "rb") as fh:
        data = fh.read()
    from app import config

    if len(data) > config.MAX_UPLOAD_BYTES:
        raise CliError(
            f"file exceeds the upload cap ({config.MAX_UPLOAD_BYTES} bytes).", EXIT_USAGE
        )
    filename = os.path.basename(args.file)
    oid = op_org_id(args)
    with _session() as session:
        project = get_scoped_or_404(
            session.get(Project, args.project_id), args.project_id, "project", oid
        )
        from app.kernel import connected_projects

        if connected_projects.is_connected(project):
            raise CliError(connected_projects.UPLOAD_REFUSED, EXIT_USAGE)
        try:
            doc = il.ingest_image(session, project, filename, data, actor=actor)
        except il.ImageLocalizationError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        except Exception as exc:
            raise CliError(f"could not ingest image: {exc}") from exc
        skel = doc.skeleton or {}
        n = len(skel.get("regions") or skel.get("units") or [])
        detector = skel.get("detector", "none")
        emit.result(
            f"Uploaded image #{doc.id}: {doc.filename} ({skel.get('kind', '?')}, "
            f"{n} region(s), detector={detector}) into project #{project.id}.",
            {
                "ok": True,
                "id": doc.id,
                "filename": doc.filename,
                "fmt": doc.fmt,
                "kind": skel.get("kind"),
                "regions": n,
                "detector": detector,
                "project_id": project.id,
            },
        )
    return 0


def _image_list(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    _flag_gate()
    from sqlmodel import select

    from app.kernel import image_localization as il
    from app.models import Document, Project

    oid = op_org_id(args)
    with _session() as session:
        if oid is not None:
            project = session.get(Project, args.project_id)
            if project is None or getattr(project, "org_id", None) != oid:
                raise CliError(f"no such project: {args.project_id}", EXIT_USAGE)
        docs = session.exec(
            select(Document)
            .where(Document.project_id == args.project_id, Document.fmt.in_(list(il.IMAGE_EXTS)))
            .order_by(Document.created_at.desc())
        ).all()
        rows = []
        for d in docs:
            skel = d.skeleton or {}
            n = len(skel.get("regions") or skel.get("units") or [])
            rows.append((d.id, d.filename, skel.get("kind", d.fmt), n, d.status))
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "images": [
                        {
                            "id": d.id,
                            "filename": d.filename,
                            "fmt": d.fmt,
                            "kind": (d.skeleton or {}).get("kind"),
                            "regions": len(
                                (d.skeleton or {}).get("regions")
                                or (d.skeleton or {}).get("units")
                                or []
                            ),
                            "status": d.status,
                        }
                        for d in docs
                    ],
                }
            )
        else:
            emit.table(rows, headers=("id", "filename", "kind", "regions", "status"))
    return 0


def _image_show(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    _flag_gate()
    from sqlmodel import select

    from app.kernel import image_localization as il
    from app.models import Segment

    with _session() as session:
        doc = _get_image_doc(session, args.doc_id, op_org_id(args))
        locales = sorted(
            {
                s.target_locale
                for s in session.exec(select(Segment).where(Segment.document_id == doc.id)).all()
            }
        )
        locale = args.locale or (locales[0] if locales else None)
        if args.locale and args.locale not in locales:
            raise CliError(f"locale not present on image: {args.locale}", EXIT_USAGE)
        rows = il.region_rows(session, doc, locale) if locale else []
        skel = doc.skeleton or {}
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "id": doc.id,
                    "filename": doc.filename,
                    "fmt": doc.fmt,
                    "kind": skel.get("kind"),
                    "locales": locales,
                    "locale": locale,
                    "regions": _region_payload(rows),
                }
            )
        else:
            emit.say(
                f"Image #{doc.id}: {doc.filename} ({skel.get('kind', doc.fmt)}, "
                f"locales: {', '.join(locales) or 'none'})"
            )
            first_vlm_error = next((r["vlm_error"] for r in rows if r.get("vlm_error")), "")
            if first_vlm_error:
                # #125 (MED): make the egress refusal visible on the CLI, mirroring OCR's
                # "[OCR error: ...]"; the per-row ENGINE-BLOCKED flag marks each affected row.
                emit.say(
                    f"Vision engine target refused by egress policy; low-confidence regions "
                    f"were not re-read. {first_vlm_error}"
                )
            table = []
            for r in rows:
                flags = []
                if r["overflow"]:
                    flags.append("OVERFLOW")
                if r["low_confidence"]:
                    flags.append("LOW-CONF")
                if r.get("vlm_error"):
                    flags.append("ENGINE-BLOCKED")
                table.append(
                    (
                        r["region"]["reading_order_index"] + 1,
                        _clip(r["source_text"]),
                        _clip(r["target_text"]),
                        f"{r['confidence']:.2f}",
                        ",".join(flags) or "-",
                    )
                )
            emit.table(table, headers=("#", "source", "target", "conf", "flags"))
    return 0


def _clip(s: str, n: int = 40) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _image_translate(args, emit) -> int:
    if remote_mode(args):
        # Server enforces the kill switch (404); no local _flag_gate on the remote arm.
        return _remote_impl.image_translate(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import image_localization as il

    actor = getattr(args, "actor", None) or "cli"
    from app.kernel.providers import DeadEngineError

    with _session() as session:
        doc = _get_image_doc(session, args.doc_id, op_org_id(args))
        try:
            stats = il.translate_image(session, doc, args.locale, actor=actor)
        except il.ImageLocalizationError as exc:
            raise CliError(str(exc)) from exc
        except DeadEngineError as exc:
            # A stored engine:<id> ref went dead: FAIL LOUD, never mock-substitute
            # (parity with `doc translate`).
            raise CliError(str(exc)) from exc
        emit.result(
            f"Translated image #{doc.id} into {args.locale}: "
            f"{stats.get('translated', 0)} region segment(s) "
            f"({stats.get('from_segment_tm', 0)} from TM, "
            f"{stats.get('provider_calls', 0)} provider call(s), "
            f"{stats.get('errors', 0)} flagged/error).",
            {"ok": True, "doc_id": doc.id, "locale": args.locale, "stats": stats},
        )
    return 0


def _render_common(args, emit, *, audit_export: bool) -> int:
    from app.kernel import audit
    from app.kernel import image_localization as il

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        doc = _get_image_doc(session, args.doc_id, op_org_id(args))
        from sqlmodel import select

        from app.models import Segment

        locales = {
            s.target_locale
            for s in session.exec(select(Segment).where(Segment.document_id == doc.id)).all()
        }
        if args.locale not in locales:
            raise CliError(
                f"locale {args.locale!r} not on image #{doc.id} "
                f"(present: {', '.join(sorted(locales)) or 'none'}).",
                EXIT_USAGE,
            )
        try:
            data, flags = il.render_localized(session, doc, args.locale)
        except il.ImageLocalizationError as exc:
            raise CliError(str(exc)) from exc
        out_path = None
        if getattr(args, "out", None) or audit_export:
            # Issue #142, same defence in depth as `doc export`: safe_output_name
            # basenames the STORED filename and bounds the composed component's length,
            # default_out_in_cwd proves the resolved path is in the CWD, and an OSError
            # at open() becomes a clean one-line error. An explicit -o is the operator's
            # own choice and is NOT constrained.
            from app.kernel import content as _content

            out_path = args.out or default_out_in_cwd(
                _content.safe_output_name(doc.filename, args.locale, doc.fmt)
            )
            if os.path.exists(out_path) and not args.force:
                raise CliError(
                    f"destination exists: {out_path} (pass --force to overwrite).", EXIT_USAGE
                )
            try:
                with open(out_path, "wb") as fh:
                    fh.write(data)
            except OSError as exc:
                raise CliError(f"could not write {out_path}: {exc}", EXIT_USAGE) from exc
        if audit_export:
            audit.log(
                actor=actor,
                action="document.export",
                target=f"doc {doc.id} {args.locale}",
                customer_id=audit.customer_for_project(doc.project_id),
            )
        overflow = [f for f in flags if f.get("overflow")]
        human = (
            f"Rendered image #{doc.id} ({args.locale}): {len(flags)} region(s) "
            f"drawn, {len(overflow)} OVERFLOW flag(s)"
            + (f" -> {out_path} ({len(data)} bytes)." if out_path else ".")
        )
        emit.result(
            human,
            {
                "ok": True,
                "doc_id": doc.id,
                "locale": args.locale,
                "bytes": len(data),
                "path": out_path,
                "flags": flags,
                "overflow_count": len(overflow),
            },
        )
    return 0


def _image_render(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    _flag_gate()
    return _render_common(args, emit, audit_export=False)


def _image_export(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    _flag_gate()
    return _render_common(args, emit, audit_export=True)
