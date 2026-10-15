"""cmd_workflow: part 4 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    remote_mode,
    require_org_scope,
)
from app.cli.cmd_workflow_common import (
    _batch_or_usage,
    _get_wf,
    _parse_pair,
    _run_doc,
    _run_step_line,
    _run_step_summary,
)
from app.cli.cmd_workflow_part3 import _wf_run_batch, batch_matrix_ascii


# --- workflow run (synchronous) ----------------------------------------------
def _wf_run(args, emit) -> int:
    if getattr(args, "plan", False):
        import json

        if not args.locale or getattr(args, "all_locales", False) or getattr(args, "locales", ""):
            raise CliError("--plan needs one positional target locale.", EXIT_USAGE)
        if remote_mode(args):
            from app.cli._remote_impl_common import _client

            out = _client(args).get(
                f"/api/workflows/{args.workflow_id}/plan",
                params={"doc_id": args.doc_id, "locale": args.locale},
            )
        else:
            from app.kernel import workflow_plan

            require_org_scope(args, emit)
            with _session() as session:
                _get_wf(session, args.workflow_id, op_org_id(args))
                doc = _run_doc(session, args.doc_id, op_org_id(args))
                try:
                    out = {
                        "ok": True,
                        **workflow_plan.plan(session, args.workflow_id, doc, args.locale),
                    }
                except ValueError as exc:
                    raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(json.dumps(out, indent=2, ensure_ascii=True), out)
        return 0
    if remote_mode(args):
        return _remote_impl.workflow_run(args, emit)
    require_org_scope(args, emit)
    # Fan-out batch (Phase 5a): --all-locales / --locales run MANY locales as one batch,
    # SYNCHRONOUSLY in-process (never a daemon that dies with this one-shot process).
    batch_mode = bool(
        getattr(args, "all_locales", False) or (getattr(args, "locales", "") or "").strip()
    )
    if batch_mode:
        if args.locale is not None:
            raise CliError(
                "give a positional locale for a single run OR --all-locales/--locales "
                "for a batch, not both.",
                EXIT_USAGE,
            )
        return _wf_run_batch(args, emit)
    if args.locale is None:
        raise CliError(
            "a target locale is required (or use --all-locales / --locales to batch).", EXIT_USAGE
        )
    from sqlmodel import select

    from app.kernel import orchestrator
    from app.models import Segment

    oid = op_org_id(args)
    with _session() as session:
        from app.models import Project

        doc = _run_doc(session, args.doc_id, oid)
        _get_wf(session, args.workflow_id, oid)
        present = {
            s.target_locale
            for s in session.exec(select(Segment).where(Segment.document_id == args.doc_id)).all()
        }
        if args.locale not in present:
            raise CliError(
                f"locale {args.locale!r} not on document #{args.doc_id} "
                f"(present: {', '.join(sorted(present)) or 'none'}).",
                EXIT_USAGE,
            )

        # P5 dispatch: pick the multilingual-group variant for this doc's pair BEFORE
        # running (orchestrator signature unchanged). Ungrouped -> itself, unchanged.
        from app.kernel import workflows as wf_service

        src_locale = doc and session.get(Project, doc.project_id)
        doc_customer_id = getattr(src_locale, "customer_id", None)
        src_locale = src_locale.source_locale if src_locale else None
        run_wf_id = wf_service.resolve_variant(
            session, args.workflow_id, args.locale, src_locale, doc_customer_id=doc_customer_id
        )
        # Archive gate (residual sweep): a clean usage error, not a traceback, when the
        # workflow/project is archived (start_run enforces the same block).
        from app.kernel import content as content_svc

        try:
            summary = orchestrator.run_workflow(
                session,
                run_wf_id,
                args.doc_id,
                args.locale,
                created_by=getattr(args, "actor", None) or "cli",
            )
        except orchestrator.ProjectBudgetExhausted as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc  # CV-2 per-project USD cap
        except (
            content_svc.ProjectArchivedError,
            wf_service.WorkflowArchivedError,
        ) as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    run_id = summary.get("run_id")
    n_steps = len(summary.get("steps", []))
    status = summary.get("status")
    if status == "paused":
        step_idx = summary.get("paused_step")
        emit.result(
            f"Run #{run_id} PAUSED at human-review step {step_idx} "
            f"({n_steps} step(s) done). Resume with:\n"
            f"  python -m app.cli workflow resume {run_id} "
            f'--edited-text "..." --locale {args.locale}',
            {
                "ok": True,
                "run_id": run_id,
                "status": "paused",
                "paused_step": step_idx,
                "steps": n_steps,
            },
        )
        return 0
    if status != "completed":
        # A run that FAILED (e.g. a failed step) must report ok:false / exit 1
        # rather than fall through to the "completed" branch (same success-lie
        # fix as resume).
        err = summary.get("error") or "run did not complete (run failed)"
        emit.result(
            f"Run #{run_id} FAILED: {err}",
            {
                "ok": False,
                "run_id": run_id,
                "status": status or "failed",
                "steps": n_steps,
                "error": err,
            },
        )
        return EXIT_FAIL
    qe_stats = summary.get("qe")
    qe_line = ""
    if qe_stats:
        qe_line = (
            f"\nQE gate: {qe_stats.get('scored', 0)} scored, "
            f"{qe_stats.get('passed', 0)} passed, {qe_stats.get('failed', 0)} "
            f"routed to review, {qe_stats.get('skipped', 0)} skipped."
        )
    # #20(a): a per-step trace (status/provider/score/attempts) instead of the bare
    # "N step(s)" line, plus a structured step_summary on the --json path (the `steps`
    # key stays the int COUNT for back-compat with existing consumers/tests).
    step_summary = _run_step_summary(summary.get("steps", []))
    per_step = "".join("\n  " + _run_step_line(s) for s in step_summary)
    emit.result(
        f"Run #{run_id} completed: {n_steps} step(s).{per_step}{qe_line}",
        {
            "ok": True,
            "run_id": run_id,
            "status": status,
            "steps": n_steps,
            "step_summary": step_summary,
            "text": summary.get("text", ""),
            **({"qe": qe_stats} if qe_stats else {}),
        },
    )
    return 0


def _batch_show(args, emit) -> int:
    """Show a batch's locale x steps matrix (ASCII; --json = the detail/poll shape)."""
    if remote_mode(args):
        return _remote_impl.workflow_batch_show(args, emit)
    require_org_scope(args, emit)
    from app.kernel import batch_runs

    oid = op_org_id(args)
    with _session() as session:
        batch = _batch_or_usage(session, args.batch_id, oid)
        view = batch_runs.batch_as_dict(session, batch, detail=True)
    if emit.as_json:
        emit.json({"ok": True, **view})
        return 0
    emit.say(batch_matrix_ascii(view))
    return 0


def _batch_rerun(args, emit) -> int:
    """Re-run ONE locale of a batch as a fresh member run into the same batch (SYNCHRONOUS)."""
    if remote_mode(args):
        return _remote_impl.workflow_batch_rerun(args, emit)
    require_org_scope(args, emit)
    from app.kernel import batch_runs

    oid = op_org_id(args)
    with _session() as session:
        batch = _batch_or_usage(session, args.batch_id, oid)
        try:
            batch = batch_runs.rerun_batch(
                session,
                batch=batch,
                locales=[args.locale],
                created_by=getattr(args, "actor", None) or "cli",
                synchronous=True,
            )
        except batch_runs.BatchError as exc:
            raise CliError(
                str(exc), EXIT_FAIL if isinstance(exc, batch_runs.BatchMissing) else EXIT_USAGE
            ) from exc
        view = batch_runs.batch_as_dict(session, batch, detail=True)
    emit.result(batch_matrix_ascii(view), {"ok": view["status"] != "failed", **view})
    return EXIT_FAIL if view["status"] == "failed" or view.get("error") else 0


def _batch_retry_failed(args, emit) -> int:
    """Re-run EVERY failed locale of a batch as one action (SYNCHRONOUS)."""
    if remote_mode(args):
        return _remote_impl.workflow_batch_retry_failed(args, emit)
    require_org_scope(args, emit)
    from app.kernel import batch_runs

    oid = op_org_id(args)
    with _session() as session:
        batch = _batch_or_usage(session, args.batch_id, oid)
        reason = batch_runs.rerun_unavailable_reason(session, batch)
        if reason:
            raise CliError(reason, EXIT_USAGE)
        locales = batch_runs.failed_locales(session, batch)
        if not locales:
            view = batch_runs.batch_as_dict(session, batch, detail=True)
            emit.result("No failed locales to retry.", {"ok": True, "retried": [], **view})
            return 0
        try:
            batch = batch_runs.rerun_batch(
                session,
                batch=batch,
                locales=locales,
                created_by=getattr(args, "actor", None) or "cli",
                synchronous=True,
            )
        except batch_runs.BatchError as exc:
            raise CliError(
                str(exc), EXIT_FAIL if isinstance(exc, batch_runs.BatchMissing) else EXIT_USAGE
            ) from exc
        view = batch_runs.batch_as_dict(session, batch, detail=True)
    emit.result(
        batch_matrix_ascii(view),
        {"ok": view["status"] != "failed" and not view.get("error"), "retried": locales, **view},
    )
    return EXIT_FAIL if view["status"] == "failed" or view.get("error") else 0


def _wf_copy(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_copy(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    pairs = [_parse_pair(p) for p in args.to_pairs.split(",") if p.strip()]
    if not pairs:
        raise CliError("--to-pairs must list at least one src:tgt pair.", EXIT_USAGE)
    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        try:
            result = workflows.copy_to_pairs(
                session, args.workflow_id, pairs, adapt=args.adapt, created_by=args.actor
            )
        except workflows.WorkflowError as exc:
            # R8: an unknown locale code, refused at the service seam before any write
            # (same refusal both web copy surfaces get -- one seam, dual-surface parity).
            raise CliError(str(exc), EXIT_USAGE) from exc
        # group_id None is the ONLY "source workflow does not exist" signal now: an
        # all-duplicate submit (issue #46) legitimately creates nothing and must report
        # the skips instead of a bogus "no such workflow".
        if result.get("group_id") is None:
            raise CliError(f"no such workflow: {args.workflow_id}", EXIT_USAGE)
        lines = [f"Copied workflow #{args.workflow_id} into group #{result['group_id']}:"]
        for c in result["created"]:
            lines.append(
                f"  -> #{c['workflow_id']} {c['pair']}{' (adapted)' if c['adapted'] else ''}"
            )
            for w in c["warnings"]:
                lines.append(f"     ! {w}")
        # Issue #46: a pair the group already covers is SKIPPED, never duplicated --
        # say so per pair, and say plainly that nothing was replaced.
        for sk in result.get("skipped") or []:
            lines.append(f"  -- skipped {sk['pair']}: {sk['reason']} (nothing was replaced)")
        if not result["created"]:
            lines.append("No new variants were created.")
        if args.adapt:
            lines.append(
                "REMINDER: LLM-adapted prompts are marked needs_review -- a "
                "linguist should post-edit each variant before activating it."
            )
        emit.result("\n".join(lines), {"ok": True, **result})
    return 0


def register_cmd_workflow_part4_sub(sub) -> None:
    p = sub.add_parser(
        "run",
        help="Run a workflow over a document/locale "
        "SYNCHRONOUSLY (pauses at a human_review step). With "
        "--all-locales / --locales it fans out across MANY locales as "
        "one batch (still synchronous).",
    )
    p.add_argument("doc_id", type=bounded_id, help="document id.")
    p.add_argument("workflow_id", type=bounded_id, help="workflow id.")
    # Optional (nargs='?') so the same verb serves the single-locale run (positional
    # locale) AND the batch fan-out (--all-locales / --locales, no positional). A bare
    # `workflow run <doc> <wf> <locale>` is byte-identical to before.
    p.add_argument(
        "locale",
        nargs="?",
        default=None,
        help="target locale (omit when using --all-locales / --locales).",
    )
    # --- fan-out batch (SPEC-workflows-redesign Phase 5a) --------------------------
    # Flags on the EXISTING run verb (no new leaf command -> no verify_cli_parity entry
    # needed; the parity table keys on leaf command PATHS, which flags do not change).
    p.add_argument(
        "--all-locales",
        dest="all_locales",
        action="store_true",
        help="fan out across ALL the document's target locales as one batch.",
    )
    p.add_argument(
        "--locales",
        dest="locales",
        default="",
        help="comma-separated subset of the document's target locales to "
        "batch (each must be a real target; a bogus one is refused).",
    )
    p.add_argument(
        "--max-parallel",
        dest="max_parallel",
        type=int,
        default=None,
        help="run-level concurrency for a batch (clamped 1..8; default from "
        "the batch_max_parallel setting). Ignored for a single-locale run.",
    )
    p.add_argument(
        "--fail-fast",
        dest="fail_fast",
        action="store_true",
        help="stop LAUNCHING further locales once a member fails (in-flight "
        "members still finish -- there is no cancellation hook).",
    )
    p.add_argument(
        "--plan",
        action="store_true",
        help="Preview resolved variant and spend without running (one locale).",
    )
    p.set_defaults(func=_wf_run)


def register_cmd_workflow_part4_bsub(bsub) -> None:
    b = bsub.add_parser(
        "show",
        help="Show a batch's locale x steps matrix (ASCII table; "
        "--json emits the detail shape the web monitor polls).",
    )
    b.add_argument(
        "batch_id", type=bounded_id, help="batch id (from `workflow run --all-locales`)."
    )
    b.set_defaults(func=_batch_show)

    b = bsub.add_parser(
        "rerun",
        help="Re-run ONE locale of a batch as a fresh member run "
        "INTO the same batch (its latest run must be terminal).",
    )
    b.add_argument("batch_id", type=bounded_id, help="batch id.")
    b.add_argument(
        "--locale",
        required=True,
        help="the locale to re-run (must be one of the batch's requested targets).",
    )
    b.set_defaults(func=_batch_rerun)

    b = bsub.add_parser(
        "retry-failed",
        help="Re-run EVERY failed locale of a batch as one "
        "action (bounded by the batch's max_parallel).",
    )
    b.add_argument("batch_id", type=bounded_id, help="batch id.")
    b.set_defaults(func=_batch_retry_failed)


def register_cmd_workflow_part4_sub_2(sub) -> None:
    p = sub.add_parser(
        "copy",
        help="Copy a workflow to other language pair(s), "
        "optionally LLM-adapting each step's instruction.",
    )
    p.add_argument("workflow_id", type=bounded_id, help="source workflow id.")
    p.add_argument(
        "--to-pairs",
        required=True,
        help="comma-separated src:tgt pairs "
        "(e.g. en-US:fr-FR,en-US:de-DE). A pair the group already has is "
        "reported skipped, never duplicated.",
    )
    p.add_argument(
        "--adapt",
        action="store_true",
        help="LLM-adapt each step's prompt "
        "from the source pair to each target pair (post-edit required).",
    )
    p.set_defaults(func=_wf_copy)
