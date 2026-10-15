"""Prompt Tap actions, runs, and envelope inspection CLI subcommands.

Provides inspection and verification of translation actions, run envelopes,
step-to-call hierarchies, and bodies-withheld summaries across local and
remote surfaces (`actions`, `show-action`, `runs`, `run`).

Documented in: docs/architecture/connectors.md
"""

from ._common import (
    EXIT_USAGE,
    CliError,
    op_org_scope,
    remote_client,
    remote_mode,
)
from .prompt_tap_common import (
    _strip_record_keys,
    _tap_gate,
)

_SUMMARY_HEADERS = (
    "action",
    "kind",
    "status",
    "project/doc",
    "calls",
    "fails",
    "finish",
    "errors",
    "spend",
    "started",
)


def _register_actions(sub) -> None:
    ac = sub.add_parser(
        "actions",
        help="List translation ACTIONS newest-first (the "
        "run-envelope view: one entry per click, with its language-pair "
        "runs). GET /prompt-tap parity (the Runs section).",
    )
    ac.add_argument("--project", type=int, default=None, metavar="ID", help="filter by project id.")
    ac.add_argument(
        "--document", type=int, default=None, metavar="ID", help="filter by document id."
    )
    ac.add_argument(
        "--batch",
        type=int,
        default=None,
        metavar="ID",
        help="filter by batch id (a project-wide multi-locale launch).",
    )
    ac.add_argument(
        "--status",
        default=None,
        metavar="S",
        help="filter by status (running|completed|failed|paused|abandoned|orphaned).",
    )
    ac.add_argument(
        "--failures", action="store_true", help="only actions with at least one failed call."
    )
    ac.add_argument(
        "--limit", type=int, default=50, metavar="N", help="show the newest N actions (default 50)."
    )
    ac.add_argument(
        "--summary",
        action="store_true",
        help="BODIES-WITHHELD summary (counts, statuses, error classes, finish "
        "reasons, spend, timings) - the ONLY prompt-tap view that works over --remote "
        "(the raw call bodies stay host-only).",
    )
    ac.set_defaults(func=_actions)


def _register_show_action(sub) -> None:
    sa = sub.add_parser(
        "show-action",
        help="Show ONE action's envelope + its "
        "language-pair runs (add --calls for the nested call payloads).",
    )
    sa.add_argument("action_id", metavar="ID", help="the action id (26-char ULID).")
    sa.add_argument(
        "--calls",
        action="store_true",
        help="also print each run's captured call payloads (implies --json "
        "for the full records; the table view lists run summaries). HOST-ONLY "
        "(the raw bodies never leave the host; not available over --remote).",
    )
    sa.add_argument(
        "--summary",
        action="store_true",
        help="BODIES-WITHHELD summary (per-run counts, statuses, error classes, "
        "finish reasons, spend, timings, and the per-call failure shape) - works "
        "over --remote. Mutually exclusive with --calls (which is host-only bodies).",
    )
    sa.set_defaults(func=_show_action)


def _register_runs(sub) -> None:
    rn = sub.add_parser(
        "runs",
        help="List translation RUNS newest-first (the run-first "
        "view: one entry per translation run, grouped from the captures' own run id). "
        "Bodies withheld; works over --remote. GET /api/prompt-tap/runs parity.",
    )
    rn.add_argument(
        "--since",
        default=None,
        metavar="DATE",
        help="only runs started on/after DATE (yyyy-mm-dd).",
    )
    rn.add_argument(
        "--until",
        default=None,
        metavar="DATE",
        help="only runs started on/before DATE (yyyy-mm-dd).",
    )
    rn.add_argument(
        "--failures", action="store_true", help="only runs with at least one failed call."
    )
    rn.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="show the newest N runs (default 50, max 500).",
    )
    rn.add_argument(
        "--cursor",
        default=None,
        metavar="TOKEN",
        help="page past --limit: pass the `next_cursor` from the previous page's --json.",
    )
    rn.set_defaults(func=_runs)


def _register_run(sub) -> None:
    ru = sub.add_parser(
        "run",
        help="Show ONE run's calls grouped by step then unit "
        "(bodies withheld by default). GET /api/prompt-tap/runs/{id} parity.",
    )
    ru.add_argument("run_id", metavar="ID", help="the run id (a 26-char ULID, or advrun-<n>).")
    ru.add_argument(
        "--bodies",
        action="store_true",
        help="include the full prompt/response payloads (implies --json). HOST-ONLY "
        "(the raw bodies never leave the host; not available over --remote).",
    )
    ru.set_defaults(func=_run)


def register_prompt_tap_verify_commands(sub) -> None:
    """Register all Prompt Tap action and run inspection subcommands."""
    _register_actions(sub)
    _register_show_action(sub)
    _register_runs(sub)
    _register_run(sub)


def _action_flags(a) -> str:
    """Compact ASCII honesty markers for an action row (N8/N9), matching the web badges:
    `wh` capture withheld (totals under-count), `tr` enrichment truncated, `~$` spend
    estimated (not ledger-exact)."""
    tot = a.get("totals") or {}
    flags = []
    if a.get("capture_withheld") or tot.get("capture_withheld"):
        flags.append("wh")
    if tot.get("enrichment_truncated"):
        flags.append("tr")
    if tot.get("spend_source") == "estimated_from_usage":
        flags.append("~$")
    return ",".join(flags) or "-"


def _summary_action_rows(actions):
    """Table rows for a list of BODIES-WITHHELD action summaries (shared local + remote)."""
    rows = []
    for a in actions:
        tot = a.get("totals") or {}
        spend = tot.get("spend_usd")
        where = a.get("project_name") or (
            "project #{}".format(a["project_id"]) if a.get("project_id") is not None else "ad-hoc"
        )
        if a.get("document_name"):
            where += "/" + a["document_name"]
        elif a.get("document_id") is not None:
            where += "/doc{}".format(a["document_id"])
        # aggregate finish reasons / error classes across the action's runs
        fins: dict = {}
        errs: dict = {}
        calls = fails = 0
        for rv in a.get("runs") or []:
            cs = rv.get("calls_summary") or {}
            calls += int(cs.get("calls") or 0)
            fails += int(cs.get("failures") or 0)
            for k, v in (cs.get("finish_reasons") or {}).items():
                fins[k] = fins.get(k, 0) + v
            for k, v in (cs.get("error_classes") or {}).items():
                errs[k] = errs.get(k, 0) + v
        fin_txt = ",".join(f"{k}:{v}" for k, v in sorted(fins.items())) or "-"
        err_txt = ",".join(f"{k}:{v}" for k, v in sorted(errs.items())) or "-"
        rows.append(
            (
                a["id"][:12],
                a.get("kind") or "",
                a.get("status") or "",
                where,
                str(calls),
                str(fails),
                fin_txt,
                err_txt,
                (f"${spend:.4f}") if spend is not None else "-",
                (a.get("started_at") or "")[:19].replace("T", " "),
            )
        )
    return rows


def _actions(args, emit) -> int:
    """List translation actions newest-first (the run-envelope grouped view). Same read
    model as the /prompt-tap Runs section (tap_view.list_actions).

    run-190 Item C: `--summary` gives the BODIES-WITHHELD view (counts / statuses / error
    classes / finish reasons / spend / timings) and is the ONE prompt-tap read that works
    over `--remote` - it routes through the new GET /api/prompt-tap/actions, which never
    returns a prompt or response body. Every other prompt-tap read stays host-only."""
    summary = bool(getattr(args, "summary", False))
    if summary and remote_mode(args):
        return _remote_actions(args, emit)
    _tap_gate(args, emit)
    from app.kernel import tap_view

    if summary:
        actions = tap_view.list_action_summaries(
            limit=max(1, args.limit),
            project_id=args.project,
            document_id=args.document,
            batch_id=args.batch,
            status=args.status,
            has_failures=bool(args.failures),
        )
        if emit.as_json:
            emit.json(
                {"ok": True, "count": len(actions), "bodies_withheld": True, "actions": actions}
            )
            return 0
        if not actions:
            emit.warn("no translation runs recorded yet.")
            return 0
        emit.table(_summary_action_rows(actions), headers=_SUMMARY_HEADERS)
        emit.say("")
        emit.say("(bodies withheld; use `show-action <id> --calls` on the host for the payloads)")
        return 0

    actions = tap_view.list_actions(
        limit=max(1, args.limit),
        project_id=args.project,
        document_id=args.document,
        batch_id=args.batch,
        status=args.status,
        has_failures=bool(args.failures),
    )
    if emit.as_json:
        emit.json({"ok": True, "count": len(actions), "actions": _strip_record_keys(actions)})
        return 0
    if not actions:
        emit.warn("no translation runs recorded yet (run a workflow or batch, then look here).")
        return 0
    rows = []
    for a in actions:
        tot = a.get("totals") or {}
        spend = tot.get("spend_usd")
        human = tot.get("human_spend_usd") or 0.0
        where = a.get("project_name") or (
            "project #{}".format(a["project_id"]) if a.get("project_id") is not None else "ad-hoc"
        )
        if a.get("document_name"):
            where += "/" + a["document_name"]
        elif a.get("document_id") is not None:
            where += "/doc{}".format(a["document_id"])
        rows.append(
            (
                a["id"][:12],
                a.get("status") or "",
                where,
                ",".join(a.get("requested_locales") or []) or "-",
                str(len(a.get("runs") or [])),
                str(tot.get("calls", "-")),
                (f"${spend:.4f}") if spend is not None else "-",
                (f"${human:.2f}") if human else "-",
                _action_flags(a),
                (a.get("started_iso") or "")[:19].replace("T", " "),
            )
        )
    emit.table(
        rows,
        headers=(
            "action",
            "status",
            "project/doc",
            "locales",
            "runs",
            "calls",
            "ai$",
            "human$",
            "flags",
            "started",
        ),
    )
    emit.say("")
    emit.say("(flags: wh=capture withheld, tr=enrichment truncated, ~$=spend estimated)")
    emit.say("(use `prompt-tap show-action <id>` for one action's runs + calls)")
    return 0


def _remote_actions(args, emit) -> int:
    """`prompt-tap actions --summary --remote`: the bodies-withheld action list over /api."""
    c = remote_client(args)
    params = {"limit": max(1, args.limit)}
    for k, v in (
        ("project_id", args.project),
        ("document_id", args.document),
        ("batch_id", args.batch),
        ("status", args.status),
    ):
        if v is not None:
            params[k] = v
    if args.failures:
        params["failures"] = "1"
    body = c.get("/api/prompt-tap/actions", params=params)
    actions = body.get("actions", [])
    if emit.as_json:
        emit.json({"ok": True, "count": len(actions), "bodies_withheld": True, "actions": actions})
        return 0
    if not actions:
        emit.warn("no translation runs recorded yet.")
        return 0
    emit.table(_summary_action_rows(actions), headers=_SUMMARY_HEADERS)
    emit.say("")
    emit.say("(bodies withheld over --remote; raw call payloads are host-only)")
    return 0


def _remote_show_action(args, emit) -> int:
    """`prompt-tap show-action <id> --summary --remote`: one action's bodies-withheld
    summary over /api."""
    c = remote_client(args)
    aid = args.action_id.strip()
    body = c.get(f"/api/prompt-tap/actions/{aid}/summary")
    detail = body.get("action")
    if detail is None:
        raise CliError(f"no such action {args.action_id}.", EXIT_USAGE)
    if emit.as_json:
        emit.json({"ok": True, "bodies_withheld": True, "action": detail})
        return 0
    _render_action_summary(detail, emit)
    return 0


def _render_action_summary(detail, emit) -> None:
    """Human render of a bodies-withheld action summary (shared local + remote)."""
    where = detail.get("project_name") or (
        "project #{}".format(detail["project_id"])
        if detail.get("project_id") is not None
        else "ad-hoc"
    )
    if detail.get("document_name"):
        where += " / " + detail["document_name"]
    emit.say("action {}  [{}]  {}".format(detail["id"], detail.get("status") or "", where))
    emit.say("  (bodies withheld - summary only; raw call payloads are host-only)")
    # L5 (gate round 1): surface capture_withheld too. bodies_withheld (always true here) is
    # the deliberate summary-mode omission; capture_withheld is DIFFERENT - the disk-budget
    # backstop DROPPED raw calls for this action, so its per-run counts may UNDER-report. A
    # reader must see that the counts are partial, not mistake them for complete.
    if detail.get("capture_withheld") or (detail.get("totals") or {}).get("capture_withheld"):
        emit.say(
            "  (capture withheld - the disk-budget backstop dropped some raw calls; "
            "the counts below may under-report)"
        )
    rows = []
    for run in detail.get("runs") or []:
        cs = run.get("calls_summary") or {}
        fins = ",".join(f"{k}:{v}" for k, v in sorted((cs.get("finish_reasons") or {}).items()))
        errs = ",".join(f"{k}:{v}" for k, v in sorted((cs.get("error_classes") or {}).items()))
        spend = cs.get("spend_usd")
        rows.append(
            (
                run.get("target_locale") or "-",
                run.get("status") or "",
                str(cs.get("calls", 0)),
                str(cs.get("failures", 0)),
                fins or "-",
                errs or "-",
                (f"${spend:.6f}") if spend is not None else "-",
            )
        )
    if rows:
        emit.table(
            rows, headers=("locale", "status", "calls", "fails", "finish", "errors", "spend")
        )
    else:
        emit.say("  (no runs recorded)")


def _show_action(args, emit) -> int:
    """Show one action's envelope + runs (+ calls with --calls). Same tree the web
    show-action / Runs section renders (tap_view.action_detail).

    run-190 Item C: `--summary` gives the BODIES-WITHHELD view and works over `--remote`
    (GET /api/prompt-tap/actions/{id}/summary). `--calls` (raw bodies) stays HOST-ONLY and
    is refused over --remote by _tap_gate; the two flags are mutually exclusive."""
    summary = bool(getattr(args, "summary", False))
    if summary and getattr(args, "calls", False):
        raise CliError(
            "--summary and --calls are mutually exclusive (bodies are host-only).", EXIT_USAGE
        )
    if summary and remote_mode(args):
        return _remote_show_action(args, emit)
    _tap_gate(args, emit)
    from app.kernel import tap_view

    if summary:
        detail = tap_view.action_summary(args.action_id.strip())
        if detail is None:
            raise CliError(
                f"no such action {args.action_id} (it may have been purged).", EXIT_USAGE
            )
        if emit.as_json:
            emit.json({"ok": True, "bodies_withheld": True, "action": detail})
            return 0
        _render_action_summary(detail, emit)
        return 0

    detail = tap_view.action_detail(args.action_id.strip())
    if detail is None:
        raise CliError(
            f"no such action {args.action_id} (it may have been purged).",
            EXIT_USAGE,  # noqa: RUF100, UP032
        )
    if emit.as_json or args.calls:
        emit.json({"ok": True, "action": _strip_record_keys(detail)})
        return 0
    tot = detail.get("totals") or {}
    where = detail.get("project_name") or (
        "project #{}".format(detail["project_id"])
        if detail.get("project_id") is not None
        else "ad-hoc"
    )
    if detail.get("document_name"):
        where += " / " + detail["document_name"]
    emit.say("action {}  [{}]  {}".format(detail["id"], detail.get("status") or "", where))
    locs = ",".join(detail.get("requested_locales") or []) or "-"
    emit.say(
        "  {} -> {}   kind={}{}".format(
            detail.get("source_locale") or "?",
            locs,
            detail.get("kind") or "",
            ("  batch #{}".format(detail["batch_id"]))
            if detail.get("batch_id") is not None
            else "",
        )
    )
    if detail.get("workflow_name"):
        emit.say("  workflow: {}".format(detail["workflow_name"]))
    if tot:
        _human = tot.get("human_spend_usd") or 0.0
        _human_txt = (
            ("; human ${:.2f} ({})".format(_human, tot.get("human_spend_source", "")))
            if _human
            else ""
        )
        emit.say(
            "  totals: {} call(s), {} failed, AI spend ${}{}".format(
                tot.get("calls", "-"),
                tot.get("failures", 0),
                ("{:.6f}".format(tot["spend_usd"])) if tot.get("spend_usd") is not None else "-",
                _human_txt,
            )
        )
        # Honesty markers (N8/N9), matching the web badges.
        marks = []
        if tot.get("spend_source") == "estimated_from_usage":
            marks.append("spend estimated from usage")
        if tot.get("capture_withheld"):
            marks.append("capture withheld (totals may under-count)")
        if tot.get("enrichment_truncated"):
            marks.append("enrichment truncated")
        if marks:
            emit.say("  note: {}".format("; ".join(marks)))
    emit.say("")
    rows = []
    for run in detail.get("runs") or []:
        summ = run.get("summary") or {}
        spend = summ.get("spend_usd")
        rows.append(
            (
                run.get("target_locale") or "-",
                run.get("status") or "",
                str(summ.get("calls", "-")),
                str(summ.get("failures", 0)),
                (f"${spend:.6f}") if spend is not None else "-",
            )
        )
    if rows:
        emit.table(rows, headers=("locale", "status", "calls", "failed", "spend"))
    else:
        emit.say("  (no runs recorded)")
    # Wave C: the vendor dispatch legs on this action's runs (host only, never the token).
    vlegs = detail.get("vendor_legs") or []
    if vlegs:
        emit.say("")
        emit.say("  vendor legs:")
        vrows = [
            (
                ("#{}".format(lg["id"])),
                str(lg.get("vendor") or ""),
                lg.get("target_locale") or "-",
                lg.get("status") or "",
                "{}/{}".format(lg.get("returned_ids", 0), lg.get("dispatched_ids", 0)),
                ("$%.2f" % (lg.get("estimate_cost_usd") or 0)),
                (
                    "-"
                    if lg.get("actual_cost_usd") is None
                    else "${:.2f}".format(lg["actual_cost_usd"])
                ),
                lg.get("endpoint_host") or "-",
            )
            for lg in vlegs
        ]
        emit.table(
            vrows,
            headers=("leg", "vendor", "locale", "status", "returned", "est", "actual", "host"),
        )
    emit.say("")
    emit.say("(use --calls or --json for the nested call payloads)")
    return 0


# --- ROADMAP #6: the RUN-first view (`runs` / `run <id>`) ---------------------
_RUNS_HEADERS = (
    "run",
    "kind",
    "calls",
    "fails",
    "engines",
    "spend",
    "tokens",
    "started",
)


def _runs_rows(runs):
    """Table rows for a list of bodies-withheld run summaries (shared local + remote)."""
    rows = []
    for r in runs:
        spend = r.get("spend_usd")
        eng = ",".join(r.get("engines") or []) or "-"
        rows.append(
            (
                str(r.get("run_id") or "")[:26],
                r.get("kind") or "",
                str(r.get("calls", 0)),
                str(r.get("failures", 0)),
                eng if len(eng) <= 28 else eng[:25] + "...",
                (f"${spend:.6f}") if spend is not None else "-",
                "{}/{}".format(r.get("tokens_in", 0), r.get("tokens_out", 0)),
                (r.get("first_ts") or "")[:19].replace("T", " "),
            )
        )
    return rows


def _norm_run_date(label, value):
    """L2 (gate round 2): validate + normalise a --since/--until date for the LOCAL arm. A
    malformed date is a usage error (exit 2), matching the --remote arm's 400; an accepted date
    is returned canonical (yyyy-mm-dd) so the service's lexical `ts` comparison is correct rather
    than a silent 'no runs'/'no filter'. None for an absent value. tap_view._parse_date is the
    shared validator the /api route uses."""
    if not value:
        return None
    from app.kernel import tap_view

    d = tap_view._parse_date(value)
    if d is None:
        raise CliError(f"--{label} must be a yyyy-mm-dd date.", EXIT_USAGE)
    return d.date().isoformat()


def _runs(args, emit) -> int:
    """List translation runs newest-first (the run-first view). Bodies-withheld, so it works
    both host-local and over `--remote` (GET /api/prompt-tap/runs) with no extra flag."""
    if remote_mode(args):
        return _remote_runs(args, emit)
    _tap_gate(args, emit)
    from app.kernel import tap_runs

    # M2 (gate round 1): pass the #73 operator scope, not a bare None. _tap_gate refuses
    # --org (a capture has no org), so this is access.UNSCOPED for the key-verified --operator
    # door (see-all) or a plain None otherwise -- which the converged list_runs treats as a
    # no-op single-tenant OR fail-closed under an active gate, never a blind bare-None block.
    page = tap_runs.list_runs_page(
        limit=max(1, args.limit),
        cursor=getattr(args, "cursor", None),
        since=_norm_run_date("since", args.since),
        until=_norm_run_date("until", args.until),
        has_failures=bool(args.failures),
        org_id=op_org_scope(args),
    )
    return _emit_runs_page(page, emit)


def _remote_runs(args, emit) -> int:
    """`prompt-tap runs --remote`: the bodies-withheld run list over /api/prompt-tap/runs."""
    c = remote_client(args)
    params = {"limit": max(1, args.limit)}
    for k, v in (
        ("since", args.since),
        ("until", args.until),
        ("cursor", getattr(args, "cursor", None)),
    ):
        if v is not None:
            params[k] = v
    if args.failures:
        params["failures"] = "1"
    body = c.get("/api/prompt-tap/runs", params=params)
    # L2 (gate round 1): carry the API's truncated + next_cursor through -- the old CLI dropped
    # them, so `--remote --limit 600` reported 500 runs as if that were all of them.
    page = {
        "runs": body.get("runs", []),
        "truncated": bool(body.get("truncated")),
        "next_cursor": body.get("next_cursor"),
    }
    return _emit_runs_page(page, emit, remote=True)


def _emit_runs_page(page, emit, *, remote=False) -> int:
    """Render a run-list page (shared local + remote): --json carries truncated + next_cursor;
    the human table appends a 'more runs' line with the cursor to page on."""
    runs = page.get("runs") or []
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "count": len(runs),
                "truncated": bool(page.get("truncated")),
                "next_cursor": page.get("next_cursor"),
                "bodies_withheld": True,
                "runs": runs,
            }
        )
        return 0
    if not runs:
        emit.warn("no translation runs recorded yet (run a workflow or batch, then look here).")
        return 0
    emit.table(_runs_rows(runs), headers=_RUNS_HEADERS)
    emit.say("")
    tail = (
        "(bodies withheld over --remote; raw call payloads are host-only)"
        if remote
        else ("(bodies withheld; use `prompt-tap run <id>` for one run's calls)")
    )
    emit.say(tail)
    if page.get("truncated") and page.get("next_cursor"):
        emit.say("(more runs -- re-run with --cursor {})".format(page["next_cursor"]))
    return 0


def _render_run_detail(detail, emit) -> None:
    """Human render of a run's step -> unit -> call tree (bodies-withheld digests)."""
    spend = detail.get("spend_usd")
    emit.say(
        "run {}  [{}]  {} call(s), {} failed{}".format(
            detail.get("run_id") or "",
            detail.get("kind") or "",
            detail.get("calls", 0),
            detail.get("failures", 0),
            (f"  ${spend:.6f}") if spend is not None else "",
        )
    )
    eng = ",".join(detail.get("engines") or []) or "-"
    emit.say(f"  engines: {eng}")
    if detail.get("bodies_withheld"):
        emit.say("  (bodies withheld - lengths + sha256 + head only; use --bodies on the host)")
    for step in detail.get("steps") or []:
        emit.say("")
        emit.say("  step: {}".format(step.get("step") if step.get("step") is not None else "-"))
        for unit in step.get("units") or []:
            uk = unit.get("unit")
            emit.say(
                "    unit {} ({} call(s)):".format(
                    uk if uk is not None else "-", unit.get("call_count", 0)
                )
            )
            rows = []
            for call in unit.get("calls") or []:
                prm = call.get("prompt") or {}
                rsp = call.get("response") or {}
                rows.append(
                    (
                        (call.get("ts") or "")[:19].replace("T", " "),
                        call.get("outcome") or "",
                        call.get("purpose") or "-",
                        call.get("round") if call.get("round") is not None else "-",
                        (call.get("provider") or "")
                        + (("/" + call["model"]) if call.get("model") else ""),
                        "p{}/r{}".format(prm.get("chars", 0), rsp.get("chars", 0)),
                        (call.get("finish_reason") or "-"),
                    )
                )
            if rows:
                emit.table(
                    rows,
                    headers=(
                        "ts",
                        "outcome",
                        "purpose",
                        "round",
                        "provider/model",
                        "p/r chars",
                        "finish",
                    ),
                )
            if unit.get("more"):
                emit.say("      (+{} more calls not shown)".format(unit["more"]))


def _run(args, emit) -> int:
    """Show one run's calls grouped by step then unit. Bodies withheld by default; `--bodies`
    adds the full payloads and is HOST-ONLY (refused over --remote), matching `show-action
    --calls`. Over --remote (no --bodies) it reads the bodies-withheld
    GET /api/prompt-tap/runs/{id}."""
    bodies = bool(getattr(args, "bodies", False))
    if remote_mode(args):
        if bodies:
            raise CliError(
                "--bodies is host-only (the raw payloads never leave the host); "
                "omit it to read the bodies-withheld view over --remote.",
                EXIT_USAGE,
            )
        return _remote_run(args, emit)
    _tap_gate(args, emit)
    from app.kernel import tap_runs

    # M2 (gate round 1): the #73 operator scope, not a bare None (see _runs above).
    # L1 (gate round 1): include_keys=False on the CLI -- the human render never shows the
    # per-call download key, and the --json emit must stay byte-identical to the --remote arm
    # (which gets no key from /api). The web page is the only surface that needs the key.
    detail = tap_runs.run_detail(
        args.run_id.strip(), bodies=bodies, include_keys=False, org_id=op_org_scope(args)
    )
    if detail is None:
        raise CliError(f"no such run {args.run_id} (it may have been swept).", EXIT_USAGE)
    if emit.as_json or bodies:
        emit.json({"ok": True, "bodies_withheld": not bodies, "run": _strip_record_keys(detail)})
        return 0
    _render_run_detail(detail, emit)
    return 0


def _remote_run(args, emit) -> int:
    """`prompt-tap run <id> --remote`: one run's bodies-withheld detail over /api."""
    c = remote_client(args)
    rid = args.run_id.strip()
    body = c.get(f"/api/prompt-tap/runs/{rid}")
    detail = body.get("run")
    if detail is None:
        raise CliError(f"no such run {args.run_id}.", EXIT_USAGE)
    if emit.as_json:
        emit.json({"ok": True, "bodies_withheld": True, "run": detail})
        return 0
    _render_run_detail(detail, emit)
    return 0


__all__ = [
    "_RUNS_HEADERS",
    "_SUMMARY_HEADERS",
    "_action_flags",
    "_actions",
    "_emit_runs_page",
    "_norm_run_date",
    "_register_actions",
    "_register_run",
    "_register_runs",
    "_register_show_action",
    "_remote_actions",
    "_remote_run",
    "_remote_runs",
    "_remote_show_action",
    "_render_action_summary",
    "_render_run_detail",
    "_run",
    "_runs",
    "_runs_rows",
    "_show_action",
    "_summary_action_rows",
    "register_prompt_tap_verify_commands",
]
