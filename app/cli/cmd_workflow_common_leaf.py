"""cmd_workflow_common: shared leaf helpers (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

import argparse

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    get_scoped_or_404,
    op_org_id,
    remote_mode,
    require_org_scope,
)

_MAX_WAIT_HOURS = 100_000


# M6: a bounded non-negative hours type for --external-wait-hours, so an absurd value
# (10**30) is a clean argparse usage error, never a raw OverflowError at DB commit.
def _wait_hours(value: str) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of hours.") from e
    if not (0 <= n <= _MAX_WAIT_HOURS):
        raise argparse.ArgumentTypeError(f"wait hours must be between 0 and {_MAX_WAIT_HOURS}.")
    return n


def _tm_match_config_args(p, *, editing: bool) -> None:
    """The six tm_match per-step config flags (Batch 15 T1, SPEC-tm-match-step-config
    §8.D), shared by add-step and edit-step. On ADD (editing=False) the tri-state bools
    default to "auto" (inherit -> SEED a concrete snapshot) and the scalars/mode/pool
    default to None/"" (inherit/seed). On EDIT (editing=True) every default is None =
    "not passed = leave unchanged" (the partial contract); an explicit "auto"/"" value
    clears the override back to inherit. LIVE only on a tm_match step (refused elsewhere)."""
    _tri_default = None if editing else "auto"
    _pool_default = None if editing else ""
    # M5: the scalar + mode flags are PLAIN STRINGS (not type=int / closed mode choices), so
    # 'auto' / 'inherit' / '' all clear the override back to inherit -- LAW-TM-1/TM-11 are
    # fully true on the CLI (web / /api / MCP already clear all six). The service coercers
    # (_coerce_tm_scalar_int / _coerce_tm_fuzzy_lock_mode) map auto/inherit/blank/junk -> None
    # and a real value -> the override; the engine clamps the threshold [50,100] / floor
    # [50,99] at read.
    p.add_argument(
        "--fill-threshold",
        dest="tm_fill_threshold",
        default=None,
        metavar="PCT",
        help="tm_match: minimum match %% to FILL a segment (clamped 50-100; 100 = "
        "exact matches only). 'auto'/'inherit' (or blank) = inherit the global "
        "(tm_fill_threshold). Refused on non-tm_match steps.",
    )
    p.add_argument(
        "--fuzzy-lock-mode",
        dest="tm_fuzzy_lock_mode",
        default=None,
        choices=["auto", "inherit", "none", "soft", "policy"],
        help="tm_match: how a below-100 fuzzy fill locks -- 'none' (a suggestion, "
        "overwritten by a downstream translate step), 'soft' (enters LQA), or "
        "'policy' (the same lock the 100 band would give). 'auto'/'inherit' (or "
        "not passed) = inherit the global (tm_fuzzy_lock_mode). Refused on "
        "non-tm_match steps.",
    )
    p.add_argument(
        "--lock-ice",
        dest="tm_lock_ice",
        default=_tri_default,
        choices=["auto", "on", "off"],
        help="tm_match: lock in-context exact (ICE) matches ('auto' = inherit the "
        "global tm_lock_ice; 'on'/'off' override). Refused on non-tm_match steps.",
    )
    p.add_argument(
        "--hundreds-enter-lqa",
        dest="tm_hundreds_enter_lqa",
        default=_tri_default,
        choices=["auto", "on", "off"],
        help="tm_match: send 100%% matches to LQA (soft lock) instead of hard-locking "
        "('auto' = inherit the global tm_hundreds_enter_lqa; 'on'/'off' override). "
        "Refused on non-tm_match steps.",
    )
    p.add_argument(
        "--context-floor",
        dest="tm_context_floor",
        default=None,
        metavar="PCT",
        help="tm_match: attach near matches at or above this score as context "
        "(clamped 50-99; forward-compat until the ICE/context wave ships). "
        "'auto'/'inherit' (or blank) = inherit the global (tm_context_floor). "
        "Refused on non-tm_match steps.",
    )
    p.add_argument(
        "--tm-pools",
        dest="tm_pool_selection",
        default=_pool_default,
        metavar="IDS",
        help="tm_match: comma-separated readable project ids whose TM this step reads "
        "(e.g. '1,2,3'); blank / 'auto' = all readable pools; '(none)' = read NO pool. "
        "An id outside the run's readable set is dropped + noted at run time; a value that "
        "names no valid id is refused. Refused on non-tm_match steps.",
    )


# --- helpers -----------------------------------------------------------------
def _tristate_bool(p, flag, dest, help_on) -> None:
    """Register a THREE-STATE boolean flag pair for `edit-step`: `--flag` -> True,
    `--no-flag` -> False, neither -> None (default sentinel = leave unchanged). Both
    write the same dest with default None, so an unpassed toggle is distinguishable from
    an explicit false (the partial-update contract update_step relies on)."""
    p.add_argument(flag, dest=dest, action="store_true", default=None, help=help_on)
    p.add_argument(
        "--no-" + flag[2:],
        dest=dest,
        action="store_false",
        default=None,
        help=f"turn OFF: {help_on}",
    )


def _reject_archived_customer(session, customer_id, what, org_id=None) -> None:
    """T2-01 (web+CLI parity): refuse a create that names an ARCHIVED customer,
    mirroring the web route's _archived_customer_block. Archive retires a customer from
    every 'choose a customer' surface, so a new workflow / group can't attach to one.
    A non-digit id is left to the service; a digit id that names no customer is
    "no such customer: N" (exit 2).

    Scoping gate LOW: the foreign-org check comes FIRST. Under multi-org an archived
    customer that lives in another org (or has no org) answers exactly like a live one
    does ("no such customer: N"), so its name never reaches the caller. `org_id` is the
    caller's op_org_id; None (gate off, or the key-verified --operator door) skips it."""
    from app.kernel import access, ids
    from app.models import Customer

    # coerce_id never raises: a malformed CLI id (Unicode digit / >2**63) resolves to None
    # and is left to the service, exactly as a non-digit was, never a traceback.
    cid = ids.coerce_id(customer_id)
    if cid is None:
        return
    cust = session.get(Customer, cid)
    # Review gate: a nonexistent customer gets the same answer as a foreign one (workflow
    # create used to insert it and die on the FK constraint with a traceback). The RAW
    # id is echoed, as the callers' WorkflowForeignCustomerError mapping does, so "03"
    # reads the same whether customer 3 is foreign (live or archived) or missing.
    if cust is None:
        raise CliError(f"no such customer: {customer_id}", EXIT_USAGE)
    if access.multi_org_active():
        scope = access.org_scope_value(org_id)
        if scope is not None and getattr(cust, "org_id", None) != scope:
            raise CliError(f"no such customer: {customer_id}", EXIT_USAGE)
    if cust is not None and getattr(cust, "archived_at", None) is not None:
        raise CliError(
            f"customer {cid} ({cust.name}) is archived - unarchive it first, or pick "
            f"an active customer, to {what}.",
            EXIT_USAGE,
        )


def _get_wf(session, workflow_id: int, org_id=None):
    from app.models import Workflow

    return get_scoped_or_404(session.get(Workflow, workflow_id), workflow_id, "workflow", org_id)


def _run_doc(session, doc_id: int, org_id=None):
    """The document `workflow run` / `workflow run --all-locales` may target, or a
    CliError — the CLI twin of `api_cli._visible_document` (dual-surface law).

    Three refusals, all spelled the SAME way so none of them is an existence oracle:
    a missing id, a document in another org, and (fix round 2, W1) a key project's
    INTERNAL documents. `POST /api/workflows/{id}/run` and `/run-batch` already
    resolve through `_visible_document`, which hides every `fmt="keys"` document;
    these two CLI verbs took a bare `session.get`, so `workflow run <trash_id>` and
    `workflow run <trash_id> --all-locales` each ran a soft-deleted key's parked
    values — spending provider budget on strings the customer had just deleted and
    emitting zero delta entries. A key project's own Translate control launches its
    NAMESPACE documents from the key grid, which never hands out a document id."""
    from app.models import Document, Project

    doc = session.get(Document, doc_id)
    if doc is None:
        raise CliError(f"no such document: {doc_id}", EXIT_USAGE)
    if doc.fmt == "keys":
        raise CliError(f"no such document: {doc_id}", EXIT_USAGE)
    if org_id is not None:
        proj = session.get(Project, doc.project_id)
        if proj is None or getattr(proj, "org_id", None) != org_id:
            raise CliError(f"no such document: {doc_id}", EXIT_USAGE)
    return doc


def _doc_locale(session, doc_id: int) -> str | None:
    """The first target locale present on a document (for `resume` when --locale
    is omitted)."""
    from sqlmodel import select

    from app.models import Segment

    locs = sorted(
        {
            s.target_locale
            for s in session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        }
    )
    return locs[0] if locs else None


# --- workflow create ---------------------------------------------------------
def _wf_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        _reject_archived_customer(
            session, args.customer_id, "create a workflow for it", op_org_id(args)
        )
        try:
            wf = workflows.create_workflow(
                session,
                args.name,
                scope=args.scope,
                customer_id=args.customer_id,
                org_id=op_org_id(args),
            )
        except workflows.WorkflowForeignCustomerError as exc:
            raise CliError(f"no such customer: {args.customer_id}", EXIT_USAGE) from exc
        if wf is None:
            raise CliError("workflow name must not be blank.", EXIT_USAGE)
        emit.result(
            f"Created workflow #{wf.id}: {wf.name} (scope={wf.scope}).",
            {
                "ok": True,
                "id": wf.id,
                "name": wf.name,
                "scope": wf.scope,
                "customer_id": wf.customer_id,
            },
        )
    return 0


def _last_run_utc_label(iso_str) -> str:
    """BUG-09(5): render a naive-UTC `last_run_at` isoformat() string as a
    minute-precision, explicitly-UTC label ('YYYY-MM-DD HH:MM UTC') so the human table
    can never read as a same-timezone timestamp. '-' when there is no run yet or the
    stored value doesn't parse (defensive; never raises on odd/legacy data)."""
    if not iso_str:
        return "-"
    try:
        return str(iso_str)[:16].replace("T", " ") + " UTC"
    except Exception:
        return str(iso_str)[:10] or "-"


# --- workflow list -----------------------------------------------------------
def _wf_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_list(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.kernel import orchestrator, workflow_stats
    from app.kernel import workflow_shapes as wshapes
    from app.models import Workflow

    oid = op_org_id(args)
    with _session() as session:
        stmt = select(Workflow).order_by(Workflow.created_at.desc())
        if oid is not None:
            stmt = (
                select(Workflow).where(Workflow.org_id == oid).order_by(Workflow.created_at.desc())
            )
        wfs = session.exec(stmt).all()
        # Archive (residual sweep): default hides ARCHIVED workflows (matching the web
        # dashboard + GET /api/workflows); --archived all|only widens/inverts it. Active
        # branch routed through the shipped predicate seam (M4).
        from app.kernel import workflows as _wf

        arch = (getattr(args, "archived", "active") or "active").strip().lower()
        if arch == "only":
            wfs = [w for w in wfs if not _wf.workflow_is_active(w)]
        elif arch != "all":
            wfs = [w for w in wfs if _wf.workflow_is_active(w)]
        # Phase 3: each workflow's steps -> primary SHAPE tag + its origin stamp, mirrored
        # on both the human table (shape column) and --json (shape + created_via).
        # Wave A: single-sourced usage + quality stats (same service the dashboard + /api
        # use) so the CLI shows the SAME per-workflow figures. In-process = the trusted
        # operator; the org filter above already confines which workflows appear.
        #
        # Issue #49: this used to pass user=None with a comment claiming "None ->
        # unrestricted". That is only true with the login wall OFF. With auth ON,
        # access.can_see_all(None) is False, visible_project_ids(session, None) is [], and
        # workflow_stats.collect short-circuits to all-zeros -- so EVERY workflow reported
        # runs_total=0 / last_run_at=null on any auth-on install (seen on prod, where the
        # workflows demonstrably ran). The whole suite runs auth-off, which is exactly why
        # 213 suites never caught it. `unrestricted=True` says out loud what the old
        # comment only claimed. (The synthetic SYSTEM operator would fix the single-tenant
        # case but NOT multi-org: current_org_id(SYSTEM) is None, so visible_project_ids
        # would confine to org-NULL projects and this command would still read zero on a
        # multi-org auth-on install. The `--org` filter above is this command's scope --
        # and R10: pass it THROUGH, so the run rollup is confined to the same org the
        # workflow list is. Unrestricted globally would have printed org 2's workflows
        # with org 1's runs counted beside them.)
        stats = workflow_stats.stats_for(
            session, [w.id for w in wfs], unrestricted=True, org_id=oid
        )
        info = []
        for w in wfs:
            steps = orchestrator.get_steps(session, w.id)
            info.append((w, len(steps), wshapes.detect_shapes(steps)))
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "workflows": [
                        {
                            "id": w.id,
                            "name": w.name,
                            "scope": w.scope,
                            "steps": n,
                            "shape": shape.primary,
                            "shape_tags": shape.sorted_tags(),
                            "created_via": getattr(w, "created_via", None),
                            "archived": w.archived_at is not None,
                            **stats[w.id],
                        }
                        for w, n, shape in info
                    ],
                }
            )
        else:
            # Lean human table: add runs + last-run columns (don't blow up the width;
            # the fuller stats live in --json / the web dashboard).
            # BUG-09(5): `last_run_at` is a NAIVE-UTC isoformat() string with no zone
            # marker; a bare date slice (`[:10]`) silently rendered a UTC date that can
            # be TOMORROW relative to the operator's local clock (seen: stored
            # 2026-07-29 while the local wall clock still read 2026-07-28 22:2x) with
            # nothing to say why. An explicit " UTC" suffix on the minute-precision
            # value costs one column-width but removes the ambiguity; --json is
            # unaffected (still the bare isoformat string).
            emit.table(
                [
                    (
                        w.id,
                        w.name,
                        w.scope,
                        n,
                        wshapes.SHAPE_LABELS[shape.primary],
                        stats[w.id]["runs_total"],
                        _last_run_utc_label(stats[w.id]["last_run_at"]),
                    )
                    for w, n, shape in info
                ],
                headers=("id", "name", "scope", "steps", "shape", "runs", "last run"),
            )
    return 0


def _parallel_brackets(step_rows) -> list:
    """One ASCII bracket cell per step row (SPEC-parallel-routing §6). '' for a step in
    no group; '/ <slug>' / '| <slug>' / '\\ <slug>' for the first / middle / last member
    of a run of CONSECUTIVE same-slug steps. A LONE member renders '. <slug>' -- it is
    inert (it runs like an ordinary step), and saying so plainly is the whole point:
    that is the shape a typo'd slug produces. ASCII only (Windows consoles)."""
    slugs = [(r.get("parallel_group") or "") for r in step_rows]
    out = []
    for i, g in enumerate(slugs):
        if not g:
            out.append("")
            continue
        first = i == 0 or slugs[i - 1] != g
        last = i == len(slugs) - 1 or slugs[i + 1] != g
        mark = "." if (first and last) else "/" if first else "\\" if last else "|"
        out.append(f"{mark} {g}")
    return out


def _parse_branch_flags(items) -> dict | None:
    """Turn the repeatable --branch 'on_pass=<t>'/'on_fail=<t>' flags into the branch
    dict {on_pass, on_fail} add_step validates. Thin CLI wrapper over the SHARED service
    parser (workflows.parse_branch_flags) that the /api routes also call (house parity
    law), mapping its WorkflowError to a clean CLI usage error with the --branch prefix
    the operator typed. Returns None when no branch flag was given (no branch)."""
    from app.kernel import workflows

    try:
        return workflows.parse_branch_flags(items)
    except workflows.WorkflowError as exc:
        # Re-prefix the shared 'branch ...' message with the CLI flag spelling.
        msg = str(exc)
        if msg.startswith("branch "):
            msg = "--" + msg
        raise CliError(msg, EXIT_USAGE) from exc


# --- workflow move-step (reorder; SPEC-workflow-canvas 4.2) -------------------
def _resolve_step_ref(rest, ref: str):
    """Index of `ref` (a step KEY or a numeric step id) within `rest` -- the ordered
    step list WITHOUT the step being moved. `rest` is the right list to resolve against
    because --before/--after describe the FINAL arrangement: the moved step is gone from
    its old slot by then, so 'before X' is exactly X's index in the remainder.

    AMBIGUITY IS REFUSED, NOT RESOLVED. A numeric ref can match one step's KEY and a
    DIFFERENT step's ID (keys are free-form slugs, so '3' is a legal key). Silently
    letting the key win would move the step to a position the author did not name, with
    no way to tell from the output -- so this raises instead. Returns the index, or None
    when nothing matches.

    Thin CLI wrapper over the SHARED service resolver (workflows.resolve_step_ref) that
    the /api move route also calls (house parity law), mapping its WorkflowError to a
    clean CLI usage error and re-spelling the position hint as `--to <position>`."""
    from app.kernel import workflows

    try:
        return workflows.resolve_step_ref(rest, ref)
    except workflows.WorkflowError as exc:
        raise CliError(
            str(exc).replace("an explicit position", "--to <position>"), EXIT_USAGE
        ) from exc


# --- workflow author (promptable setup, SPEC-promptable-setup Branch B) -------
def _summarize_repair_changes(result: dict) -> str:
    """ASCII one-line summary of what a repair round changed (workflow-level fields +
    per-step changed field names), read off the repair result's diff markers. For the
    CLI --auto-fix round log; Windows-console safe."""
    bits = []
    wf_ch = result.get("workflow_repaired_fields") or []
    if wf_ch:
        bits.append("workflow[" + ", ".join(wf_ch) + "]")
    for i, st in enumerate(result.get("fields", {}).get("steps") or []):
        rf = st.get("repaired_fields") or []
        if rf:
            bits.append(f"step {i + 1}[" + ", ".join(rf) + "]")
    return "; ".join(bits)


def batch_result_human(view: dict) -> str:
    """The human-readable batch rollup, shared by the in-process AND --remote CLI arms so
    the two transports print identically. `view` is the batch_as_dict shape (also the
    /api/batches/{id} + /api/workflows/{id}/run-batch body)."""
    c = view.get("counts", {})
    head = (
        f"Batch #{view.get('id')} {view.get('status')}: "
        f"{c.get('completed', 0)} of {c.get('requested', 0)} locale(s) completed, "
        f"{c.get('failed', 0)} failed"
        + (f", {c.get('paused', 0)} paused" if c.get("paused") else "")
        + (f", {c.get('unlaunched', 0)} not launched" if c.get("unlaunched") else "")
        + "."
    )
    lines = [head]
    for m in view.get("members", []):
        lines.append(f"  {m.get('locale')}: {m.get('status')} (run #{m.get('run_id')})")
    for loc in view.get("unlaunched_locales", []):
        lines.append(f"  {loc}: not launched")
    if view.get("notes"):
        lines.append(f"  note: {view['notes']}")
    if view.get("error"):
        error = view["error"]
        lines.append(
            f"  {error['code']}: {error['reason']} Queued members: {error['queued_members']}"
        )
    return "\n".join(lines)


_CELL_CODE = {
    "completed": "ok",
    "passed": "ok",
    "failed": "FAIL",
    "running": "run",
    "paused": "rev",
    "skipped": "skip",
    "pending": ".",
}


# --- batch monitor + actions (SPEC-workflows-redesign Phase 5b) ------------------
# Short, ASCII-only per-cell status codes for the `workflow batch show` matrix (the CLI
# is ASCII-safe — verify_cli_ascii — so NO unicode chips/em-dash; a diverged-variant gap
# is a plain '-', a missing/pending cell '.').
def _ascii_cell(c) -> str:
    """One ASCII cell token: a status code + optional score + 'xN' loop-visit marker, or
    '-' where a diverged variant lacks that column."""
    if c is None:
        return "-"
    code = _CELL_CODE.get(c.get("status"), (c.get("status") or ".")[:4])
    score = c.get("score")
    if score is not None:
        code += f"({score:g})" if isinstance(score, (int, float)) else f"({score})"
    if (c.get("visits") or 0) > 1:
        code += f"x{c['visits']}"
    return code


def _batch_or_usage(session, batch_id, oid):
    """Fetch a batch, org-scoped (a foreign/missing batch is a 404-equivalent usage error,
    not enumerable — mirrors _wf_show_run's run scoping)."""
    from app.models import Document, Project, WorkflowBatch

    batch = session.get(WorkflowBatch, batch_id)
    if batch is None:
        raise CliError(f"no such batch: {batch_id}", EXIT_USAGE)
    if oid is not None:
        doc = session.get(Document, batch.document_id)
        proj = session.get(Project, doc.project_id) if doc else None
        if proj is None or getattr(proj, "org_id", None) != oid:
            raise CliError(f"no such batch: {batch_id}", EXIT_USAGE)
    return batch


def _run_step_summary(step_events) -> list[dict]:
    """A compact, machine-readable per-step trace built from the orchestrator's `step`
    events (#20a). Trims the events to the fields worth reporting on a run summary
    (index/name/type/provider/model/attempts/error and the LQA score+gate when the step
    scored), dropping the potentially-large `output` blob."""
    out = []
    for ev in step_events:
        row = {
            "index": ev.get("step_index"),
            "name": ev.get("step_name") or "",
            "type": str(ev.get("step_type") or ""),
            "provider": ev.get("provider"),
            "model": ev.get("model"),
            "attempts": ev.get("attempts"),
            "error": ev.get("error"),
        }
        if ev.get("lqa"):
            row["lqa"] = ev["lqa"]
        out.append(row)
    return out


def _run_step_line(row: dict) -> str:
    """One ASCII console line for a step_summary row (#20a)."""
    parts = [
        f"[{row.get('index')}]",
        row.get("name") or row.get("type") or "step",
        f"({row.get('type')})",
    ]
    if row.get("provider"):
        parts.append(
            f"provider={row['provider']}" + (f"/{row['model']}" if row.get("model") else "")
        )
    if row.get("attempts"):
        parts.append(f"attempts={row['attempts']}")
    lqa = row.get("lqa")
    if lqa:
        parts.append(f"lqa={lqa.get('score')} gate={lqa.get('gate')}")
        if lqa.get("adjudicated"):
            parts.append(f"adjudicated={lqa['adjudicated']}")
    parts.append("ERROR: " + str(row["error"]) if row.get("error") else "ok")
    return " ".join(parts)


# --- workflow show-run (inspect a paused run's review text) -------------------
def _wf_show_run(args, emit) -> int:
    """Issue #23 (a): read a run's status and, for a PAUSED human-review run, the
    text-to-review with EXPLICIT, machine-usable segment boundaries — so a headless
    operator can inspect (and safely edit via `resume --edited-segments`) without ever
    hand-touching the delimiter."""
    if remote_mode(args):
        return _remote_impl.workflow_show_run(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.kernel import orchestrator, run_overlay
    from app.models import Document, Project, StepRun, WorkflowRun

    oid = op_org_id(args)
    with _session() as session:
        run = session.get(WorkflowRun, args.run_id)
        if run is None:
            raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        if oid is not None:
            doc = session.get(Document, run.document_id)
            proj = session.get(Project, doc.project_id) if doc else None
            if proj is None or getattr(proj, "org_id", None) != oid:
                raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        view = orchestrator.paused_review(session, args.run_id)
        status = str(run.status)
        # Parity with the web run-view GRAPH (SPEC-conditional-workflows Phase 4 deferred
        # item): the per-step-key status/visits the graph draws, available headless.
        step_runs = session.exec(
            select(StepRun).where(StepRun.workflow_run_id == args.run_id).order_by(StepRun.id)
        ).all()
        steps = orchestrator.get_steps(session, run.workflow_id)
        graph = run_overlay.run_step_states(step_runs, steps, run_status=run.status)
        graph["qe_steps"] = (run.context or {}).get("qe_steps")
        # run-190 Item C: the per-step FAILURE SHAPE (metadata only), attached BY DEFAULT to
        # mirror GET /api/runs/{id} (local/remote KEY-IDENTICAL).
        graph["failure_shape"] = run_overlay.failure_shape(step_runs, steps)
        # --visits (CLI-parity backlog item 3): additive, opt-in per-VISIT row-level
        # trace (status_states above is latest-per-step only). Computed ONLY when
        # asked, so the default payload/output stays byte-identical without it.
        if getattr(args, "visits", False):
            graph["visit_history"] = run_overlay.visit_history(step_runs, steps)
        # --findings (BUG-07): the LQA MQM findings block, read-only from the run's
        # ALREADY-PERSISTED scorecard (WorkflowRun.context["lqa_findings_block"] --
        # the orchestrator's LQA step renders + stores it there so a resume can feed
        # it back to a fixer step; this is a pure read of that same field, no re-run,
        # no spend). None when the run never reached an LQA step.
        findings = (
            (run.context or {}).get("lqa_findings_block")
            if getattr(args, "findings", False)
            else None
        )
        # BUG-09(1): mirror the /api run-detail envelope's metadata fields exactly
        # (workflow_id/document_id/target_locale/created_by/started_at/finished_at) --
        # local and --remote must stay KEY-IDENTICAL (verify_multiorg's envelope
        # parity check), so these are unconditional here too, not just server-side.
        meta = {
            "workflow_id": run.workflow_id,
            "document_id": run.document_id,
            "target_locale": run.target_locale,
            "created_by": run.created_by or None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            # Run-level warnings, mirrored across local and remote views.
            "warnings": (run.context or {}).get("warnings") or [],
            **(
                {"tm_offers": run.context["tm_offers"]}
                if (run.context or {}).get("tm_offers") is not None
                else {}
            ),
            # #610: how many segments the tm_match step filled from the TM ({filled, relocked,
            # unmatched, total}); absent on a run with no segmented tm_match step.
            **(
                {"tm_match": run.context["tm_match_summary"]}
                if (run.context or {}).get("tm_match_summary") is not None
                else {}
            ),
            # #101 / #106: mirrored KEY-IDENTICALLY with the /api run envelope (local and
            # --remote must stay identical). `progress` = "N of M segments/chunks" published
            # from INSIDE a long step + when it last moved; `reason` = why a reconciler put
            # this run in its terminal state. Both null on a run that has neither.
            "progress": orchestrator.run_progress(run),
            "reason": orchestrator.run_reason(run),
            # Run notes, mirrored KEY-IDENTICALLY with the /api run envelope (local and
            # --remote must stay identical). A glossary skip / content-type skip note;
            # null when the run has none.
            "notes": run.notes or None,
        }
        run_warnings = meta["warnings"]
    states_block = run_overlay.format_step_states_ascii(graph)
    visits_block = (
        run_overlay.format_visit_history_ascii(graph.get("visit_history"))
        if getattr(args, "visits", False)
        else ""
    )
    # L4 (gate round 1): the failure-shape block prints in the HUMAN output too (it already
    # rode --json on both arms + rendered in run_view.html; the CLI human view did not).
    # Empty string on a clean run -> byte-identical to before.
    failure_block = run_overlay.format_failure_shape_ascii(graph.get("failure_shape"))
    # #101 / #106: the in-step progress line and the terminal reason line, rendered through
    # the SAME run_overlay formatters the --remote arm uses. Empty strings when the run has
    # neither -> byte-identical output for every run that came before.
    progress_block = run_overlay.format_progress_ascii(meta.get("progress"))
    reason_block = run_overlay.format_reason_ascii(meta.get("reason"))
    notes = meta.get("notes")
    notes_block = f"  note: {notes}" if notes else ""
    findings_extra = {"lqa_findings": findings} if getattr(args, "findings", False) else {}
    # Both paused and completed views display warnings and offer counts.
    warn_block = "".join("\n  ! " + w.get("message", "") for w in run_warnings)
    if meta.get("tm_offers"):
        warn_block += "\n  " + meta["tm_offers"]["message"]
    warn_block += run_overlay.format_tm_summary_ascii(meta.get("tm_match"))
    if view is None:
        # Not paused: report status + the per-step trace (no live review to hand out).
        line = f"Run #{args.run_id}: status={status} (no paused human-review step)."
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
        emit.result(
            line,
            {
                "ok": True,
                "run_id": args.run_id,
                "status": status,
                "paused": False,
                **meta,
                **graph,
                **findings_extra,
            },
        )
        return 0
    ps = view["paused_step"]
    if emit.as_json:
        emit.json({"ok": True, "paused": True, **view, **meta, **graph, **findings_extra})
        return 0
    emit.say(f"Run #{args.run_id}: status={view['status']} (PAUSED for human review)")
    if reason_block:
        emit.say(reason_block)
    if progress_block:
        emit.say(progress_block)
    if notes_block:
        emit.say(notes_block)
    if warn_block:
        emit.say(warn_block.lstrip("\n"))
    emit.say(
        f"  paused step: index={ps['index']} type={ps['type']}"
        f"{' key=' + ps['key'] if ps['key'] else ''}"
        f"{' name=' + ps['name'] if ps['name'] else ''}"
    )
    emit.say(f"  {view['segment_count']} segment(s) to review:")
    for i, seg in enumerate(view["segments"]):
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
        f"  python -m app.cli workflow resume {args.run_id} "
        f"--edited-segments <json-file> --locale {view['target_locale'] or '<locale>'}"
    )
    return 0


# --- workflow step-diff (GitHub #340: what each step changed) -----------------
def _wf_step_diff_text(view) -> str:
    """The human output of a step diff, shared by the in-process AND --remote CLI
    arms so both transports print IDENTICALLY (run_overlay convention). ASCII-only:
    insertions in [+ ... +], deletions in [- ... -], unchanged segments as plain
    text. The word markers double as the non-colour a11y signal on this surface."""
    frm = view.get("from") or {}
    to = view.get("to") or {}
    steps = view.get("steps") or []
    overview = ["  step runs (use the # as --from / --to):"]
    for st in steps:
        label = (
            f"    #{st.get('step_run_id')} {st.get('step_name') or st.get('step_type') or 'step'}"
        )
        if st.get("changed") is not None:
            label += (
                f": {st.get('changed')} of {st.get('total')} segments changed "
                f"vs #{st.get('vs_step_run_id')}"
            )
        elif not st.get("recorded"):
            label += ": not recorded"
        overview.append(label)
    if not frm.get("step_run_id") or not to.get("step_run_id"):
        return "\n".join(
            [f"Run #{view.get('run_id')}: no comparable pair recorded for this run."]
            + (overview if steps else [])
        )
    lines = [
        f"Run #{view.get('run_id')}: step diff #{frm.get('step_run_id')} "
        f"{frm.get('step_name') or frm.get('step_type') or ''} -> "
        f"#{to.get('step_run_id')} {to.get('step_name') or to.get('step_type') or ''}"
    ]
    not_rec = view.get("not_recorded") or {}
    if view.get("changed") is None:
        missing = [label for label in ("from", "to") if not_rec.get(label)]
        lines.append(f"  not recorded for the {' and '.join(missing)} step: nothing to compare")
        lines.extend(overview if steps else [])
        return "\n".join(lines)
    lines.append(f"  changed: {view.get('changed')} of {view.get('total')} segments")
    lines.extend(overview if steps else [])
    if not_rec.get("from") or not_rec.get("to"):
        missing = [label for label in ("from", "to") if not_rec.get(label)]
        lines.append(f"  (not recorded for the {' and '.join(missing)} step)")
    for seg in view.get("segments") or []:
        if seg.get("changed"):
            bits = []
            for op, text in seg.get("diff") or []:
                if op == "insert":
                    bits.append(f"[+ {text} +]" if text.strip() else text)
                elif op == "delete":
                    bits.append(f"[- {text} -]" if text.strip() else text)
                else:
                    bits.append(text)
            lines.append(f"  [{seg.get('index')}] changed: " + "".join(bits))
        else:
            lines.append(f"  [{seg.get('index')}] unchanged: {seg.get('after')}")
    return "\n".join(lines)


def _wf_step_diff(args, emit) -> int:
    """GitHub #340: the per-segment word diff between two step runs of ONE run --
    what a post-edit / fix round changed vs the previous translating step. Read-only
    (SEG_JOINER-split the StepRun blobs the orchestrator already recorded; no
    provider call, no run state). --json = the /api payload; default pair = the
    run's newest translating output vs its previous translating step."""
    if remote_mode(args):
        return _remote_impl.workflow_step_diff(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.kernel import orchestrator, run_overlay
    from app.models import Document, Project, StepRun, WorkflowRun

    oid = op_org_id(args)
    with _session() as session:
        run = session.get(WorkflowRun, args.run_id)
        if run is None:
            raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        if oid is not None:
            doc = session.get(Document, run.document_id)
            proj = session.get(Project, doc.project_id) if doc else None
            if proj is None or getattr(proj, "org_id", None) != oid:
                raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        step_runs = session.exec(
            select(StepRun).where(StepRun.workflow_run_id == args.run_id).order_by(StepRun.id)
        ).all()
        steps = orchestrator.get_steps(session, run.workflow_id)
        if args.step_from is not None or args.step_to is not None:
            from_id = args.step_from
            to_id = args.step_to
            run_ids = {sr.id for sr in step_runs}
            if from_id not in run_ids or to_id not in run_ids:
                raise CliError(
                    "--from/--to must both be StepRun ids of this run "
                    "(run `workflow step-diff <run_id>` to list them).",
                    EXIT_USAGE,
                )
        else:
            from_id, to_id = run_overlay.step_diff_default_pair(step_runs, steps)
        view = run_overlay.step_diff_view(run, step_runs, steps, from_id=from_id, to_id=to_id)
    if emit.as_json:
        emit.json({"ok": True, **view})
        return 0
    emit.result(_wf_step_diff_text(view), {"ok": True, **view})
    return 0


# --- workflow resume (synchronous) -------------------------------------------
def _load_edited_segments(path: str):
    """Read a --edited-segments JSON file into a list[str], with clean CliError on a
    missing file / bad JSON / wrong shape (validated BEFORE any run-state change)."""
    import json
    import os

    if not os.path.isfile(path):
        raise CliError(f"edited-segments file not found: {path}", EXIT_USAGE)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CliError(f"could not read edited-segments JSON: {exc}", EXIT_USAGE) from exc
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise CliError("edited-segments file must contain a JSON array of strings.", EXIT_USAGE)
    return data


# --- workflow cancel-run (abort an in-flight run) ----------------------------
def _wf_cancel_run(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_cancel_run(args, emit)
    require_org_scope(args, emit)
    from app.kernel import orchestrator
    from app.models import Document, Project, WorkflowRun

    oid = op_org_id(args)
    with _session() as session:
        run = session.get(WorkflowRun, args.run_id)
        if run is None:
            raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        if oid is not None:
            doc = session.get(Document, run.document_id)
            proj = session.get(Project, doc.project_id) if doc else None
            if proj is None or getattr(proj, "org_id", None) != oid:
                raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        try:
            out = orchestrator.cancel_run(
                session, args.run_id, actor=getattr(args, "actor", None) or "cli"
            )
        except orchestrator.RunCancelError as exc:
            raise CliError(str(exc), EXIT_FAIL) from exc
    emit.result(
        f"Run #{args.run_id} CANCELLED ({out['legs_closed']} open vendor leg(s) closed). "
        f"It is terminal and cannot be resumed; a new run can be launched.",
        {"ok": True, **out},
    )
    return 0


# --- multilingual workflow groups (P5, item 4) -------------------------------
def _parse_pair(spec: str):
    """Parse a 'src:tgt' pair string into (src, tgt). Accepts ':' or '->' as the
    separator. Raises CliError on a malformed spec."""
    raw = (spec or "").replace("->", ":").split(":")
    raw = [x.strip() for x in raw if x.strip()]
    if len(raw) != 2:
        raise CliError(f"invalid pair {spec!r} (want src:tgt, e.g. en-US:fr-FR).", EXIT_USAGE)
    return raw[0], raw[1]


def _grp_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_group_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        groups = workflows.list_groups(session, org_id=op_org_id(args))
        rows = [
            (g.id, g.name, g.customer_id or "global", len(workflows.group_variants(session, g.id)))
            for g in groups
        ]
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "groups": [
                        {
                            "id": g.id,
                            "name": g.name,
                            "customer_id": g.customer_id,
                            "variants": len(workflows.group_variants(session, g.id)),
                        }
                        for g in groups
                    ],
                }
            )
        else:
            emit.table(rows, headers=("id", "name", "customer", "variants"))
    return 0


def _grp_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_group_show(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        # #515: a group of another org is "no such group" under --org (it used a raw get).
        grp = _get_grp(session, args.group_id, op_org_id(args))
        variants = workflows.group_variants(session, grp.id)
        vrows = [
            {
                "workflow_id": v.id,
                "name": v.name,
                "source": v.source_locale,
                "target": v.target_locale,
                "default": v.is_default,
            }
            for v in variants
        ]
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "id": grp.id,
                    "name": grp.name,
                    "customer_id": grp.customer_id,
                    "variants": vrows,
                }
            )
        else:
            emit.say(
                f"Group #{grp.id}: {grp.name} "
                f"({'customer ' + str(grp.customer_id) if grp.customer_id else 'global'}, "
                f"{len(variants)} variant(s))"
            )
            emit.table(
                [
                    (
                        v["workflow_id"],
                        v["name"],
                        (v["source"] or "*") + "->" + (v["target"] or "*"),
                        "yes" if v["default"] else "",
                    )
                    for v in vrows
                ],
                headers=("wf_id", "name", "pair", "default"),
            )
    return 0


def _get_grp(session, group_id: int, org_id=None):
    from app.models import WorkflowGroup

    return get_scoped_or_404(session.get(WorkflowGroup, group_id), group_id, "group", org_id)


# --- reusable prompt templates (list, rename, delete) ------------------------
def _get_pt(session, template_id: int, org_id=None):
    from app.kernel import workflows

    return get_scoped_or_404(
        workflows.get_prompt_template(session, template_id), template_id, "prompt template", org_id
    )


def _pt_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.prompt_template_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit, workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        try:
            # In-process CLI is a TRUSTED OPERATOR surface (no customer-bound identity),
            # so bound_customer_id stays None — exactly like project create. --customer
            # (validated active/same-org/not-archived in the service) attaches it; omit
            # for GLOBAL. A remote customer-bound caller would go through /api instead.
            tmpl = workflows.create_prompt_template(
                session,
                args.name,
                body=args.body,
                org_id=op_org_id(args),
                customer_id=args.customer_id,
            )
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if tmpl is None:
            raise CliError("prompt template name is blank.", EXIT_USAGE)
        audit.log(
            actor=actor,
            action="prompt_template.create",
            target=f"prompt_template {tmpl.id}",
            detail=f"scope={tmpl.scope}, customer={tmpl.customer_id or 'global'}",
        )
        emit.result(
            f"Created prompt template #{tmpl.id}: {tmpl.name} "
            f"({tmpl.scope}{', customer #%d' % tmpl.customer_id if tmpl.customer_id else ''}).",  # noqa: UP031
            {
                "ok": True,
                "id": tmpl.id,
                "name": tmpl.name,
                "scope": tmpl.scope,
                "customer_id": tmpl.customer_id,
            },
        )
    return 0


def _pt_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.prompt_template_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        # In-process CLI is the trusted operator (unbound) — customer_id stays None so
        # it lists every org-scoped template (byte-identical to today), incl. customer-
        # attached ones, labelled with their scope/customer.
        tmpls = workflows.list_prompt_templates(session, org_id=op_org_id(args))
        # #20(k): the count is "how many workflow STEPS pin this prompt template", not
        # the template's own step count -> the column/key is 'pinned_by', not 'steps'
        # (which read as a workflow's step count and misled operators).
        rows = [
            {
                "id": t.id,
                "name": t.name,
                "scope": t.scope,
                "customer_id": t.customer_id,
                "pinned_by": workflows.prompt_template_reference_count(session, t.id)["steps"],
            }
            for t in tmpls
        ]
        if emit.as_json:
            emit.json({"ok": True, "prompt_templates": rows})
        else:
            emit.table(
                [
                    (
                        r["id"],
                        r["name"],
                        r["scope"] if not r["customer_id"] else "customer #%d" % r["customer_id"],  # noqa: UP031
                        r["pinned_by"],
                    )
                    for r in rows
                ],
                headers=("id", "name", "scope", "pinned_by"),
            )
    return 0


def _pt_system(args, emit) -> int:
    """`workflow prompt system [--key K]`: the pipeline SYSTEM prompts (GitHub #356). The
    built-in text is the same for every caller; with overrides on it also shows the org's
    editable heads and what its runs send. --remote and the in-process arm print through the
    SAME emitter, from the same `system_prompts.catalogue_payload` body. With `--workflow N` it
    shows that workflow's own overrides instead (GitHub #356 phase 2b)."""
    if getattr(args, "workflow", None) is not None:
        from app.cli import workflow_system_prompts

        return workflow_system_prompts.system_show(args, emit)
    if remote_mode(args):
        return _remote_impl.system_prompt_list(args, emit)
    require_org_scope(args, emit)
    from app.cli._remote_impl_part6 import _emit_system_prompts
    from app.kernel import system_prompts

    with _session() as session:
        body = system_prompts.catalogue_payload(session, op_org_id(args))
    return _emit_system_prompts(args, emit, body)


def _sp_write_gate(system_prompts) -> None:
    """The in-process arm of the API's flag gate (409 there): refuse naming the switch."""
    if not system_prompts.overrides_enabled():
        raise CliError(system_prompts.OFF_MESSAGE, EXIT_FAIL)


def _sp_text(args) -> str:
    """The new head text from --body or --file (exactly one)."""
    body = getattr(args, "body", None)
    path = getattr(args, "file", None)
    if (body is None) == (path is None):
        raise CliError("give exactly one of --body TEXT or --file PATH.", EXIT_USAGE)
    if body is not None:
        return body
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror or exc}", EXIT_USAGE) from exc


def _pt_system_set(args, emit) -> int:
    """`workflow prompt system-set --key K (--body TEXT | --file PATH)`: save a new version of
    an editable system-prompt head. Admin only over --remote (with --workflow: admin or
    manager, #630; the server checks); the
    in-process CLI is the trusted operator. Refused while the feature flag is off."""
    text = _sp_text(args)
    if getattr(args, "workflow", None) is not None:
        from app.cli import workflow_system_prompts

        return workflow_system_prompts.system_set(args, emit, text)
    if remote_mode(args):
        return _remote_impl.system_prompt_set(args, emit, text)
    require_org_scope(args, emit)
    from app.cli._remote_impl_part6 import _emit_system_prompt_saved
    from app.kernel import system_prompts

    _sp_write_gate(system_prompts)
    if args.key not in system_prompts.EDITABLE_KEYS:
        raise CliError(f"{args.key!r} is not an editable system prompt.", EXIT_USAGE)
    with _session() as session:
        try:
            out = system_prompts.save_override(
                session, op_org_id(args), args.key, text, getattr(args, "actor", None) or "cli"
            )
        except system_prompts.OverrideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    return _emit_system_prompt_saved(args, emit, out, "Saved")


def _pt_system_reset(args, emit) -> int:
    """`workflow prompt system-reset --key K`: back to the built-in text (history is kept)."""
    if getattr(args, "workflow", None) is not None:
        from app.cli import workflow_system_prompts

        return workflow_system_prompts.system_reset(args, emit)
    if remote_mode(args):
        return _remote_impl.system_prompt_reset(args, emit)
    require_org_scope(args, emit)
    from app.cli._remote_impl_part6 import _emit_system_prompt_saved
    from app.kernel import system_prompts

    _sp_write_gate(system_prompts)
    if args.key not in system_prompts.EDITABLE_KEYS:
        raise CliError(f"{args.key!r} is not an editable system prompt.", EXIT_USAGE)
    with _session() as session:
        try:
            out = system_prompts.reset_override(
                session, op_org_id(args), args.key, getattr(args, "actor", None) or "cli"
            )
        except system_prompts.OverrideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    return _emit_system_prompt_saved(args, emit, out, "Reset")


def _pt_system_restore(args, emit) -> int:
    """`workflow prompt system-restore --key K --version N`: save a copy of an older version
    as the newest."""
    if getattr(args, "workflow", None) is not None:
        from app.cli import workflow_system_prompts

        return workflow_system_prompts.system_restore(args, emit)
    if remote_mode(args):
        return _remote_impl.system_prompt_restore(args, emit)
    require_org_scope(args, emit)
    from app.cli._remote_impl_part6 import _emit_system_prompt_saved
    from app.kernel import system_prompts

    _sp_write_gate(system_prompts)
    if args.key not in system_prompts.EDITABLE_KEYS:
        raise CliError(f"{args.key!r} is not an editable system prompt.", EXIT_USAGE)
    with _session() as session:
        try:
            out = system_prompts.restore_override(
                session,
                op_org_id(args),
                args.key,
                args.version,
                getattr(args, "actor", None) or "cli",
            )
        except system_prompts.OverrideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    return _emit_system_prompt_saved(args, emit, out, "Restored")


def _pt_system_history(args, emit) -> int:
    """`workflow prompt system-history --key K`: the version history of an editable head,
    newest first (read-only; available with the flag off)."""
    if getattr(args, "workflow", None) is not None:
        from app.cli import workflow_system_prompts

        return workflow_system_prompts.system_history(args, emit)
    if remote_mode(args):
        return _remote_impl.system_prompt_detail(args, emit)
    require_org_scope(args, emit)
    from app.cli._remote_impl_part6 import _emit_system_prompt_history
    from app.kernel import system_prompts

    if args.key not in system_prompts.EDITABLE_KEYS:
        raise CliError(f"{args.key!r} is not an editable system prompt.", EXIT_USAGE)
    with _session() as session:
        prompt = system_prompts.editable_detail(session, op_org_id(args), args.key)
    return _emit_system_prompt_history(args, emit, prompt)
