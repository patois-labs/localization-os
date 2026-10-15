"""`mcp-oauth` command group (#753): the CLI twin of the /security/mcp-oauth page and the
/account/connected-apps list for the opt-in MCP OAuth authorization server.

  mcp-oauth status               switch state, what is missing, issuer, resource, DCR, trust list
  mcp-oauth rs-secret            mint a new resource-server secret (printed ONCE; only its hash
                                 is stored) for the MCP server's LOC_STUDIO_MCP_RS_SECRET
  mcp-oauth grants [--user U]    list consented grants (metadata only, never a token)
                   [--all]       include revoked grants
  mcp-oauth revoke <grant-id>    revoke one grant (its tokens stop working at once)

The switches themselves are ordinary settings: `config set mcp_oauth_enabled 1`,
`mcp_oauth_resource`, `mcp_oauth_dcr_enabled`, `mcp_oauth_cimd_hosts` (all confirm to set and
host-only over /api). Every verb here is in-process only (the host operator surface): over
--remote a person lists or revokes their OWN grants through GET /api/mcp-oauth/grants and
POST /api/mcp-oauth/grants/{id}/revoke.

Documented in: docs/architecture/mcp-oauth.md
"""

from ._common import CliError, require_local


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "mcp-oauth",
        help="MCP OAuth authorization server (off by default): status, resource-server "
        "secret, consented grants.",
    )
    sub = grp.add_subparsers(dest="mcp_oauth_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("status", help="Show whether the MCP OAuth server is on and ready.")
    p.set_defaults(func=_status)

    p = sub.add_parser(
        "rs-secret",
        help="Mint a new resource-server secret for the MCP server (shown once).",
    )
    p.add_argument("--yes", action="store_true", help="Replace an existing secret without asking.")
    p.set_defaults(func=_rs_secret)

    p = sub.add_parser("grants", help="List consented MCP OAuth grants.")
    p.add_argument("--user", default="", help="Only this account (email or id).")
    p.add_argument("--all", action="store_true", help="Include revoked grants.")
    p.set_defaults(func=_grants)

    p = sub.add_parser("revoke", help="Revoke one MCP OAuth grant by id.")
    p.add_argument("grant_id", type=int, help="Grant id (from `mcp-oauth grants`).")
    p.set_defaults(func=_revoke)


def _status(args, emit) -> int:
    require_local(args)
    from app.kernel import mcp_as

    st = mcp_as.status()
    state = "on" if st["active"] else ("switched on, NOT ready" if st["enabled"] else "off")
    lines = [
        f"MCP OAuth authorization server: {state}",
        f"  issuer (app_base_url): {st['issuer'] or '(not set)'}",
        f"  resource (mcp_oauth_resource): {st['resource'] or '(not set)'}",
        f"  dynamic client registration: {'on' if st['dcr_enabled'] else 'off'}",
        "  client metadata hosts: " + (", ".join(st["cimd_hosts"]) or "any public host"),
        f"  resource-server secret: {'set' if st['rs_secret_set'] else 'not set'}",
    ]
    if st["enabled"] and st["problems"]:
        lines.append("  not ready because:")
        lines.extend(f"    - {p}" for p in st["problems"])
    emit.result("\n".join(lines), {"ok": True, **st})
    return 0


def _rs_secret(args, emit) -> int:
    require_local(args)
    from app.kernel import audit, mcp_as

    if mcp_as.status()["rs_secret_set"] and not args.yes:
        raise CliError(
            "A resource-server secret is already set; the MCP server stops authenticating "
            "until it gets the new one. Re-run with --yes to replace it.",
            code=2,
        )
    secret = mcp_as.generate_rs_secret(actor="cli")
    audit.log(actor="cli", action="mcp_oauth.rs_secret_rotated")
    emit.result(
        "New resource-server secret (shown once; give it to the MCP server as "
        "LOC_STUDIO_MCP_RS_SECRET):\n" + secret,
        {"ok": True, "rs_secret": secret},
    )
    return 0


def _resolve_user_id(ref: str):
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import User

    ref = (ref or "").strip()
    if not ref:
        return None
    with Session(engine) as s:
        if ref.isascii() and ref.isdigit():
            u = s.get(User, int(ref))
        else:
            u = s.exec(select(User).where(User.email == ref.lower())).first()
    if u is None:
        raise CliError(f"No such user: {ref}", code=1)
    return u.id


def _grants(args, emit) -> int:
    require_local(args)
    from app.kernel import mcp_as

    rows = mcp_as.list_grants(_resolve_user_id(args.user), include_revoked=args.all)
    if emit.as_json:
        emit.json({"ok": True, "grants": rows})
        return 0
    if not rows:
        emit.say("No MCP OAuth grants.")
        return 0
    emit.table(
        [
            (
                str(r["id"]),
                str(r["user_id"]),
                r["client_name"],
                r["redirect_host"],
                r["scopes"],
                r["last_used"] or "-",
                "revoked" if r["revoked"] else "active",
            )
            for r in rows
        ],
        headers=("id", "user", "app", "answers go to", "scopes", "last used", "state"),
    )
    return 0


def _revoke(args, emit) -> int:
    require_local(args)
    from app.kernel import audit, mcp_as

    if not mcp_as.revoke_grant(args.grant_id):
        raise CliError(f"No such grant: {args.grant_id}", code=1)
    audit.log(actor="cli", action="mcp_oauth.revoked", target=f"grant #{args.grant_id}")
    emit.result(f"Revoked grant {args.grant_id}.", {"ok": True, "revoked": args.grant_id})
    return 0
