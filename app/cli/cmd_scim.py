"""`scim` command group (Train I): the READ-ONLY CLI twin of the SCIM 2.0 provisioning
surface. Provisioning itself is IdP-push by design (Okta/Entra POST to /scim/v2), so
there is no create/update verb here — only operator visibility:

  scim status              flag state + counts of SCIM-managed users/groups + last sync
  scim users               list SCIM-managed accounts (auth_source=scim / adopted)
  scim groups              list SCIM directory groups + their member emails

In-process = trusted operator; `--org <id>` confines every listing under the multi-org
gate (the reports/databricks precedent). These are pure reads, so they are NOT
flag-gated — `scim status` reports the switch whether it is on or off.

Documented in: docs/architecture/sso-provisioning.md
"""

from ._common import op_org_id, op_org_scope, require_local, require_org_scope


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "scim",
        help="SCIM 2.0 provisioning visibility (status/users/groups) - read-only "
        "operator views of what your IdP has provisioned. Off by default.",
    )
    sub = grp.add_subparsers(dest="scim_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("status", help="Show SCIM flag state + provisioning counts.")
    p.set_defaults(func=_status)

    p = sub.add_parser("users", help="List SCIM-managed accounts.")
    p.set_defaults(func=_users)

    p = sub.add_parser("groups", help="List SCIM directory groups + members.")
    p.set_defaults(func=_groups)


# --- commands ----------------------------------------------------------------
def _last_sync(org_id):
    """The most recent SCIM audit activity (action LIKE 'scim.%'), as an ISO string or
    None. Under --org (#515) only rows written in that org are considered (AuditLog.org_id,
    #487); unscoped otherwise. SCIM audit rows are system-scoped (customer_id NULL)."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.kernel import access
    from app.models import AuditLog

    # #73 converged law: bare None under an ACTIVE gate matches nothing; UNSCOPED (the
    # key-verified --operator door, via op_org_scope) reads every org.
    if access.org_scope_blocked(org_id):
        return None, None
    org_id = access.org_scope_value(org_id)
    with Session(engine) as s:
        stmt = select(AuditLog).where(AuditLog.action.like("scim.%"))
        if org_id is not None:
            stmt = stmt.where(AuditLog.org_id == org_id)
        row = s.exec(stmt.order_by(AuditLog.ts.desc()).limit(1)).first()
        return (row.ts.isoformat() if row and row.ts else None), (row.action if row else None)


def _status(args, emit) -> int:
    require_local(args)  # in-process operator read; SCIM provisioning is IdP-push, not remote
    require_org_scope(args, emit)  # #515: without it op_org_id was None and counts spanned orgs
    from app.kernel import scim, settings

    org = op_org_id(args)
    enabled = scim.enabled()
    users = scim.managed_user_count(org)
    groups = scim.group_count(org)
    last_ts, last_action = _last_sync(op_org_scope(args))
    default_role = settings.get("scim_default_role") or "translator"
    emit.result(
        "SCIM {}: {} managed user(s), {} group(s); default role {}; last activity {}.".format(
            "enabled" if enabled else "disabled",
            users,
            groups,
            default_role,
            f"{last_action} @ {last_ts}" if last_ts else "none",
        ),
        {
            "ok": True,
            "enabled": enabled,
            "managed_users": users,
            "groups": groups,
            "default_role": default_role,
            "last_sync_ts": last_ts,
            "last_sync_action": last_action,
        },
    )
    return 0


def _users(args, emit) -> int:
    require_local(args)  # in-process operator read; SCIM provisioning is IdP-push, not remote
    require_org_scope(args, emit)
    from app.kernel import scim

    rows = scim.managed_users(op_org_id(args))
    if emit.as_json:
        emit.json({"ok": True, "users": rows})
        return 0
    if not rows:
        emit.say("No SCIM-managed accounts.")
        return 0
    emit.table(
        [
            (
                r["email"],
                r["name"] or "-",
                "active" if r["active"] else "inactive",
                r["external_id"] or "-",
                r["auth_source"],
            )
            for r in rows
        ],
        headers=("email", "name", "state", "external id", "auth source"),
    )
    return 0


def _groups(args, emit) -> int:
    require_local(args)  # in-process operator read; SCIM provisioning is IdP-push, not remote
    require_org_scope(args, emit)
    from app.kernel import scim

    groups = scim.groups_with_members(op_org_id(args))
    if emit.as_json:
        emit.json({"ok": True, "groups": groups})
        return 0
    if not groups:
        emit.say("No SCIM groups.")
        return 0
    for g in groups:
        ext = f" [externalId={g['external_id']}]" if g["external_id"] else ""
        emit.say(f"{g['name']}{ext}: {len(g['members'])} member(s)")
        for email in g["members"]:
            emit.say(f"  - {email}")
    return 0
