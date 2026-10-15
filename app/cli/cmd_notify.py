"""`notify` command group: project notification channels and routes (project automation Part B).

  notify channel list
  notify channel add KIND NAME [--url-file PATH | --url-env VAR | --url URL] [--recipients A,B]
                              [--customer ID]
  notify channel update CHANNEL_ID [--name N] [--url-file PATH | --url-env VAR | --url URL]
                              [--recipients A,B] [--enable | --disable] [--rotate-secret]
  notify channel delete CHANNEL_ID
  notify channel test CHANNEL_ID
  notify channel deliveries CHANNEL_ID [--limit N]
  notify channel retry CHANNEL_ID
  notify route list [--scope S]
  notify route set CHANNEL_ID --scope S [--ref ID] [--events E,E]
  notify route clear ROUTE_ID
  notify project show PROJECT_ID
  notify project mute PROJECT_ID
  notify project unmute PROJECT_ID

KIND is slack | teams | webhook | email. A Slack or Teams channel needs the incoming-webhook URL of
that service, a webhook channel any https URL, an email channel recipients. The URL is a bearer
credential: prefer --url-file or --url-env over --url (argv shows in process listings). It is stored
encrypted and never printed back; a webhook channel's signing secret is printed ONCE when created
or rotated. SCOPE is project | content_type | project_type | customer | org (--ref is the id, the
project type name, or omitted for org). Empty --events means the default-on events.

REMOTE (--remote URL --token lsu_): the thin client of /api/notify, scoped server-side by the
token. IN-PROCESS: no per-user RBAC (the operator surface); with multi-org on it needs --org or
--operator, like every in-process write.

Documented in: docs/architecture/notifications.md
"""

import os
import sys

from ._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    op_org_scope,
    remote_client,
    remote_mode,
    require_org_scope,
)

_KINDS = ("slack", "teams", "webhook", "email")


def register(subparsers) -> None:
    np = subparsers.add_parser(
        "notify",
        help="Notification channels (Slack, Teams, signed webhook, email) and which projects "
        "post to them.",
    )
    sub = np.add_subparsers(dest="notify_cmd", metavar="<group>")
    sub.required = True

    ch = sub.add_parser("channel", help="Create, change, test and delete channels.")
    csub = ch.add_subparsers(dest="notify_channel_cmd", metavar="<subcommand>")
    csub.required = True

    p = csub.add_parser("list", help="List the channels (no URLs, no secrets).")
    p.set_defaults(func=_channel_list)

    p = csub.add_parser("add", help="Create a channel.")
    p.add_argument("kind", choices=_KINDS)
    p.add_argument("name", help="a name for the channel (unique per org).")
    _url_args(p)
    p.add_argument("--recipients", default="", help="email channel: comma-separated addresses.")
    p.add_argument("--customer", type=bounded_id, default=None, help="pin to one customer id.")
    p.set_defaults(func=_channel_add)

    p = csub.add_parser("update", help="Rename, re-point, enable or disable a channel.")
    p.add_argument("channel_id", type=bounded_id)
    p.add_argument("--name", default=None)
    _url_args(p)
    p.add_argument("--recipients", default=None, help="email channel: comma-separated addresses.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true", help="turn the channel on.")
    g.add_argument("--disable", action="store_true", help="turn the channel off.")
    p.add_argument(
        "--rotate-secret", action="store_true", help="webhook channel: mint a new signing secret."
    )
    p.set_defaults(func=_channel_update)

    p = csub.add_parser("delete", help="Delete a channel with its routes and delivery log.")
    p.add_argument("channel_id", type=bounded_id)
    p.set_defaults(func=_channel_delete)

    p = csub.add_parser("test", help="Send a test message now.")
    p.add_argument("channel_id", type=bounded_id)
    p.set_defaults(func=_channel_test)

    p = csub.add_parser("deliveries", help="Show a channel's delivery log.")
    p.add_argument("channel_id", type=bounded_id)
    p.add_argument("--limit", type=bounded_id, default=50)
    p.set_defaults(func=_channel_deliveries)

    p = csub.add_parser("retry", help="Re-queue a channel's failed deliveries.")
    p.add_argument("channel_id", type=bounded_id)
    p.set_defaults(func=_channel_retry)

    rt = sub.add_parser("route", help="Which channel a scope posts to.")
    rsub = rt.add_subparsers(dest="notify_route_cmd", metavar="<subcommand>")
    rsub.required = True

    p = rsub.add_parser("list", help="List the routes.")
    p.add_argument("--scope", default="", help="only routes of this scope.")
    p.set_defaults(func=_route_list)

    p = rsub.add_parser("set", help="Attach a channel to a scope (idempotent).")
    p.add_argument("channel_id", type=bounded_id)
    p.add_argument("--scope", required=True)
    p.add_argument("--ref", default="", help="project/content type/customer id, or project type.")
    p.add_argument("--events", default="", help="comma-separated; empty = the default events.")
    p.set_defaults(func=_route_set)

    p = rsub.add_parser("clear", help="Remove a route.")
    p.add_argument("route_id", type=bounded_id)
    p.set_defaults(func=_route_clear)

    pj = sub.add_parser("project", help="What one project posts where.")
    psub = pj.add_subparsers(dest="notify_project_cmd", metavar="<subcommand>")
    psub.required = True

    p = psub.add_parser("show", help="Show the effective channel per event and where it came from.")
    p.add_argument("project_id", type=bounded_id)
    p.set_defaults(func=_project_show)

    p = psub.add_parser("mute", help="Make the project post to no channel.")
    p.add_argument("project_id", type=bounded_id)
    p.set_defaults(func=_project_mute)

    p = psub.add_parser("unmute", help="Let the project post to its channels again.")
    p.add_argument("project_id", type=bounded_id)
    p.set_defaults(func=_project_unmute)


def _url_args(p) -> None:
    p.add_argument("--url-file", default=None, help="read the webhook URL from this file.")
    p.add_argument("--url-env", default=None, help="read the webhook URL from this env variable.")
    p.add_argument("--url", default=None, help="the webhook URL (visible in process listings).")


def _url_of(args) -> str | None:
    if getattr(args, "url_file", None):
        try:
            with open(args.url_file, encoding="utf-8-sig") as fh:
                return (fh.readline() or "").strip()
        except OSError as exc:
            raise CliError(f"cannot read --url-file {args.url_file}: {exc}", EXIT_USAGE) from exc
    if getattr(args, "url_env", None):
        val = os.environ.get(args.url_env, "").strip()
        if not val:
            raise CliError(f"environment variable {args.url_env} is empty or unset.", EXIT_USAGE)
        return val
    if getattr(args, "url", None):
        sys.stderr.write(
            "warning: --url is visible in process listings and shell history; "
            "prefer --url-file or --url-env.\n"
        )
        return args.url.strip()
    return None


def _actor(args) -> str:
    return getattr(args, "actor", None) or "cli"


def _remote(args, method, path, **kw):
    client = remote_client(args)
    return getattr(client, method)(path, **kw)


def _local_channel(session, args, cid):
    from app.kernel import notify

    ch = notify.visible(session, cid, org_id=op_org_scope(args))
    if ch is None:
        raise CliError(f"no such channel: {cid}", EXIT_USAGE)
    return ch


def _print_channels(emit, rows) -> None:
    emit.table(
        [
            (
                c["id"],
                c["name"],
                c["kind"],
                "on" if c["enabled"] else "off",
                ", ".join(c.get("recipients") or []) or c.get("destination") or "-",
                ("ok" if c["last_test_ok"] else "failed") if c.get("last_test_at") else "never",
                c.get("failed_deliveries", 0),
            )
            for c in rows
        ],
        headers=("id", "name", "kind", "state", "destination", "last test", "failed"),
    )


def _channel_list(args, emit) -> int:
    if remote_mode(args):
        data = remote_client(args).get("/api/notify/channels")
        rows = data["channels"]
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            rows = [
                notify.to_dict(session, c)
                for c in notify.list_channels(session, org_id=op_org_scope(args))
            ]
    if emit.as_json:
        emit.json({"ok": True, "channels": rows})
        return 0
    _print_channels(emit, rows)
    return 0


def _channel_add(args, emit) -> int:
    url = _url_of(args)
    if remote_mode(args):
        body = {
            "kind": args.kind,
            "name": args.name,
            "url": url or "",
            "recipients": args.recipients,
        }
        if args.customer is not None:
            body["customer_id"] = args.customer
        data = remote_client(args).post("/api/notify/channels", json=body)
        out, shown = data["channel"], data.get("signing_secret", "")
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            try:
                ch, shown = notify.create_channel(
                    session,
                    kind=args.kind,
                    name=args.name,
                    url=url or "",
                    recipients=args.recipients,
                    customer_id=args.customer,
                    org_id=op_org_id(args),
                    created_by=_actor(args),
                )
            except notify.NotifyError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            out = notify.to_dict(session, ch)
    payload = {"ok": True, "channel": out}
    if shown:
        payload["signing_secret"] = shown
    emit.result(f"Created channel #{out['id']} ({out['kind']}) {out['name']}.", payload)
    if shown and not emit.as_json:
        emit.say(f"Signing secret (shown once): {shown}")
    return 0


def _channel_update(args, emit) -> int:
    url = _url_of(args)
    enabled = True if args.enable else False if args.disable else None
    if remote_mode(args):
        body = {}
        for key, val in (("name", args.name), ("url", url), ("recipients", args.recipients)):
            if val is not None:
                body[key] = val
        if enabled is not None:
            body["enabled"] = enabled
        if args.rotate_secret:
            body["rotate_signing_secret"] = True
        data = remote_client(args).post(f"/api/notify/channels/{args.channel_id}", json=body)
        out, shown = data["channel"], data.get("signing_secret", "")
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            ch = _local_channel(session, args, args.channel_id)
            try:
                shown = notify.update_channel(
                    session,
                    ch,
                    name=args.name,
                    url=url,
                    recipients=args.recipients,
                    enabled=enabled,
                    rotate_signing_secret=args.rotate_secret,
                    actor=_actor(args),
                )
            except notify.NotifyError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            out = notify.to_dict(session, ch)
    payload = {"ok": True, "channel": out}
    if shown:
        payload["signing_secret"] = shown
    emit.result(f"Updated channel #{out['id']}.", payload)
    if shown and not emit.as_json:
        emit.say(f"New signing secret (shown once): {shown}")
    return 0


def _channel_delete(args, emit) -> int:
    if remote_mode(args):
        remote_client(args).delete(f"/api/notify/channels/{args.channel_id}")
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            ch = _local_channel(session, args, args.channel_id)
            notify.delete_channel(session, ch, actor=_actor(args))
    emit.result(f"Deleted channel #{args.channel_id}.", {"ok": True})
    return 0


def _channel_test(args, emit) -> int:
    if remote_mode(args):
        data = remote_client(args).post(f"/api/notify/channels/{args.channel_id}/test")
        ok, msg = data["delivered"], data["message"]
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            ch = _local_channel(session, args, args.channel_id)
            res = notify.test_send(session, ch, actor=_actor(args))
        ok, msg = res["ok"], res["message"]
    emit.result(("Delivered: " if ok else "Not delivered: ") + msg, {"ok": ok, "message": msg})
    return 0 if ok else EXIT_FAIL


def _channel_deliveries(args, emit) -> int:
    if remote_mode(args):
        data = remote_client(args).get(
            f"/api/notify/channels/{args.channel_id}/deliveries", params={"limit": args.limit}
        )
        rows = data["deliveries"]
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            ch = _local_channel(session, args, args.channel_id)
            rows = notify.deliveries(
                session, channel_id=ch.id, org_id=op_org_scope(args), limit=args.limit
            )
    if emit.as_json:
        emit.json({"ok": True, "deliveries": rows})
        return 0
    emit.table(
        [
            (
                r["id"],
                r["created_at"] or "-",
                r["event"],
                r["status"],
                r["attempts"],
                r["error"] or "-",
            )
            for r in rows
        ],
        headers=("id", "created", "event", "status", "tries", "error"),
    )
    return 0


def _channel_retry(args, emit) -> int:
    if remote_mode(args):
        n = remote_client(args).post(f"/api/notify/channels/{args.channel_id}/retry")["requeued"]
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            ch = _local_channel(session, args, args.channel_id)
            n = notify.retry_failed(session, ch, actor=_actor(args))
    emit.result(
        f"Re-queued {n} failed deliver{'y' if n == 1 else 'ies'}.", {"ok": True, "requeued": n}
    )
    return 0


def _route_list(args, emit) -> int:
    if remote_mode(args):
        params = {"scope": args.scope} if args.scope else None
        rows = remote_client(args).get("/api/notify/routes", params=params)["routes"]
    else:
        require_org_scope(args, emit)
        from sqlmodel import select

        from app.kernel import notify
        from app.models import NotifyRoute

        with _session() as session:
            rows = []
            for ch in notify.list_channels(session, org_id=op_org_scope(args)):
                for r in session.exec(select(NotifyRoute).where(NotifyRoute.channel_id == ch.id)):
                    if not args.scope or r.scope == args.scope:
                        rows.append(notify.route_to_dict(r, ch))
    if emit.as_json:
        emit.json({"ok": True, "routes": rows})
        return 0
    emit.table(
        [
            (
                r["id"],
                r["scope"],
                r["scope_ref"] or "-",
                r["channel"],
                ", ".join(r["events"]) or "defaults",
            )
            for r in rows
        ],
        headers=("id", "scope", "ref", "channel", "events"),
    )
    return 0


def _route_set(args, emit) -> int:
    events = [e for e in args.events.replace(" ", "").split(",") if e]
    if remote_mode(args):
        data = remote_client(args).post(
            "/api/notify/routes",
            json={
                "channel_id": args.channel_id,
                "scope": args.scope,
                "ref": args.ref,
                "events": events,
            },
        )
        row = data["route"]
    else:
        require_org_scope(args, emit)
        from app.kernel import notify
        from app.models import Project

        with _session() as session:
            ch = _local_channel(session, args, args.channel_id)
            project = None
            if args.scope == "project":
                project = session.get(Project, int(args.ref)) if str(args.ref).isdigit() else None
                if project is None or (
                    op_org_id(args) is not None and project.org_id != op_org_id(args)
                ):
                    raise CliError(f"no such project: {args.ref}", EXIT_USAGE)
            try:
                r = notify.set_route(
                    session,
                    scope=args.scope,
                    ref=args.ref,
                    channel=ch,
                    events=events,
                    project=project,
                    created_by=_actor(args),
                )
            except notify.NotifyError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            row = notify.route_to_dict(r, ch)
    emit.result(
        f"Route #{row['id']}: {row['scope']} {row['scope_ref'] or ''} -> {row['channel']}.",
        {"ok": True, "route": row},
    )
    return 0


def _route_clear(args, emit) -> int:
    if remote_mode(args):
        remote_client(args).delete(f"/api/notify/routes/{args.route_id}")
    else:
        require_org_scope(args, emit)
        from app.kernel import notify
        from app.models import NotifyRoute

        with _session() as session:
            row = session.get(NotifyRoute, args.route_id)
            ch = _local_channel(session, args, row.channel_id) if row is not None else None
            if row is None or ch is None:
                raise CliError(f"no such route: {args.route_id}", EXIT_USAGE)
            notify.clear_route(session, row, actor=_actor(args), customer_id=ch.customer_id)
    emit.result(f"Removed route #{args.route_id}.", {"ok": True})
    return 0


def _local_project(session, args, pid):
    from app.models import Project

    proj = session.get(Project, pid)
    if proj is None or (op_org_id(args) is not None and proj.org_id != op_org_id(args)):
        raise CliError(f"no such project: {pid}", EXIT_USAGE)
    return proj


def _project_show(args, emit) -> int:
    if remote_mode(args):
        data = remote_client(args).get(f"/api/notify/projects/{args.project_id}")
        data = {k: v for k, v in data.items() if k != "ok"}
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            data = notify.effective(session, _local_project(session, args, args.project_id))
    if emit.as_json:
        emit.json({"ok": True, **data})
        return 0
    if data["muted"]:
        emit.say("This project is muted: it posts to no channel.")
    rows = [
        (ev, c["channel"], c["kind"], c["from"])
        for ev, chans in data["events"].items()
        for c in chans
    ]
    emit.table(rows, headers=("event", "channel", "kind", "from"))
    if not data["outbound_enabled"]:
        emit.warn("Outbound notifications are switched off on this install.")
    return 0


def _project_mute_set(args, emit, muted: bool) -> int:
    if remote_mode(args):
        remote_client(args).post(
            f"/api/notify/projects/{args.project_id}/mute", json={"muted": muted}
        )
    else:
        require_org_scope(args, emit)
        from app.kernel import notify

        with _session() as session:
            proj = _local_project(session, args, args.project_id)
            notify.set_project_muted(session, proj, muted, actor=_actor(args))
    emit.result(
        f"Project #{args.project_id} is now {'muted' if muted else 'unmuted'}.",
        {"ok": True, "muted": muted},
    )
    return 0


def _project_mute(args, emit) -> int:
    return _project_mute_set(args, emit, True)


def _project_unmute(args, emit) -> int:
    return _project_mute_set(args, emit, False)
