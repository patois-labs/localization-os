"""`project` verbs for project automation: a project's content source and run schedule.

  project bind <id> <connection_id> [--clone] [--scope key=value ...]
  project unbind <id>
  project automation show <id>
  project automation set <id> [--times 09:00,14:30] [--days mon,wed] [--interval N]
                              [--mode delta|full] [--enable|--disable] [--auto-pause-after N]
                              [--run-cap USD|none] [--move-schedule]
  project automation pause <id> [--reason TEXT]
  project automation resume <id>
  project automation handover <id> (--to PROJECT_ID | --from PROJECT_ID)
  project run <id> [--mode delta|full]
  project runs <id> [--limit N]

Web + /api + CLI + MCP parity over app/services/project_automation.py: the same rules (times are
UTC; a connection that already feeds another project gives this one its own copy; an item another
project owns is skipped; the project budget and the per-run cap apply). Enabling a schedule never
starts a run; `project run` does, and it spends.

REMOTE (--remote URL --token lsu_): the /api/projects/{id}/automation, /source and /runs routes,
scoped server-side by the token. IN-PROCESS: no per-user RBAC (the operator surface); with
multi-org on it needs --org or --operator like every in-process read.

Documented in: docs/architecture/project-automation.md
"""

import time

from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    remote_client,
    remote_mode,
    require_org_scope,
)

_MODES = ("delta", "full")


def _parse_scope(items) -> dict:
    out = {}
    for raw in items or []:
        if "=" not in raw:
            raise CliError(f"--scope needs key=value, got {raw!r}.", EXIT_USAGE)
        k, v = raw.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _svc():
    from app.kernel import project_automation

    return project_automation


def _guard_project(session, project_id, args):
    from .cmd_project_project import _get_project

    return _get_project(session, project_id, op_org_id(args))


def _fail(exc) -> CliError:
    return CliError(str(exc), EXIT_FAIL)


def _show_lines(emit, out: dict) -> None:
    if not out.get("bound"):
        emit.say("No content source. Bind one with: project bind <id> <connection_id>")
        return
    state = "paused" if out.get("paused") else ("on" if out.get("enabled") else "off")
    emit.say(
        f"Source: {out.get('connection_name')} ({out.get('connection_kind')}, "
        f"{out.get('connection_status') or 'new'})"
        + ("  [own copy]" if out.get("own_copy") else "")
    )
    emit.say(
        f"Schedule: {state}, {out.get('schedule')}, {out.get('mode')} pull"
        + (f", run cap ${out['run_cap_usd']:.2f}" if out.get("run_cap_usd") else "")
    )
    if out.get("next_at") and state == "on":
        emit.say(f"Next run: {out['next_at']} (UTC)")
    if out.get("paused"):
        emit.say(f"Paused: {out.get('paused_reason') or 'by a person'}")
    if out.get("last_status"):
        last = f"{out.get('last_at') or ''} {out.get('last_message') or ''}".strip()
        emit.say(f"Last run: {out['last_status']} {last}".rstrip())
    for w in out.get("warnings") or []:
        emit.say(f"Note: {w}")


def register_into(sub) -> None:
    """Add the automation verbs to the `project` group's subparsers."""
    p = sub.add_parser(
        "bind",
        help="Set a project's content source: the connection it pulls from and writes back "
        "to. A connection that already feeds another project gives this one its own copy.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("connection_id", type=bounded_id, help="the connection to use.")
    p.add_argument(
        "--clone", action="store_true", help="give this project its own copy of the connection."
    )
    p.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="narrow what this project reads (repeatable). Admins may set any declared "
        "setting; managers only narrowing ones.",
    )
    p.set_defaults(func=_bind)

    p = sub.add_parser("unbind", help="Remove a project's content source (stops its schedule).")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_unbind)

    auto = sub.add_parser(
        "automation", help="A project's run schedule (show, set, pause, resume). Times are UTC."
    )
    asub = auto.add_subparsers(dest="automation_cmd", metavar="<subcommand>")
    asub.required = True

    p = asub.add_parser("show", help="Show the content source, schedule and last run.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_show)

    p = asub.add_parser(
        "set",
        help="Change the schedule and run settings. Turning it on never starts a run at once.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("--times", help="times of day in 24-hour UTC, comma separated (09:00,14:30).")
    p.add_argument("--days", help="weekdays, comma separated (mon,wed); omit for every day.")
    p.add_argument("--interval", type=int, help="run every N minutes (15 to 10080) instead.")
    p.add_argument("--mode", choices=_MODES, help="delta: new or changed only; full: everything.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true", help="turn the schedule on.")
    g.add_argument("--disable", action="store_true", help="turn the schedule off.")
    p.add_argument(
        "--auto-pause-after", type=int, help="pause after this many failed runs in a row."
    )
    p.add_argument(
        "--run-cap", help="stop paid calls in one run at this many USD (or 'none' to clear)."
    )
    p.add_argument(
        "--move-schedule",
        action="store_true",
        help="replace the connection's own interval schedule with this one.",
    )
    p.set_defaults(func=_set)

    p = asub.add_parser("pause", help="Pause the schedule.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("--reason", default="", help="why it is paused.")
    p.set_defaults(func=_pause)

    p = asub.add_parser("resume", help="Resume a paused schedule.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_resume)

    p = asub.add_parser(
        "handover",
        help="Hand items another project is waiting for over to it (--to), or take items this "
        "project is waiting for from their owner (--from). The receiving project then owns them.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--to", type=bounded_id, help="give the items this project owns to that project."
    )
    g.add_argument(
        "--from", dest="from_project", type=bounded_id, help="take the items from that project."
    )
    p.set_defaults(func=_handover)

    p = sub.add_parser(
        "run",
        help="Run a project's content source now (pull, translate, write back). Spends money.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("--mode", choices=_MODES, help="delta (default: the schedule's mode) or full.")
    p.set_defaults(func=_run)

    p = sub.add_parser("runs", help="Recent automation runs of a project.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("--limit", type=int, default=20, help="how many, 1 to 100 (default 20).")
    p.set_defaults(func=_runs)


# --- verbs ---------------------------------------------------------------------------------
def _bind(args, emit) -> int:
    scope = _parse_scope(args.scope)
    if remote_mode(args):
        body = {"connection_id": args.connection_id, "clone": bool(args.clone), "scope": scope}
        out = remote_client(args).post(f"/api/projects/{args.project_id}/source", json=body)
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
        try:
            out = pa.bind(
                args.project_id,
                args.connection_id,
                actor=getattr(args, "actor", None) or "cli",
                is_admin=True,
                clone=bool(args.clone),
                scope=scope,
            )
        except pa.AutomationError as exc:
            raise _fail(exc) from exc
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in out.items() if k != "ok"}})
    else:
        emit.say(f"Project {args.project_id} now uses connection {out.get('connection_id')}.")
        _show_lines(emit, out)
    return 0


def _unbind(args, emit) -> int:
    if remote_mode(args):
        out = remote_client(args).post(f"/api/projects/{args.project_id}/source/unbind", json={})
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
        try:
            out = pa.unbind(args.project_id, actor=getattr(args, "actor", None) or "cli")
        except pa.AutomationError as exc:
            raise _fail(exc) from exc
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in out.items() if k != "ok"}})
    else:
        emit.say(f"Project {args.project_id} has no content source now.")
    return 0


def _show(args, emit) -> int:
    if remote_mode(args):
        out = remote_client(args).get(f"/api/projects/{args.project_id}/automation")
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
            row = pa.get(session, args.project_id)
            out = pa.view(session, row)
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in out.items() if k != "ok"}})
    else:
        _show_lines(emit, out)
    return 0


def _set(args, emit) -> int:
    body: dict = {}
    if args.times is not None:
        body["times"] = [t for t in args.times.replace(" ", ",").split(",") if t]
    if args.days is not None:
        body["days"] = [d for d in args.days.replace(" ", ",").split(",") if d]
    if args.interval is not None:
        body["interval_minutes"] = args.interval
    if args.mode:
        body["mode"] = args.mode
    if args.enable:
        body["enabled"] = True
    if args.disable:
        body["enabled"] = False
    if args.auto_pause_after is not None:
        body["auto_pause_after"] = args.auto_pause_after
    if args.run_cap is not None:
        body["run_cap_usd"] = (
            None if str(args.run_cap).lower() in ("none", "off", "") else args.run_cap
        )
    if args.move_schedule:
        body["move_schedule"] = True
    if not body:
        raise CliError("nothing to change: pass at least one setting (see --help).", EXIT_USAGE)
    if remote_mode(args):
        out = remote_client(args).put(f"/api/projects/{args.project_id}/automation", json=body)
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
        kwargs = {
            k: body[k]
            for k in ("enabled", "mode", "times", "days", "interval_minutes", "auto_pause_after")
            if k in body
        }
        if "run_cap_usd" in body:
            kwargs["run_cap_usd"] = body["run_cap_usd"]
        try:
            out = pa.configure(
                args.project_id,
                actor=getattr(args, "actor", None) or "cli",
                is_admin=True,
                move_schedule=bool(body.get("move_schedule")),
                **kwargs,
            )
        except pa.AutomationError as exc:
            raise _fail(exc) from exc
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in out.items() if k != "ok"}})
    else:
        emit.say("Saved.")
        _show_lines(emit, out)
    return 0


def _pause(args, emit) -> int:
    if remote_mode(args):
        out = remote_client(args).post(
            f"/api/projects/{args.project_id}/automation/pause", json={"reason": args.reason}
        )
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
        try:
            out = pa.pause(
                args.project_id, reason=args.reason, actor=getattr(args, "actor", None) or "cli"
            )
        except pa.AutomationError as exc:
            raise _fail(exc) from exc
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in out.items() if k != "ok"}})
    else:
        emit.say(f"Project {args.project_id} automation paused.")
    return 0


def _resume(args, emit) -> int:
    if remote_mode(args):
        out = remote_client(args).post(
            f"/api/projects/{args.project_id}/automation/resume", json={}
        )
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
        try:
            out = pa.resume(args.project_id, actor=getattr(args, "actor", None) or "cli")
        except pa.AutomationError as exc:
            raise _fail(exc) from exc
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in out.items() if k != "ok"}})
    else:
        emit.say(f"Project {args.project_id} automation resumed.")
    return 0


def _handover(args, emit) -> int:
    give = args.to is not None
    other = args.to if give else args.from_project
    if remote_mode(args):
        body = {"to_project_id": other} if give else {"from_project_id": other}
        out = remote_client(args).post(
            f"/api/projects/{args.project_id}/automation/handover", json=body
        )
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
            _guard_project(session, other, args)
        owner, receiver = (args.project_id, other) if give else (other, args.project_id)
        try:
            out = pa.hand_over(owner, receiver, actor=getattr(args, "actor", None) or "cli")
        except pa.AutomationError as exc:
            raise _fail(exc) from exc
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in out.items() if k != "ok"}})
    else:
        emit.say(
            f"Handed {out.get('pairs')} item(s) over from project {out.get('from_project_id')} "
            f"to project {out.get('to_project_id')}: the receiving project owns them now."
        )
    return 0


def _run(args, emit) -> int:
    if remote_mode(args):
        body = {"mode": args.mode} if args.mode else {}
        out = remote_client(args).post(
            f"/api/projects/{args.project_id}/runs", json=body, idempotent=True
        )
        jid = out.get("job_id")
        existing = bool(out.get("existing"))
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
        try:
            jid, created = pa.run_now(
                args.project_id,
                actor=getattr(args, "actor", None) or "cli",
                mode=args.mode,
                trigger="manual",
            )
        except pa.AutomationError as exc:
            raise _fail(exc) from exc
        existing = not created
        # The workers live in this process: wait for the run so it is not cut off at exit.
        from app.kernel import jobs

        deadline = time.time() + 1800
        job = jobs.get(jid)
        while job is not None and job.status in ("queued", "running") and time.time() < deadline:
            time.sleep(0.5)
            job = jobs.get(jid)
        out = {
            "job_id": jid,
            "status": getattr(job, "status", ""),
            "message": (
                (getattr(job, "result", None) or {}).get("message") or getattr(job, "error", "")
            ),
        }
        if getattr(job, "status", "") == "failed":
            if emit.as_json:
                emit.json({"ok": False, "job_id": jid, "error": out["message"]})
            raise CliError(f"run failed: {out['message']}", EXIT_FAIL)
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "job_id": jid,
                "existing": existing,
                **{k: v for k, v in out.items() if k not in ("ok", "job_id")},
            }
        )
    else:
        emit.say(
            f"A run was already in progress as job {jid}."
            if existing
            else f"Run started as job {jid}. Check it with: job status {jid}"
        )
        if out.get("message"):
            emit.say(str(out["message"]))
    return 0


def _runs(args, emit) -> int:
    if remote_mode(args):
        out = remote_client(args).get(
            f"/api/projects/{args.project_id}/runs", params={"limit": args.limit}
        )
        runs = out.get("runs", [])
    else:
        require_org_scope(args, emit)
        pa = _svc()
        with _session() as session:
            _guard_project(session, args.project_id, args)
            try:
                runs = pa.history(session, args.project_id, limit=args.limit, can_see_cost=True)
            except pa.AutomationError as exc:
                raise _fail(exc) from exc
    if emit.as_json:
        emit.json({"ok": True, "project_id": args.project_id, "runs": runs})
        return 0
    emit.table(
        [
            (
                r["job_id"] if r.get("job_id") is not None else "-",
                r["started_at"],
                r.get("trigger") or "-",
                r.get("mode") or "-",
                r["status"] if r.get("kind") != "handover" else r["message"],
                r.get("created", 0) + r.get("updated", 0),
                r.get("held_back", 0) + r.get("partial_held", 0),
                r.get("failed_items", 0),
            )
            for r in runs
        ],
        headers=("job", "started (UTC)", "why", "mode", "status", "written", "held", "failed"),
    )
    return 0
