"""`init` - headless bootstrap of a fresh install.

Runs init_db(), then (if no user exists yet) seeds the first admin and turns on
the login wall via security.seed_admin() - the SAME code path the setup wizard
converges on. Optionally mints an API key ONCE and prints it with a store-it-now
banner. Idempotent: on an already-seeded install it no-ops and says so.

Default-off boot: nothing here runs at startup. `init --from-env` reads
LOC_TMS_ADMIN_EMAIL / LOC_TMS_ADMIN_PASSWORD (a container/CI can seed before
uvicorn); with those env vars UNSET the app's normal boot (wizard / dev-seed) is
completely unchanged.
"""

import os

from ._common import CliError, require_local


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "init", help="Initialize a fresh install: schema + first admin (idempotent)."
    )
    p.add_argument("--admin-email", help="admin email (or use --from-env).")
    p.add_argument("--admin-password", help="admin password (>= 12 chars).")
    p.add_argument("--admin-name", default="Administrator", help="admin display name.")
    p.add_argument(
        "--from-env",
        action="store_true",
        help="read LOC_TMS_ADMIN_EMAIL / LOC_TMS_ADMIN_PASSWORD from the "
        "environment instead of flags.",
    )
    p.add_argument(
        "--issue-key", action="store_true", help="also mint a one-time API key for the new admin."
    )
    p.add_argument(
        "--key-scopes",
        default="docs",
        help="scopes for --issue-key (default 'docs'; 'api' is implied).",
    )
    p.set_defaults(func=_init)


def _init(args, emit) -> int:
    require_local(args)
    from app.db import init_db
    from app.kernel import audit, security

    # 1) Schema always (create-if-missing + additive migrations). Safe to re-run.
    init_db()
    emit.detail("Schema ensured (init_db).")

    # 2) Resolve admin credentials (flags or env).
    if args.from_env:
        email = os.environ.get("LOC_TMS_ADMIN_EMAIL", "").strip()
        password = os.environ.get("LOC_TMS_ADMIN_PASSWORD", "")
    else:
        email = (args.admin_email or "").strip()
        password = args.admin_password or ""

    # 3) Already seeded? No-op cleanly (do NOT require credentials in that case,
    #    so `init` is a safe idempotent boot step).
    if security.user_count() > 0:
        emit.result(
            "Already initialized - an account exists; nothing to do.",
            {"ok": True, "action": "noop", "reason": "users_exist"},
        )
        return 0

    if not email or not password:
        source = (
            "LOC_TMS_ADMIN_EMAIL/LOC_TMS_ADMIN_PASSWORD"
            if args.from_env
            else "--admin-email/--admin-password"
        )
        raise CliError(f"no admin exists yet and no credentials given ({source}).", 2)

    # 4) Seed via the shared helper (enforces the 12-char policy, enables the wall).
    try:
        user, full_key, status = security.seed_admin(
            email,
            password,
            name=args.admin_name,
            issue_key=args.issue_key,
            key_scopes=args.key_scopes,
        )
    except ValueError as exc:
        raise CliError(str(exc), 1) from exc

    if status == "exists":  # race: someone seeded between the check and here
        emit.result(
            "Already initialized - nothing to do.",
            {"ok": True, "action": "noop", "reason": "users_exist"},
        )
        return 0

    actor = getattr(args, "actor", None) or "cli"
    audit.log(actor=actor, action="init.create_admin", target=user.email)

    payload = {"ok": True, "action": "created", "admin_email": user.email, "login_wall": True}
    emit.say(f"Initialized. Admin created: {user.email} (login wall ON).")
    if full_key:
        payload["api_key"] = full_key
        payload["api_key_scopes"] = args.key_scopes
        if not emit.as_json:
            emit.say("")
            emit.say("=" * 60)
            emit.say("  API KEY (shown ONCE - store it now, it cannot be recovered):")
            emit.say(f"    {full_key}")
            emit.say(f"  scopes: {args.key_scopes} (+ api)")
            emit.say("=" * 60)
    if emit.as_json:
        emit.json(payload)
    return 0
