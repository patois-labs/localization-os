"""`search` command: global search over projects, documents, TM, glossary terms, workflows and
connections (#595, #621).

  search <text> [--group projects|documents|tm|glossary|workflows|connections ...]
                [--limit N] [--offset N]

Web + /api + CLI parity over app/services/global_search.py: the same groups, the same
bounds (2 to 100 characters, at most 25 hits per group per call, counts to 1000, TM scan
limited to the newest 100,000 entries) and the same wording of what is searched.

REMOTE (--remote URL --token lsu_): GET /api/search, scoped server-side by the token to
exactly what that person can open. IN-PROCESS: no per-user RBAC (the operator surface, like
`tm list`); with multi-org on it needs --org or --operator, like every in-process read.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    op_org_scope,
    remote_client,
    remote_mode,
    require_org_scope,
)

_GROUPS = ("projects", "documents", "tm", "glossary", "workflows", "connections")


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "search",
        help="Search projects, documents (file names), TM, glossary, workflows, connections.",
        description=(
            "Searches project and customer names, document file names, translation memory "
            "text, glossary terms, workflow names and connection names. It does not search "
            "inside documents."
        ),
    )
    p.add_argument("text", help="what to look for (2 to 100 characters)")
    p.add_argument(
        "--group",
        action="append",
        choices=_GROUPS,
        help="limit to one group; repeat for several (default: all six)",
    )
    p.add_argument("--limit", type=int, default=5, help="hits per group, 1 to 25 (default 5)")
    p.add_argument("--offset", type=int, default=0, help="skip this many hits per group, 0 to 1000")
    p.set_defaults(func=_search)


def _search(args, emit) -> int:
    groups = list(args.group or [])
    if remote_mode(args):
        params = {"q": args.text, "limit": args.limit, "offset": args.offset}
        if groups:
            params["group"] = ",".join(groups)
        body = remote_client(args).get("/api/search", params=params)
        result = {k: v for k, v in body.items() if k != "ok"}
    else:
        require_org_scope(args, emit)
        from app.kernel import global_search

        with _session() as session:
            scope = global_search.scope_for_org(session, op_org_scope(args))
            try:
                result = global_search.search(
                    session,
                    scope,
                    args.text,
                    groups=groups or None,
                    limit=args.limit,
                    offset=args.offset,
                )
            except global_search.SearchQueryError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
    if emit.as_json:
        emit.json({"ok": True, **result})
        return 0
    emit.say(f'Search for "{result["query"]}": {result["total"]} match(es).')
    for block in result["groups"]:
        count = f"{block['count']}+" if block.get("capped") else str(block["count"])
        emit.say(f"{block['label']}: {count}")
        if block.get("truncated"):
            emit.say("  (only the newest 100,000 TM entries were searched)")
        emit.table(
            [
                (h["title"], h.get("subtitle", ""), h.get("detail", ""), h["href"])
                for h in block["hits"]
            ],
            headers=("title", "subtitle", "detail", "open"),
        )
    return 0
