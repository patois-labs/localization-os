"""cmd_project: part5

Documented in: docs/architecture/content-and-projects.md
"""

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    op_org_scope,
    remote_mode,
    require_local,
    require_org_scope,
)
from app.cli.cmd_project_ai import AI_CHOICES, _customer_set_ai
from app.cli.cmd_project_glossary import _meta_summary
from app.cli.cmd_project_part2 import (
    _customer_archive,
    _customer_delete,
    _customer_pairs_add,
    _customer_pairs_import,
    _customer_pairs_list,
    _customer_pairs_remove,
    _customer_rename,
    _customer_set_default,
    _customer_unarchive,
)
from app.cli.cmd_project_project import (
    _concept_term_tdoc,
    _customer_create,
    _customer_show,
    _customer_update,
    _get_project,
    _gmod_get,
    _list_window_args,
    _note_hidden,
    _note_truncated,
    _resolve_window,
    _term_meta_from_args,
)
from app.cli.cmd_project_term_extraction_candidat import _customer_list


def _register_customer(subparsers) -> None:
    cust = subparsers.add_parser(
        "customer", help="Customers (create, list, show, update, set-default, pairs)."
    )
    sub = cust.add_subparsers(dest="customer_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create a customer.")
    p.add_argument("name", help="customer name.")
    p.set_defaults(func=_customer_create)

    p = sub.add_parser("list", help="List customers (active only by default).")
    p.add_argument(
        "--archived",
        action="store_true",
        help="list ONLY archived customers (SPEC-customer-archive).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="all_customers",
        help="list ALL customers, active and archived, with a status column.",
    )
    _list_window_args(p)
    p.set_defaults(func=_customer_list)

    p = sub.add_parser(
        "show",
        help="Customer 360: projects, connectors, workflows, "
        "language pairs, TM stats (P4; --json for the full payload).",
    )
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.set_defaults(func=_customer_show)

    p = sub.add_parser(
        "update", help="Update a customer's preferences (settings blob) via key=value pairs (P4)."
    )
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="K=V",
        dest="settings",
        help="a preference key=value (repeatable), e.g. --set notes='VIP'. "
        "An empty value (K=) removes the key.",
    )
    p.set_defaults(func=_customer_update)

    p = sub.add_parser("set-default", help="Set a customer's scoped defaults (P4).")
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.add_argument("--provider", default=None, help="default engine/provider (blank clears it).")
    p.add_argument(
        "--workflow",
        dest="workflow_id",
        default=None,
        help="default workflow id (blank clears it).",
    )
    p.add_argument(
        "--segmentation",
        dest="segmentation_profile_id",
        default=None,
        help="default segmentation-profile id (P6 forward ref; blank clears).",
    )
    p.add_argument(
        "--content-type",
        dest="content_type_id",
        default=None,
        help="default content type id (SPEC-content-types; blank clears).",
    )
    p.set_defaults(func=_customer_set_default)

    pairs = sub.add_parser("pairs", help="Manage a customer's explicit active language pairs (P4).")
    psub = pairs.add_subparsers(dest="pairs_cmd", metavar="<subcommand>")
    psub.required = True
    pp = psub.add_parser(
        "list", help="List a customer's language pairs (declared + derived from projects)."
    )
    pp.add_argument("customer_id", type=bounded_id, help="customer id.")
    pp.set_defaults(func=_customer_pairs_list)
    pp = psub.add_parser("add", help="Add an explicit 'src->tgt' language pair.")
    pp.add_argument("customer_id", type=bounded_id, help="customer id.")
    pp.add_argument("pair", help="the pair, e.g. en-US->fr-FR.")
    pp.set_defaults(func=_customer_pairs_add)
    pp = psub.add_parser("remove", help="Remove an explicit 'src->tgt' language pair.")
    pp.add_argument("customer_id", type=bounded_id, help="customer id.")
    pp.add_argument("pair", help="the pair to remove.")
    pp.set_defaults(func=_customer_pairs_remove)

    pp = psub.add_parser(
        "import",
        help="Bulk-import language pairs from a CSV "
        "(source_locale,target_locale columns; atomic + idempotent).",
    )
    pp.add_argument("customer_id", type=bounded_id, help="customer id.")
    pp.add_argument("csv", help="path to the .csv file.")
    pp.set_defaults(func=_customer_pairs_import)

    p = sub.add_parser("rename", help="Rename a customer.")
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.add_argument("name", help="the new name.")
    p.set_defaults(func=_customer_rename)

    p = sub.add_parser(
        "delete",
        help="Delete a customer (DESTRUCTIVE; fails closed "
        "while anything references it -- no cascade).",
    )
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_customer_delete)

    p = sub.add_parser(
        "archive",
        help="Archive (retire) a customer: it drops out of "
        "every picker + the default list; existing projects keep working.",
    )
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.set_defaults(func=_customer_archive)

    p = sub.add_parser(
        "unarchive", help="Unarchive (reactivate) a customer: offer it in pickers again."
    )
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.set_defaults(func=_customer_unarchive)

    p = sub.add_parser(
        "set-ai",
        help="Switch AI off or back on for EVERY project of a customer (privacy "
        "opt-out). 'off' applies to current and future projects; a project cannot undo it.",
    )
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.add_argument("state", choices=AI_CHOICES, help="off = instance AI off; on = allowed.")
    p.set_defaults(func=_customer_set_ai)


def _project_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.project_list(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.models import Customer, Project, ProjectStatus

    oid = op_org_id(args)
    # Status filter (parity with GET /projects ?status=). Blank/"default" hides
    # done/closed/cancelled; "all" shows everything; a specific value filters to it.
    status = (getattr(args, "status", "") or "").strip().lower()
    with _session() as session:
        # L8: (created_at, id) so the LIMIT/OFFSET window is deterministic on Postgres.
        stmt = select(Project).order_by(Project.created_at.desc(), Project.id.desc())
        if oid is not None:
            stmt = stmt.where(Project.org_id == oid)
        connected = bool(getattr(args, "connected", False))
        if connected:
            stmt = stmt.where(Project.intake == "connected")
        if status in ("", "default"):
            stmt = stmt.where(Project.status.notin_(ProjectStatus.HIDDEN_BY_DEFAULT))
        elif status == "all":
            pass
        elif status in ProjectStatus.ALL:
            stmt = stmt.where(Project.status == status)
        else:
            raise CliError(
                f"unknown status {status!r} (choose one of: "
                f"{', '.join(ProjectStatus.ALL)}, all, default).",
                EXIT_USAGE,
            )
        # Archive (residual sweep): default hides ARCHIVED projects (matching the web
        # /projects list + GET /api/projects); --archived all|only widens/inverts it.
        # Active branch routed through the shipped seam (M4).
        from app.kernel import content as _content

        arch = (getattr(args, "archived", "active") or "active").strip().lower()
        if arch == "only":
            stmt = stmt.where(Project.archived_at.is_not(None))
        elif arch != "all":
            stmt = _content.project_active_only(stmt)
        # AU-7: bound the list in SQL (window applied after the status/archive filters),
        # capped at _LIST_CAP with a truncation note -- never a silent cutoff.
        limit, offset = _resolve_window(args)
        from app.cli.cmd_field import apply_list_options

        stmt, cf_cols = apply_list_options(session, stmt, args)
        fetched = list(session.exec(stmt.offset(offset).limit(limit + 1)).all())
        truncated = len(fetched) > limit
        projects = fetched[:limit]
        # #607: how many in-scope projects the DEFAULT archive/status filters left out.
        from sqlalchemy import func

        def _n(q):
            return int(session.exec(select(func.count()).select_from(q.subquery())).one())

        hidden_q = select(Project)
        if oid is not None:
            hidden_q = hidden_q.where(Project.org_id == oid)
        hidden = {"archived": 0, "finished": 0}
        if arch not in ("all", "only"):
            arch_q = hidden_q.where(Project.archived_at.is_not(None))
            if status not in ("", "default", "all"):
                arch_q = arch_q.where(Project.status == status)  # only what THIS query hid
            hidden["archived"] = _n(arch_q)
        if status in ("", "default"):
            fin = hidden_q.where(Project.status.in_(ProjectStatus.HIDDEN_BY_DEFAULT))
            if arch not in ("all", "only"):
                fin = _content.project_active_only(fin)
            hidden["finished"] = _n(fin)
        cust_by_id = {c.id: c.name for c in session.exec(select(Customer)).all()}
        from app.kernel import custom_fields as _cfs

        cf_vals = _cfs.payload_for_projects(session, projects, op_org_scope(args))
        from app.kernel import connected_projects as _cpj

        src_labels = _cpj.source_labels(session, [p.id for p in projects])
        rows, payload = [], []
        for p in projects:
            cust = cust_by_id.get(p.customer_id, "") if p.customer_id else ""
            shown = cf_vals.get(p.id, {})
            rows.append(
                (
                    p.id,
                    p.name,
                    p.source_locale,
                    cust,
                    p.status,
                    *((src_labels.get(p.id, {}).get("label", ""),) if connected else ()),
                    *(_cfs.flat(None, shown.get(k)) for k in cf_cols),
                )
            )
            payload.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "source_locale": p.source_locale,
                    "customer": cust,
                    "archived": p.archived_at is not None,
                    "status": p.status,
                    "intake": p.intake,
                    **({"source": src_labels[p.id]} if p.id in src_labels else {}),
                    "custom_fields": shown,
                }
            )
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "projects": payload,
                    "truncated": truncated,
                    "limit": limit,
                    "offset": offset,
                    "hidden": hidden,
                }
            )
        else:
            emit.table(
                rows,
                headers=(
                    "id",
                    "name",
                    "source",
                    "customer",
                    "status",
                    *(("connected source",) if connected else ()),
                    *cf_cols,
                ),
            )
            _note_truncated(emit, truncated, offset, limit)
            _note_hidden(emit, hidden)
    return 0


def _glossary_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.glossary_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary

    limit, offset = _resolve_window(args)
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        page = glossary.window_entries(
            session, project_id=args.project_id, cursor=args.cursor, limit=limit, offset=offset
        )
        window, truncated = page["entries"], page["has_more"]
        rows, payload = [], []
        for item in window:
            e = item["entry"]
            n_senses = len(item["senses"])
            rows.append(
                (
                    e.id,
                    e.source_term,
                    e.target_term,
                    f"{e.source_locale}->{e.target_locale}",
                    "yes" if e.dnt else "",
                    n_senses,
                )
            )
            payload.append(
                {
                    "id": e.id,
                    "source_term": e.source_term,
                    "target_term": e.target_term,
                    "source_locale": e.source_locale,
                    "target_locale": e.target_locale,
                    "dnt": e.dnt,
                    "senses": n_senses,
                }
            )
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "entries": payload,
                    "truncated": truncated,
                    "limit": limit,
                    "offset": offset,
                    "next_cursor": page["next_cursor"],
                }
            )
        else:
            emit.table(rows, headers=("id", "source", "target", "locales", "dnt", "senses"))
            if args.cursor is None:
                _note_truncated(emit, truncated, offset, limit)
            elif truncated:
                emit.warn(f"More terms: pass --cursor {page['next_cursor']} (reset --offset to 0).")
    return 0


def _gmod_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_show(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary_module as gm

    with _session() as session:
        g = _gmod_get(session, args)
        d = gm.to_dict(session, g)
        from app.kernel import glossary

        limit, offset = _resolve_window(args)
        page = glossary.window_entries(
            session, glossary_id=g.id, cursor=args.cursor, limit=limit, offset=offset
        )
        entries = page["entries"]
        paging = {
            "limit": limit,
            "offset": offset,
            "truncated": page["has_more"],
            "next_cursor": page["next_cursor"],
        }
        if emit.as_json:
            # Issue #19: --json must carry the full terms array (source/target/dnt +
            # ALL TBX-ish meta + senses) -- to_dict()'s bare entry_count made the rich
            # term-database metadata that add-term/edit-term accepts unreadable via
            # the CLI.
            terms = [
                {
                    "id": it["entry"].id,
                    "source_term": it["entry"].source_term,
                    "target_term": it["entry"].target_term,
                    "source_locale": it["entry"].source_locale,
                    "target_locale": it["entry"].target_locale,
                    "dnt": it["entry"].dnt,
                    "meta": it["entry"].meta or {},
                    "senses": [
                        {
                            "id": s.id,
                            "sense_label": s.sense_label,
                            "context_cue": s.context_cue,
                            "target_term": s.target_term,
                        }
                        for s in it["senses"]
                    ],
                }
                for it in entries
            ]
            emit.json({"ok": True, **d, "terms": terms, **paging})
        else:
            emit.say(
                f"Glossary #{g.id} '{g.name}' [{g.status}]"
                + (f" approved by {g.approved_by}" if g.approved_by else "")
            )
            emit.say(
                "  pairs: "
                + (
                    ", ".join(f"{p['source_locale']}->{p['target_locale']}" for p in d["pairs"])
                    or "-"
                )
            )
            emit.table(
                [
                    (
                        it["entry"].id,
                        it["entry"].source_term,
                        it["entry"].target_term,
                        f"{it['entry'].source_locale}->{it['entry'].target_locale}",
                        "yes" if it["entry"].dnt else "",
                        len(it["senses"]),
                        _meta_summary(it["entry"].meta),
                    )
                    for it in entries
                ],
                headers=("id", "source", "target", "locales", "dnt", "senses", "meta"),
            )
            if paging.get("truncated"):
                emit.warn(
                    f"More terms: pass --cursor {paging['next_cursor']} (reset --offset to 0)."
                )
    return 0


def _gmod_add_term(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_add_term(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary_module as gm

    meta = _term_meta_from_args(args) or None
    with _session() as session:
        _gmod_get(session, args)
        try:
            entry = gm.add_term(
                session,
                args.glossary_id,
                args.source_term,
                target_term=args.target_term,
                source_locale=(args.source or "").strip(),
                target_locale=(args.target or "").strip(),
                dnt=args.dnt,
                meta=meta,
            )
        except gm.GlossaryModuleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if entry is None:
            raise CliError("source term is blank.", EXIT_USAGE)
        emit.result(
            f"Added term '{entry.source_term}' -> '{entry.target_term}' "
            f"to glossary #{args.glossary_id}.",
            {
                "ok": True,
                "glossary_id": args.glossary_id,
                "source_term": entry.source_term,
                "target_term": entry.target_term,
                "dnt": entry.dnt,
                "meta": entry.meta or {},
            },
        )
    return 0


def _gmod_edit_term(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_edit_term(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary_module as gm

    provided = _term_meta_from_args(args)
    with _session() as session:
        _gmod_get(session, args)
        # MERGE the provided meta flags onto the term's existing meta so a per-field
        # CLI edit doesn't clobber the other cells (the web row editor posts every
        # field, so it uses replace; the CLI is field-at-a-time, so it merges). None
        # (no meta flag given) leaves meta untouched.
        meta = None
        if provided:
            from app.models import GlossaryEntry

            cur = session.get(GlossaryEntry, args.entry_id)
            meta = {**((cur.meta if cur else None) or {}), **provided}
        entry = gm.edit_term(
            session,
            args.glossary_id,
            args.entry_id,
            target_term=args.target_term,
            dnt=args.dnt,
            meta=meta,
            case_sensitive=getattr(args, "case_sensitive", None),
        )
        if entry is None:
            raise CliError(
                f"no such term {args.entry_id} in glossary #{args.glossary_id}.", EXIT_USAGE
            )
        emit.result(
            f"Updated term #{entry.id} ('{entry.source_term}' -> "
            f"'{entry.target_term}') in glossary #{args.glossary_id}.",
            {
                "ok": True,
                "glossary_id": args.glossary_id,
                "entry_id": entry.id,
                "source_term": entry.source_term,
                "target_term": entry.target_term,
                "dnt": entry.dnt,
                "case_sensitive": entry.case_sensitive,
                "meta": entry.meta or {},
            },
        )
    return 0


def _gmod_term_add(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.kernel import glossary_concepts as gc

    tdoc = _concept_term_tdoc(args)
    tdoc["term"] = args.term
    tdoc["locale"] = args.locale
    with _session() as session:
        _gmod_get(session, args)
        try:
            term = gc.add_concept_term(session, args.glossary_id, args.concept_id, **tdoc)
        except gc.ConceptError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Added term #{term.id} ('{term.term}' [{term.locale or '*'}], {term.status}) "
            f"to concept #{args.concept_id}.",
            {
                "ok": True,
                "glossary_id": args.glossary_id,
                "concept_id": args.concept_id,
                "term_id": term.id,
                "term": term.term,
                "locale": term.locale,
                "status": term.status,
                "audience": term.audience,
                "dnt": term.dnt,
            },
        )
    return 0


def _gmod_term_edit(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.kernel import glossary_concepts as gc

    tdoc = _concept_term_tdoc(args)
    if args.locale is not None:
        tdoc["locale"] = args.locale
    if args.surface is not None:
        tdoc["term"] = args.surface
    if not tdoc:
        raise CliError("nothing to edit: pass at least one field flag.", EXIT_USAGE)
    with _session() as session:
        _gmod_get(session, args)
        try:
            term = gc.edit_concept_term(
                session, args.glossary_id, args.concept_id, args.term_id, **tdoc
            )
        except gc.ConceptError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Updated term #{term.id} ('{term.term}' [{term.locale or '*'}], {term.status}) "
            f"in concept #{args.concept_id}.",
            {
                "ok": True,
                "glossary_id": args.glossary_id,
                "concept_id": args.concept_id,
                "term_id": term.id,
                "term": term.term,
                "locale": term.locale,
                "status": term.status,
                "audience": term.audience,
                "dnt": term.dnt,
            },
        )
    return 0
