"""`mcp` command group - connection help for the MCP fourth surface.

Web + CLI parity for the wave-4 "Connect an AI agent" surface: the /help page's
remote-connection section and this verb print the SAME snippet, so an operator with
no browser gets the same config a browser user would copy.

  mcp connect --base-url URL [--key K] [--path /mcp] [--toolsets NAMES]

It is a PURE FORMATTER - no DB, no /api call, no secret handling. It renders the
`claude mcp add --transport http` command and the generic-client header for a given
base URL. There are deliberately NO new /api routes behind it: everything it needs
(the URL shape, the header name, the scope guidance) is static text mirrored from
`integrations/mcp-server/README.md`. The key is a PLACEHOLDER by default (`lsu_...`)
so the printed line is copy-paste-ready without ever asking for a real credential;
pass `--key` only if you want your own value spliced in for convenience.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import hashlib
import json
import os
import re

from app import branding

from ._common import EXIT_USAGE, CliError, require_local

_DEFAULT_PATH = "/mcp"
_PLACEHOLDER_KEY = "lsu_paste-your-scoped-key-here"


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "mcp", help="MCP fourth-surface connection help (remote/streamable-http)."
    )
    sub = grp.add_subparsers(dest="mcp_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "connect",
        help="Print the remote MCP connection snippet for a base URL (the same "
        "one the /help 'Connect an AI agent' section shows).",
    )
    p.add_argument(
        "--base-url",
        required=True,
        metavar="URL",
        help="base URL the listener is reachable at, e.g. "
        "https://your-host (the /mcp path is appended).",
    )
    p.add_argument(
        "--path",
        default=_DEFAULT_PATH,
        help=f"the streamable-http mount path (default {_DEFAULT_PATH}).",
    )
    p.add_argument(
        "--key",
        default=_PLACEHOLDER_KEY,
        metavar="LSU_KEY",
        help="an lsu_ key to splice into the snippet; defaults to a "
        "copy-paste placeholder (no real credential is required "
        "or handled).",
    )
    p.add_argument(
        "--toolsets",
        default="",
        metavar="NAMES",
        help="comma list of toolsets to load on this connection (#751), e.g. "
        "orientation,connectors; appended to the endpoint as ?toolsets=. Names: the "
        "tool families plus connectors and admin. Default: every tool the key reaches.",
    )
    p.set_defaults(func=_mcp_connect)

    q = sub.add_parser(
        "client-limit",
        help="Print the LOC_STUDIO_MCP_CLIENT_LIMITS entry that gives one API key its own "
        "session-admission budget (issue #410).",
    )
    who = q.add_mutually_exclusive_group(required=True)
    who.add_argument(
        "--key-env",
        metavar="VAR",
        help="name of an environment variable holding the client's exact API key; it is "
        "hashed locally and never printed (keeps the key out of shell history).",
    )
    who.add_argument(
        "--fingerprint",
        metavar="SHA256",
        help="the client key's lowercase SHA-256 hex digest, if you already have it.",
    )
    q.add_argument(
        "--burst", type=int, required=True, help="new sessions allowed at once (1-10000)."
    )
    q.add_argument(
        "--refill-per-minute",
        type=int,
        required=True,
        help="new sessions restored per minute (1-10000).",
    )
    q.set_defaults(func=_mcp_client_limit)


def _mcp_connect(args, emit) -> int:
    # A pure local formatter: it prints a snippet and touches nothing, so --remote
    # is meaningless here (the target listener is named by --base-url, not routed
    # through the /api client). Refuse --remote LOUDLY (exit 2) rather than silently
    # ignore it - the same guard every in-process-only command uses.
    require_local(args)
    base = (args.base_url or "").strip().rstrip("/")
    if not base:
        raise CliError("--base-url must not be empty.", EXIT_USAGE)
    path = (args.path or _DEFAULT_PATH).strip() or _DEFAULT_PATH
    if not path.startswith("/"):
        path = "/" + path
    key = (args.key or _PLACEHOLDER_KEY).strip() or _PLACEHOLDER_KEY
    endpoint = base + path
    toolsets = _toolsets_arg(getattr(args, "toolsets", "") or "")
    if toolsets:
        endpoint += "?toolsets=" + ",".join(toolsets)
    header = f"Authorization: Bearer {key}"
    # A query (?toolsets=) is a glob character to zsh and similar shells: quote it then.
    shell_endpoint = f'"{endpoint}"' if "?" in endpoint else endpoint
    claude_add = (
        f'claude mcp add --transport http loc-studio {shell_endpoint} \\\n  --header "{header}"'
    )

    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "endpoint": endpoint,
                "header_name": "Authorization",
                "header_value": header,
                "x_loc_token_alias": True,
                "toolsets": toolsets or "all",
                "claude_code": claude_add,
            }
        )
        return 0

    emit.say(f"Connect an AI agent to {branding.brand_name()} over streamable-HTTP.")
    emit.say("Each request carries its own lsu_ key in the header below; scope the")
    emit.say("key minimally at /account (docs-only for translate/workflow, keys for")
    emit.say("the Delta loop). Run the listener behind TLS you control. The tool")
    emit.say("list shows only the tools the key's scopes reach.")
    if toolsets:
        emit.say(f"Toolsets on this connection: {', '.join(toolsets)}.")
    emit.say("")
    emit.say("Endpoint:")
    emit.say(f"  {endpoint}")
    emit.say("")
    emit.say("Claude Code:")
    emit.say("  {}".format(claude_add.replace("\n", "\n  ")))
    emit.say("")
    emit.say("Generic MCP client (send this header, or the 'X-Loc-Token' alias):")
    emit.say(f"  POST {endpoint}")
    emit.say(f"  {header}")
    return 0


#: The MCP registry's toolset names (integrations/mcp-server/registry.py toolsets(): the
#: seven families plus connectors and admin). The CLI does not import the MCP package;
#: verify_mcp_tool_filter pins this tuple to the registry.
TOOLSET_NAMES: tuple[str, ...] = (
    "orientation",
    "projects",
    "documents",
    "keys",
    "workflows",
    "assets",
    "ops",
    "connectors",
    "admin",
)


def _toolsets_arg(raw: str) -> list[str]:
    """The --toolsets list, validated; [] means every tool (empty or `all`)."""
    names = [p.strip().lower() for p in raw.replace(",", " ").split() if p.strip()]
    if not names or "all" in names:
        return []
    unknown = sorted({n for n in names if n not in TOOLSET_NAMES})
    if unknown:
        raise CliError(
            "Unknown toolset(s) {}; valid: {}, or all.".format(
                ", ".join(unknown), ", ".join(TOOLSET_NAMES)
            ),
            EXIT_USAGE,
        )
    return list(dict.fromkeys(names))


def _mcp_client_limit(args, emit) -> int:
    """Pure local formatter for one `LOC_STUDIO_MCP_CLIENT_LIMITS` entry (#410).

    The listener is a standalone process with no database, so its per-client admission
    budgets are listener configuration (environment), not an app setting. This verb gives
    the administrator the same validation the listener applies, so a bad value is caught
    here and not at listener startup. Merge the printed entry into the existing JSON map."""
    require_local(args)
    for flag, value in (("--burst", args.burst), ("--refill-per-minute", args.refill_per_minute)):
        if not 1 <= value <= 10000:
            raise CliError(f"{flag} must be a whole number from 1 to 10000.", EXIT_USAGE)
    if args.key_env:
        key = os.environ.get(args.key_env, "").strip()
        if not key:
            raise CliError(f"Environment variable {args.key_env} is empty or not set.", EXIT_USAGE)
        try:
            fingerprint = hashlib.sha256(key.encode("ascii")).hexdigest()
        except UnicodeEncodeError:
            raise CliError("The API key must be ASCII.", EXIT_USAGE) from None
    else:
        fingerprint = (args.fingerprint or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise CliError("--fingerprint must be 64 lowercase hex characters.", EXIT_USAGE)
    entry = {fingerprint: {"burst": args.burst, "refill_per_minute": args.refill_per_minute}}
    compact = json.dumps(entry, separators=(",", ":"))
    if emit.as_json:
        emit.json(
            {"ok": True, "fingerprint": fingerprint, "entry": entry[fingerprint], "map": entry}
        )
        return 0
    emit.say("Add this entry to the LOC_STUDIO_MCP_CLIENT_LIMITS JSON map in the listener's")
    emit.say("environment (at most 1000 keys), then restart the listener:")
    emit.say("")
    emit.say(f"  {compact}")
    emit.say("")
    emit.say("Callers whose key is not listed keep the shared default (burst 10, one per minute).")
    return 0
