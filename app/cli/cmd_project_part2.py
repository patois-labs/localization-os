"""cmd_project: part2

Documented in: docs/architecture/content-and-projects.md
"""

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _session,
    confirm,
    get_scoped_or_404,
    op_org_id,
    op_org_scope,
    remote_mode,
    require_local,
    require_org_scope,
)
from app.cli.cmd_project_project import _get_customer, _get_project, _read_bytes


def _customer_set_default(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        try:
            cust = customer_service.set_customer_defaults(
                session,
                args.customer_id,
                provider=args.provider,
                workflow_id=args.workflow_id,
                segmentation_profile_id=args.segmentation_profile_id,
                content_type_id=args.content_type_id,
            )
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="customer.set_default",
            target=f"customer {cust.id}",
            customer_id=cust.id,
        )
        emit.result(
            f"Customer #{cust.id} defaults: engine={cust.default_provider or '(none)'}, "
            f"workflow={cust.default_workflow_id or '(none)'}, "
            f"segmentation={cust.default_segmentation_profile_id or '(none)'}, "
            f"content-type={cust.default_content_type_id or '(none)'}.",
            {
                "ok": True,
                "id": cust.id,
                "default_provider": cust.default_provider,
                "default_workflow_id": cust.default_workflow_id,
                "default_segmentation_profile_id": cust.default_segmentation_profile_id,
                "default_content_type_id": cust.default_content_type_id,
            },
        )
    return 0


def _customer_pairs_list(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import customers as customer_service

    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        # op_org_scope, not op_org_id: the #73 vocabulary, so --operator still sweeps.
        data = customer_service.get_customer_360(
            session, args.customer_id, org_id=op_org_scope(args)
        )
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "active_pairs": data["active_pairs"],
                    "derived_pairs": data["derived_pairs"],
                    "all_pairs": [p["pair"] for p in data["all_pairs"]],
                }
            )
        else:
            emit.table(
                [
                    (
                        p["pair"],
                        p["label"],
                        "declared" if p["pair"] in data["active_pairs"] else "",
                        "in-use" if p["pair"] in data["derived_pairs"] else "",
                    )
                    for p in data["all_pairs"]
                ]
                or [("(none)", "", "", "")],
                headers=("pair", "label", "declared", "in-use"),
            )
    return 0


def _customer_pairs_add(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        try:
            added, canon = customer_service.add_pair(session, args.customer_id, args.pair)
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if added:
            audit.log(
                actor=actor,
                action="customer.pair_add",
                target=f"customer {args.customer_id} {canon}",
                customer_id=args.customer_id,
            )
        emit.result(
            f"{'Added' if added else 'Already present:'} pair {canon} "
            f"on customer #{args.customer_id}.",
            {"ok": True, "customer_id": args.customer_id, "pair": canon, "added": added},
        )
    return 0


def _customer_pairs_remove(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        removed, canon = customer_service.remove_pair(session, args.customer_id, args.pair)
        if removed:
            audit.log(
                actor=actor,
                action="customer.pair_remove",
                target=f"customer {args.customer_id} {canon}",
                customer_id=args.customer_id,
            )
        emit.result(
            f"{'Removed' if removed else 'Not present:'} pair {canon} "
            f"on customer #{args.customer_id}.",
            {"ok": True, "customer_id": args.customer_id, "pair": canon, "removed": removed},
        )
    return 0


def _customer_pairs_import(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.customer_pairs_import(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    data = _read_bytes(args.csv)
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        try:
            res = customer_service.import_pairs_csv(session, args.customer_id, data)
        except (customer_service.PairImportError, ValueError) as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if res["added"]:
            audit.log(
                actor=actor,
                action="customer.pairs_import",
                target=f"customer {args.customer_id}",
                detail=f"added {len(res['added'])}, skipped {len(res['skipped'])}",
                customer_id=args.customer_id,
            )
        emit.result(
            f"Imported pairs on customer #{args.customer_id}: "
            f"{len(res['added'])} added, {len(res['skipped'])} already present "
            "(skipped).",
            {"ok": True, "customer_id": args.customer_id, **res},
        )
    return 0


def _customer_rename(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        try:
            cust = customer_service.rename_customer(
                session, args.customer_id, args.name, actor=actor, org_id=op_org_id(args)
            )
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Renamed customer #{cust.id} -> {cust.name}.",
            {"ok": True, "id": cust.id, "name": cust.name},
        )
    return 0


def _customer_delete(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        cust = _get_customer(session, args.customer_id, op_org_id(args))
        # Fail closed with the itemized count BEFORE prompting (no cascade).
        refs = customer_service.reference_count(session, args.customer_id)
        if refs["total"] > 0:
            raise CliError(
                str(customer_service.CustomerDeleteBlocked(args.customer_id, refs)), EXIT_USAGE
            )
        if not confirm(emit, f"Delete customer #{cust.id} ({cust.name})?", args.yes):
            raise CliError("aborted.", 1)
        try:
            customer_service.delete_customer(
                session, args.customer_id, actor=actor, org_id=op_org_id(args)
            )
        except customer_service.CustomerDeleteBlocked as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Deleted customer #{args.customer_id} ({cust.name}).",
            {"ok": True, "id": args.customer_id, "deleted": True},
        )
    return 0


def _customer_archive(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        try:
            cust = customer_service.archive_customer(
                session, args.customer_id, actor=actor, org_id=op_org_id(args)
            )
        except customer_service.CustomerArchiveError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Archived customer #{cust.id} ({cust.name}) -- retired from every picker.",
            {"ok": True, "id": cust.id, "name": cust.name, "archived": True},
        )
    return 0


def _customer_unarchive(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import customers as customer_service

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        try:
            cust = customer_service.unarchive_customer(
                session, args.customer_id, actor=actor, org_id=op_org_id(args)
            )
        except customer_service.CustomerArchiveError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Unarchived customer #{cust.id} ({cust.name}) -- offered in pickers again.",
            {"ok": True, "id": cust.id, "name": cust.name, "archived": False},
        )
    return 0


def _ai_optout_status(session, project) -> dict:
    from app.kernel import ai_optout

    return ai_optout.status(session, project)


def _comment_status(project) -> dict:
    from app.kernel import comment_option

    return comment_option.status(project)


def _cf_values(session, project) -> dict:
    from app.kernel import custom_fields

    return custom_fields.project_values(session, project)


def _cf_flat(value) -> str:
    from app.kernel import custom_fields

    return custom_fields.flat(None, value)


def _project_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.project_show(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.kernel import content_types, glossary, pair_providers, segmentation_profiles, tm
    from app.models import Customer, Document

    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        from app.kernel import content as _content

        locales = _content.active_target_locales(session, project.id)
        stopped_locales = _content.stopped_target_locales(session, project.id)
        from app.kernel import keys as keys_svc

        # SPEC-key-based-projects 2.5: a key project's internal documents are not
        # files and never appear on a document surface.
        docs = session.exec(
            select(Document)
            .where(Document.project_id == project.id, Document.fmt != keys_svc.KEY_DOC_FMT)
            .order_by(Document.created_at.desc())
        ).all()
        from app.kernel import lifecycle

        doc_status = lifecycle.document_status_map(session, [d.id for d in docs])
        customer = session.get(Customer, project.customer_id) if project.customer_id else None
        pairs = pair_providers.mapping_for_project(session, project.id)
        # Issue #19: due_date/content_type/segmentation_profile are all directly
        # settable (project set-due / set-content-type / segprofile assign) but
        # `show` never echoed them back.
        ct = (
            content_types.get(session, project.content_type_id) if project.content_type_id else None
        )
        # segmentation_profile_id: the project's OWN override (None = inherits the
        # customer/system-default chain). resolve_profile additionally surfaces
        # WHICH profile actually governs ingest today, mirroring the web project
        # page's "resolved" row so "inherit" is never a mystery over the CLI either.
        seg_resolved = segmentation_profiles.resolve_profile(session, project)
        info = {
            "id": project.id,
            "name": project.name,
            "source_locale": project.source_locale,
            "archived": project.archived_at is not None,  # L11 (parity with /api show)
            # AI opt-out (DPAI-08), parity with the /api detail read-back.
            **_ai_optout_status(session, project),
            # #690: the per-project "translate comments" option (parity with /api).
            **_comment_status(project),
            "customer_id": project.customer_id,
            "customer": customer.name if customer else "",
            "default_provider": project.default_provider,
            "target_locales": locales,
            # Connected projects: where the content comes from (parity with the /api detail).
            "intake": project.intake,
            # Connected projects: languages that were stopped (kept, no longer worked on).
            "stopped_locales": stopped_locales,
            # Phase 2: the per-pair provider mode + overrides.
            "provider_mode": project.provider_mode,
            "pair_providers": pairs,
            # YYYY-MM-DD (matches the /api due_date format -- due dates are stored
            # as a naive-UTC day boundary, never a time-of-day).
            "due_date": (project.due_date.strftime("%Y-%m-%d") if project.due_date else None),
            "content_type_id": project.content_type_id,
            "content_type_name": ct.name if ct else None,
            "segmentation_profile_id": project.segmentation_profile_id,
            "segmentation_profile_resolved_id": seg_resolved.id if seg_resolved else None,
            "segmentation_profile_resolved_name": seg_resolved.name if seg_resolved else None,
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
            "tm_count": tm.count(session, project.id),
            "ft_tm_count": tm.ft_count(session, project.id),
            "glossary_count": glossary.count(session, project.id),
            "custom_fields": _cf_values(session, project),
        }
        if emit.as_json:
            emit.json({"ok": True, **info})
        else:
            emit.say(f"Project #{project.id}: {project.name}")
            emit.say(
                f"  source: {project.source_locale}   provider: {project.default_provider}"
                + (f"   customer: {customer.name}" if customer else "")
            )
            emit.say(f"  target locales: {', '.join(locales) or '(none)'}")
            if stopped_locales:
                emit.say(f"  stopped locales: {', '.join(stopped_locales)}")
            emit.say(f"  provider mode: {project.provider_mode}")
            if pairs:
                for pr in pairs:
                    emit.say(
                        f"    {pr['source_locale']} -> {pr['target_locale']}: {pr['provider']}"
                    )
            emit.say(f"  due date: {info['due_date'] or '(none)'}")
            for _k, _v in info["custom_fields"].items():
                emit.say(f"  field {_k}: {_cf_flat(_v)}")
            emit.say(
                "  comments in Office files: "
                + (
                    "translated"
                    if info["translate_comments"]
                    else (
                        "Word only (project predates the option)"
                        if info["translate_comments_word"]
                        else "kept in the source language"
                    )
                )
            )
            emit.say("  content type: " + (f"#{ct.id} '{ct.name}'" if ct else "(general)"))
            if project.segmentation_profile_id:
                detail = (
                    f"('{seg_resolved.name}')"
                    if seg_resolved
                    else "(profile deleted; falls through to inherit chain)"
                )
                emit.say(f"  segmentation: override #{project.segmentation_profile_id} {detail}")
            else:
                resolved = (
                    f"#{seg_resolved.id} '{seg_resolved.name}'"
                    if seg_resolved
                    else "the built-in default"
                )
                emit.say(f"  segmentation: inherit chain -> resolved to {resolved}")
            emit.say(
                f"  TM: {info['tm_count']} segment / {info['ft_tm_count']} full-text"
                f"   glossary: {info['glossary_count']}"
            )
            emit.say(f"  documents ({len(docs)}):")
            emit.table(
                [(d.id, d.filename, d.fmt, d.mode, doc_status.get(d.id, "new")) for d in docs],
                headers=("id", "filename", "fmt", "mode", "status"),
            )
    return 0


def _project_leverage(args, emit) -> int:
    """CV-1: the 7-band WORD-based TM-leverage breakdown for a project — the SAME
    vendor_cost.project_leverage seam the project page + GET /api/projects/{id}/leverage
    call, so the numbers are byte-identical across surfaces."""
    if remote_mode(args):
        return _remote_impl.project_leverage(args, emit)
    require_org_scope(args, emit)
    from app.kernel import vendor_cost

    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        estimate = vendor_cost.project_leverage(session, project.id)
        rows = vendor_cost.estimate_band_rows(estimate)
        # M6: note key-based content is excluded, so an empty result isn't misread.
        note = None
        if vendor_cost.project_has_key_docs(session, project.id):
            note = (
                "this project has key-based content; leverage covers only its "
                "non-key documents (key catalogue values are excluded)."
            )
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "project_id": project.id,
                "total_words": estimate.get("total_words", 0),
                "total_segments": estimate.get("segment_count", 0),
                "billable_words": estimate.get("billable_words", 0),
                "covered_words": estimate.get("covered_words", 0),
                "bands": [
                    {
                        "band": r["band"],
                        "label": r["label"],
                        "words": r["words"],
                        "segments": r["segments"],
                        "covered": r["covered"],
                        "billable": r["billable"],
                    }
                    for r in rows
                ],
                "note": note,
            }
        )
    else:
        emit.say(f"Project #{project.id}: {project.name} - 7-band word leverage")
        emit.table(
            [
                (r["label"], r["words"], r["segments"], "covered" if r["covered"] else "billable")
                for r in rows
            ],
            headers=("band", "words", "segments", "scope"),
        )
        emit.say(
            f"  total words: {estimate.get('total_words', 0)}   "
            f"covered: {estimate.get('covered_words', 0)}   "
            f"billable (post-edit): {estimate.get('billable_words', 0)}"
        )
        if note:
            emit.say(f"  note: {note}")
    return 0


def _project_apply_langset(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.project_apply_langset(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit, content, languages

    actor = getattr(args, "actor", None) or "cli"
    oid = op_org_id(args)
    with _session() as session:
        project = _get_project(session, args.project_id, oid)
        ls = get_scoped_or_404(languages.get(args.langset_id), args.langset_id, "language set", oid)
        added = content.apply_language_set_to_project(session, project.id, ls, actor=actor)
        audit.log(
            actor=actor,
            action="project.languages_apply",
            target=f"project {project.id} set {ls.id} ({ls.name})",
            customer_id=project.customer_id,
        )
        emit.result(
            f"Applied '{ls.name}' to project #{project.id}: added {added} locale(s).",
            {"ok": True, "project_id": project.id, "langset_id": ls.id, "added": added},
        )
    return 0


def _project_add_locale(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.project_add_locale(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            added = content.add_target_locale(session, project.id, args.locale, actor=actor)
        except content.InvalidLocaleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        # Active-pairs confinement (the maintainer's P2), CLI parity: WARN (never refuse) when
        # the locale is outside the customer's active pairs — the web picker hides it
        # behind "Show all languages" but still accepts it, so the CLI accepts + warns.
        if not getattr(args, "all_targets", False) and project.customer_id:
            from app.kernel import customers as customer_service

            _cust = customer_service.get(session, project.customer_id)
            if customer_service.targets_outside_pairs(
                _cust, [args.locale], source_locale=project.source_locale
            ):
                emit.warn(
                    f"target {args.locale} is not in customer "
                    f"#{project.customer_id}'s active pairs (added anyway; "
                    "configure pairs with `customer pairs add`, or pass "
                    "--all-targets to silence)."
                )
        emit.result(
            f"{'Added' if added else 'Already present:'} {args.locale} on project #{project.id}.",
            {"ok": True, "project_id": project.id, "locale": args.locale, "added": added},
        )
    return 0


def _project_remove_locale(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.project_remove_locale(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        preview = content.remove_locale_preview(session, project.id, args.locale)
        if preview is None:
            raise CliError(f"locale {args.locale!r} is not on project #{project.id}.", EXIT_USAGE)
        if preview["blocked"]:
            raise CliError(preview["blocked_reason"], EXIT_USAGE)
        d = preview["deletes"]
        stop = preview.get("mode") == "stop"
        if not confirm(
            emit,
            (
                f"Stop {preview['locale']} on project #{project.id}? It stops at the next run "
                "(nothing is deleted: segments, TM + glossary stay)."
                if stop
                else f"Remove {preview['locale']} from project #{project.id} "
                f"(deletes {d['segments']} segment(s); keeps TM + glossary)?"
            ),
            args.yes,
        ):
            raise CliError("aborted.", EXIT_USAGE)
        try:
            deleted = content.remove_target_locale(
                session, project.id, args.locale, actor=actor, org_id=op_org_id(args)
            )
        except content.LocaleRemovalBlocked as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if stop:
            emit.result(
                f"Stopped {preview['locale']} on project #{project.id}: the next run no longer "
                "pulls, translates or writes it (nothing deleted).",
                {
                    "ok": True,
                    "project_id": project.id,
                    "locale": preview["locale"],
                    "deleted": deleted,
                    "stopped": True,
                },
            )
            return 0
        emit.result(
            f"Removed {preview['locale']} from project #{project.id} "
            f"(deleted {deleted['segments']} segment(s); TM + glossary kept).",
            {
                "ok": True,
                "project_id": project.id,
                "locale": preview["locale"],
                "deleted": deleted,
                "stopped": False,
            },
        )
    return 0


def _project_status(args, emit) -> int:
    # In-process only (writes status + audits on the host DB). Parity with
    # POST /projects/{id}/status.
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import lifecycle
    from app.models import ProjectStatus

    actor = getattr(args, "actor", None) or "cli"
    status = (args.status or "").strip().lower()
    if status not in ProjectStatus.ALL:
        raise CliError(
            f"unknown status {status!r} (choose one of: {', '.join(ProjectStatus.ALL)}).",
            EXIT_USAGE,
        )
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        before = project.status
        # #580: same allowed graph as POST /api/projects/{id}/status and the web form.
        refused = lifecycle.transition_error(before, status)
        if refused:
            raise CliError(refused, EXIT_USAGE)
        lifecycle.set_status(session, project.id, status, actor=actor)
        emit.result(
            f"Project #{project.id} status: {before} -> {status}.",
            {"ok": True, "project_id": project.id, "status": status, "previous": before},
        )
    return 0


def _project_set_providers(args, emit) -> int:
    # Remote-capable: a remote caller dispatches to /api/projects/{id}/pair-providers
    # (the same route the web settings form posts to). The in-process arm calls the
    # shared pair_providers.apply_mode service — one code path, web+CLI parity.
    if remote_mode(args):
        return _remote_impl.project_set_providers(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit, pair_providers

    actor = getattr(args, "actor", None) or "cli"
    pair_flags = list(getattr(args, "pair_provider", []) or [])
    per_pair = getattr(args, "per_pair", False) or bool(pair_flags)
    from app.models import ProviderMode as _PM

    mode = _PM.PER_PAIR if per_pair else _PM.UNIFORM
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            mapping = pair_providers.parse_pair_mapping(pair_flags)
            pair_providers.apply_mode(session, project, mode, mapping, org_id=op_org_id(args))
        except pair_providers.PairProviderError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="project.pair_providers",
            target=f"project {project.id} -> mode {mode} ({len(mapping)} pair override(s))",
            customer_id=project.customer_id,
        )
        pairs = pair_providers.mapping_for_project(session, project.id)
        emit.result(
            f"Project #{project.id} provider mode: {mode} ({len(pairs)} pair override(s)).",
            {"ok": True, "project_id": project.id, "provider_mode": mode, "pair_providers": pairs},
        )
    return 0


def _project_set_due(args, emit) -> int:
    # Remote-capable (adversarial-review FIX 5): a remote caller dispatches to
    # /api/projects/{id}/due (the same route the web form posts to) via
    # _remote_impl, exactly like project_add_locale above. The in-process arm
    # below is unchanged -- it still writes due_date + audits on the host DB
    # directly. Parity with POST /projects/{id}/due either way.
    if remote_mode(args):
        return _remote_impl.project_set_due(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    # --clear (or an omitted date arg) clears; otherwise the positional date is set.
    due = "" if getattr(args, "clear", False) else (getattr(args, "due", "") or "")
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            content.set_project_due(session, project.id, due, actor=actor)
        except content.DueDateError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        session.refresh(project)
        shown = project.due_date.strftime("%Y-%m-%d") if project.due_date else None
        emit.result(
            f"Project #{project.id} due date: {shown or '(cleared)'}.",
            {"ok": True, "project_id": project.id, "due_date": shown},
        )
    return 0


def _project_set_content_type(args, emit) -> int:
    # Remote-capable: a remote caller dispatches to /api/projects/{id}/content-type
    # (the same route the web settings form posts to). The in-process arm calls the
    # shared content.set_project_content_type service — one code path, web+CLI parity.
    if remote_mode(args):
        return _remote_impl.project_set_content_type(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    ct_id = None if getattr(args, "clear", False) else getattr(args, "content_type_id", None)
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            content.set_project_content_type(session, project.id, ct_id, actor=actor)
        except content.InvalidContentTypeError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        session.refresh(project)
        # SPEC-content-types §4.5: reassigning does NOT retro-restamp existing TM rows —
        # they keep the content type they were WRITTEN under (honest provenance). Warn so
        # the operator isn't surprised that already-stored matches keep their old scope.
        emit.result(
            f"Project #{project.id} content type: "
            f"{project.content_type_id or '(cleared)'}. "
            "(Existing TM rows keep the content type they were written under; only "
            "future translations use the new type.)",
            {"ok": True, "project_id": project.id, "content_type_id": project.content_type_id},
        )
    return 0


def _project_set_intake(args, emit) -> int:
    """Switch a project between uploads and a connected source (only while it has no
    documents, no keys and no source binding). Remote-capable (POST /api/projects/{id}/intake)."""
    if remote_mode(args):
        return _remote_impl.project_set_intake(args, emit)
    require_org_scope(args, emit)
    from app.kernel import connected_projects

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            connected_projects.set_intake(session, project, args.intake, actor=actor)
        except connected_projects.IntakeError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Project #{project.id} intake: {project.intake}.",
            {
                "ok": True,
                "project_id": project.id,
                "intake": project.intake,
                **(
                    {"setup_url": connected_projects.setup_url(project.id)}
                    if connected_projects.is_connected(project)
                    else {}
                ),
            },
        )
    return 0


def _project_make_connected(args, emit) -> int:
    """Make an EXISTING project that a connection already feeds a connected project bound to
    that same connection (no lane, no schedule, no run). Remote-capable
    (POST /api/projects/{id}/make-connected)."""
    if remote_mode(args):
        return _remote_impl.project_make_connected(args, emit)
    require_org_scope(args, emit)
    from app.kernel import connected_projects

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            row = connected_projects.convert_existing(
                session, project, getattr(args, "connection", None), actor=actor
            )
        except connected_projects.IntakeError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Project #{project.id} is now a connected project on connection "
            f"#{row.connection_id}. Nothing is scheduled or run.",
            {
                "ok": True,
                "project_id": project.id,
                "intake": project.intake,
                "project_type": project.project_type,
                "connection_id": row.connection_id,
                "source_url": connected_projects.setup_url(project.id),
            },
        )
    return 0


def _project_set_customer(args, emit) -> int:
    """Assign a customer to a customer-LESS project (from-NULL only; reassignment is not
    supported). Remote-capable (POST /api/projects/{id}/set-customer parity)."""
    if remote_mode(args):
        return _remote_impl.project_set_customer(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            content.assign_project_customer(
                session, project.id, args.customer_id, actor=actor, org_id=op_org_id(args)
            )
        except (content.ProjectCustomerAssignError, content.ArchivedCustomerError) as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        session.refresh(project)
        emit.result(
            f"Project #{project.id} customer: {project.customer_id}.",
            {"ok": True, "project_id": project.id, "customer_id": project.customer_id},
        )
    return 0


# --- attached TM sources / glossaries (feedback item 5) ----------------------
# Six thin verbs over app/services/project_sources — the SAME seam the project page's
# two attach cards call, so the surfaces cannot drift on the scoping rules. All
# in-process (require_local): they write tenant link rows on the host DB and have no
# /api route, matching `project status` / `rename` / `delete`.
# ASCII-only output (Windows consoles): "->" for pairs and attachment direction.
def _project_tm_sources_list(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import project_sources

    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        rows = project_sources.list_tm_sources(session, project.id)
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "project_id": project.id,
                    "tm_sources": [
                        {
                            "source_project_id": r["source_project_id"],
                            "name": r["name"],
                            "entry_count": r["entry_count"],
                            "pairs": r["pairs"],
                        }
                        for r in rows
                    ],
                }
            )
        else:
            emit.say(f"Project #{project.id} TM sources ({len(rows)}):")
            emit.table(
                [
                    (
                        r["source_project_id"],
                        r["name"],
                        r["entry_count"],
                        ", ".join(r["pairs"]) or "-",
                    )
                    for r in rows
                ],
                headers=("source_id", "name", "entries", "pairs"),
            )
    return 0


def _project_tm_sources_add(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, project_sources

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            view = project_sources.add_tm_source(
                session, project.id, args.source_project_id, created_by=actor
            )
        except project_sources.AttachError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="project.tm_source.add",
            target=f"project {project.id} <- project {view['source_project_id']}",
            customer_id=project.customer_id,
        )
        emit.result(
            f"Project #{project.id} now also matches TM from "
            f"#{view['source_project_id']} '{view['name']}' "
            f"({view['entry_count']} entries). Nothing was copied.",
            {
                "ok": True,
                "project_id": project.id,
                "source_project_id": view["source_project_id"],
                "entry_count": view["entry_count"],
            },
        )
    return 0


def _project_tm_sources_remove(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, project_sources

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        removed = project_sources.remove_tm_source(session, project.id, args.source_project_id)
        if removed:
            audit.log(
                actor=actor,
                action="project.tm_source.remove",
                target=f"project {project.id} -x- project {args.source_project_id}",
                customer_id=project.customer_id,
            )
        emit.result(
            (
                f"Detached TM source #{args.source_project_id} from project #{project.id}."
                if removed
                else f"Project #{project.id} has no TM source #{args.source_project_id}."
            ),
            {
                "ok": True,
                "project_id": project.id,
                "source_project_id": args.source_project_id,
                "removed": removed,
            },
        )
    return 0


def _project_glossaries_list(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import project_sources

    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        rows = project_sources.list_glossary_pins(session, project.id)
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "project_id": project.id,
                    "glossaries": [
                        {
                            "glossary_id": r["glossary_id"],
                            "name": r["name"],
                            "status": r["status"],
                            "active": r["active"],
                            "term_count": r["term_count"],
                            "pairs": r["pairs"],
                        }
                        for r in rows
                    ],
                }
            )
        else:
            emit.say(f"Project #{project.id} attached glossaries ({len(rows)}):")
            emit.table(
                [
                    (
                        r["glossary_id"],
                        r["name"],
                        r["status"],
                        "yes" if r["active"] else "no (needs approval)",
                        r["term_count"],
                        ", ".join(r["pairs"]) or "-",
                    )
                    for r in rows
                ],
                headers=("id", "name", "status", "applied", "terms", "pairs"),
            )
    return 0


def _project_glossaries_add(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, project_sources

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            view = project_sources.add_glossary_pin(
                session,
                project.id,
                args.glossary_id,
                created_by=actor,
                # Operator scope: org-confined under the SaaS gate, never
                # customer-bound (the CLI operator is staff). Same seam the web
                # route passes its caller scope through (ISO-4).
                org_id=op_org_id(args),
                bound_customer_id=None,
            )
        except project_sources.AttachError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="project.glossary.attach",
            target=f"project {project.id} <- glossary {view['glossary_id']}",
            customer_id=project.customer_id,
        )
        note = "" if view["active"] else " It is a DRAFT and contributes nothing until approved."
        emit.result(
            f"Attached glossary #{view['glossary_id']} '{view['name']}' to project "
            f"#{project.id} ({view['term_count']} term(s)).{note}",
            {
                "ok": True,
                "project_id": project.id,
                "glossary_id": view["glossary_id"],
                "active": view["active"],
                "term_count": view["term_count"],
            },
        )
    return 0
