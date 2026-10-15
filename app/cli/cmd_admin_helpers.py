"""cmd_admin: helpers

Documented in: docs/architecture/connectors.md
"""

import sys

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _session,
    confirm,
    local_op,
    redact,
    remote_mode,
    require_operator,
    require_org_scope,
)


def _register_tick(sub) -> None:
    p = sub.add_parser(
        "tick",
        help="Run one scheduler pass NOW (piggybacked auto-close / vendor-expiry "
        "/ tap sweeps, then due connector schedules) - the external-cron "
        "entry for embedded/headless installs. --remote-capable.",
    )
    p.set_defaults(func=_tick)


def _register_oidc(sub) -> None:
    grp = sub.add_parser("oidc", help="OIDC / SSO identity (config/status).")
    s = grp.add_subparsers(dest="oidc_cmd", metavar="<subcommand>")
    s.required = True
    p = s.add_parser("config", help="Set OIDC settings.")
    p.add_argument("--enabled", choices=["on", "off"], default=None)
    p.add_argument("--issuer", default=None, help="OIDC issuer URL.")
    p.add_argument("--client-id", default=None)
    p.add_argument(
        "--client-secret", default=None, help="OIDC client secret (masked; never echoed)."
    )
    p.add_argument("--default-role", default=None, help="role granted to first-time OIDC sign-ins.")
    p.add_argument(
        "--trust-unverified-email",
        choices=["on", "off"],
        default=None,
        help="link first OIDC sign-ins by email even without email_verified "
        "(only for an IdP with admin-assigned addresses; default off).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (these settings are security-sensitive; with "
        "no terminal nothing is written without it).",
    )
    p.set_defaults(func=_oidc_config)
    p = s.add_parser("status", help="Show OIDC settings (secrets masked).")
    p.set_defaults(func=_oidc_status)


def _register_gdpr(sub) -> None:
    grp = sub.add_parser("gdpr", help="GDPR subject rights (export/erase).")
    s = grp.add_subparsers(dest="gdpr_cmd", metavar="<subcommand>")
    s.required = True
    p = s.add_parser("export", help="Subject-access export (NDJSON to stdout or -o FILE).")
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("-o", "--out", default="", help="write to FILE instead of stdout.")
    p.add_argument("--force", action="store_true", help="overwrite -o FILE if it exists.")
    p.set_defaults(func=_gdpr_export)
    p = s.add_parser("erase", help="Anonymize a user (DESTRUCTIVE, irreversible).")
    p.add_argument("user", help="email or numeric id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_gdpr_erase)


# --- helpers -----------------------------------------------------------------
def _confirm_settings_write(updates: dict, args, emit, secret_keys=()) -> None:
    """The gate `config set` / `config apply` put in front of every settings write, for the
    admin verbs that write the same keys (#529): validate through the shared registry first
    (a bad value is a usage error, never a question), then confirm any security-sensitive key
    (retention_enabled/days, ldap_enabled, sso_enabled, ...) unless --yes. Refuses BEFORE
    anything is written."""
    from app.kernel import settings

    try:
        settings.validate_values(updates)
    except settings.InvalidSettingValue as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    sensitive = sorted(k for k in updates if k in settings.SENSITIVE_KEYS)
    shown = [f"{k}={updates[k]!r}" for k in sensitive]
    # A secret being replaced is confirmed too (named, never echoed).
    shown += [f"{k}=(secret)" for k in secret_keys if k in settings.SENSITIVE_KEYS]
    if shown and not confirm(
        emit,
        "Change security-sensitive setting(s) " + ", ".join(shown) + "?",
        getattr(args, "yes", False),
    ):
        raise CliError("aborted.", 1)


def _set_settings(items: dict, args, action: str, detail: str) -> None:
    """Write plaintext settings + audit the change (secrets go via set_secret at
    the call site, not here)."""
    from app.kernel import audit, settings

    settings.set_many(items)
    audit.log(actor=_actor(args), action=action, detail=detail)


def _srx_status(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import settings

    enabled = settings.get("srx_enabled") == "1"
    srx_file = settings.get("srx_file") or ""
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "srx_enabled": "1" if enabled else "0",
                "srx_file": srx_file or "(default)",
            }
        )
    else:
        emit.say(f"SRX segmentation: {'on' if enabled else 'off'}")
        emit.say(f"  ruleset: {srx_file or '(shipped default.srx)'}")
    return 0


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _retention_status(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import retention

    enabled = retention.enabled()
    days = retention.retention_days()
    if emit.as_json:
        emit.json(
            {"ok": True, "enabled": enabled, "days": days, "would_sweep": enabled and days > 0}
        )
    else:
        emit.say(f"Retention: {'ON' if enabled else 'off'}, window {days} day(s).")
        if enabled and days <= 0:
            emit.say("  (enabled but days=0 -> no window -> nothing is ever deleted)")
    return 0


# --- admin tick --------------------------------------------------------------
def _tick(args, emit) -> int:
    """Run one scheduler pass NOW (mirrors POST /api/admin/tick). REMOTE-capable: with
    --remote it hits the route; in-process it runs scheduler.tick_now() directly — the
    SAME call the daemon loop makes. NOT org-scoped (the scheduler is install-global
    infrastructure: the daemon runs unscoped over every connection/vendor), so it cannot be
    narrowed with --org; app/cli/_scope.py classifies it OPERATOR_ONLY, and under multi-org
    the central gate requires --operator for the in-process arm (#556). --remote is unchanged."""
    if remote_mode(args):
        return _remote_impl.admin_tick(args, emit)
    from app.kernel import scheduler

    try:
        ran, fired = scheduler.tick_now()
    except Exception as exc:
        raise CliError(f"scheduler tick failed: {exc}", 1) from exc
    if not ran:
        raise CliError("a scheduler tick is already running; try again shortly.", 1)
    emit.result(
        f"Scheduler tick ran; enqueued {len(fired)} connector-sync job(s).",
        {"ok": True, "ran": True, "jobs_enqueued": len(fired)},
    )
    return 0


_LDAP_KEYS = (
    "ldap_enabled",
    "ldap_host",
    "ldap_ca_cert",
    "ldap_base_dn",
    "ldap_bind_dn",
    "ldap_bind_password_enc",
    "ldap_user_filter",
    "ldap_default_role",
)


# --- admin ldap --------------------------------------------------------------
def _ldap_config(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    from app.kernel import audit, settings
    from app.models import Role

    updates = {}
    if args.enabled is not None:
        updates["ldap_enabled"] = "1" if args.enabled == "on" else "0"
    if args.host is not None:
        updates["ldap_host"] = args.host.strip()
    if args.ca_cert is not None:
        updates["ldap_ca_cert"] = args.ca_cert.strip()
    if args.base_dn is not None:
        updates["ldap_base_dn"] = args.base_dn.strip()
    if args.bind_dn is not None:
        updates["ldap_bind_dn"] = args.bind_dn.strip()
    if args.user_filter is not None:
        updates["ldap_user_filter"] = args.user_filter.strip()
    if args.default_role is not None:
        if args.default_role not in Role.ALL:
            raise CliError(
                f"unknown role: {args.default_role} (one of: {', '.join(Role.ALL)})", EXIT_USAGE
            )
        updates["ldap_default_role"] = args.default_role
    _confirm_settings_write(
        updates,
        args,
        emit,
        secret_keys=("ldap_bind_password_enc",) if args.bind_password is not None else (),
    )
    if updates:
        try:
            settings.set_many(updates)
        except (RuntimeError, ValueError) as exc:
            # e.g. arming ldap_enabled while the opt-in ldap3 extra is absent — set_many
            # refuses atomically. Surface the actionable message as a clean CLI error
            # instead of an uncaught traceback (LOW-1).
            raise CliError(str(exc), 1) from exc
    # The bind password is a true secret: stored Fernet-encrypted, never echoed. Written only
    # AFTER set_many accepted the rest, so a refused batch cannot leave the secret changed
    # with no audit row (#529).
    if args.bind_password is not None:
        settings.set_secret("ldap_bind_password_enc", args.bind_password)
    if updates or args.bind_password is not None:
        # Audit the change WITHOUT the secret value.
        keys = list(updates) + (
            ["ldap_bind_password_enc"] if args.bind_password is not None else []
        )
        audit.log(
            actor=_actor(args), action="config.set", detail="ldap: " + ", ".join(sorted(keys))
        )
    return _ldap_status(args, emit)


def _ldap_status(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import settings

    info = {}
    for key in _LDAP_KEYS:
        info[key] = redact(key, settings.get(key))
    if emit.as_json:
        emit.json({"ok": True, **info})
    else:
        emit.say(f"LDAP: {'on' if settings.get('ldap_enabled') == '1' else 'off'}")
        emit.say(f"  host:          {info['ldap_host']}")
        emit.say(f"  CA bundle:     {info['ldap_ca_cert'] or '(system roots)'}")
        emit.say(f"  base dn:       {info['ldap_base_dn']}")
        emit.say(f"  bind dn:       {info['ldap_bind_dn']}")
        emit.say(f"  bind password: {info['ldap_bind_password_enc']}")
        emit.say(f"  user filter:   {info['ldap_user_filter'] or '(default)'}")
        emit.say(f"  default role:  {info['ldap_default_role']}")
    return 0


def _prompt_password(supplied):
    """--password value if given, else a getpass prompt. NEVER echoed."""
    import getpass

    if supplied is not None:
        return supplied
    if not sys.stdin.isatty():
        raise CliError(
            "no password supplied and stdin is not a terminal - pass --password.", EXIT_USAGE
        )
    return getpass.getpass("Password: ")


_OIDC_KEYS = (
    "sso_enabled",
    "sso_issuer",
    "sso_client_id",
    "sso_client_secret",
    "sso_default_role",
    "sso_trust_unverified_email",
)


# --- admin oidc --------------------------------------------------------------
def _oidc_config(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    from app.kernel import audit, settings
    from app.models import Role

    updates = {}
    if args.enabled is not None:
        updates["sso_enabled"] = "1" if args.enabled == "on" else "0"
    if args.issuer is not None:
        updates["sso_issuer"] = args.issuer.strip()
    if args.client_id is not None:
        updates["sso_client_id"] = args.client_id.strip()
    secret_changed = args.client_secret is not None
    if args.default_role is not None:
        if args.default_role not in Role.ALL:
            raise CliError(
                f"unknown role: {args.default_role} (one of: {', '.join(Role.ALL)})", EXIT_USAGE
            )
        updates["sso_default_role"] = args.default_role
    if args.trust_unverified_email is not None:
        updates["sso_trust_unverified_email"] = "1" if args.trust_unverified_email == "on" else "0"
    _confirm_settings_write(
        updates, args, emit, secret_keys=("sso_client_secret",) if secret_changed else ()
    )
    if updates:
        try:
            settings.set_many(updates)
        except (RuntimeError, ValueError) as exc:
            raise CliError(str(exc), 1) from exc
    if secret_changed:
        # R3 hardening ride-along: the OIDC client secret is stored Fernet-ENCRYPTED at rest
        # (settings.set_secret), not as a plaintext Setting row. Masked on every read; never
        # echoed. Written after set_many accepted the rest (#529), like the LDAP bind password.
        settings.set_secret("sso_client_secret", args.client_secret)
    if updates or secret_changed:
        detail_keys = sorted(list(updates) + (["sso_client_secret"] if secret_changed else []))
        audit.log(actor=_actor(args), action="config.set", detail="oidc: " + ", ".join(detail_keys))
    return _oidc_status(args, emit)


def _oidc_status(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import settings

    info = {key: redact(key, settings.get(key)) for key in _OIDC_KEYS}
    if emit.as_json:
        emit.json({"ok": True, **info})
    else:
        emit.say(f"OIDC: {'on' if settings.get('sso_enabled') == '1' else 'off'}")
        emit.say(f"  issuer:        {info['sso_issuer']}")
        emit.say(f"  client id:     {info['sso_client_id']}")
        emit.say(f"  client secret: {info['sso_client_secret']}")
        emit.say(f"  default role:  {info['sso_default_role']}")
        trust = "on" if info["sso_trust_unverified_email"] == "1" else "off"
        emit.say(f"  trust unverified email: {trust}")
    return 0


def _parse_since(raw: str):
    from datetime import datetime

    raw = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as e:
        raise CliError(
            f"could not parse --since {raw!r} (use ISO-8601, e.g. "
            "2026-01-01 or 2026-01-01T00:00:00).",
            EXIT_USAGE,
        ) from e
    if parsed.tzinfo is not None:
        # #546 (CLI twin of GET /api/audit/export): audit.iter_export compares against the stored
        # naive-UTC ts and SQLite binds drop a tz offset, so convert an aware bound to naive UTC.
        from datetime import timezone

        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _audit_verify(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.audit_verify(args, emit)
    local_op(args, emit)
    # BUG-5: `audit verify` (top-level) and `admin audit verify` used to carry two
    # divergent implementations. Both now run the ONE shared core (cmd_db, where the
    # better-structured version lived) so their output + error handling converge. The
    # registrations stay separate (parity-pinned); only the gate differs (local_op vs
    # require_local), applied above before delegating.
    from app.cli.cmd_db import verify_audit_chain

    return verify_audit_chain(emit)


# --- admin gdpr --------------------------------------------------------------
def _gdpr_export(args, emit) -> int:
    # Same implementation as `user export` (#533): the two copies had drifted (only this one
    # serialized non-ISO values with default=str).
    from app.cli import cmd_user

    local_op(args, emit)  # the in-process operator gate (refuses --remote), then the shared body
    return cmd_user._user_export(args, emit)


def _gdpr_erase(args, emit) -> int:
    from app.cli import cmd_user

    local_op(args, emit)
    return cmd_user._user_erase(args, emit)


# --- admin routing rule ------------------------------------------------------
# The `admin routing rule` verbs are the operator-door twins of `routing rule`: they gate with
# local_op (no feature switch, no --remote except `show`) and then run the SAME implementation
# the `routing` group runs (#533: the two copies had drifted, `admin routing rule list --json`
# dropped four fields).
def _rule_add(args, emit) -> int:
    local_op(args, emit)
    from app.cli import cmd_routing

    cmd_routing.refuse_reserved_fields(args)
    return cmd_routing.rule_add_local(args, emit)


def _rule_list(args, emit) -> int:
    local_op(args, emit)
    from app.cli import cmd_routing

    return cmd_routing.rule_list_local(args, emit)


def _rule_show(args, emit) -> int:
    # The one routing verb with REMOTE parity (read-back-completeness): --remote hits
    # GET /api/routing/rules/{id}; the in-process arm reads the same rule_to_dict seam.
    if remote_mode(args):
        return _remote_impl.routing_rule_show(args, emit)
    require_org_scope(args, emit)
    from app.cli import cmd_routing

    return cmd_routing.rule_show_local(args, emit)


def _rule_toggle(args, emit) -> int:
    local_op(args, emit)
    from app.cli import cmd_routing

    return cmd_routing.rule_toggle_local(args, emit)


def _rule_delete(args, emit) -> int:
    local_op(args, emit)
    from app.cli import cmd_routing

    return cmd_routing.rule_delete_local(args, emit)


# --- admin budget ------------------------------------------------------------
def _budget_set(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import budgets

    dept = (args.department or "").strip()
    if not dept:
        raise CliError("department must not be blank.", EXIT_USAGE)
    with _session() as session:
        # task_cca6bb89: a huge/malicious --limit used to reach a raw sqlite3
        # OverflowError -> an unhandled crash instead of a clean CLI error.
        # upsert_budget's shared bound check (budgets.validate_limit) now catches
        # it first; turn that into the same CliError shape as every other bad-input
        # rejection in this file.
        try:
            # Multi-org (leak vector 12): stamp/scope to the operator's resolved org
            # (args._op_org_id via --org) so each org keeps its OWN row for a shared
            # department name. None (--operator / single-tenant, gate off) = unscoped,
            # byte-identical to today. Mirrors the web /routing/budgets twin.
            row, created = budgets.upsert_budget(
                session,
                dept,
                args.limit,
                actor=_actor(args),
                org_id=getattr(args, "_op_org_id", None),
            )
        except budgets.BudgetLimitError as e:
            raise CliError(str(e), EXIT_USAGE) from e
        emit.result(
            f"{'Created' if created else 'Updated'} budget #{row.id}: "
            f"{row.department} = {row.monthly_token_limit} tokens/month"
            + (" (unlimited)" if row.monthly_token_limit <= 0 else "")
            + ".",
            {
                "ok": True,
                "id": row.id,
                "department": row.department,
                "monthly_token_limit": row.monthly_token_limit,
                "created": created,
            },
        )
    return 0


def _budget_list(args, emit) -> int:
    local_op(args, emit)
    from sqlmodel import select

    from app.kernel import budgets
    from app.models import DepartmentBudget

    # Multi-org (leak vector 12): confine the listing to the operator's resolved org
    # (args._op_org_id via --org) so org A never sees org B's budget rows. None
    # (--operator / single-tenant, gate off) = unscoped, byte-identical to today.
    # KNOWN LIMITATION (reviewer note): LEGACY budget rows stamped org_id=None
    # (created pre-multi-org, or single-tenant rows carried into a gate flip) are
    # HIDDEN from every scoped list -- and a scoped `budget set` for the same
    # department creates a SECOND, org-stamped row alongside the invisible legacy
    # one. They remain reachable unscoped (--operator / gate off). The eventual fix
    # is a stamp-migration backfilling legacy rows to the Default org; until then
    # this mirrors budgets.get_budget's own None-row semantics.
    caller_org_id = getattr(args, "_op_org_id", None)
    with _session() as session:
        q = select(DepartmentBudget)
        if caller_org_id is not None:
            q = q.where(DepartmentBudget.org_id == caller_org_id)
        rows_db = session.exec(q.order_by(DepartmentBudget.department)).all()
        rows, payload = [], []
        for b in rows_db:
            spent = budgets.spend_this_month(session, b.department, org_id=b.org_id)
            limit_disp = "unlimited" if b.monthly_token_limit <= 0 else b.monthly_token_limit
            rows.append((b.id, b.department, limit_disp, spent))
            payload.append(
                {
                    "id": b.id,
                    "department": b.department,
                    "monthly_token_limit": b.monthly_token_limit,
                    "spent_this_month": spent,
                }
            )
        if emit.as_json:
            emit.json({"ok": True, "budgets": payload})
        else:
            emit.table(rows, headers=("id", "department", "limit", "spent (month)"))
    return 0


def _budget_delete(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import budgets
    from app.models import DepartmentBudget

    if not confirm(emit, f"Delete department budget #{args.budget_id}?", args.yes):
        raise CliError("aborted.", 1)
    # Multi-org (leak vector 12): a scoped operator (--org N -> args._op_org_id) can
    # only delete a budget row in that org; a cross-org / missing id is refused as
    # "no such budget" (indistinguishable). None (--operator / single-tenant, gate
    # off) = unscoped, byte-identical to today. Mirrors the web budget-delete twin.
    caller_org_id = getattr(args, "_op_org_id", None)
    with _session() as session:
        if caller_org_id is not None:
            row = session.get(DepartmentBudget, args.budget_id)
            if row is None or getattr(row, "org_id", None) != caller_org_id:
                raise CliError(f"no such budget: {args.budget_id}", EXIT_USAGE)
        if not budgets.delete_budget(session, args.budget_id, actor=_actor(args)):
            raise CliError(f"no such budget: {args.budget_id}", EXIT_USAGE)
        emit.result(
            f"Deleted department budget #{args.budget_id}.",
            {"ok": True, "id": args.budget_id, "deleted": True},
        )
    return 0


# --- admin budget project (CV-2) ---------------------------------------------
def _project_in_op_org(session, project_id, caller_org_id):
    """The Project for `project_id` within the operator's resolved org (a cross-org id
    reads as nonexistent -- the same org-ring the department verbs apply). None off/
    unscoped means any project."""
    from app.models import Project

    proj = session.get(Project, project_id)
    if proj is None:
        return None
    if caller_org_id is not None and getattr(proj, "org_id", None) != caller_org_id:
        return None
    return proj


def _project_budget_list(args, emit) -> int:
    local_op(args, emit)
    from sqlmodel import select

    from app.kernel import project_budgets
    from app.models import Project, ProjectBudget

    caller_org_id = getattr(args, "_op_org_id", None)
    with _session() as session:
        q = select(ProjectBudget)
        if caller_org_id is not None:
            q = q.where(ProjectBudget.org_id == caller_org_id)
        rows_db = session.exec(q.order_by(ProjectBudget.project_id)).all()
        rows, payload = [], []
        for b in rows_db:
            proj = session.get(Project, b.project_id)
            name = getattr(proj, "name", None) or f"project {b.project_id}"
            state = project_budgets.budget_state(session, b.project_id)
            limit_disp = "unlimited" if b.monthly_usd_limit <= 0 else f"${b.monthly_usd_limit:,.2f}"
            rows.append(
                (
                    b.id,
                    b.project_id,
                    name,
                    limit_disp,
                    f"${state['enforced_usd']:,.2f}",
                    f"${state['committed_usd']:,.2f}",
                )
            )
            payload.append(
                {
                    "id": b.id,
                    "project_id": b.project_id,
                    "project_name": name,
                    "monthly_usd_limit": b.monthly_usd_limit,
                    "enforced_usd": state["enforced_usd"],
                    "committed_usd": state["committed_usd"],
                    "remaining_usd": state["remaining_usd"],
                }
            )
        if emit.as_json:
            emit.json({"ok": True, "project_budgets": payload})
        else:
            emit.table(
                rows, headers=("id", "project", "name", "limit", "spent (month)", "committed")
            )
    return 0


def _project_budget_delete(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import project_budgets
    from app.models import ProjectBudget

    if not confirm(emit, f"Delete project budget #{args.budget_id}?", args.yes):
        raise CliError("aborted.", 1)
    caller_org_id = getattr(args, "_op_org_id", None)
    with _session() as session:
        if caller_org_id is not None:
            row = session.get(ProjectBudget, args.budget_id)
            if row is None or getattr(row, "org_id", None) != caller_org_id:
                raise CliError(f"no such budget: {args.budget_id}", EXIT_USAGE)
        if not project_budgets.delete_project_budget(session, args.budget_id, actor=_actor(args)):
            raise CliError(f"no such budget: {args.budget_id}", EXIT_USAGE)
        emit.result(
            f"Deleted project budget #{args.budget_id}.",
            {"ok": True, "id": args.budget_id, "deleted": True},
        )
    return 0


# --- admin lqa rulebook ------------------------------------------------------
def _lqa_rulebook_get(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    from app.kernel import lqa

    text = lqa.load_rulebook(args.source, args.target)
    if emit.as_json:
        emit.json({"ok": True, "source": args.source, "target": args.target, "rulebook": text})
    else:
        if text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
        else:
            emit.say(f"(no rulebook for {args.source}->{args.target})")
    return 0


def _lqa_rulebook_set(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    from app.kernel import audit, lqa

    try:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise CliError(f"could not read {args.file}: {exc}") from exc
    try:
        lqa.save_rulebook(args.source, args.target, text)
    except ValueError as exc:
        # A refused locale (#174) is operator error, so report it rather than traceback.
        raise CliError(str(exc)) from exc
    audit.log(
        actor=_actor(args),
        action="lqa.rulebook",
        target=f"{args.source}->{args.target}",
        detail=f"{len(text)} chars",
    )
    emit.result(
        f"Saved LQA rulebook for {args.source}->{args.target} ({len(text)} chars).",
        {"ok": True, "source": args.source, "target": args.target, "chars": len(text)},
    )
    return 0
