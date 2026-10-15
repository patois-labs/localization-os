"""`org` command group — multi-org P7 (organization management).

CROSS-ORG OPERATOR commands (create / list / rename / show / suspend / unsuspend
an organization). Org management is inherently cross-org, so these are IN-PROCESS
ONLY (never --remote) and, when the sacred gate is ON, require --operator (env
LOC_TMS_OPERATOR_KEY vs the stored operator_key_hash) — you cannot scope org
management to a single org, so --org is not accepted. With the gate OFF they run
freely on-prem (creating a 2nd org is allowed but inert): byte-identical to today.

Business logic lives in app.services.access (create_org/list_orgs/rename_org/
set_org_active); this module is a thin CLI shell: parse, call the service, emit a
friendly line/table (or --json), audit the write.

There is NO web parity for these, and has not been since 2026-09-20: the back-office
org CRUD was removed together with the super-admin account type, so this CLI is the
only surface. Every leaf here has an EXPECTED_COMMANDS entry in
tests/verify_cli_parity.py (suite #42) — the web+CLI parity rule.

EVERY WRITE HERE AUDITS WITH force=True (gate R4 MED-2). These are OPERATOR verbs, and
audit.py's rule is that the operator class writes a row even when audit_enabled is off,
so that whoever can turn auditing off cannot then perform an un-audited install-wide
write. Until 2026-09-20 the forced sites for org.* lived in the back-office route and
these CLI twins did NOT force, because the console was the surface an operator actually
used. Deleting the console deleted the whole class: driven on a throwaway install with
audit_enabled="0", three of these verbs succeeded and wrote ZERO rows, while three
documents still said they were forced. If you add a verb here, force it.

Documented in: docs/architecture/tenancy-confinement.md
"""

from ._common import EXIT_USAGE, CliError, _actor, _session, bounded_id, confirm, require_operator


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "org",
        help="Organizations (create, list, rename, show, suspend/unsuspend) "
        "- cross-org operator commands (in-process; --operator when "
        "multi-org is on).",
    )
    sub = grp.add_subparsers(dest="org_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create an organization.")
    p.add_argument("--name", required=True, help="organization name (unique).")
    p.set_defaults(func=_org_create)

    p = sub.add_parser("list", help="List organizations (id, name, status).")
    p.set_defaults(func=_org_list)

    p = sub.add_parser("rename", help="Rename an organization.")
    p.add_argument("org_id", type=bounded_id, help="organization id.")
    p.add_argument("--name", required=True, help="the new name (unique).")
    p.set_defaults(func=_org_rename)

    p = sub.add_parser("show", help="Show one organization (name, status, counts).")
    p.add_argument("org_id", type=bounded_id, help="organization id.")
    p.set_defaults(func=_org_show)

    p = sub.add_parser(
        "suspend",
        help="Suspend an org (its users can't log in / "
        "its API tokens are rejected while multi-org is on).",
    )
    p.add_argument("org_id", type=bounded_id, help="organization id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_org_suspend)

    p = sub.add_parser("unsuspend", help="Re-activate a suspended org.")
    p.add_argument("org_id", type=bounded_id, help="organization id.")
    p.set_defaults(func=_org_unsuspend)

    p = sub.add_parser(
        "delete",
        help="Delete an EMPTY organization (DESTRUCTIVE). "
        "Refuses the default org + any org that still holds "
        "users/customers/projects/connections.",
    )
    p.add_argument("org_id", type=bounded_id, help="organization id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_org_delete)


# --- helpers -----------------------------------------------------------------
def _org_row(org) -> dict:
    return {
        "id": org.id,
        "name": org.name,
        "active": org.active,
        "status": "active" if org.active else "suspended",
    }


# --- commands ----------------------------------------------------------------
def _org_create(args, emit) -> int:
    require_operator(args, emit)
    from app.kernel import access, audit

    with _session() as s:
        try:
            org = access.create_org(s, args.name)
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    audit.log(
        actor=_actor(args),
        action="org.create",
        target=org.name,
        detail=f"org_id={org.id}",
        force=True,
    )
    emit.result(f"Created org #{org.id}: {org.name} (active).", {"ok": True, **_org_row(org)})
    return 0


def _org_list(args, emit) -> int:
    require_operator(args, emit)
    from app.kernel import access

    with _session() as s:
        orgs = access.list_orgs(s)
    payload = [_org_row(o) for o in orgs]
    if emit.as_json:
        emit.json({"ok": True, "organizations": payload})
    else:
        emit.table(
            [(o["id"], o["name"], o["status"]) for o in payload], headers=("id", "name", "status")
        )
    return 0


def _org_rename(args, emit) -> int:
    require_operator(args, emit)
    from app.kernel import access, audit

    with _session() as s:
        try:
            org = access.rename_org(s, args.org_id, args.name)
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    audit.log(
        actor=_actor(args),
        action="org.rename",
        target=org.name,
        detail=f"org_id={org.id}",
        force=True,
    )
    emit.result(f"Renamed org #{org.id} -> {org.name}.", {"ok": True, **_org_row(org)})
    return 0


def _org_show(args, emit) -> int:
    require_operator(args, emit)
    from sqlalchemy import func
    from sqlmodel import select

    from app.kernel import access
    from app.models import Project, User

    with _session() as s:
        org = access.get_org(s, args.org_id)
        if org is None:
            raise CliError(f"no such organization: {args.org_id}", EXIT_USAGE)
        users = s.exec(select(func.count()).select_from(User).where(User.org_id == org.id)).one()
        projects = s.exec(
            select(func.count()).select_from(Project).where(Project.org_id == org.id)
        ).one()
    info = {**_org_row(org), "users": users, "projects": projects}
    if emit.as_json:
        emit.json({"ok": True, **info})
    else:
        emit.say(f"Org #{org.id}: {org.name}")
        emit.say(f"  status: {info['status']}   users: {users}   projects: {projects}")
    return 0


def _org_suspend(args, emit) -> int:
    require_operator(args, emit)
    from app.kernel import access, audit

    with _session() as s:
        org = access.get_org(s, args.org_id)
        if org is None:
            raise CliError(f"no such organization: {args.org_id}", EXIT_USAGE)
        if not confirm(
            emit,
            f"Suspend org #{org.id} ({org.name})? Its users can't "
            "log in and its API tokens are rejected.",
            args.yes,
        ):
            raise CliError("aborted.", 1)
        org = access.set_org_active(s, args.org_id, False)
    audit.log(
        actor=_actor(args),
        action="org.suspend",
        target=org.name,
        detail=f"org_id={org.id}",
        force=True,
    )
    emit.result(f"Suspended org #{org.id} ({org.name}).", {"ok": True, **_org_row(org)})
    return 0


def _org_unsuspend(args, emit) -> int:
    require_operator(args, emit)
    from app.kernel import access, audit

    with _session() as s:
        org = access.get_org(s, args.org_id)
        if org is None:
            raise CliError(f"no such organization: {args.org_id}", EXIT_USAGE)
        org = access.set_org_active(s, args.org_id, True)
    audit.log(
        actor=_actor(args),
        action="org.unsuspend",
        target=org.name,
        detail=f"org_id={org.id}",
        force=True,
    )
    emit.result(f"Unsuspended org #{org.id} ({org.name}).", {"ok": True, **_org_row(org)})
    return 0


def _org_delete(args, emit) -> int:
    require_operator(args, emit)
    from app.kernel import access, audit

    # The in-process operator is cross-org (unscoped), so there is no "own org" to
    # refuse here — caller_org_id stays None; the service still refuses the default
    # org and any non-empty org (fail-closed). Look the org up first so the confirm
    # prompt names it before anything is touched.
    with _session() as s:
        org = access.get_org(s, args.org_id)
        if org is None:
            raise CliError(f"no such organization: {args.org_id}", EXIT_USAGE)
        name = org.name
        if not confirm(
            emit,
            f"Delete org #{args.org_id} ({name})? This is "
            "irreversible and only succeeds if the org is empty.",
            args.yes,
        ):
            raise CliError("aborted.", 1)
        try:
            deleted = access.delete_org(s, args.org_id, caller_org_id=None)
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    audit.log(
        actor=_actor(args),
        action="org.delete",
        target=deleted.name,
        detail=f"org_id={deleted.id}",
        force=True,
    )
    emit.result(
        f"Deleted org #{deleted.id} ({deleted.name}).",
        {"ok": True, "id": deleted.id, "name": deleted.name, "deleted": True},
    )
    return 0
