"""Document lifecycle verbs and top-level `doc` command registration.

Owns upload, list, translate, export, delivery approval, rename, and delete.
Transitional compatibility shim for one release: re-exports `_doc_id`, `_get_doc`
from doc_common and `_doc_apply_tm`, `_doc_approve_segment`, `_doc_paragraphs`,
`_doc_revert_paragraph`, `_doc_set_paragraph`, `_doc_show`, `_doc_unlock_segment`,
`_para_view`, `_register_apply_tm`, `_register_approve_segment`,
`_register_paragraphs`, `_register_revert_paragraph`, `_register_set_paragraph`,
`_register_show`, `_register_unlock_segment`, `register_doc_inspect_commands`
from doc_inspect. The compatibility names are listed in `__all__`.

All in-process. `doc translate` runs SYNCHRONOUSLY in the calling thread — it
consumes translate.pretranslate_stream() directly rather than jobs.submit()ing
and exiting, because the one-shot CLI process has no daemon worker threads to
pick a queued job up (they die with the process). That keeps the CLI honest:
when the command returns, the translation is really done.

--remote: upload / list / show / translate / export and the three #42 FULL_TEXT recovery
verbs (paragraphs / revert-paragraph / set-paragraph) dispatch to `_remote_impl`; the
remaining in-process-only verbs call require_local() so a --remote invocation fails
cleanly (exit 2) rather than silently running against the local host.

Documented in: docs/architecture/translation-pipeline.md
"""

import functools
import os

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    default_out_in_cwd,
    get_scoped_or_404,
    op_org_id,
    remote_mode,
    require_local,
    require_org_scope,
)
from .doc_common import (
    _doc_id,
    _get_doc,
)
from .doc_inspect import (
    _doc_apply_tm,
    _doc_approve_segment,
    _doc_paragraphs,
    _doc_revert_paragraph,
    _doc_set_paragraph,
    _doc_show,
    _doc_unlock_segment,
    _para_view,
    _register_apply_tm,
    _register_approve_segment,
    _register_paragraphs,
    _register_revert_paragraph,
    _register_set_paragraph,
    _register_show,
    _register_unlock_segment,
    register_doc_inspect_commands,
)


# --- registration ------------------------------------------------------------
def _register_upload(sub) -> None:
    p = sub.add_parser(
        "upload",
        help="Upload a file into a project as a document "
        "(refused over MAX_DOCUMENT_SEGMENTS, default 50000 segments).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("file", help="path to the source file (docx, txt, html, ...).")
    p.add_argument(
        "--mode",
        default="segmented",
        choices=["segmented", "full_text"],
        help="translation unit granularity (default segmented).",
    )
    p.add_argument(
        "--context-json",
        default="",
        help="optional TranslationContext package (JSON object) stored on "
        "the document (SPEC-context-metadata phase 1).",
    )
    p.add_argument(
        "--columns",
        default=None,
        help="CSV/TSV/XLSX column mapping, e.g. "
        "'source=English,target.de=German,target.fr=fr,key=id,context=notes' "
        "(also header=yes|no, delimiter=comma|semicolon|tab|pipe). A column is a header "
        "name or a 1-based number. CSV/TSV without it: read from the header row.",
    )
    p.set_defaults(func=_doc_upload)


def _register_list(sub) -> None:
    p = sub.add_parser("list", help="List documents in a project.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_doc_list)


def _register_translate(sub) -> None:
    p = sub.add_parser(
        "translate",
        help="MT pre-translate a document into a locale (runs synchronously in-process).",
    )
    p.add_argument(
        "doc_id",
        type=functools.partial(_doc_id, subcommand="doc translate"),
        help="document id (upload the file first).",
    )
    p.add_argument("--locale", required=True, help="target locale to translate into.")
    p.set_defaults(func=_doc_translate)


def _register_export(sub) -> None:
    p = sub.add_parser("export", help="Export a translated document (format-preserving).")
    p.add_argument(
        "doc_id", type=functools.partial(_doc_id, subcommand="doc export"), help="document id."
    )
    p.add_argument("--locale", required=True, help="target locale to export.")
    p.add_argument(
        "-o", "--out", help="destination path (default: <stem>.<locale>.<fmt> in the CWD)."
    )
    p.add_argument("--force", action="store_true", help="overwrite the file if it exists.")
    p.add_argument(
        "--allow-source-fallback",
        action="store_true",
        help="FULL_TEXT only (issue #42): when a paragraph's inline "
        "formatting tokens no longer match its source, export the "
        "SOURCE text for that paragraph instead of quarantining the "
        "whole document. The paragraph ships UNTRANSLATED and is "
        "reported. Refused above export_source_fallback_max_share, and "
        "never applies to a paragraph-COUNT desync.",
    )
    p.add_argument(
        "--allow-held",
        action="store_true",
        help="Export a HELD locale anyway (chip task_9c665111). A locale with "
        "no committed translation is refused by default (the file would "
        "be entirely source text that reads as delivered); pass this to "
        "ship the source-filled file explicitly.",
    )
    p.add_argument(
        "--as-fmt",
        choices=("po",),
        default=None,
        help="deliver in another format: po exports a gettext .mo upload as its .po source.",
    )
    p.set_defaults(func=_doc_export)


def _register_empties(sub) -> None:
    p = sub.add_parser(
        "empties",
        help="List a document's untranslated (empty) targets for a locale that the "
        "delivery empty gate is holding back from delivery.",
    )
    p.add_argument(
        "doc_id", type=functools.partial(_doc_id, subcommand="doc empties"), help="document id."
    )
    p.add_argument("--locale", required=True, help="target locale.")
    p.set_defaults(func=_doc_empties)


def _register_deliver_approve(sub) -> None:
    p = sub.add_parser(
        "deliver-approve",
        help="Approve delivering a held (doc, locale)'s empty targets as empty "
        "strings (audited; not bypassable with --allow-held). Refused while a run "
        "is still in flight over the (document, locale).",
    )
    p.add_argument(
        "doc_id",
        type=functools.partial(_doc_id, subcommand="doc deliver-approve"),
        help="document id.",
    )
    p.add_argument("--locale", required=True, help="target locale.")
    p.set_defaults(func=_doc_deliver_approve)


def _register_deliver_revoke(sub) -> None:
    p = sub.add_parser(
        "deliver-revoke",
        help="Revoke a prior empty-delivery approval for a (doc, locale) -> re-held.",
    )
    p.add_argument(
        "doc_id",
        type=functools.partial(_doc_id, subcommand="doc deliver-revoke"),
        help="document id.",
    )
    p.add_argument("--locale", required=True, help="target locale.")
    p.set_defaults(func=_doc_deliver_revoke)


def _register_rename(sub) -> None:
    p = sub.add_parser(
        "rename",
        help="Rename a document's filename "
        "(storage is id-keyed, so the upload blob is unaffected).",
    )
    p.add_argument(
        "doc_id", type=functools.partial(_doc_id, subcommand="doc rename"), help="document id."
    )
    p.add_argument("filename", help="the new filename.")
    p.set_defaults(func=_doc_rename)


def _register_delete(sub) -> None:
    p = sub.add_parser(
        "delete",
        help="Delete a document + its upload blob + segments "
        "(DESTRUCTIVE; does NOT touch the project's shared TM).",
    )
    p.add_argument(
        "doc_id", type=functools.partial(_doc_id, subcommand="doc delete"), help="document id."
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_doc_delete)
    p.add_argument(
        "--privacy-erasure",
        action="store_true",
        help=(
            "Also erase Lab snapshots, results and labels, and the translation memory rows "
            "from this document."
        ),
    )


def register(subparsers) -> None:
    doc = subparsers.add_parser("doc", help="Documents (upload, list, show, translate, export).")
    sub = doc.add_subparsers(dest="doc_cmd", metavar="<subcommand>")
    sub.required = True

    _register_upload(sub)
    _register_list(sub)
    _register_show(sub)
    _register_translate(sub)
    _register_export(sub)

    # --- The delivery empty gate (Batch 15 T2) -------------------------------
    # #105 round-1 MED-2: the gate keys on the durable `tm_match` MARKER, not on "a TM-only
    # workflow" -- a `tm_match -> llm_agent` run is held exactly the same way -- so the help
    # names the CONDITION (empties held back from delivery), not a workflow shape it cannot know.
    _register_empties(sub)
    _register_deliver_approve(sub)
    _register_deliver_revoke(sub)

    # --- FULL_TEXT paragraph view + recovery (issue #42) ---------------------
    # Thin callers of app.services.fulltext_paragraphs, the SAME seam the web page
    # (/documents/{id}/paragraphs) and /api call.
    _register_paragraphs(sub)
    _register_revert_paragraph(sub)
    _register_set_paragraph(sub)

    _register_apply_tm(sub)
    _register_unlock_segment(sub)
    _register_approve_segment(sub)

    _register_rename(sub)
    _register_delete(sub)


# --- doc upload --------------------------------------------------------------
def _doc_upload(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_upload(args, emit)
    require_org_scope(args, emit)
    from app.kernel.ingest import NotADocumentProject, UnsupportedFormat, ingest_document
    from app.models import Project

    actor = getattr(args, "actor", None) or "cli"
    if not os.path.isfile(args.file):
        raise CliError(f"file not found: {args.file}", EXIT_USAGE)
    with open(args.file, "rb") as fh:
        data = fh.read()
    filename = os.path.basename(args.file)
    oid = op_org_id(args)
    # Optional context package (SPEC-context-metadata phase 1): validate+sanitize via
    # the shared seam so the CLI rejects a bad package with a clean usage error and
    # never ingests, exactly like the API 422.
    ctx_pkg = None
    if (getattr(args, "context_json", "") or "").strip():
        import json as _json

        from app import config
        from app.kernel import context_pkg

        # Adversarial review 2026-07-22, Findings A+B (identical fix to the API's
        # api_upload_document -- see app/web/api.py): reject an oversized
        # --context-json BEFORE json.loads (a deeply-nested payload otherwise blows
        # the parser's internal recursion handling -- an uncaught RecursionError
        # traceback instead of a clean usage error), and widen the except to
        # RecursionError as defense-in-depth (a char-cheap shape like [[[...]]] can
        # still exceed recursion while staying under the length cap).
        if len(args.context_json) > config.CONTEXT_JSON_MAX_CHARS:
            raise CliError(
                f"--context-json is too large (limit {config.CONTEXT_JSON_MAX_CHARS} chars).",
                EXIT_USAGE,
            )
        try:
            raw = _json.loads(args.context_json)
        except (ValueError, TypeError, RecursionError) as exc:
            raise CliError(f"--context-json is not valid JSON: {exc}", EXIT_USAGE) from exc
        try:
            ctx_pkg = context_pkg.validate_and_sanitize(raw)
        except context_pkg.ContextError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    with _session() as session:
        project = get_scoped_or_404(
            session.get(Project, args.project_id), args.project_id, "project", oid
        )
        from app.kernel import connected_projects

        if connected_projects.is_connected(project):
            # A connected project takes its content from its source, never from uploads.
            raise CliError(connected_projects.UPLOAD_REFUSED, EXIT_USAGE)
        from app.filters.zip_batch import is_batch_upload

        if is_batch_upload(filename):
            return _doc_upload_zip_batch(args, emit, session, project, filename, data, ctx_pkg)
        try:
            doc = ingest_document(
                session,
                project,
                filename,
                data,
                args.mode,
                created_by=actor,
                context=ctx_pkg,
                columns=getattr(args, "columns", None),
            )
        except NotADocumentProject as exc:
            # A-F4: a key project takes no documents on ANY surface.
            raise CliError(str(exc), EXIT_USAGE) from exc
        except UnsupportedFormat as exc:
            raise CliError(f"unsupported file format: {exc}", EXIT_USAGE) from exc
        except Exception as exc:
            raise CliError(f"could not ingest file: {exc}") from exc
        warnings = (doc.skeleton or {}).get("file_warnings", [])
        if warnings and not emit.as_json:
            for w in warnings:  # every warning, #687's target-language note included
                emit.say(w["message"])
        pkg = None
        if doc.fmt == "sdlrpx":  # a Trados package: one document per target SDLXLIFF
            from app.kernel.ingest import sdl_package_summary

            pkg = sdl_package_summary(session, doc)
            if pkg and not emit.as_json:
                for d in pkg["documents"]:
                    emit.say(
                        f"  document #{d['id']}: {d['filename']} ({d['locale']}) <- {d['path']}"
                    )
                for s in pkg["skipped"]:
                    emit.say(f"  skipped {s['path']}: {s['reason']}")
        emit.result(
            f"Uploaded #{doc.id}: {doc.filename} ({doc.fmt}, {doc.mode}) "
            f"into project #{project.id}.",
            {
                "ok": True,
                "id": doc.id,
                "filename": doc.filename,
                "fmt": doc.fmt,
                "mode": doc.mode,
                "project_id": project.id,
                **({"file_warnings": warnings} if warnings else {}),
                **({"sdl_package": pkg} if pkg else {}),
            },
        )
    return 0


def emit_zip_batch_result(emit, result: dict, project_id: int) -> int:
    """Print a ZIP batch upload result (shared by the in-process and --remote arms):
    each created document, then every skipped / failed member with its reason."""
    if not emit.as_json:
        for d in result["documents"]:
            emit.say(f"  #{d['id']}  {d['path']}  ({d['fmt']}, {d['segments']} segments)")
        for s in result["skipped"]:
            emit.say(f"  skipped  {s['path']}: {s['reason']}")
        for f in result["failed"]:
            emit.say(f"  failed   {f['path']}: {f['error']}")
    if not result["documents"]:
        raise CliError(
            "no document was created from the ZIP archive "
            f"({len(result['skipped'])} skipped, {len(result['failed'])} failed)."
        )
    emit.result(
        f"Uploaded ZIP batch {result['batch_id']} ({result['archive']}) into project "
        f"#{project_id}: {len(result['documents'])} document(s), "
        f"{len(result['skipped'])} skipped, {len(result['failed'])} failed. "
        f"Download it back: loc-tms project export {project_id} --batch {result['batch_id']}",
        {"ok": True, "project_id": project_id, **result},
    )
    return 0


def _doc_upload_zip_batch(args, emit, session, project, filename, data, ctx_pkg) -> int:
    """Batch 13: `doc upload <pid> files.zip` -> one document per supported member."""
    from app.filters.base import InputLimitError
    from app.kernel.ingest import NotADocumentProject, UnsupportedFormat, ingest_zip_batch

    try:
        result = ingest_zip_batch(
            session,
            project,
            filename,
            data,
            args.mode,
            created_by=getattr(args, "actor", None) or "cli",
            context=ctx_pkg,
        )
    except NotADocumentProject as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    except UnsupportedFormat as exc:
        raise CliError(f"unsupported file format: {exc}", EXIT_USAGE) from exc
    except InputLimitError as exc:
        raise CliError(f"could not ingest file: {exc}") from exc
    return emit_zip_batch_result(emit, result, project.id)


# --- doc list ----------------------------------------------------------------
def _doc_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_list(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.models import Document, Project

    oid = op_org_id(args)
    with _session() as session:
        if oid is not None:
            project = session.get(Project, args.project_id)
            if project is None or getattr(project, "org_id", None) != oid:
                raise CliError(f"no such project: {args.project_id}", EXIT_USAGE)
        from app.kernel import keys as keys_svc

        # SPEC-key-based-projects 2.5: a key project's internal documents are not
        # files and never appear on a document surface.
        docs = session.exec(
            select(Document)
            .where(Document.project_id == args.project_id, Document.fmt != keys_svc.KEY_DOC_FMT)
            .order_by(Document.created_at.desc())
        ).all()
        from app.kernel import lifecycle

        doc_status = lifecycle.document_status_map(session, [d.id for d in docs])
        rows = [(d.id, d.filename, d.fmt, d.mode, doc_status.get(d.id, "new")) for d in docs]
        from app.kernel import delivery

        batches = delivery.zip_batches(session, args.project_id, documents=docs)
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "documents": [
                        {
                            "id": d.id,
                            "filename": d.filename,
                            "fmt": d.fmt,
                            "mode": d.mode,
                            "status": doc_status.get(d.id, "new"),
                        }
                        for d in docs
                    ],
                    "zip_batches": batches,
                }
            )
        else:
            emit.table(rows, headers=("id", "filename", "fmt", "mode", "status"))
            emit_zip_batches(emit, batches, args.project_id)
    return 0


def emit_zip_batches(emit, batches: list, project_id) -> None:
    """Batch 13: list a project's ZIP batch uploads under `doc list` (both arms)."""
    for b in batches:
        emit.say(
            f"ZIP batch {b['id']}: {b['archive']} ({b['documents']} document(s)); "
            f"download: loc-tms project export {project_id} --batch {b['id']}"
        )


# --- doc translate (synchronous, in-process) ---------------------------------
def _doc_translate(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_translate(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.kernel import budgets, project_budgets, translate
    from app.models import Segment

    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        # --- per-department budget parity (T4-02) -----------------------------
        # POST /api/documents/{id}/translate has gated the monthly department
        # cap since Phase B chunk 4; this in-process CLI path submitted with NO
        # budget check at all, so a capped department could keep translating
        # unbounded via the CLI even while the API refused it. Same shared
        # budgets.check seam (the CLI's --actor is a free-text email, not an
        # authenticated `ident` — department_for_actor looks up the matching
        # User row, "" / no-match behaving exactly like an unbound API key).
        actor = getattr(args, "actor", None) or "cli"
        dept = budgets.department_for_actor(session, actor)
        allowed, _remaining, msg = budgets.check(dept, org_id=op_org_id(args))
        if not allowed:
            raise CliError(msg)
        # --- per-PROJECT USD budget parity (CV-2) -----------------------------
        # The money twin of the department token cap, on the same in-process CLI
        # translate path: at 100% of the project's monthly_usd_limit, refuse before
        # translating. Keyed to the project (org from the project, not the caller).
        p_allowed, _p_rem, p_msg = project_budgets.project_check(doc.project_id)
        if not p_allowed:
            # H1: the blocked submission is the 100% crossing — alert before refusing.
            project_budgets.evaluate_thresholds(doc.project_id, actor=actor)
            raise CliError(p_msg)
        project_budgets.evaluate_thresholds(doc.project_id, actor=actor)
        # Guard: the locale must actually exist on the document (ingest seeds one
        # segment set per project TargetLocale). Otherwise pretranslate_stream
        # would find no segments and silently do nothing.
        present = {
            s.target_locale
            for s in session.exec(select(Segment).where(Segment.document_id == doc.id)).all()
        }
        if args.locale not in present:
            raise CliError(
                f"locale {args.locale!r} not on document #{doc.id} "
                f"(present: {', '.join(sorted(present)) or 'none'}).",
                EXIT_USAGE,
            )

        # Pre-flight estimate (Feature 7): show expected size + duration BEFORE the
        # translation starts, so a big job's cost is visible up front.
        est = translate.estimate_translation(session, doc.id, args.locale)
        emit.say(
            f"Estimate: {est['segments']} segment(s), ~{est['input_tokens']} in / "
            f"~{est['output_tokens']} out tokens, ~{est['seconds']}s "
            f"(@{est['tokens_per_sec']} tok/s on {est['provider']})."
        )

        stats = {}
        n_segments = 0
        running: dict = {}  # #364: the stream's booked USD ("cost_usd"), for the Job row only
        # Drain the generator synchronously; count segment events for a progress
        # line, keep the summary for the result.
        from app.kernel import segment_review as _segment_review
        from app.kernel.providers import DeadEngineError

        try:
            for ev in translate.pretranslate_stream(
                session, doc.id, args.locale, stats_sink=running
            ):
                if ev.get("type") == "segment":
                    n_segments += 1
                    emit.detail(f"  segment {ev.get('index')}: {ev.get('source')}")
                elif ev.get("type") == "summary":
                    stats = {k: v for k, v in ev.items() if k != "type"}
        except _segment_review.AwaitingReviewError as exc:
            # #617: 409 on web and /api; a run waits on a human review step for this locale.
            raise CliError(str(exc)) from exc
        except DeadEngineError as exc:
            # A stored engine:<id> ref went dead (deleted/disabled/keyless): FAIL
            # LOUD with a clean stderr message + non-zero exit, never silently
            # substitute mock output. (Surfaced as a CliError, matching the CLI's
            # convention for expected failures — no scary traceback.)
            raise CliError(str(exc)) from exc  # default exit code 1 (EXIT_FAIL)
        # P1: auto-complete the project if every locale/doc is now translated.
        from app.kernel import lifecycle

        lifecycle.mark_completed_if_done(
            session, doc.project_id, actor=getattr(args, "actor", None) or "cli"
        )
        # Record a translate_document Job row for this synchronous CLI translation so
        # it is visible to reports (cost/tokens/volume) and the unified jobs history,
        # identically to the async web/api path (issue #16b). This path consumes
        # pretranslate_stream directly (streaming UX) rather than run_sync, so it never
        # wrote the Job row reports+history read — record it now with the SAME
        # {document_id, locale} payload + _new_stats result the async handler writes.
        # Spend was already enforced/recorded by the provider seam during the stream;
        # this only persists the attributable row (never re-runs work, never double-
        # bills). Best-effort: a bookkeeping hiccup must not fail a completed
        # translation the operator already paid for.
        from app.kernel import jobs as _jobs

        # #572/#612: every segment failing, or any segment failing, is a FAILED run (same
        # rule as the background job); the segments that landed stay saved.
        _dead = _jobs.run_error(stats, n_segments or None)
        try:  # noqa: SIM105
            _jobs.record_sync_job(
                "translate_document",
                {"document_id": doc.id, "locale": args.locale},
                actor=actor,
                result={**stats, "cost_usd": running.get("cost_usd")} if stats else {},
                total=n_segments,
                status=_jobs.JobStatus.FAILED if _dead else _jobs.JobStatus.COMPLETED,
                error=_dead,
            )
        except Exception:  # noqa: S110 -- job-history row only; the translation already committed
            pass
        if _dead:
            raise CliError(f"Translation of document #{doc.id} into {args.locale} failed: {_dead}")
        # #58/#59: surface the needs_review count when nonzero, so a wholly wrong-language
        # document no longer reads as "10 segment(s) ... 0 error(s)" with nothing else. A
        # soft-flagged segment is committed (not an error) but must NOT ship unreviewed.
        _need = stats.get("needs_review", 0)
        _need_clause = f", {_need} need review" if _need else ""
        emit.result(
            f"Translated document #{doc.id} into {args.locale}: "
            f"{stats.get('translated', 0)} segment(s) "
            f"({stats.get('from_segment_tm', 0)} from TM, "
            f"{stats.get('provider_calls', 0)} provider call(s), "
            f"{stats.get('errors', 0)} error(s){_need_clause}).",
            {
                "ok": True,
                "doc_id": doc.id,
                "locale": args.locale,
                "segments_seen": n_segments,
                "stats": stats,
                "estimate": est,
            },
        )
    return 0


# --- doc rename / delete (SPEC-rename-delete) --------------------------------
def _doc_rename(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_doc(session, args.doc_id, op_org_id(args))
        try:
            doc = content.rename_document(
                session, args.doc_id, args.filename, actor=actor, org_id=op_org_id(args)
            )
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Renamed document #{doc.id} -> {doc.filename}.",
            {"ok": True, "id": doc.id, "filename": doc.filename},
        )
    return 0


def _doc_delete(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        fname = doc.filename
        if not confirm(emit, f"Delete document #{doc.id} ({fname})?", args.yes):
            raise CliError("aborted.", 1)
        counts = content.delete_document(
            session,
            args.doc_id,
            actor=actor,
            org_id=op_org_id(args),
            privacy_erasure=getattr(args, "privacy_erasure", False),
        )
        emit.result(
            f"Deleted document #{args.doc_id} ({fname})."
            + (
                " Upload erasure is pending; automatic retry scheduled."
                if counts.get("file_pending")
                else ""
            )
            + (" " + counts["warning"] if counts.get("warning") else ""),
            {"ok": True, "id": args.doc_id, "deleted": True, "counts": counts},
        )
    return 0


# --- doc export --------------------------------------------------------------
def _doc_export(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_export(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit, export, tm_delivery

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        as_fmt = getattr(args, "as_fmt", None)
        if as_fmt and as_fmt not in export.export_as_formats(doc.fmt):
            raise CliError(f"--as-fmt {as_fmt} is not available for a {doc.fmt} file.", EXIT_USAGE)
        # AI Tagging 1.6: a FULL_TEXT paragraph/token desync quarantines the WHOLE
        # export (no corrupt file). Surface a clean op-failure, not a traceback.
        # AI Tagging 1.7 (#41): collect the SHIP-AND-REPORT substitutions so the CLI is
        # not the one surface that hides them (web/api put the same fact in
        # X-Export-Substituted).
        # Issue #42 (C-3): --allow-source-fallback is the EXPLICIT per-export override
        # for a FULL_TEXT paragraph whose inline tags desynced. Same #41 report channel.
        # chip task_9c665111: a HELD locale (no committed translation) is refused, never
        # written out as a pure-source file. Same delivery guard as web + /api (409).
        # M1: --allow-held is the explicit override; the refusal names it.
        # Batch 15 T2 (§9.1/LAW-TM-14): the TM-only empty gate COMPANION, checked before the
        # classic held check and NOT bypassable by --allow-held. HOLD refuses (way-out is
        # `doc deliver-approve`, never --allow-held); RELEASE_EMPTIES delivers empties; PASS
        # falls through to the unchanged classic held/--allow-held logic.
        # ISSUE #95 (gate C round 1, H1): the QUALITY-GATE hold COMPANION, consulted
        # first and NOT bypassable by allow_held -- these segments were produced by a
        # run whose correction loop ended with its gate still FAILING, so they are
        # unreviewed failing text, not a missing translation.
        _qgate = export.gate_held_reason(session, doc, args.locale)
        if _qgate is not None:
            raise CliError(f"export refused: {_qgate}")
        _gate = tm_delivery.empty_gate_decision(session, doc=doc, locale=args.locale)
        if _gate == tm_delivery.HOLD:
            n = len(tm_delivery.doc_empties(session, doc, args.locale))
            # #105: shared diagnosis seam (names an in-flight run) + the CLI way-out.
            raise CliError(
                "export refused: "
                + tm_delivery.held_message(
                    session,
                    doc=doc,
                    locale=args.locale,
                    count=n,
                    unit="segment",
                    way_out=(
                        f"Run `loc-tms doc deliver-approve {doc.id} --locale {args.locale}` "
                        "to approve delivering empty targets (--allow-held cannot bypass this)."
                    ),
                )
            )
        if _gate != tm_delivery.RELEASE_EMPTIES and not getattr(args, "allow_held", False):
            held = export.held_locale_reason(session, doc, args.locale)
            if held is not None:
                raise CliError(
                    f"export refused: {held} Pass --allow-held to export the source-filled "
                    "file anyway."
                )
        rep: list = []
        try:
            data = export.build_translated_file(
                session,
                doc,
                args.locale,
                report=rep,
                allow_source_fallback=bool(getattr(args, "allow_source_fallback", False)),
                as_fmt=as_fmt,
            )
        except (export.ExportMissingBlob, export.ExportUnreadableBlob) as exc:
            # Issue #54 L3: a missing OR corrupt original upload blob is an honest
            # op-failure, not a traceback-class 500. Same clean-error surface as the
            # quarantine below; the exception's own message distinguishes missing from
            # unreadable/corrupt.
            raise CliError(f"export failed: {exc}", EXIT_USAGE) from exc
        except export.TagExportQuarantine as exc:
            # ASCII-safe: str(exc) is written pure-ASCII at the raise site.
            hint = ""
            if exc.kind == "tokens":
                hint = (
                    f"  Run:  loc-tms doc paragraphs --doc {doc.id} --locale {args.locale}   to see "  # noqa: E501
                    "which tokens diverged, then revert-paragraph / set-paragraph "
                    "(or re-export with --allow-source-fallback)."
                )
            elif exc.kind == "untranslated":
                # Tagless under-translation (blank real paragraph(s), fewer real paragraphs
                # than the source, or a blank stored body): no source-fallback override, but
                # `doc paragraphs` DIAGNOSES which paragraphs are blank/missing so they can
                # be filled (set-paragraph) or reverted to source (revert-paragraph).
                hint = (
                    f"  Run:  loc-tms doc paragraphs --doc {doc.id} --locale {args.locale}   to see "  # noqa: E501
                    "which paragraphs are blank/missing, then fill them "
                    "(doc set-paragraph) or revert to source (doc revert-paragraph), "
                    "or re-run the translation; then re-export."
                )
            raise CliError(f"export quarantined for review: {exc}{hint}") from exc
        # Issue #142, defence in depth: the DEFAULT name is composed from the STORED
        # filename, and a row renamed before the rename guard landed can still hold
        # `../../../../Windows/win.ini.txt` or a 300-character name (there is no
        # migration). safe_output_name basenames it AND keeps the composed component
        # inside the per-component length ceiling; default_out_in_cwd then proves the
        # RESOLVED path is in the CWD (it cannot escape even through a symlinked name).
        # Whatever the OS still refuses at open() becomes a clean one-line error below,
        # so no path through this verb ends in a traceback. An explicit -o is the
        # operator's own choice and is NOT constrained.
        from app.kernel import content as _content
        from app.kernel.mime import export_ext_for

        default_name = _content.safe_output_name(
            doc.filename, args.locale, as_fmt or export_ext_for(doc.fmt)
        )
        out_path = args.out or default_out_in_cwd(default_name)
        if os.path.exists(out_path) and not args.force:
            raise CliError(
                f"destination exists: {out_path} (pass --force to overwrite).", EXIT_USAGE
            )
        try:
            with open(out_path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            # LAST RESORT (gate round-1 HIGH, layer 3): a path the OS will not open -- too
            # long for THIS filesystem, a read-only directory, a bad device name, a full
            # disk -- is an honest op-failure, not a traceback-class crash.
            raise CliError(f"could not write {out_path}: {exc}", EXIT_USAGE) from exc
        # F4/F6: one seam-rendered detail — names the KIND (routine #41 substitution vs
        # this run's explicit --allow-source-fallback) and the UNIT (segment order_index
        # vs paragraph index), instead of a bare count that read the same either way.
        audit.log(
            actor=actor,
            action="document.export",
            target=f"doc {doc.id} {args.locale}",
            detail=export.substitution_detail(rep),
            customer_id=audit.customer_for_project(doc.project_id),
        )
        if rep:
            # "segment(s)" was a lie on the FULL_TEXT path, where these are PARAGRAPHS.
            unit = "paragraph" if any(x.get("unit") == "paragraph" for x in rep) else "segment"
            emit.say(
                "WARNING: %d %s(s) carried broken inline-formatting tokens or placeholders; "  # noqa: UP031
                "their SOURCE text was exported instead (%s)."
                % (len(rep), unit, export.substitution_detail(rep))
            )
            emit.say(
                "  The file is well-formed and keeps its formatting, but those "
                f"{unit}s are UNTRANSLATED."
            )
        emit.result(
            f"Exported document #{doc.id} ({args.locale}) -> {out_path} ({len(data)} bytes).",
            {
                "ok": True,
                "doc_id": doc.id,
                "locale": args.locale,
                "path": out_path,
                "bytes": len(data),
                "substituted": rep,
                # Envelope parity with the --remote arm (R-MED-6): the remote path
                # can only see the count + summary detail header, so both surfaces
                # now carry these alongside the richer `substituted` list.
                "substituted_count": len(rep),
                "substituted_detail": export.substitution_detail(rep),
            },
        )
    return 0


# --- TM-only delivery empty gate (Batch 15 T2) ------------------------------
def _doc_empties(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import tm_delivery

    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        decision = tm_delivery.empty_gate_decision(session, doc=doc, locale=args.locale)
        empties = tm_delivery.doc_empties(session, doc, args.locale)
        row = tm_delivery._doc_approval_row(session, doc.id, args.locale)
        approved = bool(row is not None and row.status == "approved")
        for e in empties:
            emit.say(f"  [{e['order_index']}] {e['source_text']}")
        emit.result(
            f"Document #{doc.id} ({args.locale}): {len(empties)} empty target(s); "
            f"decision={decision}; approved={approved}.",
            {
                "ok": True,
                "doc_id": doc.id,
                "locale": args.locale,
                "decision": decision,
                "empty_count": len(empties),
                "empties": empties,
                "approved": approved,
            },
        )
    return 0


def _doc_deliver_approve(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import tm_delivery

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        try:
            row = tm_delivery.approve_doc(session, doc, args.locale, approved_by=actor)
        except tm_delivery.LocaleNotDeliverable as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Approved empty delivery for document #{doc.id} ({args.locale}); "
            f"covered {row.covered_count} empty segment(s).",
            {
                "ok": True,
                "doc_id": doc.id,
                "locale": args.locale,
                "approved": True,
                "covered_count": row.covered_count,
            },
        )
    return 0


def _doc_deliver_revoke(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import tm_delivery

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        try:
            revoked = tm_delivery.revoke_doc(session, doc, args.locale, approved_by=actor)
        except tm_delivery.LocaleNotDeliverable as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            (
                f"Revoked empty-delivery approval for document #{doc.id} ({args.locale})."
                if revoked
                else f"No empty-delivery approval to revoke for document #{doc.id} "
                f"({args.locale}) (already held)."
            ),
            {"ok": True, "doc_id": doc.id, "locale": args.locale, "revoked": revoked},
        )
    return 0


__all__ = [
    "_doc_apply_tm",
    "_doc_approve_segment",
    "_doc_delete",
    "_doc_deliver_approve",
    "_doc_deliver_revoke",
    "_doc_empties",
    "_doc_export",
    "_doc_id",
    "_doc_list",
    "_doc_paragraphs",
    "_doc_rename",
    "_doc_revert_paragraph",
    "_doc_set_paragraph",
    "_doc_show",
    "_doc_translate",
    "_doc_unlock_segment",
    "_doc_upload",
    "_get_doc",
    "_para_view",
    "_register_apply_tm",
    "_register_approve_segment",
    "_register_paragraphs",
    "_register_revert_paragraph",
    "_register_set_paragraph",
    "_register_show",
    "_register_unlock_segment",
    "register",
    "register_doc_inspect_commands",
]
