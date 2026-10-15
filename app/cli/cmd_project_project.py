"""cmd_project: project

Documented in: docs/architecture/content-and-projects.md
"""

import os

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    get_scoped_or_404,
    op_creator,
    op_org_id,
    op_org_scope,
    remote_mode,
    require_local,
    require_org_scope,
)
from app.cli._common import TERM_META_FLAGS as _TERM_META_FLAGS  # noqa: F401 (re-exported)
from app.cli._common import get_customer as _get_customer
from app.cli._common import term_meta_from_args as _term_meta_from_args  # noqa: F401

_LIST_CAP = 1000


#: AU-7: the generous hard cap the list commands enforce even with no --limit, so a
#: bounded list is never silently unbounded and never silently cut (mirrors the /api
#: LIST_HARD_CAP). A caller pages past it with --offset.
def _list_window_args(p) -> None:
    """Attach the shared --limit/--offset paging flags to a list subparser (AU-7).
    Both surfaces honour them; absent, the list is capped at %d with a truncation
    note (never a silent cutoff).""" % _LIST_CAP  # noqa: UP031
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max rows to show (default and hard cap %d). Page past the "  # noqa: UP031
        "cap with --offset. A non-positive value is refused." % _LIST_CAP,
    )
    p.add_argument(
        "--offset",
        type=int,
        default=None,
        help="skip this many rows before listing (paging). Must be >= 0.",
    )


def _resolve_window(args):
    """(limit, offset) from the paging flags. L5: refuse LOUDLY (exit 2) rather than
    silently coercing -- an explicit non-positive --limit or a negative --offset is a
    usage error, matching the API's 400, not a silent clamp/"use cap". An OMITTED
    --limit (None) means the default cap; it is the only value that maps to the cap."""
    limit = getattr(args, "limit", None)
    if limit is None:
        limit = _LIST_CAP
    elif limit < 1:
        raise CliError("--limit must be a positive integer.", EXIT_USAGE)
    offset = getattr(args, "offset", None) or 0
    if offset < 0:
        raise CliError("--offset must be >= 0.", EXIT_USAGE)
    return min(limit, _LIST_CAP), offset


def _note_truncated(emit, truncated: bool, offset: int, limit: int) -> None:
    """A stderr note when a list was cut at the server page size (never a silent cutoff,
    per doctrine). M3: the advice is based on the SERVER WINDOW (offset + limit), not the
    post-filter survivor count -- so it always points at the NEXT page's offset even when
    a client-side filter left zero survivors on this one. No-op when nothing was cut;
    suppressed under --json (the payload's `truncated` field carries it there)."""
    if truncated and not emit.as_json:
        emit.warn(
            "results truncated at the server page size (%d row(s)); pass "  # noqa: UP031
            "--offset %d to page further." % (limit, offset + limit)
        )


def _note_hidden(emit, hidden) -> None:
    """#607: a stderr note when the default archive/status filters left in-scope projects
    out of `project list` (the --json payload carries `hidden` instead)."""
    if emit.as_json or not hidden:
        return
    arch, fin = int(hidden.get("archived", 0) or 0), int(hidden.get("finished", 0) or 0)
    if arch or fin:
        emit.warn(
            "%d archived and %d completed/closed/cancelled project(s) hidden by default; "  # noqa: UP031
            "pass --archived all and --status all to include them." % (arch, fin)
        )


def _add_term_meta_args(p, edit: bool = False) -> None:
    """Attach the TBX-ish term-metadata flags shared by add-term / edit-term. On the
    ADD path an omitted flag stays "" (nothing set); on the EDIT path an omitted flag
    is None (that field left unchanged)."""
    default = None if edit else ""
    p.add_argument(
        "--part-of-speech",
        dest="part_of_speech",
        default=default,
        help="part of speech (noun, verb...).",
    )
    p.add_argument("--definition", dest="definition", default=default, help="a definition / gloss.")
    p.add_argument(
        "--usage-example",
        dest="usage_example",
        default=default,
        help="an example of the term in use.",
    )
    p.add_argument(
        "--subject-field",
        dest="subject_field",
        default=default,
        help="subject field / domain (software UI, finance...).",
    )
    p.add_argument("--notes", dest="notes", default=default, help="free-text notes.")


_CONCEPT_TERM_STR_FIELDS = (
    ("status", "status", "term status: preferred|admitted|deprecated|forbidden."),
    ("term-type", "term_type", "term type vocabulary value (fullForm, acronym...)."),
    ("audience", "audience", "comma-separated subset of human,mt,llm."),
    ("part-of-speech", "part_of_speech", "part of speech (noun, verb...)."),
    ("usage-note", "usage_note", "a usage note."),
    ("context-example", "context_example", "an example of the term in context."),
    ("geographical-usage", "geographical_usage", "geographical usage (e.g. es-MX)."),
    ("grammatical-gender", "grammatical_gender", "masculine|feminine|neuter|..."),
    ("grammatical-number", "grammatical_number", "singular|plural|dual|mass|..."),
    ("source", "source", "provenance / source string."),
)


_CONCEPT_TERM_BOOL_FIELDS = (
    ("case-sensitive", "case_sensitive", "match case-sensitively."),
    ("exact", "exact", "match the exact surface only (stored; T1-inert)."),
    ("dnt", "dnt", "do-not-translate (source side)."),
)


#: The concept-model GlossaryTerm string fields shared by `terms add|edit`
#: (SPEC-terminology-backend §4.2). Every flag defaults to None so an omitted flag is
#: left to the service default on add and unchanged on edit; the service sanitizes and
#: validates each (status/audience/term_type have their own vocab coercion).
def _add_concept_term_args(p, edit: bool = False) -> None:
    """Attach the concept-model GlossaryTerm metadata flags shared by `glossary module
    terms add` / `edit`. Every flag defaults to None so an omitted flag is dropped from
    the term-doc (the service applies its own default on add, leaves the field unchanged
    on edit). Booleans are tri-state (--flag / --no-flag) so an edit can clear one."""
    for flag, dest, helptext in _CONCEPT_TERM_STR_FIELDS:
        p.add_argument(f"--{flag}", dest=dest, default=None, help=helptext)
    for flag, dest, helptext in _CONCEPT_TERM_BOOL_FIELDS:
        grp = p.add_mutually_exclusive_group()
        grp.add_argument(f"--{flag}", dest=dest, action="store_true", default=None, help=helptext)
        grp.add_argument(f"--no-{flag}", dest=dest, action="store_false", help=f"clear: {helptext}")


def _concept_term_tdoc(args) -> dict:
    """Build the term-doc (kwargs) for the glossary_concepts term seams from the parsed
    flags. Only provided (non-None) fields are included, so the service applies its own
    default on add and leaves an omitted field unchanged on edit."""
    tdoc: dict = {}
    for _flag, dest, _help in _CONCEPT_TERM_STR_FIELDS:
        v = getattr(args, dest, None)
        if v is not None:
            tdoc[dest] = v
    for _flag, dest, _help in _CONCEPT_TERM_BOOL_FIELDS:
        v = getattr(args, dest, None)
        if v is not None:
            tdoc[dest] = v
    return tdoc


def _parse_locale_surface_terms(specs) -> list:
    """Parse repeatable `--term LOCALE:SURFACE` values into ``[{locale, term}]``. A
    value with no colon is a wildcard-locale surface (locale ''); a blank surface is
    skipped."""
    out = []
    for raw in specs or []:
        s = str(raw)
        loc, surface = s.split(":", 1) if ":" in s else ("", s)
        surface = surface.strip()
        if not surface:
            continue
        out.append({"locale": loc.strip(), "term": surface})
    return out


def _register_langset(subparsers) -> None:
    ls = subparsers.add_parser("langset", help="Language Sets (create, list, update, delete).")
    sub = ls.add_subparsers(dest="langset_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create a Language Set.")
    p.add_argument("name", help="set name.")
    p.add_argument("--source", default="en-US", help="source locale (default en-US).")
    p.add_argument("--targets", default="", help="target locales, comma/space separated.")
    p.set_defaults(func=_langset_create)

    p = sub.add_parser("list", help="List Language Sets.")
    p.set_defaults(func=_langset_list)

    p = sub.add_parser("update", help="Update a Language Set.")
    p.add_argument("langset_id", type=bounded_id, help="language set id.")
    p.add_argument("--name", default="", help="new name (unchanged if omitted).")
    p.add_argument("--source", default="", help="new source locale (unchanged if omitted).")
    p.add_argument(
        "--targets", default="", help="REPLACE the target locales (comma/space separated)."
    )
    p.set_defaults(func=_langset_update)

    p = sub.add_parser("delete", help="Delete a Language Set (DESTRUCTIVE).")
    p.add_argument("langset_id", type=bounded_id, help="language set id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_langset_delete)


# --- helpers -----------------------------------------------------------------
def _get_project(session, project_id: int, org_id=None):
    from app.models import Project

    return get_scoped_or_404(session.get(Project, project_id), project_id, "project", org_id)


def _read_bytes(path: str) -> bytes:
    if not os.path.isfile(path):
        raise CliError(f"file not found: {path}", EXIT_USAGE)
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path: str, data: bytes, force: bool) -> None:
    if os.path.exists(path) and not force:
        raise CliError(f"destination exists: {path} (pass --force to overwrite).", EXIT_USAGE)
    with open(path, "wb") as fh:
        fh.write(data)


# --- customer ----------------------------------------------------------------
def _customer_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.customer_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content

    with _session() as session:
        try:
            customer = content.customer_create(
                session, args.name, creator=op_creator(args), reject_duplicate=True
            )
        except content.DuplicateNameError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if customer is None:
            raise CliError("customer name is blank.", EXIT_USAGE)
        emit.result(
            f"Created customer #{customer.id}: {customer.name}",
            {"ok": True, "id": customer.id, "name": customer.name},
        )
    return 0


def _customer_show(args, emit) -> int:
    # In-process only (assembles the 360 view from the host DB).
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
            emit.json({"ok": True, **data})
        else:
            emit.say(f"Customer #{data['id']}: {data['name']}")
            emit.say(
                f"  default engine: {data['default_provider'] or '(none)'}"
                f"   default workflow: {data['default_workflow_id'] or '(none)'}"
            )
            emit.say(
                f"  language pairs (declared: {len(data['active_pairs'])}, "
                f"in-use: {len(data['derived_pairs'])}):"
            )
            emit.table(
                [(p["pair"], p["label"]) for p in data["all_pairs"]] or [("(none)", "")],
                headers=("pair", "label"),
            )
            emit.say(f"  projects ({len(data['projects'])}):")
            emit.table(
                [(p["id"], p["name"], p["status"], p["documents"]) for p in data["projects"]]
                or [("(none)", "", "", "")],
                headers=("id", "name", "status", "docs"),
            )
            emit.say(f"  connectors ({len(data['connections'])}):")
            emit.table(
                [(c["id"], c["name"], c["kind"], c["scope"]) for c in data["connections"]]
                or [("(none)", "", "", "")],
                headers=("id", "name", "kind", "scope"),
            )
            emit.say(f"  workflows ({len(data['workflows'])}):")
            emit.table(
                [(w["id"], w["name"], w["scope"]) for w in data["workflows"]]
                or [("(none)", "", "")],
                headers=("id", "name", "scope"),
            )
            emit.say(
                f"  TM: {data['tm_stats']['segments']} segment / "
                f"{data['tm_stats']['fulltext']} full-text"
            )
    return 0


def _customer_update(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import customers as customer_service

    updates = {}
    for raw in args.settings or []:
        if "=" not in raw:
            raise CliError(f"bad --set (expected KEY=VALUE): {raw!r}", EXIT_USAGE)
        k, v = raw.split("=", 1)
        updates[k.strip()] = v
    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        cust = customer_service.update_customer_settings(session, args.customer_id, updates)
        audit.log(
            actor=actor,
            action="customer.settings",
            target=f"customer {cust.id}",
            detail=f"keys: {', '.join(sorted(updates)) or 'none'}",
            customer_id=cust.id,
        )
        emit.result(
            f"Updated customer #{cust.id} preferences.",
            {"ok": True, "id": cust.id, "settings": cust.settings or {}},
        )
    return 0


# --- project -----------------------------------------------------------------
def _project_create(args, emit) -> int:
    pkg_path = getattr(args, "from_sdl_package", "") or ""
    if pkg_path:
        require_local(args)
    if getattr(args, "connected", False) and pkg_path:
        raise CliError(
            "a connected project takes its content from its source: --connected cannot be "
            "combined with --from-sdl-package.",
            EXIT_USAGE,
        )
    if remote_mode(args):
        return _remote_impl.project_create(args, emit)
    require_org_scope(args, emit)
    pkg_data = None
    if pkg_path:
        # A Trados project package: its .sdlproj names the languages (same seam as the
        # web form and POST /api/documents sdl_new_project=1).
        from app.filters.base import InputLimitError
        from app.kernel import ingest as _sdl

        if not os.path.isfile(pkg_path):
            raise CliError(f"file not found: {pkg_path}", EXIT_USAGE)
        with open(pkg_path, "rb") as fh:
            pkg_data = fh.read()
        try:
            _info = _sdl.inspect_sdl_package(pkg_data, os.path.basename(pkg_path))
        except InputLimitError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        args.source = _info["source_locale"]
        args.targets = " ".join(_info["target_locales"])
    from app import config
    from app.kernel import connected_projects, content
    from app.kernel import segmentation_profiles as _segprof

    actor = getattr(args, "actor", None) or "cli"
    # Issue #55: an OMITTED --provider inherits the install's CONFIGURED default_provider
    # (config.DEFAULT_PROVIDER — pushed from the stored Setting by _apply_settings_at_boot
    # at CLI bootstrap), exactly as the web new-project dialog pre-fills its provider select
    # from config.DEFAULT_PROVIDER. The old hardcoded "claude" argparse literal meant a CLI
    # create on an install whose default_provider is (e.g.) "local" silently stamped "claude"
    # — a surface divergence from the web form. An EXPLICIT --provider is respected verbatim;
    # when the setting is the out-of-box "claude" this resolves to "claude" (byte-identical,
    # and content.project_create's ("" | "claude") customer-default inheritance still fires).
    _provider = args.provider if args.provider is not None else config.DEFAULT_PROVIDER
    seg_profile = (getattr(args, "seg_profile", "") or "").strip()
    with _session() as session:
        # Custom project fields (--field KEY=VALUE): validated BEFORE anything is written,
        # so a bad value or a missing required field leaves no half-made project.
        from app.cli.cmd_field import parse_field_args
        from app.kernel import custom_fields as _cf
        from app.kernel import ids as _ids

        cf_plan = []
        _cf_values = parse_field_args(getattr(args, "field", []))
        if _cf_values or _cf.list_defs(session, op_org_scope(args)):
            try:
                cf_plan = _cf.validate_for_create(
                    session, op_org_scope(args), _ids.coerce_id(args.customer_id), _cf_values
                )
            except _cf.FieldError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
        if seg_profile:
            # #5: validate the profile id BEFORE project_create commits anything
            # — a bad/foreign profile must fail the WHOLE create (exit 2, no
            # project row), not leave an orphaned project behind on inherit.
            try:
                _segprof.validate_for_create(
                    session, seg_profile, args.customer_id, org_id=op_org_id(args)
                )
            except _segprof.SegProfileError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
        # Phase 2: --pair-provider implies PER_PAIR; --per-pair alone opts into the
        # mode with no overrides yet (falls back to the project default per pair).
        pair_flags = list(getattr(args, "pair_provider", []) or [])
        per_pair = getattr(args, "per_pair", False) or bool(pair_flags)
        # SPEC-key-based-projects: a key project needs somewhere to put a base value,
        # and that somewhere is its per-locale value rows -- refused HERE with the same
        # message the web create surface gives, rather than one failed save at a time.
        if getattr(args, "project_type", "") == "key_based" and not (args.targets or "").strip():
            raise CliError(
                "a key-based project needs at least one target language "
                "(--targets): key values are stored per locale.",
                EXIT_USAGE,
            )
        from app.models import ProviderMode as _PM

        try:
            project = content.project_create(
                session,
                args.name,
                source_locale=args.source,
                target_locales=args.targets,
                customer_id=args.customer_id,
                default_provider=_provider,
                created_by=actor,
                creator=op_creator(args),
                # Phase 2 per-pair provider mode + mapping (validated in the service).
                provider_mode=(_PM.PER_PAIR if per_pair else _PM.UNIFORM),
                pair_providers=pair_flags,
                org_id=op_org_id(args),
                # #73 COUPLING: content_types.attachable now fails closed on a bare None
                # under an active gate; project_create validates the type against the
                # poison-collapsed STAMP org (proj_org). op_org_SCOPE is the DELIBERATE
                # resolution scope (access.UNSCOPED for --operator so a cross-org create
                # still resolves; concrete for --org; None -> falls back to proj_org for
                # gate-off) -- the persisted org (creator/proj_org) is unchanged.
                content_type_scope=op_org_scope(args),
                # Feature A: optional due date (blank = none). A bad date -> EXIT_USAGE.
                due_date=getattr(args, "due", ""),
                # SPEC-content-types: optional content type (blank = General / the
                # customer's default, inherited in the service). A bad/foreign id ->
                # InvalidContentTypeError -> EXIT_USAGE.
                content_type_id=getattr(args, "content_type", ""),
                # SPEC-key-based-projects 2.1: blank = "document". The service
                # validates the value AND re-checks the module kill switch, so a
                # stray flag on a switched-off install is a clean refusal.
                project_type=getattr(args, "project_type", ""),
                # Connected projects: --connected = intake "connected" (blank = upload).
                intake="connected" if getattr(args, "connected", False) else "",
                # R2-3 (Finding E): in-process CLI is a TRUSTED OPERATOR surface (no
                # customer-bound web/API identity here) — bound_customer_id stays
                # None/unchanged, exactly like the from-template apply path. A
                # customer-bound REMOTE caller goes through _remote_impl -> the
                # /api/projects route above, which IS confined.
                # 2026-07-14: `project create` is a USER-FACING seam — a
                # customer is required (--customer-id). Missing -> MissingCustomerError
                # -> EXIT_USAGE. (Hermetic fixtures call content.project_create
                # directly WITHOUT require_customer and stay customer-less.)
                require_customer=True,
                # #580: a same-named active project of this customer is refused.
                reject_duplicate=True,
            )
        except (
            content.InvalidLocaleError,
            content.DueDateError,
            content.PairProviderError,
            content.InvalidContentTypeError,
            content.ArchivedCustomerError,
            content.ForeignCustomerError,
            content.MissingCustomerError,
            content.InvalidProjectTypeError,
            content.UnknownCustomerError,
            content.DuplicateNameError,
            connected_projects.IntakeError,
        ) as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if project is None:
            raise CliError("project name is blank.", EXIT_USAGE)
        if seg_profile:
            # Already validated above; this just performs the assignment (the
            # customer scope can't have changed within this same transaction).
            _segprof.assign_to_project(session, project.id, seg_profile)
        cf_changes = []
        if cf_plan:
            try:
                cf_changes = _cf.apply_plan(session, project, cf_plan, actor=actor)
            except _cf.FieldError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            from app.kernel import audit as _audit

            _audit.log(
                actor=actor,
                action="field.set",
                target=f"project {project.id}",
                detail=_cf.audit_detail(cf_changes),
                customer_id=project.customer_id,
            )
        pkg_summary = None
        if pkg_data is not None:
            from app.kernel import ingest as _sdl

            try:
                first = _sdl.ingest_sdl_package(
                    session,
                    project,
                    os.path.basename(pkg_path),
                    pkg_data,
                    "segmented",
                    created_by=actor,
                )
            except Exception as exc:
                session.rollback()
                content.delete_project(session, project.id, actor=actor)
                raise CliError(f"could not import the Trados package: {exc}") from exc
            pkg_summary = _sdl.sdl_package_summary(session, first)
        from sqlmodel import select

        from app.models import TargetLocale

        locales = [
            tl.locale
            for tl in session.exec(
                select(TargetLocale).where(TargetLocale.project_id == project.id)
            ).all()
        ]
        # Active-pairs confinement (the maintainer's P2), CLI parity: WARN (never refuse —
        # matching the web's soft escape hatch) when a created target is outside the
        # customer's active pairs. No pairs configured -> no confinement -> no warning.
        if not getattr(args, "all_targets", False) and project.customer_id:
            from app.kernel import customers as customer_service

            _cust = customer_service.get(session, project.customer_id)
            _off = customer_service.targets_outside_pairs(
                _cust, locales, source_locale=project.source_locale
            )
            for _t in _off:
                emit.warn(
                    f"target {_t} is not in customer #{project.customer_id}'s "
                    "active pairs (added anyway; configure pairs with "
                    "`customer pairs add`, or pass --all-targets to silence)."
                )
        emit.result(
            f"Created project #{project.id}: {project.name} "
            f"({project.source_locale} -> {', '.join(locales) or 'no targets'}"
            + (", keys" if project.project_type == "key_based" else "")
            + (", connected" if connected_projects.is_connected(project) else "")
            + ")",
            {
                "ok": True,
                "id": project.id,
                "name": project.name,
                "source_locale": project.source_locale,
                "target_locales": locales,
                # SPEC-key-based-projects 2.1: echoed on BOTH arms (the /api create
                # route carries it too), so `--json` tells a caller which type it got.
                "project_type": project.project_type,
                "intake": project.intake,
                **(
                    {"setup_url": connected_projects.setup_url(project.id)}
                    if connected_projects.is_connected(project)
                    else {}
                ),
                "custom_fields": _cf.project_values(session, project),
                **({"sdl_package": pkg_summary} if pkg_summary else {}),
            },
        )
        if pkg_summary and not emit.as_json:
            _say_package(emit, pkg_summary)
    return 0


def _say_package(emit, summary: dict) -> None:
    """The documents a Trados package import made and the files it skipped (ASCII)."""
    for d in summary["documents"]:
        emit.say(f"  document #{d['id']}: {d['filename']} ({d['locale']}) <- {d['path']}")
    if summary["skipped_total"]:
        emit.say(f"  skipped {summary['skipped_total']} package file(s):")
        for s in summary["skipped"]:
            emit.say(f"    - {s['path']}: {s['reason']}")


def _project_author(args, emit) -> int:
    # In-process only (drafts + optionally creates on the host DB). The authoring
    # call, --create and --save-template all reuse the SAME service seams the web
    # routes use (setup_author + content.project_create / templates), so web+CLI
    # stay byte-identical. A remote caller has no session-authed author.json parity
    # in wave 1 (recorded deferred), so this is a local operator command.
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import content, setup_author
    from app.kernel import templates as template_service
    from app.models import ProviderMode as _PM

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        try:
            draft = setup_author.author_project_setup(
                session,
                description=args.description,
                provider=(args.provider or None),
                org_id=op_org_id(args),
                actor=actor,
                bound_customer_id=None,
                model=(getattr(args, "model", "") or None),
            )
        except setup_author.SetupAuthorError as exc:
            # Clean stderr message + non-zero exit, never a traceback (matches the
            # workflow author-prompt CLI convention).
            raise CliError(str(exc)) from exc
        fields = draft["fields"]
        warnings = list(draft["warnings"])
        # BUG 2: the draft OMITS default_provider when the description named none, so
        # the create/template path keeps the configured default (the CLI analogue of
        # the web form's pre-selected provider) instead of an arbitrary model guess.
        from app import config as _config

        draft_provider = fields.get("default_provider") or _config.DEFAULT_PROVIDER

        # --save-template: create a ProjectTemplate from the draft (no project).
        save_name = (getattr(args, "save_template", "") or "").strip()
        if save_name:
            try:
                tpl, twarn = template_service.create_template_from_fields(
                    session,
                    save_name,
                    source_locale=fields["source_locale"],
                    target_locales=fields["target_locales"],
                    default_provider=draft_provider,
                    customer_id=fields["customer_id"],
                    scope=("customer" if fields["customer_id"] else "global"),
                    actor=actor,
                    org_id=op_org_id(args),
                )
            except template_service.TemplateError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            for w in warnings + twarn:
                emit.warn(w)
            emit.result(
                f"Saved template #{tpl.id}: {tpl.name}.",
                {"ok": True, "template_id": tpl.id, "name": tpl.name, "warnings": warnings + twarn},
            )
            return 0

        # --create: apply the draft via the same content.project_create seam.
        if getattr(args, "create", False):
            pair_flags = [
                f"{fields['source_locale']}:{t}={p}" for t, p in fields["pair_providers"].items()
            ]
            per_pair = fields["provider_mode"] == "per_pair"
            try:
                project = content.project_create(
                    session,
                    fields["name"],
                    source_locale=fields["source_locale"],
                    target_locales=", ".join(fields["target_locales"]),
                    customer_id=(str(fields["customer_id"]) if fields["customer_id"] else ""),
                    default_provider=draft_provider,
                    created_by=actor,
                    creator=op_creator(args),
                    provider_mode=(_PM.PER_PAIR if per_pair else _PM.UNIFORM),
                    pair_providers=pair_flags,
                    org_id=op_org_id(args),
                    # #73 COUPLING: op_org_SCOPE is the DELIBERATE content-type resolution
                    # scope (access.UNSCOPED for the --operator door) so a cross-org author
                    # --create still resolves the type through the now-fail-closed
                    # attachable; the persisted org (creator/proj_org) is unchanged.
                    content_type_scope=op_org_scope(args),
                    due_date=fields["due_date"],
                    content_type_id=(
                        str(fields["content_type_id"]) if fields["content_type_id"] else ""
                    ),
                    # 2026-07-14: `project author --create` is a user-facing
                    # create seam — a customer is required. A draft with no resolvable
                    # customer -> MissingCustomerError -> EXIT_USAGE (the draft-level
                    # warning below also flags it before --create is even attempted).
                    require_customer=True,
                )
            except (
                content.InvalidLocaleError,
                content.DueDateError,
                content.PairProviderError,
                content.InvalidContentTypeError,
                content.ArchivedCustomerError,
                content.ForeignCustomerError,
                content.MissingCustomerError,
                content.UnknownCustomerError,
            ) as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            if project is None:
                raise CliError(
                    "the description did not yield a project name -- add one and retry.", EXIT_USAGE
                )
            from sqlmodel import select

            from app.models import TargetLocale

            locales = [
                tl.locale
                for tl in session.exec(
                    select(TargetLocale).where(TargetLocale.project_id == project.id)
                ).all()
            ]
            for w in warnings:
                emit.warn(w)
            emit.result(
                f"Created project #{project.id}: {project.name} "
                f"({project.source_locale} -> {', '.join(locales) or 'no targets'})",
                {
                    "ok": True,
                    "id": project.id,
                    "name": project.name,
                    "source_locale": project.source_locale,
                    "target_locales": locales,
                    "warnings": warnings,
                },
            )
            return 0

        # Default: print the validated draft + a WARNINGS block; exit 0.
        if emit.as_json:
            emit.json({"ok": True, "draft": fields, "warnings": warnings})
        else:
            emit.say("Drafted project setup:")
            emit.say(f"  name: {fields['name'] or '(none)'}")
            emit.say(f"  customer: {fields['customer_name'] or '(none)'}")
            emit.say(f"  content type: {fields['content_type_name'] or '(none)'}")
            emit.say(f"  source locale: {fields['source_locale']}")
            emit.say(f"  target locales: {', '.join(fields['target_locales']) or '(none)'}")
            emit.say(f"  due date: {fields['due_date'] or '(none)'}")
            emit.say(
                f"  default provider: {fields.get('default_provider') or '(form default kept)'}"
            )
            emit.say(f"  provider mode: {fields['provider_mode']}")
            for t, p in fields["pair_providers"].items():
                emit.say(f"    {t}: {p}")
            if fields["notes_for_reviewer"]:
                emit.say(f"  notes: {fields['notes_for_reviewer']}")
            if warnings:
                emit.say("WARNINGS:")
                for w in warnings:
                    emit.say(f"  - {w}")
    return 0


# --- TM MANAGEMENT module (browse / usage / lifecycle) -----------------------
# In-process (local-root, same trust as editing the DB directly). Scoping mirrors
# the F2 lesson: under multi-org an --org A operator must only ever see/delete org
# A's TM. SegmentTM has no org column, so it is scoped THROUGH project -> org: we
# resolve the operator's --org to the set of that org's project ids and pass it as
# the visible_pids= scope to every tm_module query (None = unscoped, gate off).
def _tm_visible_pids(session, args):
    """The project-id scope set for an in-process --org operator, or None (unscoped:
    gate off, or --operator cross-org). SegmentTM is scoped through project_id ->
    Project.org_id, so a foreign org's entries are invisible + un-deletable (F2).

    Customer-first browse: when --customer is set (tm_customer) the scope is narrowed
    to that customer's projects (∩ the org base), so the CLI browses one customer's TM
    exactly like the web/API customer selection."""
    from sqlmodel import select

    from app.models import Project

    oid = op_org_id(args)
    if oid is None:
        base = None
    else:
        base = [pid for pid in session.exec(select(Project.id).where(Project.org_id == oid)).all()]  # noqa: C416
    cid = getattr(args, "tm_customer", None)
    if cid is not None:
        from app.kernel import tm_module

        return tm_module.scoped_pids_for_customer(session, base, cid)
    return base


def _tm_filters(args):
    """Assemble the tm_module filter kwargs from the parsed --f_* flags. A blank
    date is ignored; an invalid date raises a clean CLI error."""
    from datetime import datetime, timezone

    def _date(raw, label):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            d = datetime.strptime(raw, "%Y-%m-%d")
            return d.replace(tzinfo=timezone.utc).replace(tzinfo=None)
        except ValueError as e:
            raise CliError(f"--{label} must be YYYY-MM-DD, got {raw!r}.", EXIT_USAGE) from e

    def _content_type(raw):
        # Finding 4 (parity, unchanged): OMITTING --content-type still means "no
        # filter", byte-identical to the web / api / _remote TM-browse surfaces (their
        # own malformed-value handling, in separate functions, is untouched by this).
        # SPEC-content-types phase-3 re-review LOW (CLI-flag UX only): a value the
        # OPERATOR explicitly TYPED that is neither a digit id nor 'general'/'none'/
        # 'untyped' is far more likely a typo than a deliberate "show me everything"
        # request -- silently downgrading it to no-filter would show the FULL
        # unfiltered TM instead of erroring, the opposite of what an explicit filter
        # flag implies. Raise a clean error instead; an OMITTED flag is unaffected.
        raw = (raw or "").strip().lower()
        if not raw:
            return None
        if raw in ("general", "none", "untyped"):
            return "__none__"
        if raw.isdigit():
            return raw
        raise CliError(
            f"--content-type must be an id or 'general'/'none'/'untyped', got {raw!r}.", EXIT_USAGE
        )

    return {
        "source_locale": getattr(args, "f_source", None),
        "target_locale": getattr(args, "f_target", None),
        "project_id": getattr(args, "f_project", None),
        "content_type_id": _content_type(getattr(args, "f_content_type", None)),
        "q": getattr(args, "f_query", None),
        "created_before": _date(getattr(args, "f_created_before", None), "created-before"),
        "created_after": _date(getattr(args, "f_created_after", None), "created-after"),
        "min_use": getattr(args, "f_min_use", None),
        "max_use": getattr(args, "f_max_use", None),
        "unused": bool(getattr(args, "f_unused", False)),
    }


def _tm_row(e: dict):
    src = (e["source_text"] or "")[:40]
    tgt = (e["target_text"] or "")[:40]
    ct = e.get("content_type_id")
    return (
        e["id"],
        src,
        tgt,
        f"{e['source_locale']}->{e['target_locale']}",
        ct if ct is not None else "general",
        e["use_count"],
        e["last_used_at"] or "-",
    )


def _skipped_note(res: dict) -> str:
    """ASCII-safe trailer naming TBX entries dropped for want of a language pair.
    Empty for a CSV import (and for a TBX where every entry had both sides), so the
    existing one-line output is unchanged."""
    n = res.get("skipped") or 0
    return (
        f" Skipped {n} entr{'y' if n == 1 else 'ies'} with no matching language pair." if n else ""
    ) + _report_note(res)


def _report_note(res: dict) -> str:
    """The per-entry termbase import report (TBX / MultiTerm XML): totals of imported,
    merged and skipped entries, each skip reason with its count, forbidden/deprecated
    terms kept, and the first non-imported entries by id. Empty for a CSV import (no
    "report" key), so the CSV output is unchanged. ASCII-only (terminal rule)."""
    from app.kernel import glossary_tbx

    lines = glossary_tbx.report_lines(res.get("report") or {})
    if not lines:
        return ""
    text = "\n".join(lines)
    return "\n" + text.encode("ascii", "replace").decode("ascii")


def _changed_note(res: dict) -> str:
    """ASCII-safe trailer naming how many rows an import VALUE-CHANGED vs left
    truly untouched -- the counters that close the "0 entries, 0 senses" gap: an
    import that only REWRITES every existing term (new targets/DNT/metadata for a
    termbase that already exists) creates nothing, so entries/senses alone stayed
    0 while the import silently changed real content (and, for a module glossary,
    reverted an APPROVED glossary to DRAFT -- see glossary_module.import_bytes).
    Empty when the res shape carries neither counter (the narrow legacy
    {"entries", "senses"} shape), so an older caller's one-line output is
    unchanged."""
    if "changed" not in res and "unchanged" not in res:
        return ""
    changed = res.get("changed") or 0
    unchanged = res.get("unchanged") or 0
    return f" {changed} term(s) updated, {unchanged} unchanged." + _chunks_note(
        res.get("write_chunks"), res.get("concurrent_deleted")
    )


def _chunks_note(write_chunks, concurrent_deleted) -> str:
    """ASCII-safe trailer for a glossary import (#615): how many short transactions a big file
    was stored in (a failure part way keeps the stored entries; the same file finishes it) and
    how many entries someone deleted while it ran (they are not restored). Shared by the local
    and --remote verbs so the wording cannot drift."""
    return (
        f" Written in {write_chunks} short transactions (a failure part way keeps the"
        " entries already stored; re-run the same file to finish)."
        if (write_chunks or 0) > 1
        else ""
    ) + (
        f" Did not restore {concurrent_deleted} glossary entr"
        f"{'y' if concurrent_deleted == 1 else 'ies'} someone deleted while the import"
        " ran (import the file again to store them)."
        if concurrent_deleted
        else ""
    )


# --- glossary module (Phase 5-B) --------------------------------------------
# In-process (local-root, no RBAC — same trust as editing the DB directly, the CLI
# convention). Web+CLI parity: every module action (create/list/show/approve/
# set-pairs/add-term/import/delete) has a command here.
def _gmod_get(session, args):
    """Fetch a module glossary in the operator's --org scope, or 404-as-usage.

    R2-3 (Finding F2): the item-commands must NOT use a bare gm.get() — under
    multi-org that lets an --org A operator show/approve/delete org B's glossary.
    Scope the fetch through gm.visible() with the operator's org, exactly as
    _gmod_list scopes its listing (customer_id stays None — the in-process CLI is a
    trusted operator surface with no customer-bound identity, mirroring _get_project
    which only enforces org).

    #73 COUPLING: op_org_SCOPE (not op_org_id) — gm.visible now fails closed on a bare
    None under an active gate, so the key-verified --operator door must hand it
    access.UNSCOPED (op_org_scope) to keep the cross-org read; op_org_id would return a
    bare None and the operator would 404 on every glossary."""
    from app.kernel import glossary_module as gm

    g = gm.visible(session, args.glossary_id, org_id=op_org_scope(args))
    if g is None:
        raise CliError(f"no such glossary: {args.glossary_id}", EXIT_USAGE)
    return g


# --- concept/term rich store (SPEC-terminology-backend T1) -------------------
# In-process (local-root) like the DB-direct module commands: reads render the
# concept/term rows; mutations delegate to the shared glossary_concepts service so
# the CLI and the web routes go through one seam (parity). Every handler scopes the
# glossary through _gmod_get first (LAW-TERM-12 org/customer confinement), then
# confines concept/term ids to that glossary before acting.
def _concept_payload(concept, terms) -> dict:
    """A concept + its terms as a plain dict for --json (carries the numeric ids the
    show/edit/delete/terms leaves take, which the glossary-concept/v1 export hides
    behind minted doc ids)."""
    return {
        "id": concept.id,
        "external_id": concept.external_id,
        "domain": concept.domain,
        "subdomain": concept.subdomain,
        "definition": concept.definition,
        "notes": concept.notes,
        "source": concept.source,
        "homonym_group": concept.homonym_group,
        "terms": [
            {
                "id": t.id,
                "locale": t.locale,
                "term": t.term,
                "status": t.status,
                "term_type": t.term_type,
                "audience": t.audience,
                "part_of_speech": t.part_of_speech,
                "usage_note": t.usage_note,
                "context_example": t.context_example,
                "geographical_usage": t.geographical_usage,
                "grammatical_gender": t.grammatical_gender,
                "grammatical_number": t.grammatical_number,
                "case_sensitive": t.case_sensitive,
                "exact": t.exact,
                "dnt": t.dnt,
                "source": t.source,
            }
            for t in terms
        ],
    }


def _concept_terms(session, concept_id):
    from sqlmodel import select

    from app.models import GlossaryTerm

    return session.exec(
        select(GlossaryTerm).where(GlossaryTerm.concept_id == concept_id).order_by(GlossaryTerm.id)
    ).all()


# --- langset -----------------------------------------------------------------
def _langset_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.langset_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import languages

    actor = getattr(args, "actor", None) or "cli"
    try:
        ls = languages.create(
            args.name, args.source, args.targets, actor=actor, org_id=op_org_id(args)
        )
    except languages.InvalidLocaleError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(
        f"Created language set #{ls.id}: {ls.name} "
        f"({ls.source_locale} -> {', '.join(ls.target_locales or []) or 'no targets'})",
        {
            "ok": True,
            "id": ls.id,
            "name": ls.name,
            "source_locale": ls.source_locale,
            "target_locales": ls.target_locales or [],
        },
    )
    return 0


def _langset_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.langset_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import languages

    sets = languages.list_sets(org_id=op_org_id(args))
    rows = [(ls.id, ls.name, ls.source_locale, ", ".join(ls.target_locales or [])) for ls in sets]
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "language_sets": [
                    {
                        "id": ls.id,
                        "name": ls.name,
                        "source_locale": ls.source_locale,
                        "target_locales": ls.target_locales or [],
                    }
                    for ls in sets
                ],
            }
        )
    else:
        emit.table(rows, headers=("id", "name", "source", "targets"))
    return 0


def _langset_update(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.langset_update(args, emit)
    require_org_scope(args, emit)
    from app.kernel import languages

    oid = op_org_id(args)
    ls = get_scoped_or_404(languages.get(args.langset_id), args.langset_id, "language set", oid)
    # Preserve unspecified fields: languages.update falls back to the current
    # value when name/source are blank; targets are ONLY replaced when given.
    targets = args.targets if args.targets else ls.target_locales
    try:
        languages.update(args.langset_id, args.name, args.source, targets)
    except languages.InvalidLocaleError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    fresh = languages.get(args.langset_id)
    emit.result(
        f"Updated language set #{fresh.id}: {fresh.name} "
        f"({fresh.source_locale} -> {', '.join(fresh.target_locales or []) or 'no targets'})",
        {
            "ok": True,
            "id": fresh.id,
            "name": fresh.name,
            "source_locale": fresh.source_locale,
            "target_locales": fresh.target_locales or [],
        },
    )
    return 0


def _langset_delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.langset_delete(args, emit)
    require_org_scope(args, emit)
    from app.kernel import languages

    oid = op_org_id(args)
    ls = get_scoped_or_404(languages.get(args.langset_id), args.langset_id, "language set", oid)
    if not confirm(emit, f"Delete language set #{ls.id} ({ls.name})?", args.yes):
        raise CliError("aborted.", 1)
    # SPEC-rename-delete §7: fails closed while a RoutingRule / Connection config still
    # references the set (clean usage error, never a traceback).
    try:
        languages.delete(args.langset_id)
    except languages.LanguageSetInUseError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(
        f"Deleted language set #{ls.id} ({ls.name}).", {"ok": True, "id": ls.id, "deleted": True}
    )
    return 0
