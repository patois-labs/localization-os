"""cmd_project: term extraction + candidate review (feat/term-extraction)

Documented in: docs/architecture/content-and-projects.md
"""

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    glossary_import_cli_error,
    op_org_id,
    projected_entries_sentence,
    quiet_import_log,
    remote_mode,
    require_local,
    require_org_scope,
)
from app.cli.cmd_project_glossary import (
    _tm_delete,
    _tm_edit,
    _tm_export,
    _tm_import,
    _tm_list,
    _tm_scrub_hedges,
    _tm_search,
    _tm_show,
    _tm_stats,
)
from app.cli.cmd_project_project import (
    _changed_note,
    _concept_payload,
    _concept_terms,
    _gmod_get,
    _note_truncated,
    _parse_locale_surface_terms,
    _read_bytes,
    _resolve_window,
    _skipped_note,
    _write_bytes,
)


def _register_tm(subparsers) -> None:
    tmp = subparsers.add_parser(
        "tm", help="Translation memory (import TMX or Trados SDLTM, export TMX)."
    )
    sub = tmp.add_subparsers(dest="tm_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("import", help="Import a TMX or Trados .sdltm file into a project's TM.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("tmx", help="path to the .tmx or .sdltm file.")
    p.add_argument("--source", required=True, help="source locale to import.")
    p.add_argument("--target", required=True, help="target locale to import.")
    p.add_argument(
        "--acknowledge-placeholder-loss",
        action="store_true",
        help="import units even though the target drops or adds a placeholder ({0}, %%s, "
        "{{name}}); by default those units are withheld and listed. Inline-tag breakage "
        "is never imported.",
    )
    p.set_defaults(func=_tm_import)

    p = sub.add_parser("export", help="Export a project's TM to a TMX file.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("tmx", help="destination .tmx path.")
    p.add_argument("--source", required=True, help="source locale to export.")
    p.add_argument("--target", required=True, help="target locale to export.")
    p.add_argument("--force", action="store_true", help="overwrite the file if it exists.")
    p.set_defaults(func=_tm_export)

    # --- TM MANAGEMENT module: browse / usage / lifecycle over segment TM rows. ---
    # Shared filter flags (added to list / search / delete) so a bulk delete uses the
    # very same filters the browse commands do.
    def _add_tm_filters(sp, with_search=True):
        # Customer-first browse (2026-07-14: TM is organized by customer, there
        # is NO global TM). --customer scopes the browse to ONE customer's projects.
        # In-process is a trusted operator (no bound web/API identity), so it is an
        # explicit optional scope here — the web/API forcing + single-customer
        # auto-select semantics are session concepts; the CLI surfaces the scope flag.
        sp.add_argument(
            "--customer",
            dest="tm_customer",
            type=bounded_id,
            default=None,
            help="scope the browse to one customer's TM (customer id).",
        )
        sp.add_argument("--source", dest="f_source", default=None, help="filter by source locale.")
        sp.add_argument("--target", dest="f_target", default=None, help="filter by target locale.")
        sp.add_argument(
            "--project",
            dest="f_project",
            type=bounded_id,
            default=None,
            help="filter by project id.",
        )
        sp.add_argument(
            "--content-type",
            dest="f_content_type",
            default=None,
            help="filter by content type: an id, or 'general'/'none' for "
            "the untyped (General) bucket.",
        )
        if with_search:
            sp.add_argument(
                "--query",
                dest="f_query",
                default=None,
                help="substring match on source or target text.",
            )
        sp.add_argument(
            "--created-before",
            dest="f_created_before",
            default=None,
            help="only entries created before YYYY-MM-DD.",
        )
        sp.add_argument(
            "--created-after",
            dest="f_created_after",
            default=None,
            help="only entries created on/after YYYY-MM-DD.",
        )
        sp.add_argument(
            "--min-use",
            dest="f_min_use",
            type=int,
            default=None,
            help="only entries used at least N times.",
        )
        sp.add_argument(
            "--max-use",
            dest="f_max_use",
            type=int,
            default=None,
            help="only entries used at most N times.",
        )
        sp.add_argument(
            "--unused",
            dest="f_unused",
            action="store_true",
            help="only never-used entries (use_count 0).",
        )

    p = sub.add_parser("list", help="Browse TM entries (paginated, filterable).")
    _add_tm_filters(p)
    p.add_argument(
        "--order",
        default="recent",
        choices=("recent", "oldest", "most_used", "least_used", "recently_used"),
        help="sort order (default recent).",
    )
    p.add_argument("--page", type=int, default=1, help="page number (default 1).")
    p.add_argument(
        "--per-page",
        dest="per_page",
        type=int,
        default=50,
        help="entries per page (default 50, max 500).",
    )
    p.set_defaults(func=_tm_list)

    p = sub.add_parser("search", help="Substring search across TM source/target text.")
    p.add_argument("text", help="the substring to search for.")
    _add_tm_filters(p, with_search=False)
    p.add_argument("--page", type=int, default=1, help="page number (default 1).")
    p.add_argument(
        "--per-page",
        dest="per_page",
        type=int,
        default=50,
        help="entries per page (default 50, max 500).",
    )
    p.set_defaults(func=_tm_search)

    p = sub.add_parser("show", help="Show one TM entry (source/target, usage stats).")
    p.add_argument("entry_id", type=bounded_id, help="TM entry id.")
    p.set_defaults(func=_tm_show)

    p = sub.add_parser(
        "edit",
        help="Edit a TM entry's TARGET text in place "
        "(source is the match key and cannot be edited).",
    )
    p.add_argument("entry_id", type=bounded_id, help="TM entry id.")
    p.add_argument("--target", required=True, help="the new target text.")
    p.add_argument(
        "--acknowledge-placeholder-loss",
        action="store_true",
        help="store the edit even though it drops or adds a placeholder ({0}, %%s, "
        "{{name}}); by default such an edit is refused. Inline-tag breakage is never stored.",
    )
    p.set_defaults(func=_tm_edit)

    p = sub.add_parser("stats", help="TM summary: totals, per-pair, usage split.")
    p.set_defaults(func=_tm_stats)

    p = sub.add_parser("delete", help="Delete TM entries (one id, or bulk by filter).")
    p.add_argument(
        "entry_id",
        type=bounded_id,
        nargs="?",
        default=None,
        help="a single TM entry id (omit to bulk-delete by filter).",
    )
    _add_tm_filters(p)
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="report how many entries WOULD be deleted, delete nothing.",
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_tm_delete)

    p = sub.add_parser(
        "scrub-hedges",
        help="Scan stored TM (segment + full-text) for refusal hedges; "
        "--apply removes the poisoned entries.",
    )
    p.add_argument(
        "--customer",
        dest="tm_customer",
        type=bounded_id,
        default=None,
        help="scope the scrub to one customer's TM (customer id); required when you "
        "can see more than one customer unless --project is given.",
    )
    p.add_argument(
        "--project",
        dest="scrub_project",
        type=bounded_id,
        default=None,
        help="limit the scan to one project id.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="delete the poisoned TM entries (default: dry-run, report only).",
    )
    p.set_defaults(func=_tm_scrub_hedges)


def _customer_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.customer_list(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.models import Customer

    # SPEC-customer-archive: default = ACTIVE customers only (matching the default web
    # list + every picker). --archived shows only archived; --all shows both with a
    # status column. This is the MANAGEMENT surface (a name-resolution read like the
    # project list stays unfiltered elsewhere).
    show_archived = bool(getattr(args, "archived", False))
    show_all = bool(getattr(args, "all_customers", False))
    limit, offset = _resolve_window(args)
    # #62: _customer_list passed the require_org_scope gate but then IGNORED the
    # resolved scope, so --org N still returned every org's customers. Confine to the
    # scoped org (customer show/rename/delete already scope via _get_customer). None
    # (single-tenant / --operator) is unscoped — byte-identical, matching web
    # /api/customers' org filter.
    org_id = op_org_id(args)
    with _session() as session:
        # L8: (name, id) so the LIMIT/OFFSET window is deterministic on Postgres.
        q = select(Customer).order_by(Customer.name, Customer.id)
        if org_id is not None:
            q = q.where(Customer.org_id == org_id)
        if show_all:
            pass
        elif show_archived:
            q = q.where(Customer.archived_at.is_not(None))
        else:
            q = q.where(Customer.archived_at.is_(None))
        # AU-7: bound the list in SQL, capped at _LIST_CAP with a truncation note.
        fetched = list(session.exec(q.offset(offset).limit(limit + 1)).all())
        truncated = len(fetched) > limit
        customers = fetched[:limit]
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "customers": [
                        {"id": c.id, "name": c.name, "archived": c.archived_at is not None}
                        for c in customers
                    ],
                    "truncated": truncated,
                    "limit": limit,
                    "offset": offset,
                }
            )
        elif show_all:
            emit.table(
                [(c.id, c.name, "archived" if c.archived_at else "active") for c in customers],
                headers=("id", "name", "status"),
            )
            _note_truncated(emit, truncated, offset, limit)
        else:
            emit.table([(c.id, c.name) for c in customers], headers=("id", "name"))
            _note_truncated(emit, truncated, offset, limit)
    return 0


def _gmod_set_pairs(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_set_pairs(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary_module as gm

    with _session() as session:
        _gmod_get(session, args)
        try:
            result = gm.set_pairs(session, args.glossary_id, args.pair)
        except gm.GlossaryModuleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        d = gm.to_dict(session, gm.get(session, args.glossary_id))
        created = getattr(result, "bindings_created", 0)
        dnt_ext = getattr(result, "dnt_bindings_extended", 0)
        # A.4-8 (M-2): report the widening counts on the CLI too (new payload keys, not in
        # to_dict whose shape other surfaces pin).
        emit.result(
            f"Set {len(d['pairs'])} pair(s) on glossary #{args.glossary_id} "
            f"({created} binding(s) created, {dnt_ext} DNT binding(s) extended).",
            {"ok": True, "bindings_created": created, "dnt_bindings_extended": dnt_ext, **d},
        )
    return 0


def _gmod_set_content_type(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_set_content_type(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import glossary_module as gm

    actor = getattr(args, "actor", None) or "cli"
    # --clear (or an omitted id) clears; otherwise attach the given content type id.
    ct_id = None if getattr(args, "clear", False) else getattr(args, "content_type_id", None)
    with _session() as session:
        _gmod_get(session, args)  # R2-3: fetch in the operator's org scope first
        try:
            g = gm.set_content_type(session, args.glossary_id, ct_id)
        except gm.GlossaryModuleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(actor=actor, action="glossary.content_type_set", target=f"glossary {g.id}")
        emit.result(
            (
                f"Cleared content type on glossary #{g.id}."
                if g.content_type_id is None
                else f"Set content type {g.content_type_id} on glossary #{g.id}."
            ),
            {"ok": True, **gm.to_dict(session, g)},
        )
    return 0


def _gmod_import(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_import(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary_module as gm

    data = _read_bytes(args.csv)
    with _session() as session:
        _gmod_get(session, args)
        try:
            # CSV, native TBX, or concept-aware .glossary.json, chosen by the file's
            # extension (.tbx / .glossary.json / .json), else CSV.
            with quiet_import_log():
                res = gm.import_bytes(
                    session,
                    args.glossary_id,
                    data,
                    args.source,
                    args.target,
                    filename=args.csv,
                    strict=getattr(args, "strict", False),
                    concepts=getattr(args, "concepts", False),
                )
        except Exception as exc:
            raise glossary_import_cli_error(exc) from exc
        # Only a concept-aware `.glossary.json` import reports concepts/terms without an
        # `entries`/`senses` key; CSV and every TBX import take the flat entry path (T2 scope
        # decision: the concept-preserving TBX arm is deferred to the terminology authority
        # train), so they report entries/senses. Route on the presence of `entries`.
        if "entries" not in res:
            label = ".glossary.json"
            msg = (
                f"Imported {res.get('concepts', 0)} concept(s), "
                f"{res.get('terms', 0)} term(s) into glossary #{args.glossary_id} "
                f"({label})."
            )
            if "projected_entries" in res:
                # round-4 M2: the shared wording (parity with --remote/web/api and the three
                # descriptors) -- what THIS import's terms enforce, not a whole-glossary total.
                msg += projected_entries_sentence(res["projected_entries"])
            if res.get("dropped_refs"):
                msg += f" {res['dropped_refs']} unresolved ref(s) dropped."
            if res.get("truncated_fields"):
                msg += f" {res['truncated_fields']} field(s) truncated."
            if res.get("id_conflicts"):
                # R4-9: a foreign document id that collided with a local concept's — the
                # incoming concept was imported as NEW (never aliased onto the local one).
                msg += f" {res['id_conflicts']} foreign id conflict(s) imported as new."
            if res.get("bindings_skipped"):
                # L-1: a bindings-bearing concept restored into a WIDER glossary is not
                # auto-widened into a configured pair its bindings never named (M-7/LT3-1).
                msg += (
                    f" {res['bindings_skipped']} configured pair-binding(s) skipped "
                    "(a bindings-bearing concept is not auto-widened)."
                )
            emit.result(msg, {"ok": True, "glossary_id": args.glossary_id, **res})
        else:
            emit.result(
                f"Imported {res['entries']} entr"
                f"{'y' if res['entries'] == 1 else 'ies'}, "
                f"{res['senses']} sense(s) into glossary #{args.glossary_id}."
                + _changed_note(res)
                + _skipped_note(res),
                {"ok": True, "glossary_id": args.glossary_id, **res},
            )
    return 0


def _gmod_export(args, emit) -> int:
    # In-process (local-root), like the sibling module commands + the legacy
    # `glossary export`: reads the DB directly and writes the file. Web parity:
    # GET /glossaries/{id}/export(?format=tbx); /api GET /api/glossaries/{id}/export.
    # The output PATH picks the format: .tbx -> TBX, else CSV.
    if remote_mode(args):
        return _remote_impl.gmod_export(args, emit)
    require_org_scope(args, emit)
    from app.kernel import glossary as gloss_engine
    from app.kernel import glossary_module as gm
    from app.kernel import glossary_tbx

    fmt = gloss_engine.export_format_for(args.csv)
    drops: list[str] = []
    with _session() as session:
        _gmod_get(session, args)
        try:
            data = gm.export_bytes(
                session,
                args.glossary_id,
                fmt=fmt,
                dialect=getattr(args, "dialect", "tbx-basic"),
                strict_portable=getattr(args, "strict_portable", False),
                drops_out=drops,
            )
        except glossary_tbx.EmptyGlossaryError as exc:
            # concept-format (TBX/JSON) export of an empty glossary refuses (§6.4/M8).
            raise CliError(str(exc)) from exc
    _write_bytes(args.csv, data, args.force)
    # M1: a portable / tbx-core export drops the los: private fields; NAME them so the
    # operator sees exactly what a stock-TBX-Basic instance loses (matching the CLI help /
    # docs claim). The list is empty for a full tbx-basic export.
    msg = f"Wrote glossary {fmt.upper()} -> {args.csv}."
    if drops:
        msg += f" Dropped {len(drops)} los: field type(s): {', '.join(drops)}."
    emit.result(
        msg,
        {
            "ok": True,
            "path": args.csv,
            "bytes": len(data),
            "format": fmt,
            "glossary_id": args.glossary_id,
            "dropped_fields": drops,
        },
    )
    return 0


def _gmod_delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_delete(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import glossary_module as gm

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        g = _gmod_get(session, args)
        if not confirm(emit, f"Delete glossary #{g.id} ('{g.name}')?", args.yes):
            raise CliError("aborted.", 1)
        gm.delete(session, args.glossary_id)
        audit.log(actor=actor, action="glossary.delete", target=f"glossary {args.glossary_id}")
        emit.result(
            f"Deleted glossary #{args.glossary_id}.", {"ok": True, "glossary_id": args.glossary_id}
        )
    return 0


def _gmod_rename(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_rename(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import glossary_module as gm

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _gmod_get(session, args)
        try:
            g = gm.rename(session, args.glossary_id, args.name)
        except gm.GlossaryModuleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(actor=actor, action="glossary.rename", target=f"glossary {g.id}")
        emit.result(
            f"Renamed glossary #{g.id} -> {g.name}.", {"ok": True, **gm.to_dict(session, g)}
        )
    return 0


# --- term extraction + candidate review (feat/term-extraction) ---------------
def _gmod_extract(args, emit) -> int:
    # R-MED-11: WIRED --remote to POST /api/glossaries/{gid}/extract (the route exists +
    # works, gated can_tune_ai). The local arm reads the DB directly and runs the
    # spend-capped/mock-exempt two-stage extraction, landing candidates PENDING.
    if remote_mode(args):
        return _remote_impl.gmod_extract(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import term_extraction as tex

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        g = _gmod_get(session, args)
        doc = tex.visible_document(session, g, args.document_id, org_id=op_org_id(args))
        if doc is None:
            raise CliError(
                f"no such document {args.document_id} in this glossary's scope.", EXIT_USAGE
            )
        try:
            summary = tex.extract(
                session,
                g,
                (args.target_locale or "").strip(),
                document=doc,
                source_locale=(args.source_locale or "").strip(),
            )
        except tex.ExtractionEngineUnavailable as exc:
            raise CliError(str(exc), 1) from exc
        if summary.get("blocked"):
            raise CliError("spend cap reached - extraction refused (no candidates created).", 1)
        audit.log(actor=actor, action="glossary.extract", target=f"glossary {g.id} <- doc {doc.id}")
        degraded = ""
        if summary.get("degraded"):
            degraded = (
                " WARNING: spend cap reached mid-run - the "
                + " and ".join(summary.get("degraded_steps") or [])
                + " step was skipped, so these candidates were extracted but "
                "not fully processed (more noise - review carefully)."
            )
        emit.result(
            f"Proposed {summary['proposed']} candidate(s) into glossary #{g.id} "
            f"(extracted {summary['extracted']}, pruned {summary['pruned']}, "
            f"skipped {summary['filtered_existing']} existing + "
            f"{summary['filtered_history']} decided).{degraded} Review with "
            f"'glossary module candidates list {g.id}'.",
            {"ok": True, "glossary_id": g.id, **summary},
        )
    return 0


def _gmod_candidates_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_candidates_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import term_extraction as tex

    with _session() as session:
        g = _gmod_get(session, args)
        cands = tex.list_candidates(session, g.id, status=args.status)
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "glossary_id": g.id,
                    "status": args.status,
                    "candidates": [
                        {
                            "id": c.id,
                            "source_term": c.source_term,
                            "proposed_target": c.proposed_target,
                            "source_locale": c.source_locale,
                            "target_locale": c.target_locale,
                            "frequency": c.frequency,
                            "status": c.status,
                            "context_snippet": c.context_snippet,
                            "source_document_id": c.source_document_id,
                        }
                        for c in cands
                    ],
                }
            )
        else:
            emit.table(
                [
                    (
                        c.id,
                        c.source_term,
                        c.proposed_target or "-",
                        f"{c.source_locale or '-'}->{c.target_locale or '-'}",
                        c.frequency,
                        c.status,
                    )
                    for c in cands
                ],
                headers=("id", "source", "proposed", "locales", "freq", "status"),
            )
    return 0


def _gmod_candidate_accept(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_candidate_accept(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import term_extraction as tex

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        g = _gmod_get(session, args)
        entry = tex.accept(
            session, g.id, args.candidate_id, decided_by=actor, target_term=args.target_term
        )
        if entry is None:
            raise CliError(
                f"candidate {args.candidate_id} is not a pending candidate of glossary #{g.id}.",
                EXIT_USAGE,
            )
        audit.log(
            actor=actor,
            action="glossary.candidate_accept",
            target=f"glossary {g.id} candidate {args.candidate_id}",
        )
        emit.result(
            f"Accepted candidate #{args.candidate_id} ('{entry.source_term}' -> "
            f"'{entry.target_term}') into glossary #{g.id}.",
            {
                "ok": True,
                "glossary_id": g.id,
                "candidate_id": args.candidate_id,
                "source_term": entry.source_term,
                "target_term": entry.target_term,
            },
        )
    return 0


def _gmod_candidate_drop(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.gmod_candidate_drop(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import term_extraction as tex

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        g = _gmod_get(session, args)
        ok = tex.drop(session, g.id, args.candidate_id, decided_by=actor)
        if not ok:
            raise CliError(
                f"candidate {args.candidate_id} is not a pending candidate of glossary #{g.id}.",
                EXIT_USAGE,
            )
        audit.log(
            actor=actor,
            action="glossary.candidate_drop",
            target=f"glossary {g.id} candidate {args.candidate_id}",
        )
        emit.result(
            f"Dropped candidate #{args.candidate_id} from glossary #{g.id} "
            "(it will never be re-proposed).",
            {"ok": True, "glossary_id": g.id, "candidate_id": args.candidate_id},
        )
    return 0


def _gmod_concepts_list(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.models import GlossaryConcept

    with _session() as session:
        g = _gmod_get(session, args)
        concepts = session.exec(
            select(GlossaryConcept)
            .where(GlossaryConcept.glossary_id == g.id)
            .order_by(GlossaryConcept.id)
        ).all()
        rows, payload = [], []
        for c in concepts:
            terms = _concept_terms(session, c.id)
            payload.append(_concept_payload(c, terms))
            rows.append(
                (
                    c.id,
                    c.external_id or "-",
                    c.domain or "-",
                    (c.definition or "-")[:40] or "-",
                    len(terms),
                )
            )
        if emit.as_json:
            emit.json({"ok": True, "glossary_id": g.id, "concepts": payload})
        else:
            emit.say(f"Glossary #{g.id} '{g.name}' concepts ({len(concepts)}):")
            emit.table(rows, headers=("id", "external_id", "domain", "definition", "terms"))
    return 0


def _gmod_concept_show(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.models import GlossaryConcept

    with _session() as session:
        _gmod_get(session, args)
        c = session.get(GlossaryConcept, args.concept_id)
        if c is None or c.glossary_id != args.glossary_id:
            raise CliError(
                f"no such concept {args.concept_id} in glossary #{args.glossary_id}.", EXIT_USAGE
            )
        terms = _concept_terms(session, c.id)
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "glossary_id": args.glossary_id,
                    "concept": _concept_payload(c, terms),
                }
            )
        else:
            emit.say(
                f"Concept #{c.id} (external_id: {c.external_id or '-'}) "
                f"in glossary #{args.glossary_id}"
            )
            emit.say(f"  domain: {c.domain or '-'}   subdomain: {c.subdomain or '-'}")
            emit.say(f"  definition: {c.definition or '-'}")
            if c.notes:
                emit.say(f"  notes: {c.notes}")
            if c.homonym_group:
                emit.say(f"  homonym_group: {c.homonym_group}")
            emit.table(
                [
                    (
                        t.id,
                        t.locale or "*",
                        t.term,
                        t.status,
                        t.audience,
                        t.term_type or "-",
                        "yes" if t.dnt else "",
                    )
                    for t in terms
                ],
                headers=("term_id", "locale", "term", "status", "audience", "type", "dnt"),
            )
    return 0


def _gmod_concept_add(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.kernel import glossary_concepts as gc

    terms = _parse_locale_surface_terms(args.term)
    with _session() as session:
        _gmod_get(session, args)
        try:
            concept = gc.create_concept(
                session,
                args.glossary_id,
                definition=args.definition,
                domain=args.domain,
                subdomain=args.subdomain,
                notes=args.notes,
                external_id=args.external_id,
                terms=terms,
            )
        except gc.ConceptError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Created concept #{concept.id} in glossary #{args.glossary_id} "
            f"({len(terms)} term(s)).",
            {"ok": True, "glossary_id": args.glossary_id, "concept_id": concept.id},
        )
    return 0


def _gmod_concept_edit(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.kernel import glossary_concepts as gc

    fields = {}
    for key in ("definition", "domain", "subdomain", "notes", "external_id", "source"):
        v = getattr(args, key, None)
        if v is not None:
            fields[key] = v
    if not fields:
        raise CliError("nothing to edit: pass at least one field flag.", EXIT_USAGE)
    with _session() as session:
        _gmod_get(session, args)
        try:
            concept = gc.edit_concept(session, args.glossary_id, args.concept_id, **fields)
        except gc.ConceptError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Updated concept #{concept.id} in glossary #{args.glossary_id}.",
            {"ok": True, "glossary_id": args.glossary_id, "concept_id": concept.id},
        )
    return 0


def _gmod_concept_delete(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.kernel import glossary_concepts as gc
    from app.models import GlossaryConcept

    with _session() as session:
        _gmod_get(session, args)
        c = session.get(GlossaryConcept, args.concept_id)
        if c is None or c.glossary_id != args.glossary_id:
            raise CliError(
                f"no such concept {args.concept_id} in glossary #{args.glossary_id}.", EXIT_USAGE
            )
        # LT-5: refuse BEFORE the destructive confirm prompt when the editor is off, so an
        # interactive operator is not asked to confirm an operation the install cannot perform
        # (the seam refuses too, but only after the prompt). Same switch-naming message.
        if not gc.concept_editor_enabled():
            raise CliError(
                "the concept editor is not enabled on this install; enable feature "
                "terminology_concept_editor",
                EXIT_USAGE,
            )
        if not confirm(
            emit,
            f"Delete concept #{args.concept_id} (and its terms) from glossary #{args.glossary_id}?",
            args.yes,
        ):
            raise CliError("aborted.", 1)
        try:
            gc.delete_concept(session, args.glossary_id, args.concept_id)
        except gc.ConceptError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Deleted concept #{args.concept_id} from glossary #{args.glossary_id}.",
            {"ok": True, "glossary_id": args.glossary_id, "concept_id": args.concept_id},
        )
    return 0


def _gmod_term_delete(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.kernel import glossary_concepts as gc
    from app.models import GlossaryConcept, GlossaryTerm

    with _session() as session:
        _gmod_get(session, args)
        concept = session.get(GlossaryConcept, args.concept_id)
        term = session.get(GlossaryTerm, args.term_id)
        if (
            concept is None
            or concept.glossary_id != args.glossary_id
            or term is None
            or term.concept_id != args.concept_id
        ):
            raise CliError(
                f"no such term {args.term_id} in concept #{args.concept_id} "
                f"of glossary #{args.glossary_id}.",
                EXIT_USAGE,
            )
        # LT-5: refuse BEFORE the destructive confirm prompt when the editor is off.
        if not gc.concept_editor_enabled():
            raise CliError(
                "the concept editor is not enabled on this install; enable feature "
                "terminology_concept_editor",
                EXIT_USAGE,
            )
        if not confirm(
            emit,
            f"Delete term #{args.term_id} from concept #{args.concept_id}?",
            args.yes,
        ):
            raise CliError("aborted.", 1)
        try:
            ok = gc.delete_concept_term(session, args.glossary_id, args.concept_id, args.term_id)
        except gc.ConceptError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if not ok:
            raise CliError(f"could not delete term {args.term_id}.", EXIT_USAGE)
        emit.result(
            f"Deleted term #{args.term_id} from concept #{args.concept_id}.",
            {
                "ok": True,
                "glossary_id": args.glossary_id,
                "concept_id": args.concept_id,
                "term_id": args.term_id,
            },
        )
    return 0


def _gmod_term_set_audience(args, emit) -> int:
    require_local(args)  # in-process concept store; --remote is the /api concepts route
    require_org_scope(args, emit)
    from app.kernel import glossary_concepts as gc

    canon = gc.canon_audience(args.audience)
    with _session() as session:
        _gmod_get(session, args)
        try:
            n = gc.set_audience(session, args.glossary_id, args.term_ids, args.audience)
        except gc.ConceptError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Set audience '{canon}' on {n} term(s) in glossary #{args.glossary_id}.",
            {
                "ok": True,
                "glossary_id": args.glossary_id,
                "updated": n,
                "audience": canon,
                "term_ids": args.term_ids,
            },
        )
    return 0
