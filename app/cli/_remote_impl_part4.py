"""_remote_impl: part 4 (layered pure move)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import time

from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _branch_label,
    _opts_label,
    _prompt_snippet,
    render_package_view,
)
from app.cli._remote_impl_common import (
    _POLL_CEILING_S,
    _POLL_INTERVAL_S,
    _add_step_body,
    _client,
    _edit_step_body,
    _poll_run,
    _write_bytes,
)


def connector_sync(args, emit) -> int:
    """--remote `connector sync`. Uses the route's deduplicated ASYNC mode (#530): a
    synchronous POST outlives the client timeout on any real base, and a retry after
    that timeout starts a second paid run. The server de-dupes against a live run for
    the same connection, and this polls the job to its end."""
    c = _client(args)
    queued = c.post(
        f"/api/connectors/{args.connection_id}/sync", json={"async": True}, idempotent=True
    )
    if "job_id" not in queued:
        # An older server without async mode answers the finished run directly.
        body = queued
    else:
        job_id = queued["job_id"]
        if queued.get("existing"):
            emit.say(
                f"A sync for connection #{args.connection_id} is already running (job #{job_id})."
            )
        deadline = time.time() + _POLL_CEILING_S
        while True:
            job = c.get(f"/api/jobs/{job_id}")
            if str(job.get("status") or "").lower() in ("completed", "failed"):
                break
            if time.time() >= deadline:
                emit.result(
                    f"Sync job #{job_id} is still running on the server (ceiling reached). "
                    f"Check it with: job status {job_id}",
                    {
                        "ok": False,
                        "connection_id": args.connection_id,
                        "job_id": job_id,
                        "status": job.get("status"),
                        "still_running": True,
                    },
                )
                return 1
            time.sleep(_POLL_INTERVAL_S)
        body = dict(job.get("result") or {})
        failed = str(job.get("status") or "").lower() == "failed"
        body["ok"] = bool(body.get("ok", not failed)) and not failed
        if not body.get("message"):
            body["message"] = job.get("error") or job.get("message")
    ok = body.get("ok", False)
    summary = {k: v for k, v in body.items() if k not in ("ok", "message")}
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {body.get('message')}",
        {
            "ok": ok,
            "connection_id": args.connection_id,
            "message": body.get("message"),
            "summary": summary,
        },
    )
    return 0 if ok else 1


def connector_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/connectors")
    conns = body.get("connections", [])
    # --kind: the route has no ?kind= server filter, so narrow client-side (the rows
    # carry `kind`). R-MED-9 -- was parsed (argparse choices=_KINDS) then dropped, so a
    # remote `connector list --kind jira` listed EVERY kind. Parity with _list's
    # store.list_connections(kind=args.kind).
    kind = (getattr(args, "kind", None) or "").strip()
    if kind:
        conns = [x for x in conns if x.get("kind") == kind]
    if emit.as_json:
        emit.json({"ok": True, "connections": conns})
    else:
        # Issue #17: the human table hardcoded '' for auth (the server never sent
        # it) -- now that /api/connectors carries it, show the real value like the
        # in-process `connector list` table does.
        emit.table(
            [(x["id"], x["kind"], x["name"], x["status"], x.get("auth") or "") for x in conns],
            headers=("id", "kind", "name", "status", "auth"),
        )
    return 0


# ============================================================================
# report (R2-1)
# ============================================================================
def report_run(args, emit) -> int:
    c = _client(args)
    params = {"kind": args.kind, "group_by": args.group_by}
    if args.date_from:
        params["from"] = args.date_from
    if args.date_to:
        params["to"] = args.date_to
    if args.customer_id is not None:
        params["customer_id"] = args.customer_id
    if args.project_id is not None:
        params["project_id"] = args.project_id
    if args.provider:
        params["provider"] = args.provider
    # Always fetch the JSON report so the emitted envelope (rows/totals/CAVEATS, + the
    # human "Note:" lines) matches the in-process arm whether or not -o was given
    # (R-MED-7: the old --out path emitted only {csv_path,bytes} and dropped the report
    # + caveats entirely, and the human path never printed caveats).
    body = c.get("/api/reports/run", params=params)
    csv_path = None
    if args.out:
        # Fetch the CSV separately so the written file is byte-identical to the server's
        # to_csv (the same report the in-process arm writes). _write_bytes refuses an
        # existing path (EXIT_USAGE), matching the local _write_out guard.
        resp = c.download("/api/reports/run", params=dict(params, format="csv"))
        _write_bytes(args.out, resp.content, force=False)
        csv_path = args.out
    caveats = body.get("caveats") or []
    if emit.as_json:
        out = {"ok": True, **body}
        if csv_path:
            out["csv_path"] = csv_path
        emit.json(out)
        return 0
    columns = body.get("columns", [])
    headers = tuple(col.replace("_", " ") for col in columns)
    rows = [tuple(r.get(col, "") for col in columns) for r in body.get("rows", [])]
    totals = body.get("totals") or {}
    if body.get("rows"):
        rows.append(tuple(totals.get(col, "") for col in columns))
    emit.table(rows or [tuple("" for _ in columns)], headers=headers)
    for caveat in caveats:
        emit.say(f"Note: {caveat}")
    if csv_path:
        emit.say(f"Wrote CSV -> {csv_path}.")
    return 0


# ============================================================================
# workflow
# ============================================================================
def workflow_list(args, emit) -> int:
    from app.kernel import workflow_shapes as wshapes

    c = _client(args)
    # --archived (active|all|only): forward as ?archived= (the route honours only/all;
    # anything else, incl. "active", is active-only). Parity with _workflow_list.
    arch = (getattr(args, "archived", "active") or "active").strip().lower()
    path = "/api/workflows"
    if arch in ("all", "only"):
        path += f"?archived={arch}"
    body = c.get(path)
    wfs = body.get("workflows", [])
    if emit.as_json:
        emit.json({"ok": True, "workflows": wfs})
    else:
        # Phase 3 + Wave A: mirror the in-process `workflow list` human table exactly
        # (shape + the lean runs/last-run stats columns) so --remote never shows less
        # than the local path. BUG-09(5): last-run parity too -- the explicit " UTC"
        # label (see cmd_workflow._last_run_utc_label), not a bare ambiguous date slice.
        from app.cli.cmd_workflow import _last_run_utc_label

        emit.table(
            [
                (
                    w["id"],
                    w["name"],
                    w["scope"],
                    w["steps"],
                    wshapes.SHAPE_LABELS.get(w.get("shape"), w.get("shape") or "-"),
                    w.get("runs_total", 0),
                    _last_run_utc_label(w.get("last_run_at")),
                )
                for w in wfs
            ],
            headers=("id", "name", "scope", "steps", "shape", "runs", "last run"),
        )
    return 0


def workflow_show(args, emit) -> int:
    from app.kernel import workflow_shapes as wshapes

    c = _client(args)
    body = c.get(f"/api/workflows/{args.workflow_id}")
    steps = body.get("steps", [])
    if emit.as_json:
        emit.json({"ok": True, **body})
    else:
        # Phase 3: mirror the local `workflow show` header (shape + origin).
        shape_lbl = wshapes.SHAPE_LABELS.get(body.get("shape"), body.get("shape") or "-")
        extra = ", ".join(
            wshapes.SHAPE_LABELS.get(tag, tag)
            for tag in sorted(body.get("shape_tags", []))
            if tag != body.get("shape")
        )
        if extra:
            shape_lbl += f" (+{extra})"
        origin = wshapes.origin_label(body.get("created_via")) or "unknown"
        emit.say(
            f"Workflow #{body['id']}: {body['name']} (scope={body['scope']}, "
            f"{len(steps)} step(s), shape={shape_lbl}, origin={origin})"
        )
        # Issue #19: mirror the local `workflow show` human table exactly (glossary
        # name, qe-gate, engine-layer opts, branch, prompt snippet) so --remote never
        # shows less than the in-process path.
        emit.table(
            [
                (
                    s["index"],
                    s["id"],
                    s.get("key") or "",
                    s["name"] or "",
                    s["type"],
                    s["provider"] or "",
                    s.get("glossary_name")
                    or (str(s["glossary_id"]) if s.get("glossary_id") else "-"),
                    (
                        f"on({s.get('qe_threshold')})"
                        + (
                            "+fix"
                            if s.get("qe_autofix_status") == "auto-fix on"
                            else " " + s.get("qe_autofix_status", "auto-fix status unavailable")
                            if s.get("qe_autofix")
                            else ""
                        )
                        if s.get("qe_gate")
                        else s.get("qe_autofix_status", "auto-fix cannot run: the QE gate is off")
                        if s.get("qe_autofix")
                        else "-"
                    ),
                    _opts_label(s),
                    _branch_label(s.get("branch")),
                    _prompt_snippet(s.get("prompt_text"), s.get("prompt_template_id")),
                )
                for s in steps
            ],
            headers=(
                "idx",
                "id",
                "key",
                "name",
                "type",
                "provider",
                "glossary",
                "qe",
                "opts",
                "branch",
                "prompt",
            ),
        )
        # B0: the same per-step PACKAGE/VERIFY block as the in-process arm (the api
        # response's step rows already carry package/verify -- single-sourced).
        render_package_view(emit, steps)
    return 0


def _poll_batch(c, batch_id, interval=None, ceiling=None):
    """Poll GET /api/batches/{id} until the batch is finalized (status leaves 'running'
    -- batch_status reads RUNNING until finished_at is stamped, then the terminal rollup)
    or the ceiling is hit. Returns (body, timed_out). interval/ceiling default to the
    module constants, read at call time."""
    interval = _POLL_INTERVAL_S if interval is None else interval
    deadline = time.time() + (_POLL_CEILING_S if ceiling is None else ceiling)
    while True:
        body = c.get(f"/api/batches/{batch_id}")
        if str(body.get("status") or "").lower() != "running":
            return body, False
        if time.time() >= deadline:
            return body, True
        time.sleep(interval)


# --- workflow AUTHORING over --remote (SPEC-workflow-authoring-api) -----------
# Thin HTTP twins of `workflow create / add-step / edit-step / delete-step /
# move-step`. Each packages the argparse flags into the /api route's JSON body and
# emits the SAME --json payload the in-process arm does (the routes were built to
# return exactly that shape), so `--json` is byte-identical across surfaces; the
# human line is the local arm's, reconstructed from the response.
def workflow_create(args, emit) -> int:
    c = _client(args)
    body = {"name": args.name, "scope": getattr(args, "scope", "global")}
    if str(getattr(args, "customer_id", "") or "").strip():
        body["customer_id"] = args.customer_id
    resp = c.post("/api/workflows", json=body)
    emit.result(
        f"Created workflow #{resp['id']}: {resp['name']} (scope={resp['scope']}).",
        {
            "ok": True,
            "id": resp["id"],
            "name": resp["name"],
            "scope": resp["scope"],
            "customer_id": resp.get("customer_id"),
        },
    )
    return 0


def workflow_add_step(args, emit) -> int:
    c = _client(args)
    resp = c.post(f"/api/workflows/{args.workflow_id}/steps", json=_add_step_body(args))
    text = (
        f"Added step #{resp['id']} ({resp['step_type']}, index "
        f"{resp['order_index']}) to workflow #{args.workflow_id}."
    )
    warnings = resp.get("warnings") or []
    if warnings:
        text += "".join("\nwarning: " + warning for warning in warnings)
    emit.result(text, {"ok": True, **resp})
    return 0


def workflow_edit_step(args, emit) -> int:
    c = _client(args)
    resp = c.post(
        f"/api/workflows/{args.workflow_id}/steps/{args.step_id}/edit", json=_edit_step_body(args)
    )
    # Pure pin/unpin path: the route returns just {id, workflow_id, overridden}.
    if "order_index" not in resp:
        verb = "Pinned" if resp.get("overridden") else "Unpinned"
        emit.result(
            f"{verb} step #{resp['id']} in workflow #{args.workflow_id}.", {"ok": True, **resp}
        )
        return 0
    lines = [
        f"Edited step #{resp['id']} ({resp['step_type']}, index "
        f"{resp['order_index']}) in workflow #{args.workflow_id}. Changed: "
        f"{', '.join(resp.get('changed') or [])}."
    ]
    prop = resp.get("propagation") or {}
    if prop.get("propagated"):
        lines.append("Propagated to: {}.".format(", ".join(prop["propagated"])))
    if resp.get("auto_pinned"):
        lines.append("This step is now pinned to this variant (a direct edit).")
    warnings = resp.get("warnings") or []
    text = " ".join(lines)
    if warnings:
        text += "".join("\nwarning: " + warning for warning in warnings)
    emit.result(text, {"ok": True, **resp})
    return 0


def workflow_delete_step(args, emit) -> int:
    c = _client(args)
    resp = c.post(f"/api/workflows/{args.workflow_id}/steps/{args.step_id}/delete", json={})
    emit.result(
        f"Deleted step #{args.step_id} from workflow #{args.workflow_id}.", {"ok": True, **resp}
    )
    return 0


def workflow_move_step(args, emit) -> int:
    c = _client(args)
    body: dict = {}
    if getattr(args, "to_position", None) is not None:
        body["position"] = args.to_position
    elif getattr(args, "before_ref", None) is not None:
        body["before"] = args.before_ref
    elif getattr(args, "after_ref", None) is not None:
        body["after"] = args.after_ref
    if getattr(args, "clear_parallel_group", False):
        body["clear_parallel_group"] = True
    elif getattr(args, "parallel_group", None) is not None:
        body["parallel_group"] = args.parallel_group
    resp = c.post(f"/api/workflows/{args.workflow_id}/steps/{args.step_id}/move", json=body)
    order = resp.get("order") or []
    listing = ", ".join(
        "%d:%s" % (r["position"], r.get("key") or r.get("name") or ("#%d" % r["id"]))  # noqa: UP031
        for r in order  # noqa: RUF100, UP031
    )
    if resp.get("noop"):
        text = (
            "Step #%d is already at position %d in workflow #%d; nothing changed. "  # noqa: UP031
            "Order is: %s" % (resp["step_id"], resp["position"], args.workflow_id, listing)
        )
    else:
        text = "Moved step #%d to position %d in workflow #%d. Order is now: %s" % (  # noqa: UP031
            resp["step_id"],
            resp["position"],
            args.workflow_id,
            listing,
        )
    if resp.get("tm_first_note"):
        text += " " + resp["tm_first_note"]
    emit.result(text, {"ok": True, **resp})
    return 0


def workflow_batch_show(args, emit) -> int:
    """SPEC Phase 5b: read a batch's locale x steps matrix over /api (detail=steps)."""
    c = _client(args)
    body = c.get(f"/api/batches/{args.batch_id}?detail=steps")
    if emit.as_json:
        emit.json({"ok": True, **body})
        return 0
    from app.cli.cmd_workflow import batch_matrix_ascii

    emit.say(batch_matrix_ascii(body))
    return 0


def workflow_show_run(args, emit) -> int:
    """Issue #23 (a): read a paused run's review text + segment boundaries over /api."""
    from app.kernel import run_overlay

    c = _client(args)
    # --visits (CLI-parity backlog item 3): mirrors doc_show's ?detail=segments
    # convention -- an explicit, additive query param so the default /api payload
    # stays byte-identical without it.
    path = f"/api/runs/{args.run_id}"
    qs = []
    if getattr(args, "visits", False):
        qs.append("visits=1")
    if getattr(args, "findings", False):
        qs.append("findings=1")
    if qs:
        path += "?" + "&".join(qs)
    body = c.get(path)
    # The API carries the per-step overlay (step_states/orphan_steps) in BOTH the paused and
    # non-paused branches; render it identically to the in-process arm for CLI parity.
    states_block = run_overlay.format_step_states_ascii(body)
    visits_block = (
        run_overlay.format_visit_history_ascii(body.get("visit_history"))
        if getattr(args, "visits", False)
        else ""
    )
    # L4 (gate round 1): render the failure-shape block in the --remote human output too,
    # IDENTICALLY to the in-process arm (the API attaches failure_shape by default). Empty
    # string on a clean run.
    failure_block = run_overlay.format_failure_shape_ascii(body.get("failure_shape"))
    # #101 / #106: the in-step progress + terminal-reason lines, rendered through the SAME
    # run_overlay formatters the in-process arm uses, from the fields the /api envelope now
    # carries in BOTH its paused and non-paused branches. Empty when the run has neither.
    progress_block = run_overlay.format_progress_ascii(body.get("progress"))
    reason_block = run_overlay.format_reason_ascii(body.get("reason"))
    notes = body.get("notes")
    notes_block = f"  note: {notes}" if notes else ""
    findings = body.get("lqa_findings") if getattr(args, "findings", False) else None
    # Mirror the local view, including unverified offer counts.
    run_warnings = body.get("warnings") or []
    warn_block = "".join("\n  ! " + (w.get("message", "") or "") for w in run_warnings)
    if body.get("tm_offers"):
        warn_block += "\n  " + body["tm_offers"]["message"]
    warn_block += run_overlay.format_tm_summary_ascii(body.get("tm_match"))
    if not body.get("paused"):
        line = f"Run #{args.run_id}: status={body.get('status')} (no paused human-review step)."
        if reason_block:
            line += "\n" + reason_block
        if progress_block:
            line += "\n" + progress_block
        if notes_block:
            line += "\n" + notes_block
        if warn_block:
            line += warn_block
        if states_block:
            line += "\n" + states_block
        if failure_block:
            line += "\n" + failure_block
        if visits_block:
            line += "\n" + visits_block
        if getattr(args, "findings", False):
            line += "\n" + (findings or "(no LQA findings recorded for this run.)")
        emit.result(line, {"ok": True, **body})
        return 0
    if emit.as_json:
        emit.json({"ok": True, **body})
        return 0
    ps = body.get("paused_step") or {}
    segs = body.get("segments") or []
    emit.say(f"Run #{args.run_id}: status={body.get('status')} (PAUSED for human review)")
    if reason_block:
        emit.say(reason_block)
    if progress_block:
        emit.say(progress_block)
    if notes_block:
        emit.say(notes_block)
    if warn_block:
        emit.say(warn_block.lstrip("\n"))
    emit.say(
        f"  paused step: index={ps.get('index')} type={ps.get('type')}"
        f"{' key=' + ps['key'] if ps.get('key') else ''}"
        f"{' name=' + ps['name'] if ps.get('name') else ''}"
    )
    emit.say(f"  {body.get('segment_count', len(segs))} segment(s) to review:")
    for i, seg in enumerate(segs):
        emit.say(f"  [{i}] {seg}")
    if states_block:
        emit.say(states_block)
    if failure_block:
        emit.say(failure_block)
    if visits_block:
        emit.say(visits_block)
    if getattr(args, "findings", False):
        emit.say(findings or "(no LQA findings recorded for this run.)")
    emit.say("")
    emit.say("Edit individual segments and resume safely with:")
    emit.say(
        f"  python -m app.cli --remote {args.remote} --token ... workflow resume "
        f"{args.run_id} --edited-segments <json-file> "
        f"--locale {body.get('target_locale') or '<locale>'}"
    )
    return 0


def workflow_step_diff(args, emit) -> int:
    """GitHub #340: the per-segment word diff of two step runs over /api. A missing
    run (or one in another org) is the route's uniform 404 -> the http layer's
    clean CliError, same as every other run read here."""
    c = _client(args)
    path = f"/api/runs/{args.run_id}/step-diff"
    qs = []
    if getattr(args, "step_from", None) is not None:
        qs.append("from=" + str(args.step_from))
    if getattr(args, "step_to", None) is not None:
        qs.append("to=" + str(args.step_to))
    if qs:
        path += "?" + "&".join(qs)
    body = c.get(path)
    if emit.as_json:
        emit.json({"ok": True, **body})
        return 0
    from app.cli.cmd_workflow_common_leaf import _wf_step_diff_text

    emit.result(_wf_step_diff_text(body), {"ok": True, **body})
    return 0


def workflow_resume(args, emit) -> int:
    c = _client(args)
    payload = {"edited_text": args.edited_text, "locale": args.locale}
    # Issue #23 (b): the structured, delimiter-free path. Read the JSON file locally
    # and send the array; the server count-validates BEFORE any state change.
    seg_path = (getattr(args, "edited_segments", "") or "").strip()
    if seg_path:
        # Surface parity with the in-process resume: the two edit paths are mutually
        # exclusive (a stray --edited-text would be silently dropped server-side).
        if (args.edited_text or "").strip():
            raise CliError("pass either --edited-text or --edited-segments, not both.", EXIT_USAGE)
        from . import cmd_workflow

        payload["edited_segments"] = cmd_workflow._load_edited_segments(seg_path)
    # LAUNCH-AND-POLL (AUDIT follow-up to Q2): the /resume route now advances past the human
    # step and continues the run ASYNC (it no longer runs the remainder of the workflow inline
    # on the server's event loop). A validation refusal (segment-count mismatch / bad payload)
    # is a non-launch 4xx the http layer already turns into a clean CliError before we get
    # here; a 200 is the launch ack. POST to get the run id, then poll it to a terminal/paused
    # state and print the SAME human line the in-process arm prints.
    launched = c.post(f"/api/runs/{args.run_id}/resume", json=payload)
    run_id = launched.get("run_id") or args.run_id
    emit.say(f"Run #{run_id} resumed; polling to completion...")
    body, timed_out = _poll_run(c, run_id)
    if timed_out:
        emit.result(
            f"Run #{run_id} still running (ceiling reached). Poll with:\n"
            f"  python -m app.cli --remote {args.remote} --token ... workflow show-run "
            f"{run_id}",
            {"ok": True, "run_id": run_id, "status": "running", "timed_out": True},
        )
        return 0
    status = str(body.get("status") or "").lower()
    n_steps = len(body.get("visit_history") or body.get("step_states") or [])
    if status == "paused":
        step_idx = (body.get("paused_step") or {}).get("index")
        emit.result(
            f"Run #{run_id} PAUSED again at human-review step {step_idx} ({n_steps} step(s) done).",
            {
                "ok": True,
                "run_id": run_id,
                "status": "paused",
                "paused_step": step_idx,
                "steps": n_steps,
            },
        )
        return 0
    if status in ("failed", "cancelled", "rejected"):
        emit.result(
            f"Run #{run_id} {status.upper()} ({n_steps} step(s) done).",
            {"ok": False, "run_id": run_id, "status": status, "steps": n_steps},
        )
        return 1
    emit.result(
        f"Run #{run_id} completed: {n_steps} step(s).",
        {"ok": True, "run_id": run_id, "status": status, "steps": n_steps},
    )
    return 0


def workflow_cancel_run(args, emit) -> int:
    """--remote arm of `workflow cancel-run`: POST /api/runs/{id}/cancel (can_write).
    An already-terminal run answers 409 -> the http layer raises a clean CliError."""
    c = _client(args)
    body = c.post(f"/api/runs/{args.run_id}/cancel", json={})
    emit.result(
        f"Run #{args.run_id} CANCELLED ({body.get('legs_closed', 0)} open vendor leg(s) "
        f"closed). It is terminal and cannot be resumed; a new run can be launched.",
        {"ok": True, **body},
    )
    return 0
