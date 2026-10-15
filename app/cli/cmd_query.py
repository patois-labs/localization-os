"""`query` command group (SPEC-wave3 §4.4): threaded queries / comments.

  query open   --segment|--key|--document|--project N --subject S --body B [--assignee E]
  query list   [--status][--project][--document][--segment][--key][--assignee][--mine]
  query show   <id>
  query reply  <id> --body B
  query answer <id> --body B          (marks the thread answered)
  query close  <id> [--reopen]
  query assign <id> [--assignee E]    (empty clears)

Parity with the web /queries surface + GET|POST /api/queries*: all three call
``app.services.query_threads``. IN-PROCESS this is the TRUSTED-OPERATOR surface
(``__main__.py:35-39``): no RBAC, no session user — every verb passes
``trusted_operator=True`` and the author is ``--actor`` or ``"cli"`` (§4.4, deferral
#10). Under the multi-org gate it threads ``--org`` via ``require_org_scope`` so a
shared-host operator cannot sweep every org unscoped. ``--remote --token`` hits the /api,
which is org-bound + RBAC-enforced server-side by the token.

Documented in: docs/architecture/collaboration-and-review.md
"""

from . import _remote_impl
from ._common import (
    CliError,
    _actor,
    _session,
    bounded_id,
    op_org_scope,
    remote_mode,
    require_org_scope,
)


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    q = subparsers.add_parser(
        "query", help="Threaded queries + comments on a segment / key / document / project."
    )
    sub = q.add_subparsers(dest="query_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("open", help="Open a query thread.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--segment", type=bounded_id, help="scope to a segment id.")
    g.add_argument("--key", type=bounded_id, help="scope to a string-key id.")
    g.add_argument("--document", type=bounded_id, help="scope to a document id.")
    g.add_argument("--project", type=bounded_id, help="scope to a project id.")
    p.add_argument("--subject", default="", help="a short subject (<=200 chars).")
    p.add_argument("--body", required=True, help="the first post's text (<=4000 chars).")
    p.add_argument(
        "--assignee",
        default="",
        help="assignee email (must be an existing active user in scope who can "
        "also see the thread's project).",
    )
    p.set_defaults(func=_query_open)

    p = sub.add_parser("list", help="List query threads over your visible projects.")
    p.add_argument(
        "--status",
        default="",
        choices=("", "open", "answered", "closed"),
        help="filter by lifecycle status.",
    )
    p.add_argument("--project", type=bounded_id, default=None, help="filter to a project.")
    p.add_argument("--document", type=bounded_id, default=None, help="filter to a document.")
    p.add_argument("--segment", type=bounded_id, default=None, help="filter to a segment.")
    p.add_argument("--key", type=bounded_id, default=None, help="filter to a key.")
    p.add_argument("--assignee", default="", help="filter to an assignee email.")
    p.add_argument("--mine", action="store_true", help="only threads I authored (--actor).")
    p.add_argument("--limit", type=int, default=50, help="page size (max 50).")
    p.add_argument("--offset", type=int, default=0, help="page offset.")
    p.set_defaults(func=_query_list)

    p = sub.add_parser("show", help="Show one thread + its posts.")
    p.add_argument("id", type=bounded_id, help="the thread id.")
    p.set_defaults(func=_query_show)

    p = sub.add_parser("reply", help="Post a reply (does NOT mark answered).")
    p.add_argument("id", type=bounded_id, help="the thread id.")
    p.add_argument("--body", required=True, help="the reply text (<=4000 chars).")
    p.set_defaults(func=_query_reply)

    p = sub.add_parser("answer", help="Post a reply that marks the thread answered.")
    p.add_argument("id", type=bounded_id, help="the thread id.")
    p.add_argument("--body", required=True, help="the answer text (<=4000 chars).")
    p.set_defaults(func=_query_answer)

    p = sub.add_parser("close", help="Close a thread (or --reopen it).")
    p.add_argument("id", type=bounded_id, help="the thread id.")
    p.add_argument("--reopen", action="store_true", help="reopen a closed thread instead.")
    p.set_defaults(func=_query_close)

    p = sub.add_parser("assign", help="Set or clear a thread's assignee.")
    p.add_argument("id", type=bounded_id, help="the thread id.")
    p.add_argument(
        "--assignee",
        default="",
        help="assignee email (empty clears; a set address must be an existing "
        "active user who can see the thread's project).",
    )
    p.set_defaults(func=_query_assign)


# --- helpers -----------------------------------------------------------------
def _flag_gate():
    """Layer-2 CLI kill switch (the message lives in the registry; see feature_gate)."""
    from ._common import feature_gate

    feature_gate("queries")


def _svc():
    from app.kernel import query_threads as qt

    return qt


def _notify(fn_name, *args):
    """M-5: fire a query notification from the in-process CLI too (web/api already do), so
    the surfaces are at parity and the docs' 'it mails' claim holds. Same guarded shape:
    deferred kernel import, local try/except, never crashes the verb."""
    try:
        from app.kernel import notifications

        getattr(notifications, fn_name)(*args)
    except Exception:  # noqa: S110 -- notification mail is a side effect; never crash the verb
        pass


# M-6: these dicts are BYTE-IDENTICAL in key set to api_cli._qthread_dict /
# _qthread_row_dict / _qpost_dict, so the in-process CLI --json shape never drifts from
# /api (which the --remote arm reads verbatim). Includes vendor_id/dispatch_id/etc. —
# Train E's fields, NULL on a human thread but always present.
def _thread_dict(t) -> dict:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "document_id": t.document_id,
        "segment_id": t.segment_id,
        "key_id": t.key_id,
        "target_locale": t.target_locale,
        "subject": t.subject,
        "status": t.status,
        "author": t.author,
        "author_kind": t.author_kind,
        "assignee_email": t.assignee_email,
        "vendor_id": t.vendor_id,
        "dispatch_id": t.dispatch_id,
        "post_count": t.post_count,
        "created_at": str(t.created_at),
        "updated_at": str(t.updated_at),
        "closed_at": str(t.closed_at) if t.closed_at else None,
        "closed_by": t.closed_by,
    }


def _row_dict(r) -> dict:
    return {
        "id": r.id,
        "project_id": r.project_id,
        "document_id": r.document_id,
        "segment_id": r.segment_id,
        "key_id": r.key_id,
        "target_locale": r.target_locale,
        "subject": r.subject,
        "status": r.status,
        "author": r.author,
        "author_kind": r.author_kind,
        "assignee_email": r.assignee_email,
        "vendor_id": r.vendor_id,
        "dispatch_id": r.dispatch_id,
        "post_count": r.post_count,
        "created_at": str(r.created_at),
        "updated_at": str(r.updated_at),
        "closed_at": str(r.closed_at) if r.closed_at else None,
        "closed_by": r.closed_by,
    }


def _post_dict(p) -> dict:
    return {
        "id": p.id,
        "thread_id": p.thread_id,
        "body": p.body,
        "author": p.author,
        "author_kind": p.author_kind,
        "author_label": p.author_label,
        "vendor_id": p.vendor_id,
        "dispatch_id": p.dispatch_id,
        "external_id": p.external_id,
        "created_at": str(p.created_at),
    }


def _run(fn):
    """Map the service's typed QueryError to a clean CliError (exit 1)."""
    qt = _svc()
    try:
        return fn(qt)
    except qt.QueryError as exc:
        raise CliError(str(exc)) from exc


# --- commands ----------------------------------------------------------------
def _query_open(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.query_open(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    with _session() as s:

        def go(qt):
            scope = qt.resolve_scope_in_ring(
                s,
                user=None,
                trusted=True,
                org_id=op_org_scope(args),
                segment_id=args.segment,
                key_id=args.key,
                document_id=args.document,
                project_id=args.project,
            )
            return qt.open_thread(
                s,
                user=None,
                scope=scope,
                subject=args.subject,
                body=args.body,
                assignee_email=args.assignee,
                actor=_actor(args),
                trusted_operator=True,
                org_id=op_org_scope(args),
            )

        thread = _run(go)
        out = _thread_dict(thread)
        tid = thread.id
    _notify("on_query_opened", tid, _actor(args))  # M-5 parity; L-5 skips self-assign
    emit.result(
        "Opened query #{} ({}) on project {}.".format(out["id"], out["status"], out["project_id"]),
        {"ok": True, "thread": out},
    )
    return 0


def _query_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.query_list(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    qt = _svc()
    with _session() as s:
        rows, total = qt.list_threads(
            s,
            None,
            status=(args.status or None),
            project_id=args.project,
            document_id=args.document,
            segment_id=args.segment,
            key_id=args.key,
            assignee=(args.assignee or None),
            mine=args.mine,
            author=(_actor(args) if args.mine else None),
            limit=max(1, min(args.limit, qt.LIST_PAGE_MAX)),
            offset=max(0, args.offset),
            trusted_operator=True,
            org_id=op_org_scope(args),
        )
    if emit.as_json:
        emit.json({"ok": True, "total": total, "threads": [_row_dict(r) for r in rows]})
        return 0
    emit.table(
        [
            (
                r.id,
                r.status,
                r.project_id,
                (r.subject or "")[:40],
                r.assignee_email or "-",
                r.post_count,
                str(r.updated_at),
            )
            for r in rows
        ],
        headers=("id", "status", "project", "subject", "assignee", "posts", "updated"),
    )
    emit.say("%d thread(s)%s." % (total, "" if total <= len(rows) else " (showing %d)" % len(rows)))  # noqa: UP031
    return 0


def _query_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.query_show(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    with _session() as s:
        thread, posts = _run(
            lambda qt: qt.get_thread(
                s, user=None, thread_id=args.id, trusted_operator=True, org_id=op_org_scope(args)
            )
        )
        pd = [_post_dict(p) for p in posts]
    if emit.as_json:
        emit.json({"ok": True, "thread": _thread_dict(thread), "posts": pd})
        return 0
    emit.say(
        "Query #{} [{}]  project={}  author={}  assignee={}".format(
            thread.id,
            thread.status,
            thread.project_id,
            thread.author or "-",
            thread.assignee_email or "-",
        )
    )
    if thread.subject:
        emit.say(f"Subject: {thread.subject}")
    emit.say("")
    for p in posts:
        who = p.author or (p.author_label and f"{p.author_label} (vendor)") or "vendor"
        emit.say(f"  [{str(p.created_at)}] {who}:")  # noqa: RUF010
        emit.say("    {}".format((p.body or "").replace("\n", "\n    ")))
    return 0


def _query_reply(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.query_reply(args, emit, answer=False)
    _flag_gate()
    require_org_scope(args, emit)
    with _session() as s:
        post = _run(
            lambda qt: qt.add_post(
                s,
                user=None,
                thread_id=args.id,
                body=args.body,
                answer=False,
                actor=_actor(args),
                trusted_operator=True,
                org_id=op_org_scope(args),
            )
        )
        out = _post_dict(post)
    _notify("on_query_post", args.id, out["id"], _actor(args))  # M-5 parity
    emit.result(
        "Posted reply #{} to query #{}.".format(out["id"], args.id), {"ok": True, "post": out}
    )
    return 0


def _query_answer(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.query_reply(args, emit, answer=True)
    _flag_gate()
    require_org_scope(args, emit)
    with _session() as s:
        post = _run(
            lambda qt: qt.add_post(
                s,
                user=None,
                thread_id=args.id,
                body=args.body,
                answer=True,
                actor=_actor(args),
                trusted_operator=True,
                org_id=op_org_scope(args),
            )
        )
        out = _post_dict(post)
    _notify("on_query_post", args.id, out["id"], _actor(args))  # M-5 parity
    emit.result(
        "Answered query #{} (post #{}).".format(args.id, out["id"]), {"ok": True, "post": out}
    )
    return 0


def _query_close(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.query_status(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    target = "open" if args.reopen else "closed"
    with _session() as s:
        from app.models import QueryStatus, QueryThread

        prior = s.get(QueryThread, args.id)
        prior_status = prior.status if prior else None
        thread = _run(
            lambda qt: qt.set_status(
                s,
                user=None,
                thread_id=args.id,
                status=target,
                actor=_actor(args),
                trusted_operator=True,
                org_id=op_org_scope(args),
            )
        )
        out = _thread_dict(thread)
        newly_closed = (
            thread.status == QueryStatus.CLOSED and prior_status != QueryStatus.CLOSED
        )  # M-2 transition guard
    if newly_closed:
        _notify("on_query_closed", args.id, _actor(args))
    emit.result(
        "Query #{} is now {}.".format(out["id"], out["status"]), {"ok": True, "thread": out}
    )
    return 0


def _query_assign(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.query_assign(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    with _session() as s:
        from app.models import QueryThread

        prior = s.get(QueryThread, args.id)
        prior_assignee = (prior.assignee_email or "") if prior else ""
        thread = _run(
            lambda qt: qt.assign(
                s,
                user=None,
                thread_id=args.id,
                assignee_email=args.assignee,
                actor=_actor(args),
                trusted_operator=True,
                org_id=op_org_scope(args),
            )
        )
        out = _thread_dict(thread)
        changed = bool(out["assignee_email"]) and out["assignee_email"] != prior_assignee
    if changed:
        _notify("on_query_opened", args.id, _actor(args))  # M-3/M-5 parity
    emit.result(
        "Query #{} assignee: {}.".format(out["id"], out["assignee_email"] or "(cleared)"),
        {"ok": True, "thread": out},
    )
    return 0
