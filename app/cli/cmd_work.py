"""`work` command group (SPEC-wave3 §8.4): the reviewer worklist ("My work").

  work list [--actor E] [--days N] [--quality needs_review] [--limit N] [--offset N]

Parity with the web /my-work surface + GET /api/worklist: all three call
``app.services.worklist.my_work``. IN-PROCESS this is the TRUSTED-OPERATOR surface
(``__main__.py:35-39``): no session user, so the worklist needs an IDENTITY to answer
FOR — ``work list`` REQUIRES ``--actor <email>`` in-process (or ``--remote``), and refuses
with a named message rather than returning an empty list that would read as "you have no
work" (§8.4, default #12). The trusted operator is root, so the scope ring is
unrestricted and the quality section is shown; the ``--actor`` email drives the
assignee/author matching. ``--remote --token`` hits GET /api/worklist, whose identity is
the token's own user, RBAC-enforced server-side.

Documented in: docs/architecture/collaboration-and-review.md
"""

from . import _remote_impl
from ._common import EXIT_USAGE, CliError, _session, op_org_id, remote_mode, require_org_scope


class _EmailIdentity:
    """A minimal identity carrying just the acting email, for the trusted-operator arm
    when ``--actor`` does not resolve to a real User row (a reviewer can be named on a
    workflow step's ``assignee_email`` without being a platform User). The service reads
    only ``.email`` under ``trusted_operator=True``; scope is root, can_review is implied.
    """

    __slots__ = ("email",)

    def __init__(self, email: str):
        self.email = email


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    w = subparsers.add_parser(
        "work",
        help="Your reviewer worklist: paused review steps, queries and "
        "documents needing quality attention.",
    )
    sub = w.add_subparsers(dest="work_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "list",
        help="List my work across every project I can see (needs --actor in-process, or --remote).",
    )
    p.add_argument(
        "--days", type=int, default=90, help="quality-section recency window in days (default 90)."
    )
    p.add_argument(
        "--quality",
        default="",
        choices=("", "needs_review"),
        help="'needs_review' swaps the quality section for the "
        "off-by-default NEEDS_REVIEW segment list (a machine flag, not "
        "an assignment; it floods on a mature install).",
    )
    p.add_argument("--limit", type=int, default=50, help="page size (max 50).")
    p.add_argument("--offset", type=int, default=0, help="page offset.")
    p.set_defaults(func=_work_list)


# --- helpers -----------------------------------------------------------------
def _flag_gate():
    """Layer-2 CLI kill switch (the message lives in the registry; see feature_gate)."""
    from ._common import feature_gate

    feature_gate("worklist")


def _svc():
    from app.kernel import worklist

    return worklist


def _item_dict(it) -> dict:
    """BYTE-IDENTICAL in key set to api_cli._worklist_item_dict, so the in-process CLI
    --json shape never drifts from /api (which the --remote arm reads verbatim)."""
    return {
        "section": it.section,
        "ref_id": it.ref_id,
        "title": it.title,
        "project_id": it.project_id,
        "project_name": it.project_name,
        "document_id": it.document_id,
        "target_locale": it.target_locale,
        "status": it.status,
        "reason": it.reason,
        "due": it.due,
        "url": it.url,
        "updated_at": str(it.updated_at) if it.updated_at else None,
    }


# --- commands ----------------------------------------------------------------
def _work_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.work_list(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    # §8.4 / default #12: the in-process console has no session user, so it MUST be told
    # whose work to show. --actor defaults to "cli" (the audit sentinel); treat that as
    # "not given" and refuse, rather than answer for a pseudo-user or return an empty list.
    actor = (getattr(args, "actor", "") or "").strip()
    if not actor or actor == "cli":
        raise CliError("work list needs --actor <email> in-process, or --remote.", EXIT_USAGE)
    worklist = _svc()
    with _session() as s:
        from sqlalchemy import func
        from sqlmodel import select

        from app.models import User

        u = s.exec(select(User).where(func.lower(User.email) == actor.lower())).first()
        ident = u if u is not None else _EmailIdentity(actor)
        rows, total = worklist.my_work(
            s,
            ident,
            days=max(1, args.days),
            quality=(args.quality or ""),
            limit=max(1, min(args.limit, worklist.LIST_PAGE_MAX)),
            offset=max(0, args.offset),
            trusted_operator=True,
            org_id=op_org_id(args),
        )
        items = [_item_dict(it) for it in rows]
    if emit.as_json:
        emit.json({"ok": True, "total": total, "items": items})
        return 0
    emit.table(
        [
            (
                it["section"],
                it["ref_id"],
                it["project_id"],
                (it["title"] or "")[:36],
                it["target_locale"] or "-",
                it["reason"],
                str(it["updated_at"]),
            )
            for it in items
        ],
        headers=("section", "ref", "project", "title", "locale", "reason", "updated"),
    )
    emit.say(
        "%d work item(s)%s for %s."  # noqa: UP031
        % (total, "" if total <= len(items) else " (showing %d)" % len(items), actor)  # noqa: UP031
    )
    return 0
