"""`field` command group + the custom-field verbs on `project` (SPEC-project-automation,
MVP part C): define typed project fields, set a project's values, filter and list by them.

In-process (local-root, no RBAC: the same trust as editing the DB directly) OR --remote
(a thin HTTP client of /api/custom-fields and /api/projects/{id}/fields, org-scoped
server-side by --token). All rules live in app.services.custom_fields; this module is a
thin CLI shell. ASCII-safe output.

Documented in: docs/architecture/content-and-projects.md
"""

import json

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    op_org_scope,
    parse_kv,
    remote_mode,
    require_org_scope,
)
from ._common import get_customer as _get_customer


def parse_field_args(pairs) -> dict:
    """``--field key=value`` (repeatable) -> ``{key: value}``. A link value may be given
    as ``key=https://...`` or ``key=LOC-123`` or, for url+key+label, as a JSON object."""
    out = {}
    for key, val in parse_kv(pairs, "--field").items():
        v = val.strip()
        if v.startswith("{"):
            try:
                out[key] = json.loads(v)
            except ValueError as exc:
                raise CliError(f"--field {key}: the JSON object is invalid.", EXIT_USAGE) from exc
        else:
            out[key] = val
    return out


def _cf():
    from app.kernel import custom_fields

    return custom_fields


def _flag_args(p) -> None:
    p.add_argument("--required", action="store_true", help="a value is required at project create.")
    p.add_argument(
        "--list-column",
        action="store_true",
        dest="list_column",
        help="show this field as a column in the projects list by default.",
    )
    p.add_argument(
        "--no-filter", action="store_true", dest="no_filter", help="do not allow filtering on it."
    )
    p.add_argument("--help-text", dest="help_text", default="", help="short help shown on forms.")
    p.add_argument("--order", type=int, default=0, help="column/form order (lower first).")
    p.add_argument(
        "--key-pattern",
        dest="key_pattern",
        default="",
        help="regular expression a link key (or a text value) must match, e.g. '[A-Z]+-[0-9]+'.",
    )
    p.add_argument(
        "--system",
        default="",
        help="external system of a link field, e.g. jira (a Jira key pattern is then built in).",
    )


def register(subparsers) -> None:
    fld = subparsers.add_parser(
        "field",
        help="Custom project fields (typed, e.g. a Jira ticket link): list, create, update.",
    )
    sub = fld.add_subparsers(dest="field_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("list", help="List custom project fields.")
    p.add_argument("--archived", action="store_true", help="include archived fields.")
    p.set_defaults(func=_field_list)

    p = sub.add_parser(
        "create", help="Define a custom project field (the key cannot change later)."
    )
    p.add_argument(
        "key", help="field key: lowercase letters, digits, underscores (e.g. jira_ticket)."
    )
    p.add_argument("label", help="label shown in the app (e.g. 'Jira ticket').")
    p.add_argument(
        "--type",
        default="text",
        choices=["text", "long_text", "number", "date", "select", "url", "external_link"],
        help="field type (default text). external_link = a link to an external ticket or document.",
    )
    p.add_argument("--options", default="", help="select options, comma separated.")
    p.add_argument(
        "--customer",
        type=bounded_id,
        default=None,
        help="customer scope id (omit = every customer).",
    )
    _flag_args(p)
    p.set_defaults(func=_field_create)

    p = sub.add_parser("update", help="Change a custom field (key and type are fixed).")
    p.add_argument("field_id", type=bounded_id, help="field id.")
    p.add_argument("--label", default=None, help="new label.")
    p.add_argument("--options", default=None, help="select options, comma separated (replaces).")
    p.add_argument("--required", choices=["on", "off"], default=None, help="required at create.")
    p.add_argument(
        "--list-column",
        choices=["on", "off"],
        dest="list_column",
        default=None,
        help="default projects-list column.",
    )
    p.add_argument("--filterable", choices=["on", "off"], default=None, help="allow filtering.")
    p.add_argument("--help-text", dest="help_text", default=None, help="short help shown on forms.")
    p.add_argument("--order", type=int, default=None, help="column/form order.")
    p.add_argument(
        "--key-pattern", dest="key_pattern", default=None, help="key regular expression."
    )
    p.add_argument("--system", default=None, help="external system of a link field.")
    p.set_defaults(func=_field_update)

    p = sub.add_parser("archive", help="Archive a field (its values are kept).")
    p.add_argument("field_id", type=bounded_id, help="field id.")
    p.set_defaults(func=_field_archive)

    p = sub.add_parser("restore", help="Restore an archived field.")
    p.add_argument("field_id", type=bounded_id, help="field id.")
    p.set_defaults(func=_field_restore)


def register_project_verbs(sub) -> None:
    """Add `project set-field` to the `project` subparser group."""
    p = sub.add_parser(
        "set-field",
        help="Set (or clear) a custom field value on a project.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("key", help="field key (see `field list`).")
    p.add_argument(
        "value",
        nargs="?",
        default="",
        help="the value; omit or pass '' to clear. A link takes a URL, a key, or a JSON object.",
    )
    p.set_defaults(func=_project_set_field)


def _sort_spec(args):
    raw = (getattr(args, "sort_field", "") or "").strip()
    if not raw:
        return "", False
    key, _, direction = raw.partition(":")
    return key.strip(), direction.strip().lower() == "desc"


def _columns(args) -> list:
    return [c.strip() for c in (getattr(args, "columns", "") or "").split(",") if c.strip()]


def apply_list_options(session, stmt, args):
    """Apply `--field` filters and `--sort-field` to an in-process `select(Project)` and
    return ``(stmt, extra column keys)`` for `--columns`."""
    cf = _cf()
    scope = op_org_scope(args)
    wanted = parse_kv(getattr(args, "field", []), "--field")
    sort_key, desc = _sort_spec(args)
    cols = _columns(args)
    if not (wanted or sort_key or cols):
        return stmt, []
    by_key = cf.group_by_key(cf.list_defs(session, scope))

    def _def(key):
        d = by_key.get(key.lower())
        if d is None:
            raise CliError(f"unknown custom field {key!r} (see `field list`).", EXIT_USAGE)
        return d

    from app.models import Project

    for key, expr in wanted.items():
        try:
            stmt = stmt.where(Project.id.in_(cf.project_ids_matching(session, _def(key), expr)))
        except cf.FieldError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    if sort_key:
        stmt = cf.apply_sort(stmt.order_by(None), _def(sort_key), descending=desc)
    return stmt, [_def(c)[0].key for c in cols]


def remote_list_params(args, params: dict) -> list:
    """Add `--field` / `--sort-field` to a remote list request; return the `--columns` keys."""
    for key, expr in parse_kv(getattr(args, "field", []), "--field").items():
        params[f"field.{key}"] = expr
    sort_key, desc = _sort_spec(args)
    if sort_key:
        params["sort"] = f"field.{sort_key}"
        if desc:
            params["dir"] = "desc"
    return _columns(args)


# --- field list / create / update / archive ----------------------------------
def _row(d) -> tuple:
    return (
        d["id"],
        d["key"],
        d["label"],
        d["type"],
        d["customer_id"] if d["customer_id"] else "all",
        "yes" if d["required"] else "",
        "archived" if d["archived"] else "",
    )


_HEADERS = ("id", "key", "label", "type", "scope", "required", "state")


def _field_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_field_list(args, emit)
    require_org_scope(args, emit)
    cf = _cf()
    with _session() as session:
        defs = [
            cf.to_dict(d)
            for d in cf.list_defs(
                session, op_org_scope(args), include_archived=bool(getattr(args, "archived", False))
            )
        ]
    if emit.as_json:
        emit.json({"ok": True, "fields": defs})
    else:
        emit.table([_row(d) for d in defs], headers=_HEADERS)
    return 0


def _field_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_field_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit

    cf = _cf()
    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        if args.customer is not None:
            _get_customer(session, args.customer, op_org_id(args))
        try:
            d = cf.create_def(
                session,
                args.key,
                args.label,
                args.type,
                options=args.options,
                key_pattern=args.key_pattern,
                link_system=args.system,
                help_text=args.help_text,
                required=args.required,
                show_in_list=args.list_column,
                filterable=not args.no_filter,
                list_order=args.order,
                org_id=op_org_id(args),
                customer_id=args.customer,
                created_by=actor,
            )
        except cf.FieldError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="field.define",
            target=f"custom_field {d.id} '{d.key}'",
            detail=f"type={d.type} required={d.required}",
            customer_id=d.customer_id,
        )
        out = cf.to_dict(d)
    emit.result(f"Created field #{out['id']}: {out['key']} ({out['type']}).", {"ok": True, **out})
    return 0


def _update_changes(args) -> dict:
    ch = {}
    if args.label is not None:
        ch["label"] = args.label
    if args.options is not None:
        ch["options"] = args.options
    for src, dst in (
        ("required", "required"),
        ("list_column", "show_in_list"),
        ("filterable", "filterable"),
    ):
        v = getattr(args, src)
        if v is not None:
            ch[dst] = v == "on"
    if args.help_text is not None:
        ch["help_text"] = args.help_text
    if args.order is not None:
        ch["list_order"] = args.order
    if args.key_pattern is not None:
        ch["key_pattern"] = args.key_pattern
    if args.system is not None:
        ch["link_system"] = args.system
    return ch


def _field_update(args, emit) -> int:
    if remote_mode(args):
        return _remote_field_update(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit

    cf = _cf()
    actor = getattr(args, "actor", None) or "cli"
    changes = _update_changes(args)
    if not changes:
        raise CliError("nothing to change.", EXIT_USAGE)
    with _session() as session:
        d = cf.visible(session, args.field_id, op_org_scope(args))
        if d is None:
            raise CliError(f"no such custom field: {args.field_id}", EXIT_USAGE)
        before = cf.to_dict(d)
        try:
            changed = cf.update_def(session, d, **changes)
        except cf.FieldError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if changed:
            audit.log(
                actor=actor,
                action="field.update",
                target=f"custom_field {d.id} '{d.key}'",
                detail=cf.change_detail(before, cf.to_dict(d), changed),
                customer_id=d.customer_id,
            )
        out = cf.to_dict(d)
    emit.result(f"Updated field #{out['id']} ({out['key']}).", {"ok": True, **out})
    return 0


def _field_set_archived(args, emit, archived: bool) -> int:
    if remote_mode(args):
        return _remote_field_archive(args, emit, archived)
    require_org_scope(args, emit)
    from app.kernel import audit

    cf = _cf()
    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        d = cf.visible(session, args.field_id, op_org_scope(args))
        if d is None:
            raise CliError(f"no such custom field: {args.field_id}", EXIT_USAGE)
        if cf.set_archived(session, d, archived):
            audit.log(
                actor=actor,
                action="field.archive" if archived else "field.restore",
                target=f"custom_field {d.id} '{d.key}'",
                customer_id=d.customer_id,
            )
        out = cf.to_dict(d)
    emit.result(
        f"Field #{out['id']} ({out['key']}) is {'archived' if archived else 'active'}.",
        {"ok": True, **out},
    )
    return 0


def _field_archive(args, emit) -> int:
    return _field_set_archived(args, emit, True)


def _field_restore(args, emit) -> int:
    return _field_set_archived(args, emit, False)


# --- project set-field --------------------------------------------------------
def _project_set_field(args, emit) -> int:
    values = {
        args.key: None
        if args.value == ""
        else parse_field_args([f"{args.key}={args.value}"])[args.key]
    }
    state = "cleared" if values[args.key] is None else "set"
    if remote_mode(args):
        c = _remote_impl.remote_client(args)
        body = c.patch(f"/api/projects/{args.project_id}/fields", json={"fields": values})
        emit.result(
            f"Project #{args.project_id}: {args.key} {state}.",
            {"ok": True, **body},
        )
        return 0
    require_org_scope(args, emit)
    from app.kernel import audit

    from .cmd_project_project import _get_project

    cf = _cf()
    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        try:
            changes = cf.set_values(session, project, values, actor=actor)
        except cf.FieldError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if changes:
            audit.log(
                actor=actor,
                action="field.set",
                target=f"project {project.id}",
                detail=cf.audit_detail(changes),
                customer_id=project.customer_id,
            )
        current = cf.project_values(session, project)
    emit.result(
        f"Project #{args.project_id}: {args.key} {state}.",
        {
            "ok": True,
            "project_id": args.project_id,
            "changed": [c["key"] for c in changes],
            "custom_fields": current,
        },
    )
    return 0


# --- remote arms -------------------------------------------------------------
def _remote_field_list(args, emit) -> int:
    c = _remote_impl.remote_client(args)
    body = c.get("/api/custom-fields", params={"archived": "1"} if args.archived else None)
    defs = body.get("fields", [])
    if emit.as_json:
        emit.json({"ok": True, "fields": defs})
    else:
        emit.table([_row(d) for d in defs], headers=_HEADERS)
    return 0


def _remote_field_create(args, emit) -> int:
    c = _remote_impl.remote_client(args)
    body = c.post(
        "/api/custom-fields",
        json={
            "key": args.key,
            "label": args.label,
            "type": args.type,
            "options": args.options,
            "key_pattern": args.key_pattern,
            "link_system": args.system,
            "help_text": args.help_text,
            "required": args.required,
            "show_in_list": args.list_column,
            "filterable": not args.no_filter,
            "list_order": args.order,
            "customer_id": args.customer,
        },
    )
    emit.result(
        f"Created field #{body['id']}: {body['key']} ({body['type']}).", {"ok": True, **body}
    )
    return 0


def _remote_field_update(args, emit) -> int:
    changes = _update_changes(args)
    if not changes:
        raise CliError("nothing to change.", EXIT_USAGE)
    c = _remote_impl.remote_client(args)
    body = c.patch(f"/api/custom-fields/{args.field_id}", json=changes)
    emit.result(f"Updated field #{body['id']} ({body['key']}).", {"ok": True, **body})
    return 0


def _remote_field_archive(args, emit, archived: bool) -> int:
    c = _remote_impl.remote_client(args)
    body = c.patch(f"/api/custom-fields/{args.field_id}", json={"archived": archived})
    emit.result(
        f"Field #{body['id']} ({body['key']}) is {'archived' if archived else 'active'}.",
        {"ok": True, **body},
    )
    return 0
