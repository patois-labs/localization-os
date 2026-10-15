"""Run-view GRAPH OVERLAY (SPEC-conditional-workflows Phase 4, deferred item).

A PURE, read-only projection of a run's persisted `StepRun` rows onto its workflow's
step graph, so the run-detail page can draw the *actual* per-step execution state on top
of the same server-rendered SVG the builder/author-review screens use (the `wf_graph`
macro), and so the SAME facts are available headless (CLI `workflow show-run`, GET
/api/runs/{run_id}).

Nothing here executes, mutates, or interprets run state — it only READS `StepRun` rows
(step_id, status, score, gate) and groups them per current graph node. It never raises:
a run whose workflow was edited/deleted after the fact (StepRun rows pointing at step ids
no longer in the graph) yields honest ORPHAN entries instead of a crash, and a node the
run never reached simply carries no state.

Join model: a `StepRun.step_id` is the WorkflowStep primary key (a stable FK — unlike a
human `key`, which the builder may rename). We group by that id and resolve it against the
CURRENT ordered step list (`orchestrator.get_steps`) — the same list, in the same order,
the `wf_graph` macro walks (`loop.index0`), so overlay positions and graph positions never
disagree. A step id present in the run but absent from the current graph is an orphan.

Per node we report:
  status  — the LATEST StepRun row's status (rows arrive id-ascending, so the last row is
            the most recent visit = the authoritative current state: a loop step that
            failed then re-ran to completion reads "completed"). ONE honest exception: a
            human_review step persists a PAUSED StepRun that the orchestrator never marks
            resolved on resume (orchestrator.py ~2456), so latest-row-wins would render a
            long-finished run's review node as "paused" forever. We therefore SUPERSEDE a
            latest-'paused' status to 'completed' (graph label "reviewed") WHEN THE RUN
            ITSELF is terminal (completed/failed) — a run that is genuinely still PAUSED
            keeps 'paused'. This is a display-only correction; we never touch the row or the
            state machine.
  visits  — how many StepRun rows this node has (each execution writes one row; a loop
            re-entry adds another) — the graph shows "xN" when > 1, mirroring the batch
            matrix `bm-visits` chip.
  score / gate — carried through from the last row that produced them (LQA score / QE
            aggregate + canonical pass/fail), NULL when the step produced no outcome.

Loop-back TRAVERSAL is derived per EDGE, honestly, from the row SEQUENCE (id-ascending ==
execution order): a loop-back edge S->T counts a traversal for each S row whose gate
outcome SELECTED that slot (on_pass<-passed / on_fail<-failed) and that is followed by a
later T row. Two loop-backs to one target are distinguished (only the edge that fired is
marked); the "xN" label is the counted edge traversals, not the target's visit total. When
the outcome is not determinable from the rows (gate NULL), the edge is NOT marked traversed
(dashed stays dashed — never assert an unproven loop).

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

import difflib
import re

from app.services.orchestrator_terminology_backen import SEG_JOINER
from app.services.workflow_shapes import _branch_of, _key_positions, _target_token

# Terminal/active status buckets are described by label + a semantic colour TOKEN (never a
# raw colour) so the SVG stays legible in both themes (the running label uses the accent TEXT
# token, --primary-2, which clears AA on the page surface; --primary is the button fill).
# Status text is ALWAYS rendered too (redundant with colour, so colourblind-safe).
_STATUS_META = {
    "completed": ("done", "var(--success)"),
    "failed": ("failed", "var(--danger)"),
    "skipped": ("skipped", "var(--muted)"),
    # An operator-cancelled run flips its PAUSED StepRun to CANCELLED (orchestrator.
    # cancel_run), so the run-cancel train gives that row an honest label + a muted
    # (non-alarming, non-success) token rather than falling back to the raw string.
    "cancelled": ("cancelled", "var(--muted)"),
    "paused": ("paused", "var(--warn)"),
    "running": ("running", "var(--primary-2)"),
    "pending": ("queued", "var(--muted)"),
}
_NOT_REACHED = ("not run", "var(--muted)")


def status_meta(status):
    """(label, colour-token) for a StepRun status, or the not-reached pair for None/unknown.
    Kept here so the macro and any future consumer single-source the label + token."""
    if not status:
        return _NOT_REACHED
    return _STATUS_META.get(str(status), (str(status), "var(--muted)"))


def _sid(step):
    """The primary-key id of a current graph step (ORM row or draft dict)."""
    if isinstance(step, dict):
        return step.get("id")
    return getattr(step, "id", None)


_TERMINAL = ("completed", "failed")


def _edge_traversals(rows, steps):
    """Per-EDGE loop-back traversal counts, keyed 'sourceStepId:slot'. HONEST: derived from
    the row SEQUENCE (id-ascending == execution order). A loop-back edge S->T counts a
    traversal for each S row whose GATE outcome selected that slot (on_pass<-passed /
    on_fail<-failed) AND is followed by a LATER T row (the run really re-entered T). An
    undeterminable outcome (gate NULL) is never counted — dashed stays dashed. Targets
    resolve via the workflow_shapes key map, so the graph + shape classifier never disagree."""
    steps = list(steps or [])
    pos = _key_positions(steps)  # key -> list index
    step_by_id = {_sid(s): s for s in steps if _sid(s) is not None}
    id_index = {_sid(s): i for i, s in enumerate(steps) if _sid(s) is not None}
    counts = {}
    for r in rows:
        s = step_by_id.get(getattr(r, "step_id", None))
        if s is None:
            continue
        b = _branch_of(s)
        if not b:
            continue
        gate = getattr(r, "gate", None)
        if gate not in ("passed", "failed"):
            continue  # not determinable
        selected = "on_pass" if gate == "passed" else "on_fail"
        tok = _target_token(b.get(selected))
        if not (isinstance(tok, tuple) and tok[0] == "step") or tok[1] not in pos:
            continue  # next/end/dangling
        i = id_index[_sid(s)]
        ti = pos[tok[1]]
        if ti > i:
            continue  # forward branch, not a loop
        target_id = _sid(steps[ti])
        rid = getattr(r, "id", 0) or 0
        if any(
            getattr(rr, "step_id", None) == target_id and (getattr(rr, "id", 0) or 0) > rid
            for rr in rows
        ):
            k = f"{_sid(s)}:{selected}"
            counts[k] = counts.get(k, 0) + 1
    return counts


def build_overlay(step_runs, steps, run_status=None):
    """Project `step_runs` (StepRun rows, any order — sorted here by id) onto `steps` (the
    current ordered WorkflowStep list). `run_status` (the WorkflowRun's own status) lets a
    terminal run supersede a stuck 'paused' review row (see module docstring). Returns:

      by_id       {step_id: node}   — for the macro's per-node lookup (node carries
                                       index/key/status/visits/score/gate/label/token).
      traversals  {'sid:slot': int} — per-edge loop-back traversal counts (see
                                       _edge_traversals); the macro reads this to draw a
                                       fired loop-back solid + label it xN.
      step_states [node, ...]        — ordered by graph position, the HEADLESS parity shape
                                       (key/index/status/visits/score/gate). No step_id (an
                                       internal pk); `key` is the stable human handle, NULL
                                       for a keyless step. Carries the SUPERSEDED status so
                                       web + JSON agree.
      orphan_steps [{step_id,status,visits}, ...]
                                     — StepRun rows whose step id is no longer in the graph
                                       (workflow edited/deleted after the run). Honest, not
                                       hidden; never a crash.
      any         bool               — whether ANY overlay data exists (no rows -> False, so
                                       a brand-new/legacy run degrades to the plain graph).

    Pure; never raises on malformed/legacy input (missing ids, empty lists, None)."""
    steps = list(steps or [])
    order = {}  # step_id -> (index, step)
    for i, s in enumerate(steps):
        sid = _sid(s)
        if sid is not None:
            order[sid] = (i, s)

    rows = sorted((step_runs or []), key=lambda r: getattr(r, "id", 0) or 0)
    by_id = {}
    orphans = {}
    for r in rows:
        sid = getattr(r, "step_id", None)
        status = getattr(r, "status", None)
        if sid in order:
            idx, st = order[sid]
            key = st.get("key") if isinstance(st, dict) else getattr(st, "key", None)
            node = by_id.get(sid)
            if node is None:
                node = {
                    "step_id": sid,
                    "index": idx,
                    "key": key or None,
                    "status": status,
                    "visits": 0,
                    "score": None,
                    "gate": None,
                    # Real per-step spend (feat/workflow-spend): SUM across this
                    # step's visits (a looped step folds each visit's StepRun into
                    # one node, so its node cost is the step's total across visits).
                    # Stay None until a non-NULL row is seen, so a legacy/untracked
                    # step reads NULL (honest), not a false 0.
                    "cost_usd": None,
                    "tokens_in": None,
                    "tokens_out": None,
                }
                by_id[sid] = node
            node["visits"] += 1
            node["status"] = status  # id-ascending -> last row wins (authoritative)
            sc = getattr(r, "score", None)
            gt = getattr(r, "gate", None)
            if sc is not None:
                node["score"] = sc
            if gt is not None:
                node["gate"] = gt
            for _f in ("cost_usd", "tokens_in", "tokens_out"):
                _v = getattr(r, _f, None)
                if _v is not None:
                    node[_f] = (node[_f] or 0) + _v
        else:
            o = orphans.get(sid)
            if o is None:
                o = {"step_id": sid, "status": status, "visits": 0}
                orphans[sid] = o
            o["visits"] += 1
            o["status"] = status

    # A human_review step's PAUSED StepRun is never marked resolved on resume, so on a
    # TERMINAL run latest-row-wins would lie "paused" forever. Supersede to completed (label
    # "reviewed") — display-only; a genuinely-paused run keeps 'paused'. step_states inherits
    # the corrected status below, so web + headless agree.
    terminal = str(run_status) in _TERMINAL if run_status is not None else False
    for node in by_id.values():
        superseded = terminal and node["status"] == "paused"
        if superseded:
            node["status"] = "completed"
        label, token = status_meta(node["status"])
        node["label"] = "reviewed" if superseded else label
        node["token"] = token
        node["looped"] = node["visits"] > 1

    traversals = _edge_traversals(rows, steps)
    step_states = [
        {
            "key": by_id[_sid(s)]["key"],
            "index": by_id[_sid(s)]["index"],
            "status": by_id[_sid(s)]["status"],
            "visits": by_id[_sid(s)]["visits"],
            "score": by_id[_sid(s)]["score"],
            "gate": by_id[_sid(s)]["gate"],
            # Real per-step spend (feat/workflow-spend): summed across the step's visits;
            # NULL where untracked (legacy) or the step spent nothing measurable.
            "cost_usd": by_id[_sid(s)]["cost_usd"],
            "tokens_in": by_id[_sid(s)]["tokens_in"],
            "tokens_out": by_id[_sid(s)]["tokens_out"],
        }
        for s in steps
        if _sid(s) in by_id
    ]
    orphan_steps = sorted(orphans.values(), key=lambda o: o["step_id"] or 0)
    return {
        "by_id": by_id,
        "traversals": traversals,
        "step_states": step_states,
        "orphan_steps": orphan_steps,
        "any": bool(by_id or orphan_steps),
    }


def _step_key(step):
    """A current graph step's stable human `key`, or None (keyless)."""
    if isinstance(step, dict):
        return step.get("key") or None
    return getattr(step, "key", None) or None


def _traversal_list(traversals, steps):
    """Serialize the macro's per-edge `traversals` ({'sourceStepId:slot': count}) into the
    HEADLESS shape: a list of {source_key, slot, target_key, count}, translating internal
    step ids to stable KEYS at this seam (the same _edge_traversals result the graph draws,
    single source of truth). Deterministically ordered; empty when no loop-back fired."""
    steps = list(steps or [])
    by_id = {_sid(s): s for s in steps if _sid(s) is not None}
    out = []
    for edge, count in (traversals or {}).items():
        sid_str, _, slot = str(edge).rpartition(":")
        try:
            sid = int(sid_str)
        except (TypeError, ValueError):
            sid = sid_str
        s = by_id.get(sid)
        if s is None:
            continue
        b = _branch_of(s)
        tok = _target_token(b.get(slot)) if b else None
        target_key = tok[1] if (isinstance(tok, tuple) and tok[0] == "step") else None
        out.append(
            {"source_key": _step_key(s), "slot": slot, "target_key": target_key, "count": count}
        )
    out.sort(key=lambda d: (str(d["source_key"] or ""), d["slot"]))
    return out


def run_step_states(step_runs, steps, run_status=None):
    """The HEADLESS parity payload for a run: an additive
    {"step_states": [...], "orphan_steps": [...], "loopback_traversals": [...]} dict derived
    from `build_overlay`. Shared verbatim by GET /api/runs/{run_id} and CLI `workflow
    show-run` (local + remote) so the web graph's per-step-key status/visits AND its per-edge
    loop-back traversal counts are available without a browser, and the local-vs-remote JSON
    shapes match. `run_status` feeds the terminal-run paused supersede so step_states never
    reports a resolved review as 'paused'."""
    ov = build_overlay(step_runs, steps, run_status=run_status)
    return {
        "step_states": ov["step_states"],
        "orphan_steps": ov["orphan_steps"],
        "loopback_traversals": _traversal_list(ov["traversals"], steps),
    }


def visit_history(step_runs, steps):
    """Full per-VISIT, row-level run trace (CLI-parity backlog item 3: the gap left
    by `run_step_states`, which folds every StepRun row into ONE latest-wins node per
    step — a loop step that failed then re-ran to completion only ever reports its
    LAST status/score/gate, and never exposes provider/model/attempts/error/latency
    at all). This returns one entry PER StepRun ROW, execution-ordered (id-ascending,
    matching every other ordering in this module), each carrying:

      step_id, index, key   — same step identity as step_states (index/key resolved
                               against the CURRENT graph; None for an orphaned step id
                               no longer in it — mirrors orphan_steps, but per-row
                               rather than aggregated).
      visit                 — this row's 1-based visit number for its step (the Nth
                               time this step ran in this run).
      status, score, gate   — this ROW's own outcome (not superseded/aggregated —
                               row-level history is a raw record, so the human_review
                               "paused" supersede build_overlay applies to the graph
                               display is deliberately NOT applied here).
      provider, model       — the resolved provider/model actually used for this visit.
      attempts, error       — retry count and the error string (if any) for this visit.
      latency_ms            — wall time for this visit, when recorded.
      started_at, finished_at — ISO-8601 strings (UTC, naive — same convention as the
                               rest of the app), or None. Pre-stringified here (not left
                               as datetime objects) so this function's output is directly
                               JSON-safe for BOTH the CLI (--json) and /api callers
                               without either seam needing its own conversion.

    ADDITIVE / OPT-IN ONLY (gated by --visits / ?visits=1 at the CLI/API seam — see
    cmd_workflow._wf_show_run / api_cli.api_runs_show): never computed on the default
    `workflow show-run` path, so existing output stays byte-identical. Pure; never
    raises on malformed/legacy input (missing ids, empty lists, None) — mirrors
    build_overlay's robustness contract."""
    steps = list(steps or [])
    order = {}
    for i, s in enumerate(steps):
        sid = _sid(s)
        if sid is not None:
            order[sid] = (i, s)

    rows = sorted((step_runs or []), key=lambda r: getattr(r, "id", 0) or 0)
    visit_counts: dict = {}
    out = []
    for r in rows:
        sid = getattr(r, "step_id", None)
        visit_counts[sid] = visit_counts.get(sid, 0) + 1
        idx, key = None, None
        if sid in order:
            idx, st = order[sid]
            key = st.get("key") if isinstance(st, dict) else getattr(st, "key", None)
        started = getattr(r, "started_at", None)
        finished = getattr(r, "finished_at", None)
        out.append(
            {
                "step_id": sid,
                "index": idx,
                "key": key or None,
                "visit": visit_counts[sid],
                "status": getattr(r, "status", None),
                "score": getattr(r, "score", None),
                "gate": getattr(r, "gate", None),
                "provider": getattr(r, "provider_used", None),
                "model": getattr(r, "model_used", None),
                "attempts": getattr(r, "attempts", None),
                "error": getattr(r, "error", None),
                # run-190 Item C: the per-visit failure SHAPE (metadata only) so a headless
                # reader sees WHY a visit failed (finish reason + provider error class) and
                # how much it produced, without an SSH into the host Ledger.
                "finish_reason": getattr(r, "finish_reason", None),
                "error_class": getattr(r, "error_class", None),
                "output_chars": getattr(r, "output_chars", None),
                "latency_ms": getattr(r, "latency_ms", None),
                # Real per-visit spend (feat/workflow-spend): this StepRun row's exact
                # ledger cost + tokens; NULL on legacy/untracked rows (honest).
                "cost_usd": getattr(r, "cost_usd", None),
                "tokens_in": getattr(r, "tokens_in", None),
                "tokens_out": getattr(r, "tokens_out", None),
                "started_at": started.isoformat() if started else None,
                "finished_at": finished.isoformat() if finished else None,
            }
        )
    return out


def failure_shape(step_runs, steps):
    """run-190 Item C: the per-step FAILURE SHAPE — one compact METADATA row per StepRun
    (attempts, provider, model, finish reason, provider error class, output length, elapsed),
    execution-ordered (id-ascending). No prompt/response text; engine errors are sanitized
    and capped before exposure, so it
    is safe on EVERY surface incl. `--remote`, and is attached to GET /api/runs/{id} BY
    DEFAULT — the fact the run-190 diagnosis needed (3 attempts, all finish_reason=length,
    0 output, escalating budgets) was reachable only via `fly ssh console` before this.

    Reconciles with the LLM Ledger's own per-call summary (tap_view.action_summary): a
    step's attempts/finish_reason/error_class/output_chars are the StepRun-side projection
    of the same calls the Ledger records. Pure; never raises on legacy/None input (a row
    predating the columns simply reads None for them) — mirrors visit_history."""
    from app.services.qe_jev import engine_error

    steps = list(steps or [])
    order = {}
    for i, s in enumerate(steps):
        sid = _sid(s)
        if sid is not None:
            order[sid] = (i, s)
    rows = sorted((step_runs or []), key=lambda r: getattr(r, "id", 0) or 0)
    out = []
    for r in rows:
        sid = getattr(r, "step_id", None)
        idx, key = None, None
        if sid in order:
            idx, st = order[sid]
            key = st.get("key") if isinstance(st, dict) else getattr(st, "key", None)
        row = {
            "step_id": sid,
            "index": idx,
            "key": key or None,
            "status": getattr(r, "status", None),
            "provider": getattr(r, "provider_used", None),
            "model": getattr(r, "model_used", None),
            "attempts": getattr(r, "attempts", None),
            "finish_reason": getattr(r, "finish_reason", None),
            "error_class": getattr(r, "error_class", None),
            "output_chars": getattr(r, "output_chars", None),
            "elapsed_ms": getattr(r, "latency_ms", None),
            "error": engine_error(getattr(r, "error", None), getattr(r, "provider_used", None))
            or None,
        }
        # GitHub #356 phases 2a/2b: which system-prompt HEAD the step ran with (key, override
        # version, winning scope, sha256 of the composed text; never the text). Present ONLY on
        # a visit that recorded it (the overrides flag was on), so every other row keeps its
        # exact shape.
        if getattr(r, "system_prompt_key", None):
            _ver = getattr(r, "system_prompt_version", None)
            row["system_prompt"] = {
                "key": r.system_prompt_key,
                "version": _ver,
                # #356 phase 2b: which scope's head won. A phase-2a row has no stamp: read it
                # as org when an override version was in force, else the built-in text.
                "scope": getattr(r, "system_prompt_scope", None) or ("org" if _ver else "builtin"),
                "sha256": getattr(r, "system_prompt_sha256", None),
            }
        out.append(row)
    return out


def display_status(status, run_status):
    """The status a run-view DISPLAY surface should show for a single StepRun row,
    single-sourcing the graph overlay's terminal-run supersede rule (build_overlay):
    on a TERMINAL run (completed/failed) a stuck 'paused' human_review row reads
    'reviewed' — the orchestrator never marks that row resolved on resume, so its raw
    'paused' would otherwise lie forever on a long-finished run. Everything else passes
    through untouched.

    Returns (value, label) ready to feed `m.status_badge(value, label)`: a label of
    None means "use value as the label" (so a normal row renders byte-identically to
    the old `status_badge(sr.status)`), and the superseded case returns
    ('completed', 'reviewed') so the Steps list + Gantt show the SAME 'reviewed' the
    graph shows. This is a display-only projection — the StepRun row and the raw
    per-VISIT audit trail (visit_history / CLI --visits) are deliberately left RAW."""
    terminal = str(run_status) in _TERMINAL if run_status is not None else False
    if terminal and status == "paused":
        return "completed", "reviewed"
    return status, None


def build_gantt(step_runs, steps, run_status=None, wall_ms=None):
    """Per-step-VISIT timeline rows for the run-view Gantt (VIZ-RESEARCH #1). One row
    per StepRun ROW (a looped step contributes one row per visit — the xN story made
    visible), GROUPED under its graph step (graph-index order; orphaned rows whose step
    is no longer in the workflow are appended last, honestly).

    HONEST about the timing data that actually exists: the orchestrator persists a
    per-visit `latency_ms` but NO per-step start/finish timestamps, so there is no real
    time axis to lay bars on. Each bar is therefore sized by its OWN `latency_ms`
    (relative to the run's longest visit) — a per-visit duration readout, not a
    wall-clock timeline. A visit with NULL latency (LQA / human_review / skipped /
    cap-halted rows never record one) gets no bar width and no duration label (never a
    fake 0ms). Per-visit `cost_usd` is carried as a right-aligned figure; NULL cost is
    OMITTED (never rendered as $0 — an untracked/legacy visit is not a free one).

    Status is DISPLAY-superseded via `display_status` (terminal-run paused review ->
    reviewed) so the Gantt can never disagree with the graph overlay on the same page;
    the bar fill uses the matching semantic status TOKEN (colour is redundant with the
    status_badge + label the row also carries — colourblind-safe).

    `wall_ms` (the run's real WorkflowRun.finished_at - started_at, computed by the
    caller) feeds the footer's total wall time; per-visit latency/cost totals are summed
    here. Pure; never raises on malformed/legacy input (mirrors build_overlay)."""
    steps = list(steps or [])
    name_by_id = {}
    for s in steps:
        sid = _sid(s)
        if sid is None:
            continue
        nm = s.get("name") if isinstance(s, dict) else getattr(s, "name", None)
        ky = s.get("key") if isinstance(s, dict) else getattr(s, "key", None)
        name_by_id[sid] = nm or ky or None

    visits = visit_history(step_runs, steps)
    counts = {}
    for v in visits:
        counts[v["step_id"]] = counts.get(v["step_id"], 0) + 1

    latencies = [v["latency_ms"] for v in visits if v.get("latency_ms") is not None]
    max_latency = max(latencies) if latencies else None
    costs = [v["cost_usd"] for v in visits if v.get("cost_usd") is not None]

    rows = []
    for v in visits:
        val, lbl = display_status(v.get("status"), run_status)
        _label, token = status_meta(val)
        lat = v.get("latency_ms")
        # Bar width as a percentage of the longest visit; a real (non-NULL) latency
        # always shows at least a sliver so a fast step is still visibly a bar.
        if lat is not None and max_latency:
            pct = max(3, round(100.0 * lat / max_latency))
        elif lat is not None:
            pct = 3
        else:
            pct = 0
        rows.append(
            {
                "step_id": v["step_id"],
                "index": v.get("index"),
                "key": v.get("key"),
                "name": name_by_id.get(v["step_id"]) or v.get("key") or None,
                "visit": v.get("visit"),
                "visit_of": counts.get(v["step_id"], 1),
                "status": val,
                "label": lbl,
                "token": token,
                "latency_ms": lat,
                "pct": pct,
                "cost_usd": v.get("cost_usd"),
                "orphan": v.get("index") is None,
            }
        )

    # Group under the step: graph steps in index order, then orphans last. Within a
    # step, visits stay in execution order (visit_history is id-ascending == 1..N).
    rows.sort(
        key=lambda r: (
            r["orphan"],
            r["index"] if r["index"] is not None else 0,
            r["step_id"] or 0,
            r["visit"] or 0,
        )
    )

    return {
        "rows": rows,
        "any": bool(rows),
        "max_latency_ms": max_latency,
        "total_latency_ms": sum(latencies) if latencies else None,
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "wall_ms": wall_ms,
    }


def format_visit_history_ascii(visits) -> str:
    """The human-readable per-VISIT block for `workflow show-run --visits` — one
    line per StepRun row (not per step), shared by the in-process AND --remote CLI
    arms so both transports print IDENTICALLY. ASCII-only. Empty string when there
    is nothing to show."""
    visits = visits or []
    if not visits:
        return ""
    lines = ["Per-visit history:"]
    for v in visits:
        idx = v.get("index")
        idx_s = f"[{idx}]" if idx is not None else "[orphan]"
        bits = [
            f"  {idx_s}",
            str(v.get("key") or "-"),
            f"visit={v.get('visit')}",
            str(v.get("status") or ""),
        ]
        if v.get("provider"):
            bits.append(f"provider={v['provider']}" + (f"/{v['model']}" if v.get("model") else ""))
        sc = v.get("score")
        if sc is not None:
            bits.append(f"score={sc:g}" if isinstance(sc, (int, float)) else f"score={sc}")
        if v.get("gate"):
            bits.append(f"gate={v['gate']}")
        if v.get("attempts"):
            bits.append(f"attempts={v['attempts']}")
        if v.get("latency_ms") is not None:
            bits.append(f"latency={v['latency_ms']}ms")
        if v.get("error"):
            bits.append(f"ERROR: {v['error']}")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def format_failure_shape_ascii(shape) -> str:
    """run-190 Item C / L4 (gate round 1): the human-readable per-step FAILURE-SHAPE block for
    `workflow show-run`, shared by the in-process AND --remote CLI arms so both transports
    print IDENTICALLY (the JSON already carried it on both; this closes the web/CLI parity gap
    — run_view.html renders it, the CLI did not). ASCII-only. Only rows that carry a real
    failure signal (a provider error class OR a finish reason that is not a clean 'stop')
    print, so a clean run stays an empty string (byte-identical to before)."""
    shape = shape or []
    rows = [
        r
        for r in shape
        if r.get("error_class") or (r.get("finish_reason") and r.get("finish_reason") != "stop")
    ]
    if not rows:
        return ""
    lines = ["Per-step failure shape:"]
    for r in rows:
        idx = r.get("index")
        idx_s = f"[{idx}]" if idx is not None else "[orphan]"
        bits = [f"  {idx_s}", str(r.get("key") or "-"), str(r.get("status") or "")]
        if r.get("provider"):
            bits.append(f"provider={r['provider']}" + (f"/{r['model']}" if r.get("model") else ""))
        if r.get("attempts") is not None:
            bits.append(f"attempts={r['attempts']}")
        if r.get("finish_reason"):
            bits.append(f"finish={r['finish_reason']}")
        if r.get("error_class"):
            bits.append(f"error_class={r['error_class']}")
        if r.get("output_chars") is not None:
            bits.append(f"output_chars={r['output_chars']}")
        if r.get("system_prompt"):
            sp = r["system_prompt"]
            bits.append(
                f"system_prompt={sp['key']}@v{sp.get('version')}"
                + (f" scope={sp['scope']}" if sp.get("scope") else "")
                + (f" sha256={str(sp['sha256'])[:12]}" if sp.get("sha256") else "")
            )
        if r.get("elapsed_ms") is not None:
            bits.append(f"elapsed={r['elapsed_ms']}ms")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def format_tm_summary_ascii(summary) -> str:
    """#610: the tm_match step's tally as a console line (leading newline included so it
    appends to the warning block), or "" when the run recorded none. Shared VERBATIM by the
    local and --remote arms of `workflow show-run` (the run_overlay convention).

    Example: "\n  TM match: 9 of 20 segments filled from the TM, 11 not matched (sent on)."."""
    if not isinstance(summary, dict) or not summary.get("total"):
        return ""
    filled = int(summary.get("filled") or 0)
    relocked = int(summary.get("relocked") or 0)
    total = int(summary.get("total") or 0)
    unmatched = int(summary.get("unmatched") or 0)
    line = f"\n  TM match: {filled} of {total} segments filled from the TM"
    if relocked:
        line += f", {relocked} already had the exact TM text"
    return line + f", {unmatched} not matched (sent on to the next step)."


def format_progress_ascii(progress) -> str:
    """One console line for a run's IN-STEP progress (GitHub #101), or "" when the run
    published none. Shared VERBATIM by `workflow show-run`'s local and --remote arms so the
    two outputs stay identical (the run_overlay convention).

    THE COUNTER IS ATTRIBUTED TO ITS STEP and carries the time it last moved. Both matter to
    the reader: each step publishes a fresh 0-of-M, so a bare number legitimately goes
    BACKWARDS at a step boundary (and `unit` changes under it), and a counter frozen an hour
    ago says something very different from one that ticked ten seconds ago.

    Example:
      "  progress: 47/102 segments (step 2 Translate, last update 2026-09-06T04:12:33)"."""
    if not isinstance(progress, dict):
        return ""
    total = progress.get("total") or 0
    if not total:
        return ""
    done = progress.get("done") or 0
    unit = progress.get("unit") or "units"
    at = progress.get("at") or ""
    step = (progress.get("step") or "").strip()
    line = f"  progress: {done}/{total} {unit}"
    tail = []
    if step:
        tail.append(f"step {int(progress.get('step_index') or 0)} {step}")
    if at:
        tail.append(f"last update {str(at)[:19]}")
    if tail:
        line += " (" + ", ".join(tail) + ")"
    return line


def format_reason_ascii(reason) -> str:
    """One console line for WHY a run is in its terminal state (GitHub #106), or "" when the
    run carries no reason (every ordinary run). Shared by the CLI's local and --remote arms.

    Example: "  reason: interrupted_by_restart -- Interrupted by a server restart."."""
    if not isinstance(reason, dict):
        return ""
    text = (reason.get("text") or "").strip()
    code = reason.get("code") or ""
    if not text and not code:
        return ""
    return "  reason: {}{}".format(code, (" -- " + text) if text else "")


def format_step_states_ascii(graph) -> str:
    """The human-readable per-step block for `workflow show-run`, shared by the in-process
    AND --remote CLI arms so both transports print IDENTICALLY. ASCII-only (verify_cli_ascii
    — no unicode chips/arrows): `[index] key status xN score=.. gate=..` per step, and an
    honest one-liner when the run touched steps the workflow no longer has. Empty string
    when there is nothing to show (a run with no recorded steps)."""
    states = (graph or {}).get("step_states") or []
    orphans = (graph or {}).get("orphan_steps") or []
    if not states and not orphans:
        return ""
    lines = ["Steps this run:"]
    for visit in (graph or {}).get("qe_steps") or []:
        lines.append(f"  QE: {visit.get('step_name')} visit={visit.get('visit')}")
        for family in ("qe", "autofix"):
            counts = visit.get(family) or {}
            lines.append(
                "    "
                + family
                + ": "
                + ", ".join(f"{key}={value}" for key, value in counts.items())
            )
    for r in states:
        bits = [f"  [{r.get('index')}]", str(r.get("key") or "-"), str(r.get("status") or "")]
        if (r.get("visits") or 0) > 1:
            bits.append(f"x{r['visits']}")
        sc = r.get("score")
        if sc is not None:
            bits.append(f"score={sc:g}" if isinstance(sc, (int, float)) else f"score={sc}")
        if r.get("gate"):
            bits.append(f"gate={r['gate']}")
        lines.append(" ".join(bits))
    if orphans:
        n = sum((o.get("visits") or 0) for o in orphans)
        lines.append(
            f"  ({len(orphans)} step(s) no longer in the workflow, "
            f"{n} recorded run(s) omitted from the graph)"
        )
    # Per-edge loop-back traversals (ASCII-only): only when one actually fired.
    for lb in (graph or {}).get("loopback_traversals") or []:
        lines.append(
            f"  Loop-back: {lb.get('source_key') or '-'} {lb.get('slot')} -> "
            f"{lb.get('target_key') or '-'} x{lb.get('count')}"
        )
    return "\n".join(lines)


# =============================================================================
# GitHub #340: per-step SEGMENT DIFF.
#
# DATA SOURCE (no new storage): the segmented engine writes ONE StepRun per step
# per run whose input_text / output_text is the run's per-segment working text
# JOINED with SEG_JOINER (see the `_run_workflow_stream_segmented_impl` docstring),
# and the FULL_TEXT path does the same over its single document unit. Splitting a
# StepRun's blob on SEG_JOINER therefore recovers the exact per-segment text that
# step saw (input) and produced (output) -- nothing here re-runs anything, no
# provider call, no write. A blob whose slot count no longer matches the run's
# other blobs (the model emitted a delimiter inside a segment) is diffed
# positionally up to the shorter side and the mismatch is reported honestly,
# never silently dropped.
# =============================================================================

_WORD_TOKEN = re.compile(r"(\S+)(\s*)|(\s+)")

# Cost guard for the word diff. difflib.SequenceMatcher is NOT bounded by the size
# product: with autojunk off, every find_longest_match call scans the whole remaining
# range (and every occurrence of a dominant token), and there is one call per matching
# block, so a run of short matches (`x x x ...` against `x y x y ...`, a table row
# whose cells all changed) is cubic and took a minute inside a 4M token-pair budget
# (#340 final gate). The guard therefore bounds the ACTUAL WORK: _BoundedMatcher
# charges every find_longest_match call (range scanned + b2j occurrences visited)
# against a _WorkBudget BEFORE running it and raises _WorkExceeded past the cap; the
# caller then falls back (line-level diff, then a coarse delete-then-insert flagged
# `coarse`), always lossless. _WORD_DIFF_BUDGET additionally bounds the token-pair size
# of one matcher (memory / b2j build), and _VIEW_WORK_BUDGET the whole view.
_WORD_DIFF_BUDGET = 4_000_000
_PAIR_WORK_BUDGET = 1_500_000
_VIEW_WORK_BUDGET = 20_000_000
_LINE_PAIR_WINDOW = 8
_LINE_PAIR_MIN_SIMILARITY = 0.5


class _WorkExceeded(Exception):
    """The diff's work cap was hit; the caller degrades to a coarser result."""


class _WorkBudget:
    """A shared counter of matcher work units. `used` is the total charged so far;
    one pair may spend at most `pair_cap`, and the whole view at most `total`."""

    def __init__(self, total=_VIEW_WORK_BUDGET, pair_cap=_PAIR_WORK_BUDGET):
        self.total = total
        self.pair_cap = pair_cap
        self.used = 0
        self.pair_used = 0

    def start_pair(self):
        self.pair_used = 0

    def charge(self, units):
        if self.pair_used + units > self.pair_cap or self.used + units > self.total:
            raise _WorkExceeded
        self.used += units
        self.pair_used += units


class _BoundedMatcher(difflib.SequenceMatcher):
    """SequenceMatcher whose find_longest_match is metered against a _WorkBudget."""

    def __init__(self, a, b, budget):
        self._budget = budget
        super().__init__(None, a, b, autojunk=False)

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        ahi = len(self.a) if ahi is None else ahi
        bhi = len(self.b) if bhi is None else bhi
        b2j = self.b2j
        units = ahi - alo + 1
        for i in range(alo, ahi):
            units += len(b2j.get(self.a[i], ()))
        self._budget.charge(units)
        return super().find_longest_match(alo, ahi, blo, bhi)


def _bounded_opcodes(seq_a, seq_b, budget):
    """The matcher opcodes of two sequences, or None when the work cap was hit."""
    budget.start_pair()
    try:
        return _BoundedMatcher(seq_a, seq_b, budget).get_opcodes()
    except _WorkExceeded:
        return None


def step_diff_target_types():
    """The step types whose StepRun output is the run's TARGET text (a translation,
    a reviewer's edit, a TM fill, a vendor return) -- the SAME allow-list the run
    view uses to lang-tag step output. The 'previous translating step' the default
    comparison walks back to is the nearest earlier step run of one of these types."""
    from app.models import StepType

    return frozenset(
        {StepType.LLM_AGENT, StepType.HUMAN_REVIEW, StepType.TM_MATCH, StepType.EXTERNAL}
    )


def word_tokens(text):
    """The (word, trailing_whitespace) tokens of a (possibly empty) string. A
    leading whitespace run is its own ('', ws) token. Whitespace rides on the word
    it follows, so repeated single spaces are not thousands of identical diff
    tokens (the quadratic trap of a word+space split), while joining
    word+whitespace over all tokens still reproduces the text byte-for-byte."""
    return [("", m[2]) if m[2] else (m[0], m[1]) for m in _WORD_TOKEN.findall(text or "")]


def _emit_equal_word(push, word, ws_a, ws_b):
    """One word that both sides share: it is unchanged, but the whitespace after
    it may differ (spacing edit, or a trailing space only one side has)."""
    push("equal", word)
    if ws_a == ws_b:
        push("equal", ws_a)
    else:
        push("delete", ws_a)
        push("insert", ws_b)


def _similarity(set_a, set_b):
    if not set_a and not set_b:
        return 1.0
    inter = len(set_a & set_b)  # iterates the smaller set; no union copy
    return inter / (len(set_a) + len(set_b) - inter)


def _pair_lines(push, lines_a, lines_b, budget):
    """Diff two line lists that the line matcher could not anchor (every line
    edited, no equal line). Equal counts pair positionally; otherwise a greedy
    in-order alignment pairs each line with the most similar of the next few
    candidate lines. Each pair is word-diffed; unpaired lines are plain deletes /
    inserts. True when any pair degraded to the coarse result."""
    coarse = False
    if len(lines_a) == len(lines_b):
        for la, lb in zip(lines_a, lines_b, strict=True):
            coarse = _diff_into(push, la, lb, budget, False) or coarse
        return coarse
    sets_b = [set(line.split()) for line in lines_b]
    j = 0
    for line_a in lines_a:
        set_a = set(line_a.split())
        best, best_k = 0.0, -1
        for k in range(j, min(len(lines_b), j + _LINE_PAIR_WINDOW)):
            sim = _similarity(set_a, sets_b[k])
            if sim > best:
                best, best_k = sim, k
        if best_k >= 0 and best >= _LINE_PAIR_MIN_SIMILARITY:
            if best_k > j:
                push("insert", "".join(lines_b[j:best_k]))
            coarse = _diff_into(push, line_a, lines_b[best_k], budget, False) or coarse
            j = best_k + 1
        else:
            push("delete", line_a)
    if j < len(lines_b):
        push("insert", "".join(lines_b[j:]))
    return coarse


def _diff_into(push, before, after, budget, allow_lines=True):
    """Word-diff `before` vs `after` into `push`; True when any part had to fall
    back to the coarse delete-then-insert (cost guard)."""
    a = word_tokens(before)
    b = word_tokens(after)
    head = 0
    limit = min(len(a), len(b))
    while head < limit and a[head][0] == b[head][0]:
        _emit_equal_word(push, a[head][0], a[head][1], b[head][1])
        head += 1
    tail = 0
    limit -= head
    while tail < limit and a[-1 - tail][0] == b[-1 - tail][0]:
        tail += 1
    mid_a = a[head : len(a) - tail]
    mid_b = b[head : len(b) - tail]
    coarse = False
    if mid_a or mid_b:
        text_a = "".join(w + s for w, s in mid_a)
        text_b = "".join(w + s for w, s in mid_b)
        ops = None
        if len(mid_a) * len(mid_b) <= _WORD_DIFF_BUDGET:
            ops = _bounded_opcodes([w for w, _ in mid_a], [w for w, _ in mid_b], budget)
        if ops is not None:
            for tag, i1, i2, j1, j2 in ops:
                if tag == "equal":
                    for k in range(i2 - i1):
                        _emit_equal_word(push, mid_a[i1 + k][0], mid_a[i1 + k][1], mid_b[j1 + k][1])
                else:  # replace = the deleted words, then the inserted ones
                    push("delete", "".join(w + s for w, s in mid_a[i1:i2]))
                    push("insert", "".join(w + s for w, s in mid_b[j1:j2]))
        else:
            lines_a = text_a.splitlines(True)
            lines_b = text_b.splitlines(True)
            line_ops = None
            if (
                allow_lines
                and (len(lines_a) > 1 or len(lines_b) > 1)
                and len(lines_a) * len(lines_b) <= _WORD_DIFF_BUDGET
            ):
                line_ops = _bounded_opcodes(lines_a, lines_b, budget)
            if line_ops is not None:
                for tag, i1, i2, j1, j2 in line_ops:
                    block_a = "".join(lines_a[i1:i2])
                    block_b = "".join(lines_b[j1:j2])
                    if tag == "equal":
                        push("equal", block_a)
                    elif tag == "replace":
                        coarse = _pair_lines(push, lines_a[i1:i2], lines_b[j1:j2], budget) or coarse
                    else:
                        push("delete", block_a)
                        push("insert", block_b)
            else:
                push("delete", text_a)
                push("insert", text_b)
                coarse = True
    for k in range(tail):
        w, s_a = a[len(a) - tail + k]
        _emit_equal_word(push, w, s_a, b[len(b) - tail + k][1])
    return coarse


def word_diff_result(before, after, budget=None):
    """(ops, coarse): the WORD-level diff of two strings as (op, text) pairs, op in
    'equal' | 'insert' | 'delete', plus whether the cost guard had to coarsen any
    part of it (see _WORD_DIFF_BUDGET). difflib.SequenceMatcher over the words,
    common head/tail trimmed first; a 'replace' is a 'delete' followed by an
    'insert' so every consumer has exactly three operations to render. Every piece
    funnels through one append-or-coalesce step, so no text is ever dropped:
    joining the 'equal'+'delete' pieces reproduces `before` and 'equal'+'insert'
    reproduces `after`, for any input."""
    runs = []  # [op, [pieces]]: pieces are joined once, never re-copied per append

    def push(op, text):
        if not text:
            return
        if runs and runs[-1][0] == op:
            runs[-1][1].append(text)
        else:
            runs.append([op, [text]])

    coarse = _diff_into(push, before or "", after or "", budget or _WorkBudget())
    return [(op, "".join(parts)) for op, parts in runs], coarse


def word_diff_ops(before, after):
    """The (op, text) pairs of word_diff_result (see there)."""
    return word_diff_result(before, after)[0]


def step_diff_blob_parts(blob):
    """The per-segment slots of a StepRun input/output blob (SEG_JOINER split),
    or [] for a missing/blank blob. Never raises."""
    if not blob:
        return []
    return blob.split(SEG_JOINER)


def step_diff_default_pair(step_runs, steps):
    """The default comparison for a run: the LATEST translating step run
    (step_diff_target_types) that recorded an output, against the nearest
    EARLIER one that did -- i.e. 'this post-edit round vs the previous
    translating step'. Both sides are restricted to translating types on
    purpose: a pass-through step (an LQA judge records the UNCHANGED text as
    its output) would otherwise win the 'latest output' slot and the default
    view of an LQA-terminated run would always read '0 of M segments changed'.
    (from_id, to_id), or (None, None) when the run has no comparable pair
    (fewer than two translating step runs with recorded output)."""
    step_type = {s.id: getattr(s, "step_type", "") for s in (steps or [])}
    targets = step_diff_target_types()
    runs = sorted(step_runs or [], key=lambda sr: sr.id)
    candidates = [
        sr for sr in runs if step_type.get(sr.step_id) in targets and (sr.output_text or "").strip()
    ]
    if len(candidates) < 2:
        return None, None
    return candidates[-2].id, candidates[-1].id


def step_diff_summary(step_runs, steps):
    """The per-step overview: one row per StepRun of the run, in run order, so a
    reader can both pick any two (the ids are the --from/--to values) and see at a
    glance what each translating step changed: {step_run_id, step_id, step_type,
    step_name, status, recorded, vs_step_run_id, changed, total}. `recorded` is
    False for a blank output (a failed step). For a translating step with
    recorded output, `vs_step_run_id` is the previous translating step run with
    recorded output and changed/total are its 'N of M segments changed' against
    it; every other row (the first translating step, LQA/pass-through, blank)
    carries None / None / None. Compares strings only (no word diff), so it is
    cheap on a large run."""
    step_by_id = {s.id: s for s in (steps or [])}
    targets = step_diff_target_types()
    rows = []
    prev = None  # the previous translating step run WITH recorded output
    for sr in sorted(step_runs or [], key=lambda r: r.id):
        step = step_by_id.get(sr.step_id)
        step_type = getattr(step, "step_type", "") if step is not None else ""
        recorded = bool((sr.output_text or "").strip())
        row = {
            "step_run_id": sr.id,
            "step_id": sr.step_id,
            "step_type": step_type,
            "step_name": (getattr(step, "name", None) or "") if step is not None else "",
            "status": str(sr.status),
            "recorded": recorded,
            "vs_step_run_id": None,
            "changed": None,
            "total": None,
        }
        if recorded and step_type in targets:
            if prev is not None:
                before = step_diff_blob_parts(prev.output_text)
                after = step_diff_blob_parts(sr.output_text)
                total = max(len(before), len(after))
                row["vs_step_run_id"] = prev.id
                row["total"] = total
                row["changed"] = sum(
                    1
                    for i in range(total)
                    if (before[i] if i < len(before) else "")
                    != (after[i] if i < len(after) else "")
                )
            prev = sr
        rows.append(row)
    return rows


def step_diff_view(run, step_runs, steps, from_id=None, to_id=None):
    """The per-segment before/after/changed view comparing two step runs of ONE run.

    Returns a JSON-safe dict shared by the web page, the /api route and BOTH CLI
    arms (local and --remote must stay key-identical):

      run_id                 the run being diffed
      from / to              the compared pair: {step_run_id, step_id, step_type,
                             step_name, status} (step_name is '' when the workflow
                             step was deleted after the fact)
      segments               one entry per segment POSITION: {index, before, after,
                             changed, diff} where diff is the word_diff_ops pair
                             list for that position. A slot present on one side
                             only (delimiter-count mismatch) carries '' on the
                             missing side and counts as changed.
      changed / total        the per-step count ("N of M segments changed"); None / None
                             when one side is not recorded (nothing to compare)
      not_recorded           {from: bool, to: bool} -- honest 'not recorded for
                             this step' when a side's blob is blank (e.g. a
                             failed step recorded output_text='')
      count_mismatch         True when the two blobs split into different slot
                             counts (the model emitted the delimiter in-text)
      steps                  step_diff_summary: every step run of the run with its
                             id and its own "N of M changed vs the previous
                             translating step"

    Never raises: unknown pair ids or a missing run yield the same shape with
    both sides blank (the routes turn the missing-run case into the run view's
    uniform 404 before calling this)."""
    by_id = {sr.id: sr for sr in (step_runs or [])}
    step_by_id = {s.id: s for s in (steps or [])}

    def side(sr):
        step = step_by_id.get(sr.step_id) if sr is not None else None
        return {
            "step_run_id": sr.id if sr is not None else None,
            "step_id": sr.step_id if sr is not None else None,
            "step_type": getattr(step, "step_type", "") if step is not None else "",
            "step_name": (getattr(step, "name", None) or "") if step is not None else "",
            "status": str(sr.status) if sr is not None else "",
        }

    from_sr = by_id.get(from_id) if from_id is not None else None
    to_sr = by_id.get(to_id) if to_id is not None else None
    nr_from = from_sr is None or not (from_sr.output_text or "").strip()
    nr_to = to_sr is None or not (to_sr.output_text or "").strip()
    # One side recorded nothing (a failed step) while the other did: there is no
    # per-segment comparison to make, so report 'not recorded' - no counts, no
    # segments, no delimiter-mismatch - instead of "N of N changed".
    one_side_missing = (from_sr is not None or to_sr is not None) and (nr_from or nr_to)
    before_parts = step_diff_blob_parts(from_sr.output_text if from_sr is not None else "")
    after_parts = step_diff_blob_parts(to_sr.output_text if to_sr is not None else "")
    if one_side_missing:
        before_parts, after_parts = [], []
    count = max(len(before_parts), len(after_parts))
    budget = _WorkBudget()
    segments = []
    changed = 0
    for index in range(count):
        before = before_parts[index] if index < len(before_parts) else ""
        after = after_parts[index] if index < len(after_parts) else ""
        is_changed = before != after
        changed += 1 if is_changed else 0
        ops, coarse = word_diff_result(before, after, budget) if is_changed else ([], False)
        segments.append(
            {
                "index": index,
                "before": before,
                "after": after,
                "changed": is_changed,
                # Lists, not tuples: the pair list rides a JSON payload unchanged.
                "diff": [list(op) for op in ops],
                # True when the cost guard degraded this segment to a whole-block
                # delete-then-insert (a huge single-line segment); still lossless.
                "coarse": coarse,
            }
        )
    return {
        "run_id": run.id if run is not None else None,
        "from": side(from_sr),
        "to": side(to_sr),
        "segments": segments,
        "changed": None if one_side_missing else changed,
        "total": None if one_side_missing else count,
        "not_recorded": {"from": nr_from, "to": nr_to},
        "count_mismatch": len(before_parts) != len(after_parts),
        "steps": step_diff_summary(step_runs, steps),
    }
