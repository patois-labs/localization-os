"""`account` command group — self-service account view + onboarding-flag parity
(SPEC-onboarding-tour Phase 3).

Web + /api + CLI parity for the self-service account surface: the web /account
page (Get started card, POST /account/get-started), GET /api/account and
POST /api/account/onboarding-reset, and these commands.

  account show [--user E]                show an account (locale / appearance /
                                          name / role / onboarding flag)
  account set-theme <light|dark|system> [--user E]
                                         set the appearance (Light / Dark /
                                          Match my computer), stored per account
  account reset-onboarding [--user E]    clear the onboarding dismissal (turn the
                                          "Get started" checklist back on)

The onboarding tour itself (SPEC-onboarding-tour) is web-only UX — there is no
CLI equivalent of the checklist panel. What IS surfaced here, headlessly, is
STATE parity for the `onboarding_dismissed_at` flag: `show` reports it (plus
eligibility/variant) and `reset-onboarding` clears it, mirroring the web
`?tour=1` re-summon's escape hatch for a support path with no browser.

REMOTE (--remote URL --token lsu_): the per-user token IS the account, so the
command acts on the CALLER'S OWN account (true self-service, mirroring the web
page). A non-empty --user is REFUSED (exit 2) rather than silently acting on the
caller — the remote surface can never target someone else. LOCAL (in-process,
trusted root): there is no session, so --user (email or numeric id) names the
target account and is REQUIRED.
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _resolve_user,
    _session,
    local_op,
    op_org_id,
    remote_mode,
)


def _resolve_user_scoped(session, ref, args):
    """#515 (the #61 guard cmd_user applies): resolve the user, then enforce the
    in-process --org scope. A target in ANOTHER org is indistinguishable from a missing
    one (exit 2, same message). None scope (--operator, single-tenant, gate off) is
    unscoped."""
    u = _resolve_user(session, ref)
    scope = op_org_id(args)
    if scope is not None and getattr(u, "org_id", None) != scope:
        raise CliError(f"no such user: {ref}", EXIT_USAGE)
    return u


_THEME_CHOICES = ("light", "dark", "system")


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "account", help="Self-service account view and the Get started checklist."
    )
    sub = grp.add_subparsers(dest="account_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("show", help="Show an account's profile + onboarding flag.")
    p.add_argument(
        "--user",
        default="",
        help="target account (email or id); in-process only - refused "
        "over --remote (the token identifies the account).",
    )
    p.set_defaults(func=_account_show)

    # ADR 0063: hard-coded at the parser door so `register` never imports the service;
    # security.UI_THEMES validates again server- and service-side.
    p = sub.add_parser(
        "set-theme", help="Set the appearance: light, dark or system (Match my computer)."
    )
    p.add_argument("theme", choices=_THEME_CHOICES, help="one of: " + ", ".join(_THEME_CHOICES))
    p.add_argument(
        "--user",
        default="",
        help="target account (email or id); in-process only - refused "
        "over --remote (the token identifies the account).",
    )
    p.set_defaults(func=_account_set_theme)

    p = sub.add_parser(
        "reset-onboarding",
        help="Clear the onboarding checklist dismissal (re-arm the first-run tour).",
    )
    p.add_argument(
        "--user",
        default="",
        help="target account (email or id); in-process only - refused "
        "over --remote (the token identifies the account).",
    )
    p.set_defaults(func=_account_reset_onboarding)


def _refuse_remote_user(args):
    """Remote mode is strictly self-service: the lsu_ token IS the account. A
    non-empty --user would otherwise silently act on the CALLER (the server has no
    'act as someone else' surface) — refuse it loudly instead (exit 2)."""
    if (getattr(args, "user", "") or "").strip():
        raise CliError(
            "--user is in-process only; the remote token identifies the account.", EXIT_USAGE
        )


def _require_user_ref(args):
    ref = (getattr(args, "user", "") or "").strip()
    if not ref:
        raise CliError(
            "in-process 'account' needs --user <email-or-id> (there is no session "
            "locally). Over --remote the token identifies the account.",
            EXIT_USAGE,
        )
    return ref


def _account_show(args, emit) -> int:
    if remote_mode(args):
        _refuse_remote_user(args)
        return _remote_impl.account_show(args, emit)
    local_op(args, emit)
    from app.kernel import onboarding, security

    ref = _require_user_ref(args)
    with _session() as s:
        u = _resolve_user_scoped(s, ref, args)
        data = {
            "email": u.email,
            "name": u.name,
            "role": u.role,
            "ui_locale": u.ui_locale or "",
            "ui_theme": security.normalize_theme(getattr(u, "ui_theme", "") or ""),
            "onboarding": {
                "dismissed_at": u.onboarding_dismissed_at.isoformat()
                if u.onboarding_dismissed_at
                else None,
                "eligible": onboarding.is_eligible(u),
                "variant": onboarding.variant(u),
            },
        }
    if emit.as_json:
        emit.json({"ok": True, **data})
    else:
        emit.say(f"Account: {data['email']}  (role={data['role']})")
        emit.say(f"  name: {data['name'] or '(none)'}")
        emit.say(f"  ui locale: {data['ui_locale'] or '(default)'}")
        emit.say(f"  appearance: {data['ui_theme']}")
        ob = data["onboarding"]
        variant_note = f", variant={ob['variant']}" if ob["variant"] else ""
        emit.say(
            f"  onboarding dismissed_at: {ob['dismissed_at'] or '(none)'}"
            f"  (eligible={'yes' if ob['eligible'] else 'no'}{variant_note})"
        )
    return 0


def _account_reset_onboarding(args, emit) -> int:
    if remote_mode(args):
        _refuse_remote_user(args)
        return _remote_impl.account_reset_onboarding(args, emit)
    local_op(args, emit)
    from app.kernel import audit, onboarding

    ref = _require_user_ref(args)
    with _session() as s:
        u = _resolve_user_scoped(s, ref, args)
        uid, email = u.id, u.email
        # variant/eligible are role-derived (unaffected by the reset itself) -
        # read them before the write so the response mirrors the /api shape.
        variant = onboarding.variant(u)
        eligible = onboarding.is_eligible(u)
    onboarding.reset(uid)
    audit.log(actor=_actor(args), action="account.reset_onboarding", target=email, detail="")
    emit.result(
        f"Reset onboarding for {email} (tour re-armed).",
        {
            "ok": True,
            "email": email,
            "onboarding": {"dismissed_at": None, "eligible": eligible, "variant": variant},
        },
    )
    return 0


def _account_set_theme(args, emit) -> int:
    if remote_mode(args):
        _refuse_remote_user(args)
        return _remote_impl.account_set_theme(args, emit)
    local_op(args, emit)
    from app.kernel import audit, security

    theme = (args.theme or "").strip().lower()
    if theme not in security.UI_THEMES:  # choices= already gates this; belt-and-suspenders
        raise CliError(
            "unknown theme: {!r} (valid: {})".format(theme, ", ".join(security.UI_THEMES)),
            EXIT_USAGE,
        )
    ref = _require_user_ref(args)
    with _session() as s:
        u = _resolve_user_scoped(s, ref, args)
        uid, email = u.id, u.email
    choice = security.update_theme(uid, theme)
    audit.log(actor=_actor(args), action="account.update_theme", target=email, detail=choice)
    emit.result(
        f"Set {email} appearance -> {choice}.", {"ok": True, "email": email, "ui_theme": choice}
    )
    return 0
