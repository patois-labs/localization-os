"""`template ...` - project templates (P7, item 9), the CLI half of "Save as
template" / "Create from template".

In-process, local-root. Business logic lives in app.services.templates - thin
shell only. `create --from-project` snapshots a live project's setup; `apply`
instantiates a new project (dangling refs degrade to printed warnings, exactly
like the web flow).

Documented in: docs/architecture/project-setup-and-templates.md
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    op_creator,
    op_org_id,
    op_org_scope,
    remote_mode,
    require_org_scope,
)


def register(subparsers) -> None:
    tp = subparsers.add_parser("template", help="Project templates (save + reuse project setups).")
    sub = tp.add_subparsers(dest="template_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Save a project's setup as a template.")
    p.add_argument(
        "--from-project",
        required=True,
        type=bounded_id,
        dest="project_id",
        help="project id to snapshot.",
    )
    p.add_argument("--name", required=True, help="unique template name.")
    p.add_argument(
        "--scope",
        choices=["global", "customer"],
        default="global",
        help="global, or scoped to the project's customer.",
    )
    p.add_argument("--description", default="")
    p.set_defaults(func=_create)

    p = sub.add_parser("list", help="List templates (global + a customer's own).")
    p.add_argument("--customer-id", default=None, help="include this customer's scoped templates.")
    p.add_argument("--all", action="store_true", help="every template in the install (admin view).")
    p.set_defaults(func=_list)

    p = sub.add_parser("show", help="Show one template's captured setup.")
    p.add_argument("template_id", type=bounded_id)
    p.set_defaults(func=_show)

    p = sub.add_parser("apply", help="Create a new project from a template.")
    p.add_argument("template_id", type=bounded_id)
    p.add_argument("--name", required=True, help="name for the new project.")
    p.add_argument(
        "--customer-id",
        help="customer id to attach (optional for a customer-scoped "
        "template, which always keeps its own customer; required "
        "for a global template when the org has any selectable "
        "customer).",
    )
    p.add_argument(
        "--connected",
        action="store_true",
        help="create a CONNECTED project (empty; you connect its source on the project page). "
        "A template never binds or schedules a source by itself.",
    )
    p.set_defaults(func=_apply)

    p = sub.add_parser("rename", help="Rename a template.")
    p.add_argument("template_id", type=bounded_id)
    p.add_argument("name", help="the new name.")
    p.set_defaults(func=_rename)

    p = sub.add_parser(
        "delete", help="Delete a template (snapshots only - nothing references them)."
    )
    p.add_argument("template_id", type=bounded_id)
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_delete)


def _svc():
    from app.kernel import templates

    return templates


def _create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.template_create(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    with _session() as session:
        try:
            tpl = svc.save_as_template(
                session,
                args.project_id,
                args.name,
                scope=args.scope,
                description=args.description,
                actor=getattr(args, "actor", None) or "cli",
                org_id=op_org_id(args),
            )
        except svc.TemplateError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(
        f"Saved template #{tpl.id}: {tpl.name} [{'customer' if tpl.customer_id else 'global'}].",
        {"ok": True, "template": svc.template_row(tpl)},
    )
    return 0


def _list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.template_list(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    oid = op_org_id(args)
    if args.all:
        rows = svc.all_templates(org_id=oid)
    else:
        rows = svc.list_templates(customer_id=args.customer_id, org_id=oid)
    payload = [svc.template_row(t) for t in rows]
    if emit.as_json:
        emit.json({"ok": True, "templates": payload})
        return 0
    if not payload:
        emit.say("No templates.")
        return 0
    emit.table(
        [
            (
                r["id"],
                r["name"],
                r["scope"],
                r["source_locale"],
                ",".join(r["target_locales"]),
                r["default_provider"],
            )
            for r in payload
        ],
        headers=("id", "name", "scope", "source", "targets", "provider"),
    )
    return 0


def _show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.template_show(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    tpl = svc.get_template(args.template_id, org_id=op_org_id(args))
    if tpl is None:
        raise CliError(f"no such template: {args.template_id}", EXIT_USAGE)
    r = svc.template_row(tpl)
    emit.result(
        f"#{r['id']} {r['name']} [{r['scope']}]\n"
        f"  {r['source_locale']} -> {', '.join(r['target_locales']) or '(none)'}\n"
        f"  provider: {r['default_provider']}"
        f"  workflow: {r['default_workflow_id'] or '-'}"
        f"  segmentation: {r['segmentation_profile_id'] or '-'}\n"
        f"  description: {r['description'] or '-'}",
        {"ok": True, "template": r},
    )
    return 0


def _apply(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.template_apply(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    with _session() as session:
        try:
            # bound_customer_id is intentionally NOT passed here: the in-process
            # CLI operator is a trusted, non-customer-bound identity (R2-3 binds
            # only web-session User accounts to a customer; the CLI's --org
            # scope is a separate, org-level dimension). If a future surface
            # runs this on behalf of a real bound User, thread their
            # access.current_customer_id(user) through here the same way the
            # web route does.
            # #22: mirror the web POST /projects/from-template route exactly —
            # --customer-id (blank = use the template's own customer, for a
            # customer-scoped template) plus require_customer=True, so applying a
            # GLOBAL template with no customer chosen fails cleanly
            # (TemplateError, via content.MissingCustomerError) instead of
            # silently creating a customer-less project. A customer-scoped
            # template still ignores a matching pick and rejects a conflicting
            # one (apply_template's own conflict check, unchanged). An org with
            # NO selectable customer still applies cleanly with no --customer-id
            # (org_has_selectable_customer's self-healing banner semantics).
            proj, warnings = svc.apply_template(
                session,
                args.template_id,
                args.name,
                customer_id=(getattr(args, "customer_id", None) or None),
                created_by=getattr(args, "actor", None) or "cli",
                creator=op_creator(args),
                org_id=op_org_id(args),
                # #73 COUPLING (gate catch): the applied project inherits the customer's
                # default content type, validated through the now fail-closed
                # content_types.attachable. op_creator() is None on the --operator door, so
                # hand the DELIBERATE resolution scope (access.UNSCOPED for --operator, the
                # concrete id for --org) exactly as `project create` does. --org / gate-off
                # are byte-identical.
                content_type_scope=op_org_scope(args),
                require_customer=True,
                intake=("connected" if getattr(args, "connected", False) else "upload"),
            )
        except svc.TemplateError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        pid, pname, pintake = proj.id, proj.name, proj.intake
    for w in warnings:
        emit.warn(w)
    emit.result(
        f"Created project #{pid}: {pname} from template #{args.template_id}.",
        {"ok": True, "project_id": pid, "intake": pintake, "warnings": warnings},
    )
    return 0


def _rename(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.template_rename(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit

    svc = _svc()
    oid = op_org_id(args)
    with _session() as session:
        tpl = svc.get_template(args.template_id, org_id=oid)
        if tpl is None:
            raise CliError(f"no such template: {args.template_id}", EXIT_USAGE)
        old_name = tpl.name
        try:
            # actor=None: trusted in-process CLI operator (org-gated by op_org_id
            # above); the service still surfaces any scope/dup denial as a clean error.
            tpl = svc.rename_template(session, args.template_id, args.name, org_id=oid)
        except svc.TemplateError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        row = svc.template_row(tpl)
        tid, tname = tpl.id, tpl.name
    audit.log(
        actor=getattr(args, "actor", None) or "cli",
        action="template.rename",
        target=f"template {tid}",
        detail=f"{old_name!r} -> {tname!r}",
    )
    emit.result(f"Renamed template #{tid} to {tname}.", {"ok": True, "template": row})
    return 0


def _delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.template_delete(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    oid = op_org_id(args)
    # 2026-09-20. What this actually does, corrected by gate round 2 after round 1 and I
    # both got the premise wrong:
    #
    # It is a NARROWING, not a restoration. The claim it was written under -- that removing
    # the back-office left a GLOBAL template undeletable by anyone -- is FALSE. `--operator`
    # sets `_op_org_id = None`, so `op_org_id(args)` was already None on that branch and
    # `cli template delete --operator <global>` already worked before this train. Round 1
    # said "require_org_scope so oid is never None under an active gate", which is true of
    # every branch except the operator one, and I believed it instead of driving it.
    #
    # The one real behaviour change: on main, `--operator` could also delete ANOTHER ORG'S
    # org-owned template, because the unscoped lookup found it and nothing re-checked. Now
    # `platform_operator` is passed explicitly and the service refuses an org-owned row
    # outright, so the operator door deletes global rows only. Safe direction, and the
    # reason it is worth keeping, but it is a restriction and should be read as one.
    #
    # An org-scoped run is byte-identical to before.
    #
    # GATE R2 LOW-1: the `None if operator else oid` below is REDUNDANT, not load-bearing.
    # `oid` is already None on the operator branch, so both arms are the same value today,
    # and `platform_operator=operator` is the only argument with any effect. It stays
    # spelled out because the lookup being unscoped is the precondition the refusal
    # depends on, and a future change to `op_org_id` should not silently re-scope it.
    operator = bool(getattr(args, "_op_unscoped", False))
    tpl = svc.get_template(args.template_id, org_id=None if operator else oid)
    if tpl is None:
        raise CliError(f"no such template: {args.template_id}", EXIT_USAGE)
    if not confirm(emit, f"Delete template '{tpl.name}'?", getattr(args, "yes", False)):
        return EXIT_USAGE
    from app.kernel import audit

    # Captured BEFORE the delete: reading them off `tpl` afterwards can hit an expired
    # instance and trigger a refresh of a row that is gone. `rename` above does the same.
    tid, tname, t_org = tpl.id, tpl.name, getattr(tpl, "org_id", None)
    try:
        # actor=None: the in-process CLI is a trusted operator (no role gate), but
        # the service still surfaces any scope denial as a clean CliError (#1).
        # org_id: the resolved --org scope, so a cross-org id is not-found even
        # for the trusted operator path (mirrors get_template above).
        deleted = svc.delete_template(tpl.id, actor=None, org_id=oid, platform_operator=operator)
    except svc.TemplateError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    # GATE R4 LOW-2: the return value was discarded, so a False (nothing deleted, e.g. the
    # row lost a race with another deleter) still printed "Deleted template #N." and, once
    # this verb started auditing, wrote a row claiming a deletion that did not happen. An
    # audit trail that records deletions which never occurred is worse than one that misses
    # some. main's back-office route did capture this (b9a0a958:app/backoffice/app.py:550)
    # and the CLI twin dropped it.
    if not deleted:
        raise CliError(f"template {tid} was not deleted (it may have just been removed)")
    # GATE R3 MED-2. On main, deleting a GLOBAL template went through the back-office route,
    # which wrote a `backoffice.template_delete` row; delete_template itself deliberately
    # does not audit, on the documented grounds that "the route audits it". This train
    # deleted that route, so without this line the ONLY surviving door to a GLOBAL template
    # writes nothing, on an install where audit_enabled is on, for a destructive and
    # irreversible install-wide change. Every other template surface audits (web, /api, and
    # `template rename` twenty lines above), and audit.py treats operator verbs as the class
    # that must write a row even when the global gate is off.
    #
    # Unconditional, not operator-only: `cli template delete` was unaudited on main too, and
    # a rule of "audits only sometimes" is the kind nobody can reason about at 3am. The
    # detail names the scope so a GLOBAL removal is distinguishable from an org-owned one.
    audit.log(
        actor=getattr(args, "actor", None) or "cli",
        action="template.delete",
        target=f"template {tid}",
        detail=(
            f"{tname!r} (GLOBAL, via --operator)" if operator and t_org is None else repr(tname)
        ),
        # force ONLY on the operator door. audit.log is a no-op under audit_enabled="0",
        # and audit.py's own rule is that operator verbs (org.*, impersonate.*) write
        # unconditionally so that whoever can turn the gate off cannot then perform an
        # un-audited install-wide write. Deleting a GLOBAL template is exactly that class.
        # An ordinary org-scoped delete stays gated like every other tenant-level verb.
        force=operator,
    )
    emit.result(f"Deleted template #{tid}.", {"ok": True, "deleted": tid})
    return 0
