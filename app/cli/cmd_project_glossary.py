"""cmd_project: glossary

Documented in: docs/architecture/content-and-projects.md
"""

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _session,
    confirm,
    glossary_import_cli_error,
    op_org_id,
    op_org_scope,
    quiet_import_log,
    remote_mode,
    require_local,
    require_org_scope,
)
from app.cli.cmd_project_project import (
    _TERM_META_FLAGS,
    _changed_note,
    _get_customer,
    _get_project,
    _gmod_get,
    _read_bytes,
    _skipped_note,
    _tm_filters,
    _tm_row,
    _tm_visible_pids,
    _write_bytes,
)


def _project_glossaries_remove(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, project_sources

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        removed = project_sources.remove_glossary_pin(session, project.id, args.glossary_id)
        if removed:
            audit.log(
                actor=actor,
                action="project.glossary.detach",
                target=f"project {project.id} -x- glossary {args.glossary_id}",
                customer_id=project.customer_id,
            )
        emit.result(
            (
                f"Detached glossary #{args.glossary_id} from project #{project.id}."
                if removed
                else f"Project #{project.id} has no attached glossary #{args.glossary_id}."
            ),
            {
                "ok": True,
                "project_id": project.id,
                "glossary_id": args.glossary_id,
                "removed": removed,
            },
        )
    return 0


def _project_export_sdl(args, emit, session, project, actor) -> int:
    """`project export --sdl-return-package`: one Trados return package (.sdlrpx)."""
    from app.kernel import audit, delivery

    guid = "" if args.sdl_return_package == "*" else args.sdl_return_package
    try:
        data, default_name, skipped = delivery.export_sdl_return_package(
            session, project.id, guid or None, allow_held=bool(getattr(args, "allow_held", False))
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    out_path = args.out or default_name
    _write_bytes(out_path, data, args.force)
    audit.log(
        actor=actor,
        action="project.export",
        target=f"project {project.id}",
        detail="sdl_return_package" + (f" skipped={len(skipped)}" if skipped else ""),
        customer_id=project.customer_id,
    )
    for s in skipped:
        emit.say("  left out {} [{}]: {}".format(s["filename"], s["locale"], s["reason"]))
    emit.result(
        f"Exported Trados return package for project #{project.id} -> {out_path} "
        f"({len(data)} bytes).",
        {
            "ok": True,
            "project_id": project.id,
            "path": out_path,
            "bytes": len(data),
            "skipped": skipped,
        },
    )
    return 0


def _project_export(args, emit) -> int:
    # In-process only (reads the host DB + writes a file locally). Parity with
    # GET /projects/{id}/export.
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, delivery

    actor = getattr(args, "actor", None) or "cli"
    req = [x for x in (args.locales or "").replace(",", " ").split() if x]
    if args.all_locales:
        req = []
    want = req or None
    # A single explicitly-named locale exports one file unless --zip forces a ZIP.
    as_zip = bool(args.zip) or not (len(req) == 1)  # noqa: SIM201
    from app.kernel.export import (
        ExportEmptyTargetsHeld,
        ExportHeldLocale,
        TagExportQuarantine,
    )

    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        if getattr(args, "sdl_return_package", ""):
            return _project_export_sdl(args, emit, session, project, actor)
        # BUG-04: the bundle's SUBSTITUTION channel, the CLI twin of the web route's
        # X-Export-Substituted headers. A quarantine is loud (a document is missing from
        # the bundle); a source-policy substitution is silent — the file is delivered,
        # well-formed, with one segment quietly still in English.
        rep: list = []
        try:
            data, default_name, _ctype, skipped = delivery.export_project(
                session,
                project.id,
                locales=want,
                zip=as_zip,
                report=rep,
                allow_held=bool(getattr(args, "allow_held", False)),
                batch=(getattr(args, "batch", None) or "").strip() or None,
            )
        except ValueError as exc:
            raise CliError(str(exc)) from exc
        except TagExportQuarantine as exc:
            # Single-file fast path quarantined the only doc — no clean file to write.
            raise CliError(f"export quarantined for review: {exc}") from exc
        except ExportEmptyTargetsHeld as exc:
            # Batch 15 T2 (§9.2): the single-file fast path hit a TM-only empty-held doc.
            # The way out is the AUDITED approval, NOT --allow-held (which cannot bypass).
            raise CliError(
                f"export refused: {exc} Run `loc-tms doc deliver-approve {exc.doc_id} "
                f"--locale {exc.locale}` to approve delivering empty targets."
            ) from exc
        except ExportHeldLocale as exc:
            # chip task_9c665111: the single-file fast path hit a held locale — refuse
            # rather than write a source-only file. M1: name the --allow-held way-out.
            raise CliError(
                "export refused: {} Pass --allow-held to ship the source-filled file anyway.".format(  # noqa: E501, UP032
                    exc
                )
            ) from exc
        out_path = args.out or default_name
        _write_bytes(out_path, data, args.force)
        subs = sum(r["count"] for r in rep)
        audit.log(
            actor=actor,
            action="project.export",
            target=f"project {project.id}",
            detail=f"locales={','.join(req) or 'all'}"
            + (f" skipped={len(skipped)}" if skipped else "")
            + (f" substituted={subs} docs={len(rep)}" if rep else ""),
            customer_id=project.customer_id,
        )
        # AI Tagging 1.6 / M1: SKIP-AND-REPORT — the bundle keeps the clean docs; list
        # any quarantined ones honestly (also captured in the ZIP _QUARANTINED.txt).
        if skipped:
            emit.say(
                "WARNING: %d document(s) quarantined for review and EXCLUDED "  # noqa: UP031
                "from the bundle:" % len(skipped)
            )
            for s in skipped:
                emit.say("  - {} [{}]: {}".format(s["filename"], s["locale"], s["reason"]))
        # BUG-04: and the SILENT half — documents that ARE in the bundle but carry
        # units exported as SOURCE text. `doc export` has warned about this since #41;
        # the bundle said nothing, which is the surface a delivery actually goes out on.
        if rep:
            emit.say(
                "WARNING: %d unit(s) across %d document(s) carried broken "  # noqa: UP031
                "inline-formatting tokens; their SOURCE text was exported instead "
                "(the files are well-formed, those units are UNTRANSLATED):" % (subs, len(rep))
            )
            for r in rep:
                emit.say("  - {} [{}]: {}".format(r["filename"], r["locale"], r["detail"]))
        emit.result(
            f"Exported project #{project.id} -> {out_path} ({len(data)} bytes).",
            {
                "ok": True,
                "project_id": project.id,
                "path": out_path,
                "bytes": len(data),
                "locales": req or "all",
                "zip": out_path.lower().endswith(".zip"),
                "skipped": skipped,
                "substituted": rep,
            },
        )
    return 0


def _project_rename(args, emit) -> int:
    # R1a: remote-capable — dispatches to POST /api/projects/{id}/rename (the same
    # route the web form posts to). The in-process arm calls the shared
    # content.rename_project service; one code path, web+CLI parity.
    if remote_mode(args):
        return _remote_impl.project_rename(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        try:
            project = content.rename_project(
                session, args.project_id, args.name, actor=actor, org_id=op_org_id(args)
            )
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Renamed project #{project.id} -> {project.name}.",
            {"ok": True, "id": project.id, "name": project.name},
        )
    return 0


def _project_archive(args, emit) -> int:
    # Local-only (mirrors `customer archive`): the CLI is trusted-local; the /api
    # archive route serves the web + remote-API surfaces.
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        try:
            project = content.archive_project(
                session, args.project_id, actor=actor, org_id=op_org_id(args)
            )
        except content.ProjectArchiveError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Archived project #{project.id} ({project.name}) -- retired from the "
            f"default list + pickers, blocked from new runs.",
            {"ok": True, "id": project.id, "name": project.name, "archived": True},
        )
    return 0


def _project_unarchive(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        try:
            project = content.unarchive_project(
                session, args.project_id, actor=actor, org_id=op_org_id(args)
            )
        except content.ProjectArchiveError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Unarchived project #{project.id} ({project.name}) -- listed + runnable again.",
            {"ok": True, "id": project.id, "name": project.name, "archived": False},
        )
    return 0


def _project_delete(args, emit) -> int:
    # R1a: remote-capable — dispatches to POST /api/projects/{id}/delete (admin-only,
    # typed-confirm). The in-process arm keeps the DESTRUCTIVE cascade below; --force is
    # the CLI's typed-confirmation on both arms (the remote arm echoes the project name
    # server-side after a dry_run preview).
    if remote_mode(args):
        return _remote_impl.project_delete(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        preview = content.delete_project_preview(session, args.project_id)
        # Preflight the INDEPENDENT REFERRERS only (connections / routing rules), NOT
        # blockers["total"] -- total now also carries the forecast workflow_memory_suggestions
        # count (LOW-2). A suggestion-targeted project flows past this into delete_project, whose
        # step-3c raise carries the tailored "archive instead" message, caught below (LOW-3).
        if preview["blockers"]["connections"] + preview["blockers"]["routing_rules"] > 0:
            raise CliError(
                "cannot delete project %d: still referenced by %d connection(s) and "  # noqa: UP031
                "%d routing rule(s). Reassign or delete those first."
                % (
                    args.project_id,
                    preview["blockers"]["connections"],
                    preview["blockers"]["routing_rules"],
                ),
                EXIT_USAGE,
            )
        # Itemized pre-counts (ASCII) so the destruction is visible up front. ZERO ROWS ARE
        # OMITTED: the cascade dict carries a key per owned table (26 and counting), so printing
        # every zero buries the two or three numbers the operator has to read before typing
        # --force. Same filter the web confirm dialog applies (parity); the preview dict itself is
        # unchanged, so the /api dry-run and the audit detail still carry every key.
        emit.say(f"Deleting project #{project.id} ({project.name}) will destroy:")
        _shown = [(k, v) for k, v in preview["cascade"].items() if k != "total" and v]
        for k, v in _shown:
            emit.say(f"  {k.replace('_', ' ')}: {v}")
        if not _shown:
            emit.say("  (nothing owned; only the project row is removed)")
        if not args.force:
            raise CliError(
                "refusing to delete without --force (DESTRUCTIVE cascade). "
                "Re-run with --force to proceed.",
                EXIT_USAGE,
            )
        # LOW-3: the preflight above covers only Connection/RoutingRule referrers, so the
        # step-3c workflow_memory_suggestions tombstone blocker (and any referrer that appears
        # in the window between the preview and this delete) surfaces here as
        # ProjectDeleteBlocked. Convert it to a clean EXIT_USAGE with the tailored message,
        # never a raw traceback (cli/__main__ re-raises anything that is not schema drift). The
        # delete rolled itself back whole (step-3c raises before any commit), so nothing leaked.
        try:
            counts = content.delete_project(
                session,
                args.project_id,
                actor=actor,
                org_id=op_org_id(args),
                privacy_erasure=getattr(args, "privacy_erasure", False),
            )
        except content.ProjectDeleteBlocked as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Deleted project #{args.project_id} ({project.name})."
            + (
                " Upload erasure is pending; automatic retry scheduled."
                if counts.get("file_pending")
                else ""
            )
            + (" " + counts["warning"] if counts.get("warning") else ""),
            {"ok": True, "id": args.project_id, "deleted": True, "cascade": counts},
        )
    return 0


# --- tm ----------------------------------------------------------------------
def _tm_import(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_import(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm

    data = _read_bytes(args.tmx)
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        try:
            res = tm.import_tmx(
                session,
                args.project_id,
                data,
                args.source,
                args.target,
                acknowledge_placeholder_loss=bool(
                    getattr(args, "acknowledge_placeholder_loss", False)
                ),
            )
        except Exception as exc:
            raise CliError(f"could not import TMX: {exc}") from exc
        n = res["imported"]
        # Idempotent re-import (bug #5): a unit whose source_hash already exists in
        # this project+locale-pair scope updates the stored target instead of
        # duplicating the row, or is skipped outright when nothing changed. Both
        # counts are surfaced so a re-run is visibly a no-op rather than silently
        # growing the TM.
        # skipped_refusal (Deliverable 3): units whose target read as a refusal hedge
        # were dropped at import so they can't poison the TM as a guard-bypassing
        # 100%-match. Surfaced here (CLI) and on the web import flow (parity).
        skipped_ref = res.get("skipped_refusal", 0)
        # QA BUG-03: report the locales the rows were actually STORED under, not the
        # ones asked for. `en`/`es` against an en-US -> es-ES project is coerced at the
        # service seam (an irreconcilable pair is refused there and lands in the
        # CliError above), so echoing args.source/args.target would state a bucket that
        # does not exist and hide the coercion.
        eff_src = res.get("source_locale", args.source)
        eff_tgt = res.get("target_locale", args.target)
        emit.result(
            f"Imported {n} TM entr{'y' if n == 1 else 'ies'} "
            f"({eff_src} -> {eff_tgt})"
            + (f", updated {res['updated']}" if res["updated"] else "")
            + (f", skipped {res['skipped']} unchanged" if res["skipped"] else "")
            + (f", skipped {skipped_ref} refusal(s)" if skipped_ref else "")
            + (
                f", withheld {res['skipped_integrity']} for placeholder/tag integrity"
                if res.get("skipped_integrity")
                else ""
            )
            + (
                f", could not read {res['skipped_unreadable']} SDLTM unit(s)"
                if res.get("skipped_unreadable")
                else ""
            )
            + "."
            + (
                f" Written in {res['write_chunks']} short transactions (a failure part way keeps"
                " the rows already stored; re-run the same file to finish)."
                if res.get("write_chunks", 0) > 1
                else ""
            )
            + (
                f" Did not restore {res['concurrent_deleted']} TM entr"
                f"{'y' if res['concurrent_deleted'] == 1 else 'ies'} someone deleted while the"
                " import ran (import the file again to store them)."
                if res.get("concurrent_deleted")
                else ""
            )
            + (
                f" Note: locales {args.source} -> {args.target} were normalized to the "
                f"project's {eff_src} -> {eff_tgt}."
                if res.get("locales_normalized")
                else ""
            ),
            {
                "ok": True,
                "imported": n,
                "updated": res["updated"],
                "skipped": res["skipped"],
                "skipped_refusal": skipped_ref,
                "skipped_integrity": res.get("skipped_integrity", 0),
                "skipped_by_code": res.get("skipped_by_code", {}),
                "skipped_rows": res.get("skipped_rows", []),
                "acknowledged_placeholder_loss": res.get("acknowledged_placeholder_loss", 0),
                "source": eff_src,
                "target": eff_tgt,
                "requested_source": args.source,
                "requested_target": args.target,
                "locales_normalized": bool(res.get("locales_normalized")),
                "write_chunks": res.get("write_chunks", 0),
                "concurrent_deleted": res.get("concurrent_deleted", 0),
                "format": res.get("format", "tmx"),
                "skipped_unreadable": res.get("skipped_unreadable", 0),
            },
        )
    if not emit.as_json:
        for r in res.get("skipped_rows") or []:
            line = (
                f"  withheld unit {r['row']} [{r['code']}]: {r['reason']} (source: {r['source']})"
            )
            emit.say(line.encode("ascii", "backslashreplace").decode("ascii"))
        if res.get("skipped_integrity") and not res.get("acknowledged_placeholder_loss"):
            emit.say("  Re-run with --acknowledge-placeholder-loss to import placeholder losses.")
    return 0


def _tm_export(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_export(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm

    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        # QA BUG-03 read-back leg: report the EFFECTIVE locales the rows were queried
        # under (a region/case variant is coerced to the project's stored spelling, the
        # same way import stores), so `tm export --source en --target es` into an
        # en-US -> es-419 project no longer silently writes an empty TMX (FINDING 5).
        # `count` is the REAL row count (review H2: a byte substring scan missed
        # meta-stamped `<tu creationtool=...>` human_approved rows and reported 0).
        eff_source, eff_target, count, xml = tm.export_tmx_result(
            session, args.project_id, args.source, args.target
        )
    _write_bytes(args.tmx, xml, args.force)
    emit.result(
        f"Wrote TMX -> {args.tmx} ({eff_source} -> {eff_target}).",
        {
            "ok": True,
            "path": args.tmx,
            "bytes": len(xml),
            "count": count,
            "source": eff_source,
            "target": eff_target,
            "requested_source": args.source,
            "requested_target": args.target,
        },
    )
    return 0


def _tm_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm_module

    with _session() as session:
        pids = _tm_visible_pids(session, args)
        res = tm_module.list_entries(
            session,
            page=args.page,
            per_page=args.per_page,
            order=args.order,
            visible_pids=pids,
            **_tm_filters(args),
        )
        if emit.as_json:
            emit.json({"ok": True, **res})
        else:
            emit.table(
                [_tm_row(e) for e in res["entries"]],
                headers=("id", "source", "target", "pair", "ctype", "uses", "last_used"),
            )
            emit.say(
                f"page {res['page']}/{res['pages'] or 1} | {res['total']} entr"
                f"{'y' if res['total'] == 1 else 'ies'} total"
            )
    return 0


def _tm_search(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_search(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm_module

    filters = _tm_filters(args)
    filters["q"] = args.text  # the positional search text drives the q filter
    with _session() as session:
        pids = _tm_visible_pids(session, args)
        res = tm_module.list_entries(
            session,
            page=args.page,
            per_page=args.per_page,
            order="recent",
            visible_pids=pids,
            **filters,
        )
        if emit.as_json:
            emit.json({"ok": True, **res})
        else:
            emit.table(
                [_tm_row(e) for e in res["entries"]],
                headers=("id", "source", "target", "pair", "ctype", "uses", "last_used"),
            )
            emit.say(
                f"page {res['page']}/{res['pages'] or 1} | {res['total']} match"
                f"{'' if res['total'] == 1 else 'es'}"
            )
    return 0


def _tm_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_show(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm_module

    with _session() as session:
        pids = _tm_visible_pids(session, args)
        e = tm_module.get_entry(session, args.entry_id, visible_pids=pids)
        if e is None:
            raise CliError(f"no such TM entry: {args.entry_id}", EXIT_USAGE)
        d = tm_module.entry_to_dict(e)
        if emit.as_json:
            emit.json({"ok": True, **d})
        else:
            emit.say(f"TM entry #{d['id']} [{d['source_locale']}->{d['target_locale']}]")
            emit.say(f"  source: {d['source_text']}")
            emit.say(f"  target: {d['target_text']}")
            emit.say(
                f"  project: {d['project_id'] if d['project_id'] is not None else 'global'}"
                f"   content type: {d.get('content_type_id') if d.get('content_type_id') is not None else 'general'}"  # noqa: E501
                f"   origin: {d['origin']}"
            )
            emit.say(
                f"  created: {d['created_at']}   uses: {d['use_count']}"
                f"   last used: {d['last_used_at'] or 'never'}"
            )
    return 0


def _tm_edit(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_edit(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm_module

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        pids = _tm_visible_pids(session, args)
        try:
            e = tm_module.edit_entry(
                session,
                args.entry_id,
                args.target,
                visible_pids=pids,
                actor=actor,
                acknowledge_placeholder_loss=bool(
                    getattr(args, "acknowledge_placeholder_loss", False)
                ),
            )
        except (tm_module.TMEditRefused, ValueError) as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if e is None:
            raise CliError(f"no such TM entry: {args.entry_id}", EXIT_USAGE)
        emit.result(
            f"Updated TM entry #{e.id} target.",
            {"ok": True, "id": e.id, "target_text": e.target_text},
        )
    return 0


def _tm_stats(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_stats(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm_module

    with _session() as session:
        pids = _tm_visible_pids(session, args)
        s = tm_module.stats(session, visible_pids=pids, top_n=10)
        if emit.as_json:
            emit.json({"ok": True, **s})
        else:
            emit.say(
                f"Total entries: {s['total']}   used: {s['used']}   "
                f"never used: {s['never_used']}   "
                f"total applications: {s['total_applications']}"
            )
            emit.say(
                f"Created range: {s['oldest_created_at'] or '-'} .. {s['newest_created_at'] or '-'}"
            )
            emit.say("By language pair:")
            emit.table(
                [(p["source_locale"] + "->" + p["target_locale"], p["count"]) for p in s["pairs"]],
                headers=("pair", "entries"),
            )
            if s["top_used"]:
                emit.say("Most used:")
                emit.table(
                    [
                        (
                            e["id"],
                            (e["source_text"] or "")[:40],
                            e["use_count"],
                            e["last_used_at"] or "-",
                        )
                        for e in s["top_used"]
                    ],
                    headers=("id", "source", "uses", "last_used"),
                )
    return 0


def _tm_scrub_hedges(args, emit) -> int:
    """One-off remediation (Deliverable 4): find (dry-run) / remove (--apply) stored
    TM targets that read as refusal hedges — they are guard-bypassing 100%-matches.
    Scoped through project like every other tm command; --apply is audit-logged."""
    if remote_mode(args):
        return _remote_impl.tm_scrub_hedges(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm_module

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        pids = _tm_visible_pids(session, args)
        if getattr(args, "tm_customer", None) is None:
            # #490: no --customer -> one-customer scope or a --project filter, else refuse.
            try:
                pids = tm_module.resolve_bulk_delete_scope(
                    session,
                    pids,
                    customers=None,
                    customer=None,
                    project_id=args.scrub_project,
                    what="scrub-hedges",
                )
            except tm_module.BulkDeleteRefused as exc:
                raise CliError(
                    str(exc).replace("pass customer", "pass --customer"), EXIT_USAGE
                ) from exc
        res = tm_module.scrub_hedges(
            session,
            apply=args.apply,
            actor=actor,
            visible_pids=pids,
            project_id=args.scrub_project,
            scope_label=tm_module.scope_label(args.tm_customer, args.scrub_project),
        )
        hits = res["segment"] + res["full_text"]
        n = res["deleted"] if res["applied"] else res["matched"]
        verb = "Deleted" if res["applied"] else "Would delete"
        msg = (
            f"{verb} {n} hedged TM entr{'y' if n == 1 else 'ies'}"
            + ("" if res["applied"] else " (dry-run; re-run with --apply to remove)")
            + "."
        )
        if emit.as_json:
            emit.json({"ok": True, **res})
        else:
            if hits:
                emit.table(
                    [
                        (
                            h["kind"],
                            h["id"],
                            h["pair"],
                            h["project_id"] if h["project_id"] is not None else "global",
                            h["sample"],
                        )
                        for h in hits
                    ],
                    headers=("kind", "id", "pair", "project", "target[:80]"),
                )
            emit.result(
                msg,
                {
                    "ok": True,
                    "matched": res["matched"],
                    "deleted": res["deleted"],
                    "applied": res["applied"],
                },
            )
    return 0


def _tm_delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.tm_delete(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit, tm_module

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        pids = _tm_visible_pids(session, args)
        # Single-entry delete (an id was given).
        if args.entry_id is not None:
            e = tm_module.get_entry(session, args.entry_id, visible_pids=pids)
            if e is None:
                raise CliError(f"no such TM entry: {args.entry_id}", EXIT_USAGE)
            if args.dry_run:
                emit.result(
                    f"Would delete TM entry #{args.entry_id}.",
                    {"ok": True, "dry_run": True, "count": 1, "entry_id": args.entry_id},
                )
                return 0
            if not confirm(emit, f"Delete TM entry #{args.entry_id}?", args.yes):
                raise CliError("aborted.", EXIT_USAGE)
            tm_module.delete_entry(session, args.entry_id, visible_pids=pids)
            audit.log(actor=actor, action="tm.delete", target=f"tm entry {args.entry_id}")
            emit.result(
                f"Deleted TM entry #{args.entry_id}.",
                {"ok": True, "count": 1, "entry_id": args.entry_id},
            )
            return 0

        # Bulk delete by filter — count-first.
        filters = _tm_filters(args)
        if getattr(args, "tm_customer", None) is None:
            # #490: no --customer -> refuse unless the whole visible set is one
            # customer (or a --project filter names a single project). pids from
            # _tm_visible_pids are already narrowed when --customer is given.
            try:
                pids = tm_module.resolve_bulk_delete_scope(
                    session,
                    pids,
                    customers=None,
                    customer=None,
                    project_id=filters["project_id"],
                )
            except tm_module.BulkDeleteRefused as exc:
                raise CliError(
                    str(exc).replace("pass customer", "pass --customer"), EXIT_USAGE
                ) from exc
        n = tm_module.bulk_delete(session, dry_run=True, visible_pids=pids, **filters)
        if args.dry_run:
            emit.result(
                f"Would delete {n} TM entr{'y' if n == 1 else 'ies'}.",
                {"ok": True, "dry_run": True, "count": n},
            )
            return 0
        if n == 0:
            emit.result(
                "No TM entries match the filters -- nothing to delete.", {"ok": True, "count": 0}
            )
            return 0
        if not any(filters.values()):
            # No filter at all -- this wipes the caller's ENTIRE visible TM. --yes
            # skips the interactive prompt (which is where the count would normally
            # be shown), so print the pending count to stderr BEFORE deleting -- a
            # --json/--yes caller must not be able to trigger a filterless wipe with
            # zero count visibility before the fact.
            emit.warn(
                f"about to delete ALL {n} visible TM entr"
                f"{'y' if n == 1 else 'ies'} (no filter given)."
            )
        if not confirm(emit, f"Delete {n} TM entr{'y' if n == 1 else 'ies'}?", args.yes):
            raise CliError("aborted.", EXIT_USAGE)
        deleted = tm_module.bulk_delete(session, dry_run=False, visible_pids=pids, **filters)
        scope = tm_module.scope_label(args.tm_customer, filters["project_id"])
        audit.log(actor=actor, action="tm.bulk_delete", target=f"{deleted} tm entries ({scope})")
        emit.result(
            f"Deleted {deleted} TM entr{'y' if deleted == 1 else 'ies'}.",
            {"ok": True, "count": deleted},
        )
    return 0


# --- glossary ----------------------------------------------------------------
def _glossary_import(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.glossary_import(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary

    data = _read_bytes(args.csv)
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        try:
            # CSV or native TBX, chosen by the file's extension (.tbx), with a
            # content sniff as the fallback.
            with quiet_import_log():
                res = glossary.import_bytes(
                    session, args.project_id, data, args.source, args.target, filename=args.csv
                )
        except Exception as exc:
            raise glossary_import_cli_error(exc) from exc
        emit.result(
            f"Imported glossary: {res['entries']} entr"
            f"{'y' if res['entries'] == 1 else 'ies'}, "
            f"{res['senses']} sense(s)." + _changed_note(res) + _skipped_note(res),
            {"ok": True, **res},
        )
    return 0


def _glossary_export(args, emit) -> int:
    # In-process (local-root), like the tm-management module commands: reads the DB
    # directly and writes the file. Web parity: GET /projects/{id}/glossary/export
    # (?format=tbx). The output PATH picks the format: .tbx -> TBX, else CSV.
    #
    # R-MED-11: the legacy per-PROJECT glossary export DOES have an /api route now
    # (GET /api/projects/{pid}/glossary/export -- api_cli.py), so --remote is WIRED to
    # it (the old require_local refusal + "NO /api route" comment were stale). The remote
    # arm reads the route's raw bytes and writes them byte-identically to the local file.
    if remote_mode(args):
        return _remote_impl.glossary_export(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary

    fmt = glossary.export_format_for(args.csv)
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        data = glossary.export_bytes(session, args.project_id, fmt=fmt)
    _write_bytes(args.csv, data, args.force)
    emit.result(
        f"Wrote glossary {fmt.upper()} -> {args.csv}.",
        {
            "ok": True,
            "path": args.csv,
            "bytes": len(data),
            "format": fmt,
            "project_id": args.project_id,
        },
    )
    return 0


def _glossary_add(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.glossary_add(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit, glossary

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        src = (args.source or "").strip() or project.source_locale
        entry = glossary.add_term(
            session,
            project.id,
            args.source_term,
            target_term=args.target_term,
            source_locale=src,
            target_locale=(args.target or "").strip(),
            dnt=args.dnt,
        )
        if entry is None:
            raise CliError("source term is blank.", EXIT_USAGE)
        audit.log(
            actor=actor, action="glossary.add", target=f"project {project.id} '{entry.source_term}'"
        )
        emit.result(
            f"Added term '{entry.source_term}' -> '{entry.target_term}' on project #{project.id}.",
            {
                "ok": True,
                "project_id": project.id,
                "source_term": entry.source_term,
                "target_term": entry.target_term,
                "dnt": entry.dnt,
            },
        )
    return 0


def _gmod_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import glossary_module as gm

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        # F6: a --customer must belong to the operator's --org scope (the web/api
        # validate this via _valid_customer_id) — a foreign customer must be
        # rejected, not silently stamped onto the glossary.
        customer_id = getattr(args, "customer", None)
        if customer_id is not None:
            _get_customer(session, customer_id, op_org_id(args))
        try:
            g = gm.create(
                session,
                args.name,
                # #73 COUPLING: op_org_SCOPE -- gm.create validates the content type via
                # content_types.attachable, which now fails closed on a bare None under an
                # active gate. The --operator door hands access.UNSCOPED so a cross-org
                # create still resolves global/attachable types; gm.create collapses the
                # sentinel to None for the persisted org (org_scope_value). `_get_customer`
                # above keeps op_org_id (a row-org guard). --org / gate-off byte-identical.
                org_id=op_org_scope(args),
                customer_id=customer_id,
                created_by=actor,
                content_type_id=getattr(args, "content_type_id", None),
                require_customer=True,
            )
        except gm.GlossaryModuleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(actor=actor, action="glossary.create", target=f"glossary {g.id} '{g.name}'")
        emit.result(
            f"Created glossary #{g.id} '{g.name}' (draft).", {"ok": True, **gm.to_dict(session, g)}
        )
    return 0


def _gmod_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary_module as gm

    with _session() as session:
        # #73 COUPLING: the LIST helpers now fail closed on a bare None under an active
        # gate, so feed them op_org_SCOPE (access.UNSCOPED for the --operator door, so it
        # still sweeps every org). `_get_customer` keeps op_org_id: it is a ROW-org guard
        # (get_scoped_or_404 compares org_id), where UNSCOPED would 404 every customer and
        # a bare None (operator) is the correct unrestricted no-op.
        org = op_org_id(args)
        scope = op_org_scope(args)
        if getattr(args, "unassigned", False):
            gls = gm.list_unassigned(session, org_id=scope)
        elif getattr(args, "customer", None) is not None:
            _get_customer(session, args.customer, org)
            gls = gm.list_for_customer(session, scope, args.customer)
        else:
            gls = gm.list_glossaries(session, org_id=scope)
        rows, payload = [], []
        for g in gls:
            d = gm.to_dict(session, g)
            rows.append(
                (
                    g.id,
                    g.name,
                    g.status,
                    g.customer_id or "-",
                    ", ".join(f"{p['source_locale']}->{p['target_locale']}" for p in d["pairs"])
                    or "-",
                    d["entry_count"],
                )
            )
            payload.append(d)
        if emit.as_json:
            emit.json({"ok": True, "glossaries": payload})
        else:
            emit.table(rows, headers=("id", "name", "status", "customer", "pairs", "terms"))
    return 0


def _gmod_set_customer(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_set_customer(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import glossary_module as gm

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _gmod_get(session, args)  # R2-3: fetch in org scope first
        _get_customer(session, args.customer_id, op_org_id(args))  # customer in org
        try:
            g = gm.assign_customer(session, args.glossary_id, args.customer_id)
        except gm.GlossaryModuleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="glossary.assign_customer",
            target=f"glossary {g.id} -> customer {args.customer_id}",
        )
        emit.result(
            f"Assigned glossary #{g.id} '{g.name}' to customer {args.customer_id}.",
            {"ok": True, **gm.to_dict(session, g)},
        )
    return 0


_META_COL_ABBR = {
    "part_of_speech": "pos",
    "definition": "def",
    "usage_example": "usex",
    "subject_field": "subj",
    "notes": "note",
}


def _meta_summary(meta) -> str:
    """A compact ASCII indicator of which TBX-ish meta fields are populated on a
    term, for the `glossary module show` human table ('-' = none). The full values
    are always in --json (see `terms` below)."""
    if not meta:
        return "-"
    return ",".join(_META_COL_ABBR[f] for f in _TERM_META_FLAGS if meta.get(f)) or "-"


def _gmod_approve(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_approve(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import glossary_module as gm

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _gmod_get(session, args)
        g = gm.approve(session, args.glossary_id, approver_email=actor)
        audit.log(actor=actor, action="glossary.approve", target=f"glossary {g.id}")
        emit.result(
            f"Approved glossary #{g.id} '{g.name}'.", {"ok": True, **gm.to_dict(session, g)}
        )
    return 0


def _gmod_unapprove(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_unapprove(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import glossary_module as gm

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _gmod_get(session, args)
        g = gm.unapprove(session, args.glossary_id)
        audit.log(actor=actor, action="glossary.unapprove", target=f"glossary {g.id}")
        emit.result(
            f"Reverted glossary #{g.id} '{g.name}' to draft.",
            {"ok": True, **gm.to_dict(session, g)},
        )
    return 0
