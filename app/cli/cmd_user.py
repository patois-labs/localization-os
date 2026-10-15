"""`user` / `apikey` / `member` command groups — Phase 5 (Users / Access).

All in-process (local-root, no RBAC — same trust as editing the DB directly).
Business logic lives in app.services.* (security.py, apikeys.py, access.py,
gdpr.py); this module is a thin CLI shell: parse args, open a Session where the
service needs one, call the service, emit a friendly line/table (or --json).

Secrets never surface: passwords are accepted via --password, --password-stdin OR a getpass prompt
(never echoed, never logged, never in --json); a minted API key is printed ONCE
at creation and is unrecoverable afterwards. Destructive ops (disable / erase /
revoke-all / member remove) confirm unless --yes.

--remote is reserved for a later phase; every command calls require_local() so a
--remote invocation fails cleanly rather than silently running locally.

Documented in: docs/architecture/identity-and-access.md
"""

import getpass
import os
import sys

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _resolve_user,
    _session,
    bounded_id,
    confirm,
    get_scoped_or_404,
    local_op,
    op_org_id,
    op_org_scope,
    remote_mode,
    require_operator,
    require_org_scope,
)
from ._common import apikey_expiry_cell as _apikey_expiry_cell


# --- multi-org scope guards (round-3 #61/#62) --------------------------------
# The SINGLE systematic root cause behind #61 was that require_org_scope recorded
# args._op_org_id but almost no command BODY applied it, so a non-operator caller
# passing --org <any-existing-id> read/wrote across orgs (foreign PII, cross-org
# role/enable flips, and — the headline — minting a working lsu_ key for another
# org's admin). These two helpers are the ONE guard the GDPR export/erase twins
# already used, factored out so EVERY identity leaf applies it uniformly.
def _resolve_user_scoped(session, ref: str, args):
    """Resolve a user by id/email, then enforce the in-process --org scope: a target
    in ANOTHER org is INDISTINGUISHABLE from a nonexistent one — same exit 2, same
    'no such user' message, no PII, no existence oracle. None scope (--operator /
    single-tenant / gate off) is unscoped, byte-identical to today. Callers that
    confirm()/mint MUST resolve through this BEFORE prompting or minting."""
    u = _resolve_user(session, ref)
    caller_org_id = op_org_id(args)
    if caller_org_id is not None and getattr(u, "org_id", None) != caller_org_id:
        raise CliError(f"no such user: {ref}", EXIT_USAGE)
    return u


def _scoped_key_or_404(session, key_id: int, args):
    """Load an ApiKey by id and enforce the in-process --org scope by the OWNER's org
    (ApiKey has no org_id — a key belongs to the org of its user, mirroring
    apikeys.list_keys_scoped). A key owned by a foreign-org user (or a missing id)
    is a clean 'no such API key' (exit 2). Returns the key when in scope."""
    from app.models import ApiKey, User

    key = session.get(ApiKey, key_id)
    caller_org_id = op_org_id(args)
    if key is None:
        raise CliError(f"no such API key: {key_id}", EXIT_USAGE)
    if caller_org_id is not None:
        owner = session.get(User, key.user_id)
        if owner is None or getattr(owner, "org_id", None) != caller_org_id:
            raise CliError(f"no such API key: {key_id}", EXIT_USAGE)
    return key


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    _register_user(subparsers)
    _register_apikey(subparsers)
    _register_member(subparsers)


def _register_user(subparsers) -> None:
    grp = subparsers.add_parser(
        "user",
        help="Users (create, list, show, role, department, enable/disable, "
        "reset-password, unlock, invite, GDPR export/erase).",
    )
    sub = grp.add_subparsers(dest="user_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create a user.")
    p.add_argument("--email", required=True, help="email / login (lower-cased).")
    p.add_argument("--role", required=True, help="one of: " + ", ".join(_ROLES()))
    p.add_argument("--name", default="", help="display name.")
    p.add_argument("--department", default="", help="department (routing / inbox).")
    p.add_argument(
        "--password",
        default=None,
        help="initial password (omit to prompt; blank = no local login).",
    )
    p.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the initial password from the first line of stdin (for scripts; "
        "keeps it out of argv and shell history; never echoed or logged).",
    )
    p.add_argument(
        "--customer",
        dest="customer_id",
        default="",
        help="bind to a customer id (R2-3 'customer account'; a bound "
        "user may only be translator/linguist/viewer). Blank = "
        "internal staff.",
    )
    p.set_defaults(func=_user_create)

    p = sub.add_parser("list", help="List users.")
    p.set_defaults(func=_user_list)

    p = sub.add_parser("show", help="Show one user (by email or id).")
    p.add_argument("user", help="email or numeric id.")
    p.set_defaults(func=_user_show)

    p = sub.add_parser("role", help="Set a user's role.")
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("role", help="one of: " + ", ".join(_ROLES()))
    p.set_defaults(func=_user_role)

    p = sub.add_parser("department", help="Set a user's department.")
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("department", help="department (stored lower-cased; '' clears).")
    p.set_defaults(func=_user_department)

    p = sub.add_parser(
        "customer",
        help="Bind a user to a customer (R2-3 'customer "
        "account') or clear the binding. A bound user may only be "
        "translator/linguist/viewer.",
    )
    p.add_argument("user", help="email or numeric id.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--customer", dest="customer_id", help="customer id to bind.")
    g.add_argument(
        "--clear-customer",
        dest="clear",
        action="store_true",
        help="clear the binding (back to internal staff).",
    )
    p.set_defaults(func=_user_customer)

    p = sub.add_parser(
        "org",
        help="Move a user to another organization (multi-org "
        "P7; cross-org operator action). Changes User.org_id ONLY - "
        "the user's projects/connections STAY in the old org.",
    )
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("org", help="destination organization id.")
    p.set_defaults(func=_user_org)

    p = sub.add_parser("enable", help="Re-activate a user.")
    p.add_argument("user", help="email or numeric id.")
    p.set_defaults(func=_user_enable)

    p = sub.add_parser(
        "disable",
        help="Deactivate a user (DESTRUCTIVE): ends their sessions and revokes their API keys.",
    )
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_user_disable)

    p = sub.add_parser(
        "reset-password",
        help="Admin password reset (no current password needed). Enforces the password policy; "
        "the person must choose a new password at their next sign-in. An SSO/SAML/SCIM/LDAP "
        "account becomes a local-password account (audited).",
    )
    p.add_argument("user", help="email or numeric id.")
    p.add_argument(
        "--password", default=None, help="the new password (omit to be prompted securely)."
    )
    p.set_defaults(func=_user_reset_password)

    p = sub.add_parser("unlock", help="Unlock password sign-in (host break-glass).")
    p.add_argument("user", help="email or numeric id.")
    p.set_defaults(func=_user_unlock)

    p = sub.add_parser(
        "mfa-status",
        help="Show a user's two-step sign-in (TOTP) state: on/off, when, recovery codes left, "
        "and whether mfa_required makes it mandatory. Never shows a secret or a code.",
    )
    p.add_argument("user", help="email or numeric id.")
    p.set_defaults(func=_user_mfa_status)

    p = sub.add_parser(
        "mfa-reset",
        help="Reset a user's two-step sign-in (a lost phone; the break-glass for a locked-out "
        "sole admin). Deletes their authenticator setup and recovery codes and signs them out "
        "everywhere.",
    )
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_user_mfa_reset)

    p = sub.add_parser(
        "invite",
        help="Create an invite (prints the accept link). Active accounts are refused; "
        "use reset-password instead.",
    )
    p.add_argument("--email", required=True, help="who to invite.")
    p.add_argument("--role", required=True, help="one of: " + ", ".join(_ROLES()))
    p.set_defaults(func=_user_invite)

    p = sub.add_parser("invites", help="List live pending invites (never tokens).")
    p.add_argument(
        "--expired", action="store_true", help="List expired invitations instead of pending."
    )
    p.set_defaults(func=_user_invites)

    p = sub.add_parser(
        "invite-revoke", help="Revoke a pending invitation, or delete an expired one."
    )
    p.add_argument(
        "--id",
        required=True,
        type=bounded_id,
        help="invitation id from user invites (or user invites --expired).",
    )
    p.set_defaults(func=_user_invite_revoke)

    p = sub.add_parser("export", help="GDPR subject-access export (NDJSON to stdout or -o FILE).")
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("-o", "--out", default="", help="write to FILE instead of stdout.")
    p.add_argument("--force", action="store_true", help="overwrite -o FILE if it exists.")
    p.set_defaults(func=_user_export)

    p = sub.add_parser(
        "erase", help="GDPR erasure: anonymize the user (DESTRUCTIVE, irreversible)."
    )
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_user_erase)


def _register_apikey(subparsers) -> None:
    grp = subparsers.add_parser("apikey", help="Per-user API keys (issue/list/revoke/revoke-all).")
    sub = grp.add_subparsers(dest="apikey_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("issue", help="Mint a key for a user (shown ONCE).")
    p.add_argument("--user", required=True, help="owner email or numeric id.")
    p.add_argument("--name", default="", help="a label for the key.")
    p.add_argument(
        "--scopes",
        default="docs",
        help="space/comma list; 'api' always implied, 'docs' unlocks "
        "the document endpoints, 'keys' unlocks the key-based "
        "project endpoints and the change feed, 'admin' unlocks the "
        "sharpest identity/admin verbs (GDPR, purge, revoke-all); "
        "'admin' can be minted ONLY here, locally, never over /api "
        "(default: docs).",
    )
    p.add_argument(
        "--expires-in-days",
        type=int,
        default=None,
        metavar="DAYS",
        help="optional lifetime: the key expires DAYS days from now. "
        "Omit for no expiry. Refused above this install's maximum "
        "(setting apikey_max_age_days, unset by default).",
    )
    p.set_defaults(func=_apikey_issue)

    p = sub.add_parser(
        "list",
        help="List API keys system-wide (metadata + last_used; "
        "NEVER the key value). --user filters to one owner.",
    )
    p.add_argument("--user", default="", help="filter to one owner (email or id).")
    p.set_defaults(func=_apikey_list)

    p = sub.add_parser("revoke", help="Revoke one key by its id.")
    p.add_argument("key_id", type=bounded_id, help="the ApiKey id (from `apikey list`).")
    p.set_defaults(func=_apikey_revoke)

    p = sub.add_parser("revoke-all", help="Revoke EVERY key a user holds (DESTRUCTIVE).")
    p.add_argument("user", help="owner email or numeric id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_apikey_revoke_all)


def _register_member(subparsers) -> None:
    grp = subparsers.add_parser("member", help="Project membership (add/remove/list).")
    sub = grp.add_subparsers(dest="member_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("add", help="Grant a user visibility into a project.")
    p.add_argument("user", help="member email or numeric id.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_member_add)

    p = sub.add_parser("remove", help="Revoke a membership (DESTRUCTIVE).")
    p.add_argument("user", help="member email or numeric id.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_member_remove)

    p = sub.add_parser("list", help="List a project's members.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_member_list)


# --- helpers -----------------------------------------------------------------
def _ROLES():
    from app.models import Role

    return Role.ALL


def _prompt_password(supplied):
    """Return the password to use: the --password value if given (even empty is a
    deliberate choice for create), else a confirmed getpass prompt. NEVER echoed.
    A non-interactive session with no --password is a clean usage error."""
    if supplied is not None:
        return supplied
    if not sys.stdin.isatty():
        raise CliError(
            "no password supplied and stdin is not a terminal - pass --password.", EXIT_USAGE
        )
    first = getpass.getpass("New password: ")
    again = getpass.getpass("Confirm password: ")
    if first != again:
        raise CliError("passwords did not match.", EXIT_USAGE)
    return first


def _create_password(args, emit) -> str:
    """The initial password for `user create`. --password (even empty) is a deliberate
    choice. Omitted on a terminal: prompt (confirmed, never echoed); a blank answer is the
    explicit "no local login" (SSO / API-only account). Omitted with no terminal there is no
    one to ask, so the account is created without a local login and a warning says so
    (#529: it used to do this silently on a terminal too, ignoring the documented prompt)."""
    if getattr(args, "password_stdin", False):
        if args.password is not None:
            raise CliError("use --password or --password-stdin, not both.", EXIT_USAGE)
        # One line, trailing newline stripped; the value is never echoed, logged or
        # put on argv. An empty line is a usage error (a script that meant "no local
        # login" omits both flags; a broken pipe must not create a login-less account
        # silently).
        line = sys.stdin.readline()
        pw = line.rstrip("\r\n")
        if not pw:
            raise CliError("--password-stdin read an empty password.", EXIT_USAGE)
        return pw
    if args.password is not None:
        return args.password
    if not sys.stdin.isatty() or emit.as_json or emit.quiet:
        emit.warn(
            "no --password given: the account has no local login (set one with reset-password)."
        )
        return ""
    import getpass

    first = getpass.getpass("Initial password (blank = no local login): ")
    if not first:
        return ""
    if first != getpass.getpass("Confirm password: "):
        raise CliError("passwords did not match.", EXIT_USAGE)
    return first


def _user_row(u):
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "department": u.department,
        "active": u.active,
        "auth_source": u.auth_source,
        "customer_id": u.customer_id,
    }


# --- user --------------------------------------------------------------------
def _user_create(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import audit, security
    from app.models import Customer, Role

    if args.role not in Role.ALL:
        raise CliError(f"unknown role: {args.role} (one of: {', '.join(Role.ALL)})", EXIT_USAGE)
    password = _create_password(args, emit)
    # R2-3: an optional customer binding. Validate existence here (the CLI is
    # local-root, no org context); the role<->binding invariant is enforced inside
    # security.create_user so both surfaces stay identical.
    cust_id = None
    cid = (getattr(args, "customer_id", "") or "").strip()
    if cid:
        from app.kernel.security import _coerce_customer_id

        coerced = _coerce_customer_id(cid)  # bounds the SQLite binder + drops junk
        if coerced is None:
            raise CliError(f"--customer must be a customer id, got {cid!r}.", EXIT_USAGE)
        with _session() as session:
            if session.get(Customer, coerced) is None:
                raise CliError(f"no such customer: {cid}", EXIT_USAGE)
        cust_id = coerced
    # Validate the customer binding is IN the operator's org too (a scoped caller
    # cannot bind a new user to a foreign org's customer).
    scope_org = op_org_id(args)
    if scope_org is not None and cust_id is not None:
        with _session() as session:
            cust = session.get(Customer, cust_id)
            if cust is None or getattr(cust, "org_id", None) != scope_org:
                raise CliError(f"no such customer: {cid}", EXIT_USAGE)
    # GATE R3 MED-2: the OPERATOR door had the same hole the web door and the OIDC and
    # LDAP doors had. `--operator` resolves op_org_id to None, so under an active gate this
    # minted an org_id=NULL account: absent from the scoped /users listing, 404 from every
    # /users/{id} route, and 401 for any API key it owns. Unlike a read, "no org" has no
    # sensible meaning for a CREATE, so access.UNSCOPED is not the answer here either. An
    # operator creating an account under multi-org has to say which org it belongs to.
    # #181: security.create_user enforces that now (refuse_orgless_account); this door
    # only keeps its operator-facing wording, which names the flag that fixes it.
    try:
        # #61: STAMP the new user into the scoped org (op_org_id). Leaving it None
        # under a --org scope would mint a see-all account (org_id=None reads every
        # org on browsable_customers/llmemory_view/org_id_clause) — a scoped caller
        # minting a see-all admin. None (single-tenant only) stays global.
        user = security.create_user(
            args.email, args.name, args.role, password, customer_id=cust_id, org_id=scope_org
        )
    except security.OrglessAccountError as exc:
        raise CliError(
            "multi-org is enabled, so a new account needs an organization: "
            "pass --org <id>. (--operator grants cross-org READ. You could also "
            "create org-less and then move the account with `user org`, but --org "
            "does it in one step and leaves no window where the account cannot "
            "sign in.)",
            EXIT_USAGE,
        ) from exc
    except ValueError as exc:  # reserved identity / duplicate / bad role binding
        raise CliError(str(exc)) from exc
    if args.department:
        with _session() as session:
            security.set_department(
                session, user.id, args.department, actor=_actor(args), org_id=op_org_scope(args)
            )
    audit.log(
        actor=_actor(args),
        action="user.create",
        target=user.email,
        detail=(f"customer {cust_id}" if cust_id else ""),
    )
    # re-read so department is reflected
    fresh = security.get_by_email(user.email)
    emit.result(
        f"Created user #{fresh.id}: {fresh.email} (role {fresh.role}"
        + (f", dept {fresh.department}" if fresh.department else "")
        + ").",
        {"ok": True, **_user_row(fresh)},
    )
    return 0


def _user_list(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import apikeys, security

    # Multi-org #61: confine the enumeration to the scoped org (op_org_id). None
    # (--operator / single-tenant / gate off) lists every org — byte-identical.
    org_id = op_org_id(args)
    users = [
        u
        for u in security.list_users(org_id=op_org_scope(args))
        if org_id is None or getattr(u, "org_id", None) == org_id
    ]
    rows, payload = [], []
    for u in users:
        keys = sum(1 for k in apikeys.list_keys(u.id) if k.active and not apikeys.key_is_expired(k))
        rows.append((u.id, u.email, u.role, u.department or "", "yes" if u.active else "no", keys))
        payload.append({**_user_row(u), "active_keys": keys})
    if emit.as_json:
        emit.json({"ok": True, "users": payload})
    else:
        emit.table(rows, headers=("id", "email", "role", "dept", "active", "keys"))
    return 0


def _user_show(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import apikeys
    from app.models import Customer

    with _session() as session:
        u = _resolve_user_scoped(session, args.user, args)
        keys = apikeys.list_keys(u.id)
        # Issue #19: the human table never showed the bound customer at all
        # (--json had only the bare customer_id); resolve the name too.
        customer = session.get(Customer, u.customer_id) if u.customer_id else None
        info = {
            **_user_row(u),
            "customer_name": customer.name if customer else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "api_keys": [
                {
                    "id": k.id,
                    "name": k.name,
                    "prefix": k.prefix,
                    "scopes": k.scopes,
                    "active": k.active,
                    "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                    "expired": apikeys.key_is_expired(k),
                }
                for k in keys
            ],
        }
        if emit.as_json:
            emit.json({"ok": True, **info})
        else:
            emit.say(f"User #{u.id}: {u.email}")
            emit.say(
                f"  name: {u.name or '(none)'}   role: {u.role}   "
                f"dept: {u.department or '(none)'}   "
                f"active: {'yes' if u.active else 'no'}"
            )
            emit.say(f"  auth: {u.auth_source}   last login: {info['last_login'] or 'never'}")
            emit.say(
                "  customer: "
                + (
                    f"#{u.customer_id} '{customer.name}'"
                    if customer
                    else "(none - a staff/org-wide user)"
                )
            )
            emit.say(f"  API keys ({len(keys)}):")
            emit.table(
                [
                    (
                        k.id,
                        k.name,
                        k.prefix,
                        k.scopes,
                        "yes" if k.active else "no",
                        "never"
                        if not k.expires_at
                        else k.expires_at.strftime("%Y-%m-%d")
                        + (" (expired)" if apikeys.key_is_expired(k) else ""),
                    )
                    for k in keys
                ],
                headers=("id", "name", "prefix", "scopes", "active", "expires"),
            )
    return 0


def _user_role(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import audit, security
    from app.models import Role

    if args.role not in Role.ALL:
        raise CliError(f"unknown role: {args.role} (one of: {', '.join(Role.ALL)})", EXIT_USAGE)
    with _session() as session:
        u = _resolve_user_scoped(session, args.user, args)
        res = security.set_role(
            session, u.id, args.role, actor=_actor(args), org_id=op_org_scope(args)
        )
        # #178-1: the seam refuses demoting the last active admin of the account's own org.
        if res.get("reason") == "last_admin":
            raise CliError("refusing to demote the last active administrator.")
        if not res["ok"]:
            raise CliError(f"could not set role on {u.email}.")
        audit.log(
            actor=_actor(args),
            action="user.role",
            target=res["email"],
            detail=f"{res['old']}->{res['new']}",
        )
        emit.result(
            f"{res['email']}: role {res['old']} -> {res['new']}.",
            {"ok": True, "email": res["email"], "old": res["old"], "new": res["new"]},
        )
    return 0


def _user_department(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import audit, security

    with _session() as session:
        u = _resolve_user_scoped(session, args.user, args)
        res = security.set_department(
            session, u.id, args.department, actor=_actor(args), org_id=op_org_scope(args)
        )
        audit.log(
            actor=_actor(args),
            action="user.department",
            target=res["email"],
            detail=f"{res['old'] or '(none)'}->{res['new'] or '(none)'}",
        )
        emit.result(
            f"{res['email']}: department {res['old'] or '(none)'} -> {res['new'] or '(none)'}.",
            {"ok": True, "email": res["email"], "old": res["old"], "new": res["new"]},
        )
    return 0


def _user_customer(args, emit) -> int:
    """Bind a user to a customer or clear the binding (R2-3 'customer account').
    A bound user may hold only translator/linguist/viewer; the invariant + customer
    existence live in security.set_customer (shared with the web Users page)."""
    local_op(args, emit)
    from app.kernel import audit, security

    clearing = bool(getattr(args, "clear", False))
    target = "" if clearing else (args.customer_id or "")
    if not clearing and str(target).strip() in ("", "0"):
        # #485 parity with the API: set_customer reads "" as "unbind", so a blank
        # --customer must not clear the binding; only --clear-customer does.
        raise CliError("give a customer id with --customer, or use --clear-customer.", EXIT_USAGE)
    with _session() as session:
        u = _resolve_user_scoped(session, args.user, args)
        # Multi-org #61: a scoped operator may only bind to a customer in the SAME
        # org (a foreign customer id is 404, not a cross-org binding oracle). None
        # scope leaves set_customer's own existence check unchanged.
        org_id = op_org_id(args)
        if org_id is not None and target:
            from app.kernel.security import _coerce_customer_id
            from app.models import Customer

            cid = _coerce_customer_id(target)
            cust = session.get(Customer, cid) if cid is not None else None
            if cust is None or getattr(cust, "org_id", None) != org_id:
                raise CliError(f"no such customer: {target}", EXIT_USAGE)
        res = security.set_customer(
            session, u.id, target, actor=_actor(args), org_id=op_org_scope(args)
        )
    if not res["ok"]:
        reason = res.get("reason")
        if reason == "role_bound":
            raise CliError(
                "only a translator, linguist or viewer account can be bound to a customer.",
                EXIT_USAGE,
            )
        if reason == "no_customer":
            raise CliError(f"no such customer: {target}", EXIT_USAGE)
        raise CliError("could not update the customer binding.")
    audit.log(
        actor=_actor(args),
        action="user.customer",
        target=res["email"],
        detail=f"{res['old'] or '(none)'}->{res['new'] or '(none)'}",
    )
    emit.result(
        f"{res['email']}: customer {res['old'] or '(none)'} -> {res['new'] or '(none)'}.",
        {"ok": True, "email": res["email"], "old": res["old"], "new": res["new"]},
    )
    return 0


def _user_org(args, emit) -> int:
    """Move a user to another org (cross-org operator action). Changes User.org_id
    ONLY — the user's already-created projects/connections STAY in the old org (a
    footgun we surface, never silently bulk-reparenting)."""
    require_operator(args, emit)
    import argparse

    from app.kernel import access, audit

    try:
        org_id = bounded_id(args.org)
    except argparse.ArgumentTypeError as e:
        raise CliError(
            f"org must be an organization id (integer), got {args.org!r}.", EXIT_USAGE
        ) from e
    with _session() as session:
        u = _resolve_user(session, args.user)
        try:
            user, old_org_id = access.move_user_to_org(session, u.id, org_id)
        except ValueError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    audit.log(
        actor=_actor(args),
        action="user.move_org",
        target=user.email,
        detail=f"org {old_org_id}->{org_id} (content stays in {old_org_id})",
        force=True,
    )
    emit.result(
        f"Moved {user.email} to org #{org_id}. NOTE: their existing projects/"
        f"connections stay in org #{old_org_id} (not re-parented).",
        {"ok": True, "email": user.email, "old_org_id": old_org_id, "new_org_id": org_id},
    )
    return 0


def _user_enable(args, emit) -> int:
    return _set_active(args, emit, True)


def _user_disable(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import security

    # last-admin guard: don't let the operator lock an org's admins out. #178-1: this
    # used to count admins INSTALL-WIDE, so org A's last admin could be disabled because
    # org B still had one. It now asks the shared seam, which counts the target's own
    # org; security.set_active enforces the same predicate, so this early check is only
    # there to refuse BEFORE the confirm() prompt.
    with _session() as session:
        # Scope-resolve BEFORE the confirm() prompt so a foreign-org target is a
        # clean 'no such user' (exit 2) with no email disclosed (oracle) and no flip.
        u = _resolve_user_scoped(session, args.user, args)
        if security.is_last_admin_of_own_org(session, u.id):
            raise CliError("refusing to disable the last active administrator.")
        # #422 gate: disabling revokes the account's keys for good, a SCIM token included.
        scim_note = (
            " " + security.SCIM_SYNC_STOPS_WARNING
            if security.scim_token_prefixes(session, u.id)
            else ""
        )
        if not confirm(emit, f"Disable {u.email} (#{u.id})?{scim_note}", args.yes):
            raise CliError("aborted.", 1)
    return _set_active(args, emit, False)


def _set_active(args, emit, active: bool) -> int:
    local_op(args, emit)
    from app.kernel import audit, security

    with _session() as session:
        u = _resolve_user_scoped(session, args.user, args)
        res = security.set_active(
            session, u.id, active, actor=_actor(args), org_id=op_org_scope(args)
        )
        # #178-1: the seam refuses the last active admin of the account's own org.
        if res.get("reason") == "last_admin":
            raise CliError("refusing to disable the last active administrator.")
        revoked = int(res.get("keys_revoked") or 0)
        ended_detail = f"disabled; sessions ended, {revoked} key(s) revoked"
        audit.log(
            actor=_actor(args),
            action="user.toggle",
            target=res["email"],
            detail="active" if res["active"] else ended_detail,
        )
        # #422: disabling ends the account's sessions and revokes its API keys for good.
        ended = (
            ""
            if res["active"]
            else f" Signed out everywhere; {revoked} API key{'' if revoked == 1 else 's'} revoked."
        )
        scim_tokens = res.get("scim_tokens_revoked") or []
        if scim_tokens:
            ended += " WARNING: " + security.SCIM_SYNC_STOPS_WARNING
        emit.result(
            f"{res['email']}: now {'active' if res['active'] else 'disabled'}.{ended}",
            {
                "ok": True,
                "email": res["email"],
                "active": res["active"],
                "keys_revoked": revoked,
                "scim_tokens_revoked": len(scim_tokens),
            },
        )
    return 0


def _user_reset_password(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import audit, security

    new_password = _prompt_password(args.password)
    with _session() as session:
        u = _resolve_user_scoped(session, args.user, args)
        was = (u.auth_source or "local").strip().lower()
        ok, msg = security.admin_set_password(session, u.id, new_password, actor=_actor(args))
        if not ok:
            raise CliError(msg, EXIT_USAGE)
        # Train sso-localpw: a reset switches an SSO/SAML/SCIM/LDAP account to a local
        # password on purpose (audited as user.auth_source_changed); say so.
        switched = (
            f" It now signs in with this local password instead of {was}." if was != "local" else ""
        )
        # audit the RESET, never the password.
        audit.log(
            actor=_actor(args),
            action="user.password_reset",
            target=u.email,
            detail="must change at next sign-in",
        )
        emit.result(
            f"Password reset for {u.email}. They must choose a new password at their next "
            f"sign-in.{switched}",
            {"ok": True, "email": u.email},
        )
    return 0


def _user_unlock(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import login_throttle

    with _session() as session:
        user = _resolve_user_scoped(session, args.user, args)
        uid = user.id
    result = login_throttle.unlock(uid, actor=_actor(args))
    if not result.get("ok"):
        raise CliError("Sign-in could not be unlocked. Try again.")
    emit.result(f"Sign-in unlocked for {result['email']}.", result)
    return 0


def _user_mfa_status(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import mfa

    with _session() as session:
        u = _resolve_user_scoped(session, args.user, args)
        session.expunge(u)
    st = mfa.status(u)
    if emit.as_json:
        emit.json({"ok": True, "id": u.id, "email": u.email, **st})
        return 0
    emit.say(f"User #{u.id}: {u.email}")
    if not st["applies"]:
        emit.say(
            f"  two-step sign-in: not applicable (signs in via {u.auth_source}; the identity "
            "provider's MFA applies)"
        )
        return 0
    emit.say(f"  two-step sign-in: {'ON' if st['enrolled'] else 'off'}")
    if st["enrolled"]:
        emit.say(f"  turned on: {st['enabled_at']}   last used: {st['last_used_at'] or 'never'}")
        emit.say(f"  recovery codes left: {st['recovery_codes_left']}")
    emit.say(
        f"  policy mfa_required={st['policy']}: "
        + ("REQUIRED for this account" if st["required"] else "not required for this account")
    )
    return 0


def _user_mfa_reset(args, emit) -> int:
    """Host-side break-glass (run in-process on the server, e.g. `docker exec ... app.cli`):
    the one way back in for a sole admin who lost the phone and every recovery code."""
    local_op(args, emit)
    from app.kernel import audit, mfa

    with _session() as session:
        # Scope-resolve BEFORE the prompt: a foreign-org target is a clean 'no such user'.
        u = _resolve_user_scoped(session, args.user, args)
        uid, email = u.id, u.email
    if not confirm(
        emit,
        f"Reset two-step sign-in for {email} (#{uid})? They are signed out everywhere.",
        args.yes,
    ):
        raise CliError("aborted.", 1)
    res = mfa.reset(uid)
    if not res["ok"]:
        raise CliError(f"no such user: {args.user}", EXIT_USAGE)
    audit.log(
        actor=_actor(args),
        action="mfa.reset",
        target=email,
        detail="was on" if res["was_enrolled"] else "was not set up",
    )
    emit.result(
        f"Two-step sign-in reset for {email}"
        + ("" if res["was_enrolled"] else " (it was not set up)")
        + ". They are signed out everywhere; if mfa_required applies to them they set it up "
        "again at their next sign-in.",
        {"ok": True, "email": email, "was_enrolled": res["was_enrolled"]},
    )
    return 0


def _user_invite(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import audit, security
    from app.models import Role

    role = args.role if args.role in Role.ALL else "translator"
    invite_org = getattr(args, "_op_org_id", None)
    # GATE R4 MED-1: the same hole as `user create`, one door over, and with a nastier
    # shape. `--operator` leaves the org None, create_invite accepts it, the CLI exits 0
    # and prints a working-looking link, the accept page renders with the invitee's own
    # address on it, and only the POST fails closed with "Invitation invalid... already
    # used, or expired". The refusal reaches the wrong person, too late, with the wrong
    # reason. Refuse at the point where the operator can still fix it. #181: the refusal
    # lives in security.create_invite now; this door keeps its operator wording.
    # BUG-A3-1: create_invite now format-validates the email — surface a malformed one
    # as a clean CliError, not a raw traceback (mirrors `user create`).
    try:
        inv = security.create_invite(args.email, role, _actor(args), org_id=invite_org)
    except security.OrglessAccountError as exc:
        raise CliError(
            "multi-org is enabled, so an invitation needs an organization: "
            "pass --org <id>. (--operator grants cross-org READ, but the account "
            "this invitation creates must belong somewhere.)",
            EXIT_USAGE,
        ) from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    audit.log(actor=_actor(args), action="user.invite", target=inv.email, detail="link-shown")
    emit.result(
        f"Invited {inv.email} as '{inv.role}'. Accept token: {inv.token}",
        {
            "ok": True,
            "id": inv.id,
            "email": inv.email,
            "role": inv.role,
            "token": inv.token,
            "accept_path": f"/auth/accept/{inv.token}",
        },
    )
    return 0


def _user_invites(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import security

    list_invites = security.expired_invites if args.expired else security.pending_invites
    rows = [
        security.invite_metadata(inv) for inv in list_invites(getattr(args, "_op_org_id", None))
    ]
    emit.result(
        "\n".join(f"{r['id']}: {r['email']} ({r['expires_at']})" for r in rows)
        or ("No expired invitations." if args.expired else "No pending invitations."),
        {"ok": True, "invites": rows},
    )
    return 0


def _user_invite_revoke(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import security

    org_id = getattr(args, "_op_org_id", None)
    # Pending OR expired (SPEC-expired-invite-delete): a soft delete, revoked_at only.
    expired_ids = {inv.id for inv in security.expired_invites(org_id)}
    if args.id not in {inv.id for inv in security.pending_invites(org_id)} | expired_ids:
        raise CliError("Invitation not found.", EXIT_USAGE)
    inv = security.revoke_invite(
        args.id, _actor(args), org_id=org_id, allow_expired=args.id in expired_ids
    )
    if inv is None:
        raise CliError("Invitation not found.", EXIT_USAGE)
    was_expired = inv.expires_at < inv.revoked_at
    emit.result(
        f"{'Deleted expired' if was_expired else 'Revoked'} invitation for {inv.email}.",
        {
            "ok": True,
            "email": inv.email,
            "revoked_at": inv.revoked_at.isoformat(),
            "expired": was_expired,
        },
    )
    return 0


def _user_export(args, emit) -> int:
    local_op(args, emit)
    import json

    from app.kernel import audit, gdpr

    with _session() as session:
        u = _resolve_user(session, args.user)
    uid, email = u.id, u.email
    # Multi-org P8: when the operator scoped the command to one org (--org N ->
    # args._op_org_id), gdpr export must not cross orgs — a user in another org is
    # refused (empty export). None (--operator / single-tenant) is unscoped.
    caller_org_id = getattr(args, "_op_org_id", None)
    if caller_org_id is not None and u.org_id != caller_org_id:
        raise CliError(f"no such user: {args.user}", EXIT_USAGE)
    lines = []
    for section, rec in gdpr.export_user(uid, caller_org_id=caller_org_id):
        lines.append(json.dumps({"section": section, **rec}, ensure_ascii=False, default=str))
    audit.log(actor=_actor(args), action="gdpr.export", target=email)
    body = "\n".join(lines) + ("\n" if lines else "")
    if args.out:
        if os.path.exists(args.out) and not args.force:
            raise CliError(
                f"destination exists: {args.out} (pass --force to overwrite).", EXIT_USAGE
            )
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        emit.result(
            f"Wrote GDPR export -> {args.out} ({len(lines)} record(s)).",
            {"ok": True, "email": email, "out": args.out, "records": len(lines)},
        )
    elif emit.as_json:
        emit.json(
            {
                "ok": True,
                "email": email,
                "records": len(lines),
                "sections": [json.loads(ln) for ln in lines],
            }
        )
    else:
        sys.stdout.write(body)
    return 0


def _user_erase(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import gdpr

    with _session() as session:
        u = _resolve_user(session, args.user)
    uid, email = u.id, u.email
    # Multi-org P8: honor the --org scope (args._op_org_id) — a scoped operator can
    # only erase a user in that org. The pre-check runs BEFORE confirm() and yields
    # the SAME "no such user" exit 2 as a truly-missing id, so a scoped operator can
    # neither learn a foreign uid exists nor see its email in the prompt (existence
    # oracle — mirrors the export twin). None (--operator / single-tenant) is unscoped.
    caller_org_id = getattr(args, "_op_org_id", None)
    if caller_org_id is not None and u.org_id != caller_org_id:
        raise CliError(f"no such user: {args.user}", EXIT_USAGE)
    if not confirm(
        emit, f"GDPR-erase (anonymize) {email} (#{uid})? This is irreversible.", args.yes
    ):
        raise CliError("aborted.", 1)
    ok, msg = gdpr.erase_user(uid, _actor(args), caller_org_id=caller_org_id)
    if not ok:
        raise CliError(msg)
    emit.result(f"Erased {email} (#{uid}). {msg}", {"ok": True, "uid": uid, "message": msg})
    return 0


# --- apikey ------------------------------------------------------------------
def _apikey_issue(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import apikeys, audit

    with _session() as session:
        # #61 HEADLINE: minting an lsu_ key for a FOREIGN-org user (e.g. that org's
        # admin) was the sharpest bypass — scope-resolve so a cross-org owner is a
        # clean 'no such user' and NO key is ever minted for another org.
        u = _resolve_user_scoped(session, args.user, args)
    # Optional lifetime (#270 r2-apiauth-10): the org-level maximum (setting
    # apikey_max_age_days) is enforced at the shared mint seam; its refusal is a
    # clean usage error, never a traceback.
    try:
        expires_at = apikeys.expiry_from_days(args.expires_in_days)
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    try:
        key, full_key = apikeys.create_key(
            u.id, name=args.name, scopes=args.scopes, expires_at=expires_at
        )
    except apikeys.InvalidScopeError as exc:
        # Refuse an unknown scope at mint (Q1): --scopes all used to mint a key that
        # worked nowhere. Name the vocabulary; EXIT_USAGE, no traceback.
        raise CliError(str(exc), EXIT_USAGE) from exc
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    audit.log(
        actor=_actor(args), action="apikey.create", target=u.email, detail=f"scopes={key.scopes}"
    )
    # The plaintext key is shown ONCE and is unrecoverable. It is NEVER logged.
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "id": key.id,
                "user": u.email,
                "prefix": key.prefix,
                "scopes": key.scopes,
                "api_key": full_key,
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            }
        )
    else:
        expiry = (
            f", expires {key.expires_at.strftime('%Y-%m-%d %H:%M UTC')}" if key.expires_at else ""
        )
        emit.say(f"Issued API key #{key.id} for {u.email} (scopes: {key.scopes}{expiry}).")
        emit.say("")
        emit.say("  " + full_key)
        emit.say("")
        emit.say("STORE THIS NOW - it is shown only once and cannot be recovered.")
    return 0


def _apikey_list(args, emit) -> int:
    # System-wide enumeration of issued keys (metadata + last_used) — NEVER the key
    # material/hash. Remote arm hits GET /api/apikeys (org-admin RBAC); the in-process
    # arm is the trusted local operator (org-confined only under the multi-org gate).
    if remote_mode(args):
        return _remote_impl.apikey_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import apikeys

    org_id = op_org_id(args)  # None (single-tenant / --operator) = every org's keys
    with _session() as session:
        uid = None
        if args.user:
            u = _resolve_user(session, args.user)
            # Under --org, refuse a cross-org owner as not-found (no existence oracle).
            if org_id is not None and getattr(u, "org_id", None) != org_id:
                raise CliError(f"no such user: {args.user}", EXIT_USAGE)
            uid = u.id
        payload = apikeys.list_keys_scoped(session, org_id=org_id, user_id=uid)
    if emit.as_json:
        emit.json({"ok": True, "keys": payload})
    else:
        emit.table(
            [
                (
                    k["id"],
                    k["user"],
                    k["name"],
                    k["prefix"],
                    k["scopes"],
                    "yes" if k["active"] else "no",
                    k["last_used"] or "-",
                    _apikey_expiry_cell(k),
                )
                for k in payload
            ],
            headers=("id", "user", "name", "prefix", "scopes", "active", "last_used", "expires"),
        )
    return 0


def _apikey_revoke(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import apikeys, audit

    # #61: revoke-by-id was ungated — a scoped caller could kill ANY org's key. Scope
    # by the key OWNER's org (ApiKey has no org_id) BEFORE revoking; foreign == 404.
    with _session() as session:
        _scoped_key_or_404(session, args.key_id, args)
    ok = apikeys.revoke(args.key_id)
    if not ok:
        raise CliError(f"no such API key: {args.key_id}", EXIT_USAGE)
    audit.log(actor=_actor(args), action="apikey.revoke", target=str(args.key_id))
    emit.result(
        f"Revoked API key #{args.key_id}.", {"ok": True, "id": args.key_id, "revoked": True}
    )
    return 0


def _apikey_revoke_all(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import apikeys, audit

    with _session() as session:
        # Scope-resolve BEFORE the confirm() prompt (no email oracle, no cross-org
        # mass-revoke) — foreign target is a clean 'no such user'.
        u = _resolve_user_scoped(session, args.user, args)
    if not confirm(emit, f"Revoke EVERY API key for {u.email} (#{u.id})?", args.yes):
        raise CliError("aborted.", 1)
    n = apikeys.revoke_all_for_user(u.id, org_id=op_org_scope(args))
    audit.log(
        actor=_actor(args), action="apikey.revoke_all", target=u.email, detail=f"{n} key(s) revoked"
    )
    emit.result(
        f"Revoked {n} API key(s) for {u.email}.", {"ok": True, "user": u.email, "revoked": n}
    )
    return 0


# --- member ------------------------------------------------------------------
def _member_add(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import access, audit

    with _session() as session:
        # #61: scope BOTH the member (user) AND the project to the operator's org —
        # a scoped caller may neither add a foreign user nor touch a foreign project.
        u = _resolve_user_scoped(session, args.user, args)
        _get_project(session, args.project_id, op_org_id(args))
        ok, msg = access.add_member(session, args.project_id, u.email, added_by=_actor(args))
        if not ok:
            raise CliError(msg)
        audit.log(
            actor=_actor(args),
            action="member.add",
            target=u.email,
            detail=f"project {args.project_id}",
        )
        emit.result(
            f"project {args.project_id}: {msg}",
            {"ok": True, "project_id": args.project_id, "user": u.email, "message": msg},
        )
    return 0


def _member_remove(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import access, audit

    with _session() as session:
        # Scope-resolve user + project BEFORE the confirm() prompt (no oracle).
        u = _resolve_user_scoped(session, args.user, args)
        _get_project(session, args.project_id, op_org_id(args))
        if not confirm(emit, f"Remove {u.email} from project {args.project_id}?", args.yes):
            raise CliError("aborted.", 1)
        removed = access.remove_member(session, args.project_id, u.id)
        if not removed:
            raise CliError(f"{u.email} was not a member of project {args.project_id}.")
        audit.log(
            actor=_actor(args),
            action="member.remove",
            target=u.email,
            detail=f"project {args.project_id}",
        )
        emit.result(
            f"project {args.project_id}: removed {u.email}.",
            {"ok": True, "project_id": args.project_id, "user": u.email, "removed": True},
        )
    return 0


def _member_list(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import access

    with _session() as session:
        # #61: a scoped caller may not enumerate a foreign project's members
        # (their emails/roles) — a foreign project id is a clean 'no such project'.
        _get_project(session, args.project_id, op_org_id(args))
        pairs = access.list_members(session, args.project_id)
        rows, payload = [], []
        for member, user in pairs:
            rows.append((user.id, user.email, user.role, member.added_by or ""))
            payload.append(
                {
                    "user_id": user.id,
                    "email": user.email,
                    "role": user.role,
                    "added_by": member.added_by or "",
                }
            )
        if emit.as_json:
            emit.json({"ok": True, "project_id": args.project_id, "members": payload})
        else:
            emit.table(rows, headers=("user_id", "email", "role", "added_by"))
    return 0


def _get_project(session, project_id: int, org_id=None):
    """Load a project, enforcing the in-process --org scope (a foreign-org OR missing
    project is an indistinguishable 'no such project', exit 2). org_id None (single-
    tenant / --operator / gate off) is unscoped — byte-identical to before."""
    from app.models import Project

    return get_scoped_or_404(session.get(Project, project_id), project_id, "project", org_id)
