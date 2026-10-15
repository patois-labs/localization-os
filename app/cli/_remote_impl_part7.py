"""_remote_impl: part 7 (layered pure move)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import os

from app.cli._common import (
    EXIT_USAGE,
    CliError,
)
from app.cli._remote_impl_common import (
    _client,
    _export_run_line,
    _lqa_row_line,
    _poll_run,
)
from app.cli._remote_impl_part4 import _poll_batch


def workflow_run(args, emit) -> int:
    c = _client(args)
    # Fan-out batch (Phase 5a): --all-locales / --locales LAUNCH the batch async (Q2), then
    # poll it to a terminal state. Same rollup formatting as the in-process arm.
    if getattr(args, "all_locales", False) or (getattr(args, "locales", "") or "").strip():
        if args.locale is not None:
            emit.warn(
                "give a positional locale for a single run OR --all-locales/"
                "--locales for a batch, not both."
            )
            return 2
        payload = {"doc_id": args.doc_id, "fail_fast": bool(getattr(args, "fail_fast", False))}
        if not args.all_locales:
            payload["locales"] = [x.strip() for x in (args.locales or "").split(",") if x.strip()]
        if getattr(args, "max_parallel", None) is not None:
            payload["max_parallel"] = args.max_parallel
        launched = c.post(f"/api/workflows/{args.workflow_id}/run-batch", json=payload)
        batch_id = launched.get("id")
        if batch_id is None:  # defensive: an unexpected body -> surface, don't poll null
            body = launched
        else:
            emit.say(f"Batch #{batch_id} launched; polling to completion...")
            body, timed_out = _poll_batch(c, batch_id)
            if timed_out:
                emit.result(
                    f"Batch #{batch_id} still running (ceiling reached). Poll with:\n"
                    f"  python -m app.cli --remote {args.remote} --token ... "
                    f"workflow batch show {batch_id}",
                    {
                        "ok": True,
                        "kind": "batch",
                        "id": batch_id,
                        "status": "running",
                        "timed_out": True,
                    },
                )
                return 0
        from app.cli.cmd_workflow import batch_result_human

        emit.result(batch_result_human(body), {"ok": body.get("status") != "failed", **body})
        return 1 if body.get("status") == "failed" else 0
    # Single run: LAUNCH async, then poll to a terminal state (Q2).
    launched = c.post(
        f"/api/workflows/{args.workflow_id}/run",
        json={"doc_id": args.doc_id, "locale": args.locale},
    )
    run_id = launched.get("run_id")
    if run_id is None:  # defensive: surface an unexpected body rather than poll a null id
        emit.result(str(launched), {"ok": bool(launched.get("ok")), **launched})
        return 0 if launched.get("ok") else 1
    emit.say(f"Run #{run_id} launched; polling to completion...")
    body, timed_out = _poll_run(c, run_id)
    if timed_out:
        emit.result(
            f"Run #{run_id} still running (ceiling reached). Poll with:\n"
            f"  python -m app.cli --remote {args.remote} --token ... "
            f"workflow show-run {run_id}",
            {"ok": True, "run_id": run_id, "status": "running", "timed_out": True},
        )
        return 0
    status = str(body.get("status") or "").lower()
    n_steps = len(body.get("visit_history") or body.get("step_states") or [])
    if status == "paused":
        step_idx = (body.get("paused_step") or {}).get("index")
        emit.result(
            f"Run #{run_id} PAUSED at human-review step {step_idx} "
            f"({n_steps} step(s) done). Resume with:\n"
            f"  python -m app.cli --remote {args.remote} --token ... workflow resume "
            f'{run_id} --edited-text "..." --locale {args.locale}',
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
            f"Run #{run_id} {status.upper()}.",
            {"ok": False, "run_id": run_id, "status": status, "steps": n_steps},
        )
        return 1
    emit.result(
        f"Run #{run_id} completed: {n_steps} step(s).",
        {"ok": True, "run_id": run_id, "status": status, "steps": n_steps},
    )
    return 0


def workflow_batch_rerun(args, emit) -> int:
    """SPEC Phase 5b: re-run one locale of a batch over /api. LAUNCH-AND-POLL (AUDIT
    follow-up to Q2): the /rerun route now re-dispatches ASYNC (it no longer joins the
    re-run member threads inline on the server's event loop), so this POSTs to get the
    batch id, polls it to a terminal rollup, then reads the detail matrix to render."""
    c = _client(args)
    launched = c.post(f"/api/batches/{args.batch_id}/rerun", json={"locale": args.locale})
    from app.cli.cmd_workflow import batch_matrix_ascii

    bid = launched.get("id")
    if bid is None:  # defensive: an unexpected body -> surface, don't poll a null id
        emit.result(str(launched), {"ok": bool(launched.get("ok")), **launched})
        return 0 if launched.get("ok") else 1
    emit.say(f"Batch #{bid} re-run launched; polling to completion...")
    body, timed_out = _poll_batch(c, bid)
    if timed_out:
        emit.result(
            f"Batch #{bid} still running (ceiling reached). Poll with:\n"
            f"  python -m app.cli --remote {args.remote} --token ... "
            f"workflow batch show {bid}",
            {"ok": True, "id": bid, "status": "running", "timed_out": True},
        )
        return 0
    body = c.get(f"/api/batches/{bid}?detail=steps")  # the matrix shape the poll GET omits
    emit.result(batch_matrix_ascii(body), {"ok": body.get("status") != "failed", **body})
    return 1 if body.get("status") == "failed" else 0


def query_show(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/queries/{args.id}")
    thread = body.get("thread", {})
    posts = body.get("posts", [])
    if emit.as_json:
        emit.json({"ok": True, "thread": thread, "posts": posts})
        return 0
    emit.say(
        "Query #{} [{}]  project={}  author={}  assignee={}".format(
            thread.get("id"),
            thread.get("status"),
            thread.get("project_id"),
            thread.get("author") or "-",
            thread.get("assignee_email") or "-",
        )
    )
    if thread.get("subject"):
        emit.say("Subject: {}".format(thread["subject"]))
    emit.say("")
    for p in posts:
        who = (
            p.get("author")
            or (p.get("author_label") and "{} (vendor)".format(p["author_label"]))
            or "vendor"
        )
        emit.say("  [{}] {}:".format(str(p.get("created_at")), who))
        emit.say("    {}".format((p.get("body") or "").replace("\n", "\n    ")))
    return 0


def query_reply(args, emit, answer=False) -> int:
    c = _client(args)
    resp = c.post(
        f"/api/queries/{args.id}/posts",
        json={"body": args.body, "answer": bool(answer)},  # noqa: RUF100, UP032
    )
    post = resp.get("post", {})
    if answer:
        emit.result(
            "Answered query #{} (post #{}).".format(args.id, post.get("id")),
            {"ok": True, "post": post},
        )
    else:
        emit.result(
            "Posted reply #{} to query #{}.".format(post.get("id"), args.id),
            {"ok": True, "post": post},
        )
    return 0


def query_status(args, emit) -> int:
    c = _client(args)
    target = "open" if getattr(args, "reopen", False) else "closed"
    resp = c.post(f"/api/queries/{args.id}/status", json={"status": target})
    thread = resp.get("thread", {})
    emit.result(
        "Query #{} is now {}.".format(args.id, thread.get("status")), {"ok": True, "thread": thread}
    )
    return 0


def query_assign(args, emit) -> int:
    c = _client(args)
    resp = c.post(f"/api/queries/{args.id}/assign", json={"assignee_email": args.assignee})
    thread = resp.get("thread", {})
    emit.result(
        "Query #{} assignee: {}.".format(args.id, thread.get("assignee_email") or "(cleared)"),
        {"ok": True, "thread": thread},
    )
    return 0


def work_list(args, emit) -> int:
    """`work list --remote`: GET /api/worklist. Identity is the token's OWN user
    (server-side), so --actor is NOT sent — the remote worklist always answers for the
    key's owner (§8.4). A shared connector token is refused server-side (400, named)."""
    c = _client(args)
    params = {
        "days": max(1, args.days),
        "limit": max(1, min(args.limit, 50)),
        "offset": max(0, args.offset),
    }
    if args.quality:
        params["quality"] = args.quality
    body = c.get("/api/worklist", params=params)
    items = body.get("items", [])
    total = body.get("total", len(items))
    if emit.as_json:
        emit.json({"ok": True, "total": total, "items": items})
        return 0
    emit.table(
        [
            (
                it.get("section"),
                it.get("ref_id"),
                it.get("project_id"),
                (it.get("title") or "")[:36],
                it.get("target_locale") or "-",
                it.get("reason"),
                str(it.get("updated_at")),
            )
            for it in items
        ],
        headers=("section", "ref", "project", "title", "locale", "reason", "updated"),
    )
    emit.say(
        "%d work item(s)%s." % (total, "" if total <= len(items) else " (showing %d)" % len(items))  # noqa: UP031
    )
    return 0


def lqa_log(args, emit) -> int:
    c = _client(args)
    resp = c.post(
        f"/api/documents/{args.document}/lqa/findings",
        json={
            "segment_id": args.segment,
            "dimension": args.dimension,
            "severity": args.severity,
            "note": args.note,
        },
    )
    f = resp.get("finding", {})
    emit.result(
        "Logged finding #{} ({}/{}) on segment {}.".format(
            f.get("id"), f.get("dimension"), f.get("severity"), f.get("segment_id")
        ),
        {"ok": True, "finding": f},
    )
    return 0


def lqa_findings(args, emit) -> int:
    c = _client(args)
    params = {}
    if getattr(args, "locale", ""):
        params["locale"] = args.locale
    body = c.get(f"/api/documents/{args.document}/lqa/findings", params=params)
    rows = body.get("findings", [])
    if emit.as_json:
        emit.json({"ok": True, "findings": rows})
        return 0
    emit.table(
        [_lqa_row_line(f) for f in rows],
        headers=("id", "segment", "severity", "dimension", "note", "by"),
    )
    emit.say("%d reviewer finding(s)." % len(rows))  # noqa: UP031
    return 0


def lqa_edit(args, emit) -> int:
    c = _client(args)
    resp = c.post(
        f"/api/lqa/findings/{args.id}",
        json={"dimension": args.dimension, "severity": args.severity, "note": args.note},
    )
    f = resp.get("finding", {})
    emit.result("Edited finding #{}.".format(f.get("id")), {"ok": True, "finding": f})
    return 0


def lqa_withdraw(args, emit) -> int:
    c = _client(args)
    resp = c.post(f"/api/lqa/findings/{args.id}/withdraw", json={})
    f = resp.get("finding", {})
    emit.result("Withdrew finding #{}.".format(f.get("id")), {"ok": True, "finding": f})
    return 0


def export_run(args, emit) -> int:
    import time

    c = _client(args)
    body = {"scope": args.scope, "history": args.history}
    if args.ref is not None:
        body["ref"] = args.ref
    emitters = getattr(args, "emitters", None)
    if emitters:  # forward the human-view emitter selection (jsonl|csv|xlsx) for surface parity
        body["emitters"] = [e.strip() for e in emitters.split(",") if e.strip()]
    if getattr(args, "audit", False):
        body["include_audit"] = True
    if getattr(args, "include_eval", False):
        body["include_eval"] = True
    if getattr(args, "include_embeddings", False):
        body["include_embeddings"] = True
    started = c.post("/api/exports", json=body)  # 202 {run_uuid, job_id, status_url, existing?}
    run_uuid = started.get("run_uuid")
    if not run_uuid:
        emit.result("Export started (no run_uuid returned).", {"ok": False, **started})
        return 1
    if started.get("existing"):
        emit.say(f"An identical export is already in flight; polling {run_uuid} ...")
    else:
        emit.say(f"Export {run_uuid} started; polling to completion ...")
    ceiling = time.time() + 1800.0  # 30 min: each poll is a quick GET, never a held connection
    d = c.get(f"/api/exports/{run_uuid}")
    # EXPIRED (retention reaper) and PURGED (GDPR erase) are terminal too: polling them for
    # 30 minutes and exiting 0 made a purged export look like a running one (#527).
    while d.get("status") not in ("ok", "partial", "failed", "expired", "purged"):
        if time.time() > ceiling:
            emit.say(f"Still running after 30 min; poll `export status {run_uuid} --remote ...`.")
            return 0
        time.sleep(1.5)
        d = c.get(f"/api/exports/{run_uuid}")
    return _export_run_line(emit, d)


def export_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/exports")
    runs = body.get("runs", [])
    if emit.as_json:
        emit.json({"ok": True, "runs": runs})
    else:
        rows = [
            (
                r.get("id"),
                r.get("scope_kind"),
                r.get("history"),
                r.get("status"),
                sum((r.get("counts") or {}).values()),
                r.get("artifact_bytes"),
            )
            for r in runs
        ]
        emit.table(rows, headers=("id", "scope", "history", "status", "rows", "bytes"))
    return 0


def export_status(args, emit) -> int:
    c = _client(args)
    d = c.get(f"/api/exports/{args.run}")
    d = {k: v for k, v in d.items() if k != "ok"}
    if emit.as_json:
        emit.json({"ok": True, **d})
    else:
        emit.say(
            "Export run {} ({}/{}, {})".format(
                d.get("id"), d.get("scope_kind"), d.get("history"), d.get("status")
            )
        )
        emit.say("  run_uuid:   {}".format(d.get("run_uuid")))
        emit.say("  artifact:   {} bytes".format(d.get("artifact_bytes")))
        emit.say("  counts:     {}".format(d.get("counts") or {}))
        if d.get("dropped"):
            emit.say("  dropped:    {}".format(d.get("dropped")))
        emit.say("  expires_at: {}".format(d.get("expires_at")))
    return 0


def export_download(args, emit) -> int:
    c = _client(args)
    out_path = args.out or f"dataexport_{args.run_uuid}.zip"
    if os.path.exists(out_path) and not args.force:
        raise CliError(f"destination exists: {out_path} (pass --force to overwrite).", EXIT_USAGE)
    total, _headers = c.stream_to_file(
        f"/api/exports/{args.run_uuid}/artifact", out_path, export_artifact=True
    )
    emit.result(
        f"Downloaded export {args.run_uuid} -> {out_path} ({total} bytes).",
        {"ok": True, "run_uuid": args.run_uuid, "out": out_path, "bytes": total},
    )
    return 0


# ============================================================================
# config features (the feature-flag switchboard) -- the --remote twins of
# `config features` / `config feature enable|disable`. They hit
# GET|POST /api/admin/features*, which are thin shells over the SAME
# app.services.features_admin seam the in-process arm calls, so a flag flip is
# transport-independent: same rows, same result line, same --json object.
#
# The rest of the `config` group stays in-process on purpose (`config set` reaches
# install-locked keys and the host's own store); flags are the slice a headless
# operator genuinely cannot reach any other way.
# ============================================================================
def config_features(args, emit) -> int:
    from .cmd_config import _emit_features

    c = _client(args)
    d = c.get("/api/admin/features")
    return _emit_features(emit, d.get("features") or [])


def config_feature_set(args, emit, enabled: bool) -> int:
    from urllib.parse import quote

    from .cmd_config import _emit_feature_result

    c = _client(args)
    # The slug is USER ARGV, not a numeric id like this file's other path params, so it is
    # percent-encoded. What that buys is RE-ROUTING SAFETY, nothing more: an argument
    # carrying a "/" cannot address a different route. It does NOT reach the seam's
    # unknown-slug 404 -- ASGI decodes %2F before routing, so the request lands on no
    # route at all and Starlette answers its own 404 ("Not Found"); an empty slug hits the
    # collection path and answers 405. Both surface as exit 1 with the server's message,
    # which is the right outcome by a different mechanism than the seam.
    d = c.post(
        "/api/admin/features/" + quote(str(args.slug or ""), safe=""),
        json={"enabled": bool(enabled)},
    )
    # A server-side refusal (404 unknown slug / 409 door moved / 400 local-only) already
    # became a CliError inside RemoteClient._decode, carrying the server's message
    # verbatim -- so the remote arm never invents its own wording for a refusal.
    return _emit_feature_result(emit, {k: v for k, v in d.items() if k != "ok"})
