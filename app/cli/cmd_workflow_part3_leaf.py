"""cmd_workflow_part3: shared leaf helpers (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from app import branding
from app import db as _db
from app.cli import _remote_impl
from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _branch_label,
    _session,
    op_org_id,
    remote_mode,
    require_local,
    require_org_scope,
)
from app.cli.cmd_workflow_common import (
    _ascii_cell,
    _doc_locale,
    _get_grp,
    _get_pt,
    _get_wf,
    _load_edited_segments,
    _parse_pair,
    _reject_archived_customer,
    _run_doc,
    _summarize_repair_changes,
    batch_result_human,
)


# --- workflow rename ---------------------------------------------------------
def _wf_rename(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        wf = _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        old = wf.name
        try:
            workflows.rename_workflow(session, args.workflow_id, args.name)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="workflow.rename",
            target=f"workflow {args.workflow_id}",
            detail=f"{old!r} -> {wf.name!r}",
            customer_id=wf.customer_id,
        )
        emit.result(
            f"Renamed workflow #{args.workflow_id}: {old} -> {wf.name}.",
            {"ok": True, "id": args.workflow_id, "name": wf.name, "old_name": old},
        )
    return 0


# --- workflow archive / unarchive (residual sweep: archive verbs) ------------
def _wf_archive(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        try:
            wf = workflows.archive_workflow(
                session, args.workflow_id, actor=actor, org_id=op_org_id(args)
            )
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Archived workflow #{wf.id} ({wf.name}) -- retired from list/pickers/"
            f"dispatch, blocked from new runs.",
            {"ok": True, "id": wf.id, "name": wf.name, "archived": True},
        )
    return 0


def _wf_unarchive(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        try:
            wf = workflows.unarchive_workflow(
                session, args.workflow_id, actor=actor, org_id=op_org_id(args)
            )
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Unarchived workflow #{wf.id} ({wf.name}) -- listed + runnable again.",
            {"ok": True, "id": wf.id, "name": wf.name, "archived": False},
        )
    return 0


# --- workflow delete ---------------------------------------------------------
def _wf_delete(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        wf = _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        name = wf.name
        customer_id = wf.customer_id
        refs = workflows.workflow_reference_count(session, args.workflow_id)
        if not args.force:
            emit.result(
                f"Workflow #{args.workflow_id} ({name}) delete preview: would cascade "
                f"{refs['steps']} step(s), {refs['runs']} run(s), "
                f"{refs['step_runs']} step-run(s), {refs['workflow_batches']} fan-out "
                f"batch(es); would un-scope {refs['batch_members_unscoped']} sibling batch "
                f"member(s), {refs['eval_candidates_unscoped']} eval candidate(s), "
                f"{refs['memory_events_unscoped']} memory event(s). "
                f"Blockers: {refs['routing_rules']} routing rule(s), "
                f"{refs['customer_defaults']} customer default(s), {refs['active_runs']} "
                f"in-flight run(s) or fan-out batch member(s), {refs['live_batches']} "
                f"live fan-out batch(es), {refs['group_default']} group-default sibling(s), "
                f"{refs['memory_suggestions']} {branding.llmemory_name()} suggestion(s) "
                f"and {refs['vendor_dispatches']} vendor dispatch leg(s) "
                f"(archive, do not delete). Warnings (dangling, not blocking): "
                f"{refs['template_defaults']} project template(s), "
                f"{refs['project_defaults']} project default(s). "
                f"Re-run with --force to delete.",
                {"ok": True, "preview": True, "workflow_id": args.workflow_id, "references": refs},
            )
            return 0
        try:
            refs = workflows.delete_workflow(session, args.workflow_id)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc)) from exc  # default exit 1 (EXIT_FAIL)
        audit.log(
            actor=actor,
            action="workflow.delete",
            target=f"workflow {args.workflow_id} '{name}'",
            detail=(
                "cascaded %d step(s), %d run(s), %d step-run(s), %d batch(es); un-scoped "  # noqa: UP031
                "%d batch member(s), %d eval candidate(s), %d memory event(s)"
                % (
                    refs["steps"],
                    refs["runs"],
                    refs["step_runs"],
                    refs.get("workflow_batches", 0),
                    refs.get("batch_members_unscoped", 0),
                    refs.get("eval_candidates_unscoped", 0),
                    refs.get("memory_events_unscoped", 0),
                )
            ),
            customer_id=customer_id,
        )
        emit.result(
            f"Deleted workflow #{args.workflow_id} ({name}): cascaded "
            f"{refs['steps']} step(s), {refs['runs']} run(s), "
            f"{refs['step_runs']} step-run(s), {refs.get('workflow_batches', 0)} "
            f"fan-out batch(es); un-scoped {refs.get('batch_members_unscoped', 0)} "
            f"batch member(s), {refs.get('eval_candidates_unscoped', 0)} eval candidate(s), "
            f"{refs.get('memory_events_unscoped', 0)} memory event(s).",
            {"ok": True, "deleted": True, "workflow_id": args.workflow_id, "references": refs},
        )
    return 0


# --- workflow author-prompt --------------------------------------------------
def _wf_author(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import orchestrator
    from app.kernel.providers import DeadEngineError, ProviderCallError, ProviderTruncated

    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # 404 guard
    try:
        draft = orchestrator.author_prompt(
            args.description,
            args.provider or None,
            org_id=op_org_id(args),
            model=(getattr(args, "model", "") or None),
        )
    except ProviderCallError as exc:
        # --provider named a real engine whose direct call failed (bad key /
        # unreachable local server / malformed response / NMT engine with no prompt
        # channel): fail loud with a clean stderr message + non-zero exit, never a
        # traceback -- matching the DeadEngineError/ProviderTruncated handling below.
        raise CliError(str(exc)) from exc  # default exit code 1 (EXIT_FAIL)
    except DeadEngineError as exc:
        # --provider may name a dead engine:<id> ref (deleted/disabled/keyless/
        # undecryptable): fail loud with a clean stderr message + non-zero exit,
        # matching the CLI's convention for every other DeadEngineError site
        # (e.g. cmd_doc.py's translate), never a scary traceback.
        raise CliError(str(exc)) from exc  # default exit code 1 (EXIT_FAIL)
    except ProviderTruncated as e:
        # author_prompt() routes through run_completion, which ESCALATES the output
        # budget toward the model ceiling on truncation and re-raises this only when the
        # draft STILL overflows AT the ceiling -- the output genuinely exceeds what this
        # model can emit. Fail loud with a clean stderr message + non-zero exit, matching
        # the DeadEngineError handling above. ASCII-only for Windows consoles.
        raise CliError(
            "The model ran out of output space before finishing the prompt draft, even "
            "after automatically expanding the output budget to this model's ceiling. "
            "Pick a provider/model with a larger output token limit."
        ) from e  # exit 1
    emit.result(draft, {"ok": True, "workflow_id": args.workflow_id, "draft": draft})
    return 0


def _wf_author_setup(args, emit) -> int:
    # In-process: drafts + optionally creates on the host DB, with the --auto-fix repair
    # loop. --remote dispatches to the /api twin (a SINGLE apply, no repair loop — the
    # documented remote subset). The authoring call and --create reuse the SAME service
    # seams the web routes use (setup_author.author_workflow_setup + apply_workflow_draft),
    # so web+CLI stay byte-identical. ASCII-only output for Windows consoles.
    if remote_mode(args):
        return _remote_impl.workflow_author(args, emit)
    require_local(args)
    require_org_scope(args, emit)
    # --adapt is a deprecated no-op here: a designed workflow has no language variants
    # (it takes its languages from the project it runs on). Kept so old scripts do not
    # error; it prints one honest line instead.
    if getattr(args, "adapt", False):
        emit.warn("--adapt is ignored: a designed workflow has no language variants")
    from app.kernel import orchestrator, setup_author

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        try:
            draft = setup_author.author_workflow_setup(
                session,
                description=args.description,
                provider=(args.provider or None),
                org_id=op_org_id(args),
                actor=actor,
                bound_customer_id=None,
                model=(getattr(args, "model", "") or None),
            )
        except setup_author.SetupAuthorError as exc:
            # Clean stderr message + non-zero exit, never a traceback (matches the
            # workflow author-prompt / project author CLI convention).
            raise CliError(str(exc)) from exc
        fields = draft["fields"]
        warnings = list(draft["warnings"])

        # --create: apply the draft via the SAME apply_workflow_draft seam the web
        # confirm route uses. Warnings print but do NOT block; a remaining step-level
        # validation error (e.g. a prompt missing {text}) REFUSES with a non-zero exit.
        if getattr(args, "create", False):
            for w in warnings:
                emit.warn(w)
            # --auto-fix N: on a validation failure, run the SAME repair seam as the web
            # "Fix this for me" button up to N times (default 2 when the flag is given),
            # printing each round's errors + what changed. `rounds` = 0 means no auto-fix
            # (legacy behavior: a single apply attempt that refuses on the first error).
            auto_fix = getattr(args, "auto_fix", None)
            rounds = auto_fix if auto_fix is not None else 0
            current = fields
            extra_warnings: list = []
            for attempt in range(rounds + 1):
                # SPEC-lqa-step-honest-options: strip-and-warn fields (a drafted lqa/
                # human_review step that ignored a now-dead field) collect here rather than
                # blocking --create; printed as warnings + included in the JSON result. A
                # FAILED apply is atomic (creates nothing), so a repair round starts clean.
                strip_warnings: list = []
                try:
                    wf = setup_author.apply_workflow_draft(
                        session,
                        current,
                        org_id=op_org_id(args),
                        actor=actor,
                        bound_customer_id=None,
                        warnings=strip_warnings,
                    )
                except setup_author.WorkflowApplyError as exc:
                    if attempt >= rounds:
                        # No auto-fix left: refuse cleanly (non-zero) with the errors.
                        raise CliError("; ".join(exc.errors) or str(exc), EXIT_USAGE) from exc
                    emit.warn(
                        f"auto-fix round {attempt + 1}/{rounds}: "
                        f"{len(exc.errors)} validation error(s) to fix:"
                    )
                    for e in exc.errors:
                        emit.warn(f"  - {e}")
                    try:
                        result = setup_author.repair_workflow_draft(
                            session,
                            payload=current,
                            errors=exc.errors,
                            provider=(args.provider or None),
                            org_id=op_org_id(args),
                            actor=actor,
                            bound_customer_id=None,
                            model=(getattr(args, "model", "") or None),
                        )
                    except setup_author.SetupAuthorError as rexc:
                        raise CliError(f"auto-fix could not run: {rexc}", EXIT_USAGE) from rexc
                    changed = _summarize_repair_changes(result)
                    emit.warn(f"  repaired: {changed or '(no changes)'}")
                    # F2: honestly surface any steps the repair dropped.
                    for note in result.get("removed_notes") or []:
                        emit.warn(f"  {note}")
                    current = result["fields"]
                    extra_warnings.extend(result["warnings"] + (result.get("removed_notes") or []))
                    continue
                except setup_author.SetupAuthorError as exc:
                    raise CliError(str(exc), EXIT_USAGE) from exc
                # Success: created atomically. Surface strip + repair warnings.
                for w in strip_warnings:
                    emit.warn(w)
                n = len(orchestrator.get_steps(session, wf.id))
                emit.result(
                    f"Created workflow #{wf.id}: {wf.name} ({n} step(s)).",
                    {
                        "ok": True,
                        "id": wf.id,
                        "name": wf.name,
                        "steps": n,
                        "warnings": warnings + extra_warnings + strip_warnings,
                    },
                )
                return 0

        # Default: print the validated draft + a WARNINGS block; exit 0.
        if emit.as_json:
            emit.json({"ok": True, "draft": fields, "warnings": warnings})
        else:
            emit.say("Drafted workflow setup:")
            emit.say(f"  name: {fields['name'] or '(none)'}")
            emit.say(f"  customer: {fields['customer_name'] or '(none)'}")
            emit.say(f"  steps ({len(fields['steps'])}):")
            for idx, s in enumerate(fields["steps"], 1):
                bits = [s["step_type"]]
                if s["provider"]:
                    bits.append(s["provider"])
                if s["qe_gate"]:
                    bits.append(f"qe_gate>={s['qe_threshold']}")
                # feat/lqa-configurable-threshold: an lqa step's configured pass bar.
                # "floor" because it is the numeric floor only -- a major/critical MQM
                # error routes regardless of score (Q7 discoverability).
                if s.get("lqa_threshold") is not None:
                    bits.append(f"lqa-floor>={s['lqa_threshold']}")
                if s["needs_review"]:
                    bits.append("needs_review")
                if s["glossary_name"]:
                    bits.append(f"glossary:{s['glossary_name']}")
                # Phase 2/3/4 parity: the CLI review output shows the SAME conditional
                # graph the web review screen renders — the pre-step guard, the stable
                # key, the post-step branch (ASCII arrows, loop-backs marked), and the
                # loop cap. All ASCII-safe for Windows consoles.
                if s.get("key"):
                    bits.append(f"key:{s['key']}")
                rc = s.get("run_condition")
                if rc:
                    rv = f":{rc['value']}" if rc.get("value") is not None else ""
                    bits.append(f"guard:{rc['ref']} {rc['op']}{rv}")
                if s.get("branch"):
                    bits.append(f"branch[{_branch_label(s['branch'])}]")
                if s.get("max_visits"):
                    bits.append(f"max_visits:{s['max_visits']}")
                emit.say(f"    {idx}. {s['name'] or '(unnamed)'} [{', '.join(bits)}]")
                if s.get("prompt_warning"):
                    emit.say(f"       ! {s['prompt_warning']}")
            if fields["notes_for_reviewer"]:
                emit.say(f"  notes: {fields['notes_for_reviewer']}")
            if warnings:
                emit.say("WARNINGS:")
                for w in warnings:
                    emit.say(f"  - {w}")
    return 0


def _wf_run_batch(args, emit) -> int:
    """Run a workflow across MANY of a document's locales as one batch, SYNCHRONOUSLY
    in-process (Phase 5a). Prints per-locale results as they complete + a final rollup;
    --json emits the batch object (members[] + counts). Never spawns a daemon that would
    die with the one-shot CLI process — start_batch(synchronous=True) joins every member
    before returning."""

    from app.kernel import batch_runs, orchestrator
    from app.models import WorkflowRun

    oid = op_org_id(args)
    locales = None
    if not args.all_locales:
        locales = [x.strip() for x in (args.locales or "").split(",") if x.strip()]
        if not locales:
            raise CliError(
                "--locales was empty; give a comma-separated locale list or use --all-locales.",
                EXIT_USAGE,
            )

    def _on_done(run_id: int) -> None:
        # Prints as each member reaches terminal (suppressed under --json/--quiet by say()).
        with batch_runs._batch_session() as s:
            r = s.get(WorkflowRun, run_id)
            if r is not None:
                emit.say(f"  {r.target_locale}: {r.status} (run #{r.id})")

    with _session() as session:
        _run_doc(session, args.doc_id, oid)
        # Reuse the single-run org/visibility check for the workflow (raises if foreign).
        _get_wf(session, args.workflow_id, oid)
        emit.say(f"Batch: dispatching workflow #{args.workflow_id} over document #{args.doc_id}...")
        try:
            batch = batch_runs.start_batch(
                session,
                workflow_id=args.workflow_id,
                doc_id=args.doc_id,
                locales=locales,
                created_by=getattr(args, "actor", None) or "cli",
                max_parallel=args.max_parallel,
                fail_fast=bool(args.fail_fast),
                synchronous=True,
                on_member_done=_on_done,
            )
        except batch_runs.BatchError as exc:
            raise CliError(
                str(exc), EXIT_FAIL if isinstance(exc, batch_runs.BatchMissing) else EXIT_USAGE
            ) from exc
        except _db.WriteContentionError as exc:
            # flood R2-F1 parity: a transient writer-lock the retry could not ride out is a
            # RETRYABLE failure (EXIT_FAIL), not a usage error — the honest CLI twin of the
            # /api 503. Nothing was launched (the batch-row insert rolled back).
            raise CliError(str(exc), EXIT_FAIL) from exc
        except orchestrator.RunCapacityError as exc:
            # Same shape for a CAPACITY refusal (gate A round 1, MED-1): the batch reserves
            # all `max_parallel` slots before the batch row is written, so a refusal means
            # nothing was created and retrying later is the correct move.
            raise CliError(str(exc), EXIT_FAIL) from exc
        view = batch_runs.batch_as_dict(session, batch)
    emit.result(
        batch_result_human(view),
        {"ok": view["status"] != "failed" and not view.get("error"), **view},
    )
    # A batch where every launched member FAILED is a hard failure (exit 1); a partial
    # (at least one locale completed / paused) is a success exit with the rollup shown.
    return EXIT_FAIL if view["status"] == "failed" or view.get("error") else 0


def batch_matrix_ascii(view: dict) -> str:
    """Render a batch_as_dict(detail=True) view as an ASCII locale x steps matrix — shared by
    the in-process AND --remote CLI arms so the two transports print identically. ASCII-only
    (verify_cli_ascii): a plain box table, short status codes, and a legend."""
    from app.kernel import batch_runs

    matrix = batch_runs.build_matrix(view)
    c = view.get("counts", {})
    head = (
        f"Batch #{view.get('id')} {view.get('status')}: "
        f"{c.get('completed', 0)} of {c.get('requested', 0)} completed, "
        f"{c.get('failed', 0)} failed"
        + (f", {c.get('paused', 0)} in review" if c.get("paused") else "")
        + (f", {c.get('unlaunched', 0)} not launched" if c.get("unlaunched") else "")
        + "."
    )
    lines = [head]
    cols = matrix["columns"]
    # Column headers: short step names (or the step type). Cap width so a wide axis stays
    # readable in a terminal; cells are right-padded to the same width.
    hdrs = ["Locale".ljust(12), "Status".ljust(9)]
    colw = []
    for col in cols:
        label = (col.get("name") or col.get("step_type") or "step")[:12]
        w = max(len(label), 6)
        colw.append(w)
        hdrs.append(label.ljust(w))
    lines.append("  " + " | ".join(hdrs))
    for row in matrix["rows"]:
        loc = row["locale"][:12].ljust(12)
        if row["unlaunched"]:  # noqa: SIM108
            status = "unlaunched"
        else:
            status = str(row["status"])
        cells = [loc, status[:9].ljust(9)]
        for i, col in enumerate(cols):
            cells.append(_ascii_cell(row["cells"].get(col["key"]))[: colw[i]].ljust(colw[i]))
        marker = ""
        if row.get("rerun"):
            marker += f"  (attempt {row['attempt']})"
        if row.get("diverges"):
            marker += "  (variant differs)"
        if row.get("unlaunched") and row.get("reason"):
            marker += f"  ({row['reason']})"
        lines.append("  " + " | ".join(cells) + marker)
    lines.append(
        "  codes: ok=passed/completed  run=running  rev=in-review  "
        "FAIL=failed  skip=skipped  .=pending  -=step absent in this variant"
    )
    if matrix["diverged"]:
        lines.append(
            "  note: variants differ structurally across locales; each step keeps its own column."
        )
    if view.get("notes"):
        lines.append(f"  note: {view['notes']}")
    return "\n".join(lines)


def _wf_resume(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_resume(args, emit)
    require_org_scope(args, emit)
    from app.kernel import orchestrator
    from app.models import Document, Project, RunStatus, WorkflowRun

    oid = op_org_id(args)
    seg_path = (getattr(args, "edited_segments", "") or "").strip()
    edited_segments = _load_edited_segments(seg_path) if seg_path else None
    if edited_segments is not None and (args.edited_text or "").strip():
        raise CliError("pass either --edited-text or --edited-segments, not both.", EXIT_USAGE)
    with _session() as session:
        run = session.get(WorkflowRun, args.run_id)
        if run is None:
            raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        if oid is not None:
            doc = session.get(Document, run.document_id)
            proj = session.get(Project, doc.project_id) if doc else None
            if proj is None or getattr(proj, "org_id", None) != oid:
                raise CliError(f"no such run: {args.run_id}", EXIT_USAGE)
        if run.status != RunStatus.PAUSED:
            raise CliError(
                f"run #{args.run_id} is not paused (status={run.status}); "
                f"only a paused run can be resumed.",
                EXIT_USAGE,
            )
        # HIGH-1 (step isolation) + round-3 wedge guard: a run parked at an EXTERNAL vendor
        # step must not be advanced by a human resume -- that would overwrite the vendor's
        # content with the pre-dispatch source. Refuse for an OPEN leg (return via `vendor
        # return`/`vendor poll`) AND a WEDGED RETURNED-but-unresumed leg (recover via
        # `vendor redrive`).
        from app.kernel import vendor_dispatch as _vd

        _vleg, _vstate = _vd.vendor_pause_state(session, args.run_id)
        if _vleg is not None and _vd.blocks_human_resume(_vstate):
            if _vstate == "dispatched":
                _emsg = (
                    f"run #{args.run_id} is paused at an external vendor step; its "
                    "content must return through `vendor return` / `vendor poll`, not "
                    "a human resume."
                )
            elif _vstate == "retry_wait":
                _emsg = (
                    f"run #{args.run_id}'s external dispatch is being retried after a "
                    "transient failure; it cannot be resumed by hand while the package "
                    "is still trying to go out."
                )
            else:  # wedged
                _emsg = (
                    f"run #{args.run_id}'s external vendor step returned but did not "
                    f"resume (wedged); recover it with `vendor redrive "
                    f"--dispatch-id {_vleg.id}`, not a human resume."
                )
            raise CliError(_emsg, EXIT_USAGE)
        # Issue #493: the run's own target locale is the only locale it can resume in.
        if run.target_locale and args.locale and args.locale != run.target_locale:
            raise CliError(
                f"run #{args.run_id} targets {run.target_locale}; it cannot be resumed as "
                f"{args.locale}.",
                EXIT_USAGE,
            )
        locale = run.target_locale or args.locale or _doc_locale(session, run.document_id)
        if not locale:
            raise CliError("could not infer a target locale; pass --locale.", EXIT_USAGE)
        # Issue #23 (b): validate the edited payload's segment count BEFORE any state
        # change. On mismatch this raises ResumeMismatch -> CliError, and the run is
        # left untouched (still PAUSED, re-resumable) — never a terminal FAIL.
        try:
            # LLMemory capture seam 2 (PROPOSAL-llmemory §4.2): prepare_resume_seed
            # writes a MemoryEvent per segment the operator changed, attributed to
            # `actor` -- without it every CLI-resume-derived memory is anonymous
            # (mirrors the web/api resume routes' own actor plumbing).
            seed_text = orchestrator.prepare_resume_seed(
                session,
                args.run_id,
                edited_text=args.edited_text,
                edited_segments=edited_segments,
                actor=getattr(args, "actor", None) or "cli",
            )
        except orchestrator.ResumeMismatch as exc:
            raise CliError(str(exc), EXIT_FAIL) from exc
        summary = orchestrator.resume_workflow(session, args.run_id, locale, seed_text)
    n_steps = len(summary.get("steps", []))
    status = summary.get("status")
    if status == "paused":
        step_idx = summary.get("paused_step")
        emit.result(
            f"Run #{args.run_id} PAUSED again at human-review step {step_idx}. "
            f"Resume with:\n"
            f"  python -m app.cli workflow resume {args.run_id} "
            f'--edited-text "..." --locale {locale}',
            {
                "ok": True,
                "run_id": args.run_id,
                "status": "paused",
                "paused_step": step_idx,
                "steps": n_steps,
            },
        )
        return 0
    if status == "rejected":
        # A resume was REFUSED. TWO causes with DIFFERENT run states:
        #   * Issue #23 (b): a segment-count mismatch -- prepare_resume_seed pre-validates so the
        #     CLI should not normally reach here, but if the generator refused one the run is
        #     still PAUSED and re-resumable.
        #   * NEW-ISSUE-2 / LOW-3: the run was NOT PAUSED to begin with (completed/running) --
        #     terminal for this attempt. Report the run's ACTUAL status (`run_status`, when the
        #     service supplied it) instead of always claiming "paused"; the mismatch path omits
        #     it and correctly defaults to "paused".
        err = summary.get("error") or "resume refused (segment count mismatch)"
        emit.result(
            f"Run #{args.run_id} resume REFUSED: {err}",
            {
                "ok": False,
                "run_id": args.run_id,
                "status": summary.get("run_status") or "paused",
                "steps": n_steps,
                "error": err,
            },
        )
        return EXIT_FAIL
    if status != "completed":
        # The run did NOT complete (status is 'failed' or, defensively, anything
        # that isn't paused/completed). Surface it as a real failure: ok:false,
        # exit 1, with the orchestrator's error message. This is the fix for the
        # silent success-lie where a failed multi-segment resume printed
        # "completed" and exited 0.
        err = summary.get("error") or "resume did not complete (run failed)"
        emit.result(
            f"Run #{args.run_id} FAILED: {err}",
            {
                "ok": False,
                "run_id": args.run_id,
                "status": status or "failed",
                "steps": n_steps,
                "error": err,
            },
        )
        return EXIT_FAIL
    emit.result(
        f"Run #{args.run_id} completed: {n_steps} more step(s).",
        {
            "ok": True,
            "run_id": args.run_id,
            "status": status,
            "steps": n_steps,
            "text": summary.get("text", ""),
        },
    )
    return 0


def _grp_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_group_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        _reject_archived_customer(
            session, args.customer_id, "create a workflow group for it", op_org_id(args)
        )
        try:
            grp = workflows.create_group(
                session,
                args.name,
                customer_id=args.customer_id,
                description=args.description,
                org_id=op_org_id(args),
                created_by=args.actor,
            )
        except workflows.WorkflowForeignCustomerError as exc:
            # #488 item 2: a customer from another org (or one that does not exist).
            raise CliError(f"no such customer: {args.customer_id}", EXIT_USAGE) from exc
        if grp is None:
            raise CliError("group name must not be blank.", EXIT_USAGE)
        emit.result(
            f"Created workflow group #{grp.id}: {grp.name}"
            f"{' (customer ' + str(grp.customer_id) + ')' if grp.customer_id else ' (global)'}.",
            {"ok": True, "id": grp.id, "name": grp.name, "customer_id": grp.customer_id},
        )
    return 0


def _grp_add(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_group_add(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    src = tgt = None
    if args.pair.strip():
        src, tgt = _parse_pair(args.pair)
    with _session() as session:
        existing = _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        # R4: capture the pair BEFORE the write so a --force replace can name old -> new.
        was_member = existing.group_id == args.group_id
        old_pair = workflows.pair_key(existing.source_locale, existing.target_locale)
        try:
            wf = workflows.add_to_group(
                session,
                args.workflow_id,
                args.group_id,
                source_locale=src,
                target_locale=tgt,
                make_default=args.make_default,
                force=args.force,
            )
        except workflows.WorkflowError as exc:
            # Issue #46: --pair on a workflow that is ALREADY a member of this group used
            # to REPLACE its pair silently, dropping a language from the family. Refuse
            # and name the pair it currently holds (--force overrides).
            raise CliError(str(exc), EXIT_USAGE) from exc
        if wf is None:
            raise CliError(
                f"no such workflow or group (workflow {args.workflow_id}, group {args.group_id}).",
                EXIT_USAGE,
            )
        pair = (wf.source_locale or "*") + "->" + (wf.target_locale or "*")
        # Compare through pair_key on BOTH sides: this command's own `pair` display
        # string spells a universal variant "*->*" while pair_key spells it "universal",
        # so comparing the two would report a bogus REPLACE on a plain universal re-add.
        new_pair = workflows.pair_key(wf.source_locale, wf.target_locale)
        replaced = was_member and new_pair != old_pair
        lines = [
            f"Added workflow #{wf.id} to group #{args.group_id} as variant {pair}"
            f"{' (default)' if wf.is_default else ''}."
        ]
        if replaced:
            # A replace is never silent -- that silence WAS issue #46.
            lines.append(
                f"REPLACED its language pair: {old_pair} -> {new_pair}. The group "
                f"no longer covers {old_pair} unless another variant does."
            )
        emit.result(
            "\n".join(lines),
            {
                "ok": True,
                "workflow_id": wf.id,
                "group_id": args.group_id,
                "source_locale": wf.source_locale,
                "target_locale": wf.target_locale,
                "is_default": wf.is_default,
                "replaced_pair": old_pair if replaced else None,
            },
        )
    return 0


def _grp_set_default(args, emit) -> int:
    """`workflow group set-default <workflow> <group>`: flag an existing group MEMBER as
    the group's fallback default (clearing the flag on its siblings — exactly one default
    per group). Mirrors POST /workflow-groups/{id}/default; --remote dispatches to that
    /api twin. Membership is validated BEFORE flipping so we never set the default on the
    wrong group (set_default_variant derives the group from the workflow)."""
    if remote_mode(args):
        return _remote_impl.workflow_group_set_default(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        wf = _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        _get_grp(session, args.group_id, op_org_id(args))  # org 404 guard
        if wf.group_id is None:
            raise CliError(
                f"workflow {args.workflow_id} is not in a group; add it to group "
                f"{args.group_id} first (workflow group add).",
                EXIT_USAGE,
            )
        if wf.group_id != args.group_id:
            raise CliError(
                f"workflow {args.workflow_id} belongs to group {wf.group_id}, not {args.group_id}.",
                EXIT_USAGE,
            )
        updated = workflows.set_default_variant(session, args.workflow_id)
        if updated is None:  # defensive — membership was verified above
            raise CliError(f"could not set default for workflow {args.workflow_id}.", EXIT_USAGE)
        pair = (updated.source_locale or "*") + "->" + (updated.target_locale or "*")
        emit.result(
            f"Workflow #{updated.id} ({pair}) is now the default variant of group "
            f"#{args.group_id}.",
            {
                "ok": True,
                "workflow_id": updated.id,
                "group_id": args.group_id,
                "is_default": updated.is_default,
                "source_locale": updated.source_locale,
                "target_locale": updated.target_locale,
            },
        )
    return 0


def _grp_rename(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        grp = _get_grp(session, args.group_id, op_org_id(args))
        old = grp.name
        try:
            workflows.rename_group(session, args.group_id, args.name)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="workflow_group.rename",
            target=f"workflow_group {args.group_id}",
            detail=f"{old!r} -> {grp.name!r}",
            customer_id=grp.customer_id,
        )
        emit.result(
            f"Renamed workflow group #{args.group_id}: {old} -> {grp.name}.",
            {"ok": True, "id": args.group_id, "name": grp.name, "old_name": old},
        )
    return 0


def _grp_delete(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        grp = _get_grp(session, args.group_id, op_org_id(args))
        name = grp.name
        customer_id = grp.customer_id
        refs = workflows.group_reference_count(session, args.group_id)
        if not args.force:
            state = (
                "it still has member workflow(s); delete is blocked until they are "
                "ungrouped/deleted"
                if refs["members"]
                else "it is empty and can be deleted"
            )
            emit.result(
                f"Workflow group #{args.group_id} ({name}) has {refs['members']} member "
                f"workflow(s) -- {state}. Re-run with --force to delete.",
                {"ok": True, "preview": True, "group_id": args.group_id, "references": refs},
            )
            return 0
        try:
            workflows.delete_group(session, args.group_id)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc)) from exc  # default exit 1 (EXIT_FAIL)
        audit.log(
            actor=actor,
            action="workflow_group.delete",
            target=f"workflow_group {args.group_id} '{name}'",
            customer_id=customer_id,
        )
        emit.result(
            f"Deleted workflow group #{args.group_id} ({name}).",
            {"ok": True, "deleted": True, "group_id": args.group_id},
        )
    return 0


def _pt_rename(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        tmpl = _get_pt(session, args.template_id, op_org_id(args))
        old = tmpl.name
        try:
            workflows.rename_prompt_template(session, args.template_id, args.name)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="prompt_template.rename",
            target=f"prompt_template {args.template_id}",
            detail=f"{old!r} -> {tmpl.name!r}",
        )
        emit.result(
            f"Renamed prompt template #{args.template_id}: {old} -> {tmpl.name}.",
            {"ok": True, "id": args.template_id, "name": tmpl.name, "old_name": old},
        )
    return 0


def _pt_delete(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit, workflows

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        tmpl = _get_pt(session, args.template_id, op_org_id(args))
        name = tmpl.name
        refs = workflows.prompt_template_reference_count(session, args.template_id)
        if not args.force:
            emit.result(
                f"Prompt template #{args.template_id} ({name}) is pinned by "
                f"{refs['steps']} workflow step(s). Re-run with --force to delete "
                f"(delete fails closed while any step pins it).",
                {"ok": True, "preview": True, "template_id": args.template_id, "references": refs},
            )
            return 0
        try:
            workflows.delete_prompt_template(session, args.template_id)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc)) from exc  # default exit 1 (EXIT_FAIL)
        audit.log(
            actor=actor,
            action="prompt_template.delete",
            target=f"prompt_template {args.template_id} '{name}'",
        )
        emit.result(
            f"Deleted prompt template #{args.template_id} ({name}).",
            {"ok": True, "deleted": True, "template_id": args.template_id},
        )
    return 0
