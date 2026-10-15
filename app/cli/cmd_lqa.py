"""`lqa` command group (SPEC-wave3-collaboration §6.5): reviewer-authored MQM findings.

  lqa log      --segment N --dimension D --severity S --note T
  lqa findings --document N [--locale L] [--machine]
  lqa edit     <fid> --dimension D --severity S --note T
  lqa withdraw <fid>

`lqa findings --machine` reads the STORED machine scoring (workflow-run + ad-hoc findings +
the saved score/gate) via reports.machine_findings_for_document — the SAME read the web LQA
scorecard renders, so the reviewer surface and the machine surface reach parity. In-process
only (there is no /api twin: reading/running a scoring is web-only, §6.5).

Parity with the web LQA page's reviewer surface + POST|GET /api/documents/{id}/lqa/findings
+ POST /api/lqa/findings/{fid}[/withdraw]: all three call ``app.services.lqa`` (the writers)
and ``app.services.reports.human_findings_for_document`` (the shared reader).

IMPORTANT — this group manages FINDINGS only. **Running a scoring is web-only**
(POST /documents/{id}/lqa/run has no CLI/API twin); this is the concrete reason QA-2 stays
PARTIAL (§9/§1.1). It is a CORE surface (no feature flag, §5.3).

IN-PROCESS this is the TRUSTED-OPERATOR surface (``__main__.py:35-39``): no RBAC, no session
user — every write passes ``trusted_operator=True`` and the author is ``--actor`` (default
"cli"). ``--remote --token`` hits the /api, which is REVIEWER+ RBAC-enforced server-side by
the token. (The trusted-operator asymmetry mirrors every other CLI verb; deferral #10-style.)

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _session,
    bounded_id,
    op_org_id,
    remote_mode,
    require_org_scope,
    spend_estimate,
    spend_gate,
)


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    from app.kernel import qe_jev_loop

    q = subparsers.add_parser(
        "lqa",
        help="Reviewer-authored MQM findings (log / list / edit / withdraw). "
        "Running a scoring is web-only.",
    )
    sub = q.add_subparsers(dest="lqa_cmd", metavar="<subcommand>")
    sub.required = True

    qe_compare_text = (
        "Compare the LLM and Jev QE gates on JSONL pairs. Jev here is the "
        "TYPESAFE_API_KEY setup; to try a Jev engine use `engine jev-try`."
    )
    p = sub.add_parser("qe-compare", help=qe_compare_text, description=qe_compare_text)
    p.add_argument(
        "--pairs",
        required=True,
        help="JSONL pairs. Unreadable file exits 2; malformed rows report and exit 0.",
    )
    p.add_argument("--llm-provider", default=None, help="LLM judge provider (install default).")
    p.add_argument("--threshold", type=int, default=70, help="Passing score, default 70.")
    p.add_argument("--json", dest="report_json", help="Write rows and summary to this JSON file.")
    p.add_argument(
        "--yes",
        action="store_true",
        help="confirm the printed cost estimate and run (nothing is sent without it).",
    )
    p.set_defaults(func=_qe_compare)

    p = sub.add_parser(
        "jev-refine",
        help="CLI-only Jev-guided post-edit experiment (writes no production content). "
        "A workflow step's QE auto-fix is the production version of this loop.",
    )
    refine_help = {
        "source": "the source text of one pair (with --target and both locales).",
        "target": "the translation to refine.",
        "pairs": "a JSONL file of pairs (source, target, source_locale, target_locale) "
        "instead of --source/--target.",
        "source-locale": "source locale, e.g. en.",
        "target-locale": "target locale, e.g. de.",
        "provider": "the fixer LLM that writes each new version (default: the install "
        "default provider).",
    }
    for name, text in refine_help.items():
        p.add_argument("--" + name, help=text)
    p.add_argument(
        "--target-score",
        type=int,
        default=70,
        help="whole number 0-100 (out-of-range refused); stop once a version scores "
        "at least this (default 70) with no unsure "
        "dimension and none at major or critical.",
    )
    p.add_argument(
        "--max-tries",
        type=int,
        default=10,
        help="Total versions including baseline; "
        f"default 10, range 1-{qe_jev_loop.MAX_TRIES_CEILING}; out-of-range refused.",
    )
    p.add_argument(
        "--cross-check",
        action="store_true",
        help="also score every version with the LLM QE judge, as a second opinion.",
    )
    p.add_argument(
        "--show-prompts",
        action="store_true",
        help="print the prompt the fixer gets each round.",
    )
    p.add_argument(
        "--judge",
        default=None,
        help="the Jev judge that scores every version: engine:<id> of a jev or jev_local "
        "engine (e.g. a local SemIf server), or jev for the TYPESAFE_API_KEY setup (the "
        "default when omitted).",
    )
    p.add_argument("--json", dest="report_json", help="Write the full transcript and summary.")
    p.add_argument(
        "--yes",
        action="store_true",
        help="confirm the printed cost estimate and run (nothing is sent without it).",
    )
    p.set_defaults(func=_jev_refine)

    p = sub.add_parser("log", help="Log one reviewer MQM finding on a segment.")
    p.add_argument(
        "--document",
        type=bounded_id,
        required=True,
        help="the document id (the segment must belong to it).",
    )
    p.add_argument("--segment", type=bounded_id, required=True, help="the segment id.")
    p.add_argument(
        "--dimension",
        required=True,
        help="an MQM dimension (Accuracy, Fluency, Terminology, ...); a free "
        "sub-category after '/' is allowed.",
    )
    p.add_argument("--severity", required=True, help="critical | major | minor | neutral.")
    p.add_argument("--note", required=True, help="what is wrong (non-empty).")
    p.set_defaults(func=_lqa_log)

    p = sub.add_parser(
        "findings",
        help="List reviewer findings for a document (--machine for the stored machine scoring).",
    )
    p.add_argument("--document", type=bounded_id, required=True, help="the document id.")
    p.add_argument("--locale", default="", help="filter to one target locale.")
    p.add_argument(
        "--machine",
        action="store_true",
        help="show the stored MACHINE-scored findings (workflow-run + ad-hoc) "
        "and the saved score/gate instead of the reviewer findings - the "
        "same read the web LQA scorecard renders. In-process only.",
    )
    p.set_defaults(func=_lqa_findings)

    p = sub.add_parser("edit", help="Edit a reviewer finding's dimension/severity/note.")
    p.add_argument("id", type=bounded_id, help="the finding id.")
    p.add_argument("--dimension", required=True, help="the MQM dimension.")
    p.add_argument("--severity", required=True, help="critical | major | minor | neutral.")
    p.add_argument("--note", required=True, help="the finding note (non-empty).")
    p.set_defaults(func=_lqa_edit)

    p = sub.add_parser("withdraw", help="Withdraw (soft-delete) a reviewer finding.")
    p.add_argument("id", type=bounded_id, help="the finding id.")
    p.set_defaults(func=_lqa_withdraw)


# --- helpers -----------------------------------------------------------------
def _finding_dict(f) -> dict:
    # Byte-identical key set to api_cli._lqa_finding_dict so the --remote arm's JSON never
    # drifts from the in-process --json shape.
    return {
        "id": f.id,
        "document_id": f.document_id,
        "segment_id": f.segment_id,
        "dimension": f.dimension,
        "severity": f.severity,
        "note": f.note,
        "target_locale": f.target_locale,
        "finding_source": f.finding_source,
        "actor": f.actor,
        "vendor_id": f.vendor_id,
        "dispatch_id": f.dispatch_id,
        "created_at": str(f.created_at),
        "withdrawn_at": str(f.withdrawn_at) if f.withdrawn_at else None,
    }


def _require_document_in_scope(s, document_id, org_id) -> None:
    """#515: under --org a document of another org (or none) is "no such document", the
    answer every other org-scoped verb gives. A no-op when `org_id` is None (single-tenant
    or the key-verified --operator door)."""
    if org_id is None:
        return
    from app.models import Document, Project

    doc = s.get(Document, document_id) if document_id is not None else None
    proj = s.get(Project, doc.project_id) if doc is not None else None
    if proj is None or getattr(proj, "org_id", None) != org_id:
        raise CliError(f"no such document: {document_id}", EXIT_USAGE)


def _run(fn):
    """Map the service's typed finding errors to a clean CliError (exit 1)."""
    from app.kernel import lqa

    try:
        return fn(lqa)
    except (lqa.HumanFindingError, lqa.HumanFindingForbidden) as exc:
        raise CliError(str(exc)) from exc


# --- commands ----------------------------------------------------------------
def _lqa_log(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.lqa_log(args, emit)
    require_org_scope(args, emit)
    from app.models import Segment

    with _session() as s:
        _require_document_in_scope(s, args.document, op_org_id(args))
        seg = s.get(Segment, args.segment)
        if seg is None or seg.document_id != args.document:
            raise CliError(f"no such segment {args.segment} on document {args.document}.")
        row = _run(
            lambda lqa: lqa.log_human_finding(
                s,
                segment=seg,
                dimension=args.dimension,
                severity=args.severity,
                note=args.note,
                actor=_actor(args),
                trusted_operator=True,
            )
        )
        out = _finding_dict(row)
    emit.result(
        "Logged finding #{} ({}/{}) on segment {}.".format(
            out["id"], out["dimension"], out["severity"], out["segment_id"]
        ),
        {"ok": True, "finding": out},
    )
    return 0


def _lqa_findings(args, emit) -> int:
    if getattr(args, "machine", False):
        # F7 (parity): the stored MACHINE scoring the web GET renders, via the SAME reader
        # (reports.machine_findings_for_document). In-process only — there is no /api twin
        # (running/reading a scoring is web-only, §6.5), so --remote is refused honestly
        # rather than silently returning the reviewer list.
        if remote_mode(args):
            raise CliError(
                "`lqa findings --machine` is in-process only (no --remote twin); drop --remote."
            )
        return _lqa_machine_findings(args, emit)
    if remote_mode(args):
        return _remote_impl.lqa_findings(args, emit)
    require_org_scope(args, emit)
    from app.kernel import reports

    with _session() as s:
        _require_document_in_scope(s, args.document, op_org_id(args))
        rows = reports.human_findings_for_document(
            s, document_id=args.document, target_locale=(args.locale or None)
        )
        out = [_finding_dict(f) for f in rows]
    if emit.as_json:
        emit.json({"ok": True, "findings": out})
        return 0
    emit.table(
        [
            (
                f["id"],
                f["segment_id"],
                f["severity"],
                f["dimension"],
                (f["note"] or "")[:40],
                f["actor"] or "-",
            )
            for f in out
        ],
        headers=("id", "segment", "severity", "dimension", "note", "by"),
    )
    emit.say("%d reviewer finding(s)." % len(out))  # noqa: UP031
    return 0


def _machine_finding_dict(f) -> dict:
    return {
        "id": f.id,
        "document_id": f.document_id,
        "segment_id": f.segment_id,
        "regime": ("run" if f.segment_id is None else "adhoc"),
        "dimension": f.dimension,
        "severity": f.severity,
        "note": f.note,
        "score": f.score,
        "judge": f.judge,
        "target_locale": f.target_locale,
        "finding_source": f.finding_source,
        "created_at": str(f.created_at),
    }


def _lqa_machine_findings(args, emit) -> int:
    require_org_scope(args, emit)
    from app.kernel import reports

    with _session() as s:
        _require_document_in_scope(s, args.document, op_org_id(args))
        m = reports.machine_findings_for_document(
            s, document_id=args.document, target_locale=(args.locale or None)
        )
        # THE SAME authoritative headline the web scorecard renders (no duplicated gate
        # derivation): latest workflow LQA StepRun (durable, threshold-correct gate) or the
        # last ad-hoc scoring's score (no gate).
        headline = reports.lqa_scorecard_headline(
            s, document_id=args.document, target_locale=(args.locale or None)
        )
    run = [_machine_finding_dict(f) for f in m["run"]]
    adhoc = [_machine_finding_dict(f) for f in m["adhoc"]]
    summary = {
        "stored_score": headline["score"],
        "gate": headline["gate"],
        "regime": headline["regime"],
        "has_scoring": headline["has_scoring"],
        "run_total": m["run_total"],
        "adhoc_total": m["adhoc_total"],
    }
    if emit.as_json:
        emit.json({"ok": True, "scoring": summary, "run": run, "adhoc": adhoc})
        return 0
    if not summary["has_scoring"]:
        emit.say(
            "No LQA findings on record for document {}{} (a clean scoring records none).".format(
                args.document,
                (f" ({args.locale})") if args.locale else "",
            )
        )
        return 0
    emit.table(
        [
            (
                f["regime"],
                f["segment_id"] if f["segment_id"] is not None else "-",
                f["severity"],
                f["dimension"],
                (f["note"] or "")[:40],
                f["judge"] or "-",
            )
            for f in (run + adhoc)
        ],
        headers=("regime", "segment", "severity", "dimension", "note", "judge"),
    )
    if summary["regime"] == "run":
        emit.say(
            "Last workflow LQA scoring: %s/100 - %s (%d run + %d ad-hoc finding(s))."  # noqa: UP031
            % (
                "-" if summary["stored_score"] is None else summary["stored_score"],
                summary["gate"],
                summary["run_total"],
                summary["adhoc_total"],
            )
        )
    else:
        emit.say(
            "Last ad-hoc scoring (worst segment): %s/100 - no gate "  # noqa: UP031
            "(%d run + %d ad-hoc finding(s))."
            % (
                "-" if summary["stored_score"] is None else summary["stored_score"],
                summary["run_total"],
                summary["adhoc_total"],
            )
        )
    return 0


def _lqa_edit(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.lqa_edit(args, emit)
    require_org_scope(args, emit)
    from app.models import LQAResult

    with _session() as s:
        finding = s.get(LQAResult, args.id)
        if finding is None:
            raise CliError(f"no such finding: {args.id}")
        _require_document_in_scope(s, finding.document_id, op_org_id(args))
        row = _run(
            lambda lqa: lqa.edit_human_finding(
                s,
                finding=finding,
                dimension=args.dimension,
                severity=args.severity,
                note=args.note,
                actor=_actor(args),
                trusted_operator=True,
            )
        )
        out = _finding_dict(row)
    emit.result("Edited finding #{}.".format(out["id"]), {"ok": True, "finding": out})
    return 0


def _lqa_withdraw(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.lqa_withdraw(args, emit)
    require_org_scope(args, emit)
    from app.models import LQAResult

    with _session() as s:
        finding = s.get(LQAResult, args.id)
        if finding is None:
            raise CliError(f"no such finding: {args.id}")
        _require_document_in_scope(s, finding.document_id, op_org_id(args))
        row = _run(
            lambda lqa: lqa.withdraw_human_finding(
                s, finding=finding, actor=_actor(args), trusted_operator=True
            )
        )
        out = _finding_dict(row)
    emit.result("Withdrew finding #{}.".format(out["id"]), {"ok": True, "finding": out})
    return 0


def _refine_judge(args, emit):
    """The JevTarget `--judge` names, or None for the default (the legacy env Jev,
    exactly as before). An engine ref must be a Jev judge the caller's --org scope may
    use; a dead or non-judge ref is a usage error (exit 2), never a silent fallback."""
    raw = (getattr(args, "judge", None) or "").strip()
    if not raw:
        return None
    from app.kernel import engines, qe_jev
    from app.kernel.providers import DeadEngineError

    from ._common import op_org_scope

    if not qe_jev.is_judge_ref(raw):
        raise CliError(
            f"--judge {raw!r} is not a Jev judge; use engine:<id> of a jev or jev_local "
            "engine, or jev.",
            2,
        )
    if raw.lower() != "jev":
        require_org_scope(args, emit)
        try:
            engines.assert_ref_role(raw, "judge", org_id=op_org_scope(args))
        except ValueError as exc:
            raise CliError(f"--judge: {exc}", 2) from exc
    try:
        return qe_jev.resolve_target(raw)
    except (DeadEngineError, ValueError) as exc:
        raise CliError(f"--judge: {exc}", 2) from exc


def _judge_scorer(judge, pair):
    """refine's scorer seam bound to `judge` for this pair, or None (the default env
    Jev, byte-identical to the pre-Train-C CLI)."""
    if judge is None:
        return None
    from app.kernel import qe_jev

    def scorer(text, tap_meta=None):
        return qe_jev.score_segment(
            pair["source"],
            text,
            pair["source_locale"],
            pair["target_locale"],
            tap_meta=tap_meta,
            judge=judge,
        )

    return scorer


def _cli_qe_tap_meta(tool, pair, number):
    return {
        "workflow_id": None,
        "workflow_run_id": None,
        "step_key": tool,
        "document_id": None,
        "segment_index": number,
        "source": "cli",
        "tool": tool,
        "pair": pair.get("id", number),
        "round": None,
    }


def _refuse_remote_and_clobber(args, verb: str) -> None:
    """Usage refusals shared by the two in-process experiments, raised BEFORE any file is
    read or provider called (#527): --remote used to be recorded as a row "error" and exit 0
    (a green cron on a refused run), and `--json <path>` equal to the --pairs file silently
    overwrote the operator's input."""
    import os

    if remote_mode(args):
        raise CliError(f"{verb} is in-process only; drop --remote", 2)
    pairs, report = getattr(args, "pairs", None), getattr(args, "report_json", None)
    if pairs and report:
        try:
            same = os.path.samefile(pairs, report)
        except OSError:  # one of them does not exist yet: compare the normalized paths
            same = os.path.normcase(os.path.abspath(pairs)) == os.path.normcase(
                os.path.abspath(report)
            )
        if same:
            raise CliError(
                f"--json {report} is the --pairs input file; refusing to overwrite it "
                "(pick another report path).",
                2,
            )


def _pair_tokens(pair) -> int | None:
    """Estimated tokens of one pair for pre-flight pricing, or None for a malformed row (the
    run itself reports those; they cost nothing)."""
    fields = ("source", "target", "source_locale", "target_locale")
    if not isinstance(pair, dict) or not all(isinstance(pair.get(k), str) for k in fields):
        return None
    return (len(pair["source"]) + len(pair["target"])) // 4 + 1


def _estimate_pairs(lines, tokens_for):
    """Sum `tokens_for(pair_tokens)` style estimates over the parseable rows."""
    import json

    tokens, rows = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            t = _pair_tokens(json.loads(line))
        except ValueError:
            t = None
        if t is not None:
            tokens.append(t)
            rows += 1
    return rows, tokens


def _qe_compare(args, emit) -> int:
    """Local operator report; skips are excluded from gate comparisons and gold accuracy."""
    _refuse_remote_and_clobber(args, "qe-compare")
    import json
    import math
    import statistics
    import time
    from pathlib import Path

    from app import config
    from app.kernel import qe, qe_jev
    from app.kernel.providers import ProviderSpec

    rows, errors = [], []
    threshold = qe.clamp_threshold(args.threshold)
    provider = args.llm_provider or config.DEFAULT_PROVIDER
    spec = ProviderSpec(provider=provider, model=config.DEFAULT_MODELS.get(provider))
    try:
        lines = Path(args.pairs).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        message = " ".join(str(exc).split())
        raise CliError(f"Cannot read --pairs: {message}", code=2) from exc
    # Every valid row is judged twice (the LLM judge, then hosted Jev): price it and require
    # --yes before the first paid call (#531), like `eval run start`.
    n_rows, toks = _estimate_pairs(lines, None)
    if n_rows and not spend_gate(
        emit,
        args,
        spend_estimate(
            provider,
            spec.model,
            calls=2 * n_rows,
            input_tokens=sum(t + 600 for t in toks),
            output_tokens=200 * n_rows,
            jev_input_tokens=sum(t + 300 for t in toks),
            note=f"{n_rows} row(s): one LLM judge call and one Jev call each.",
        ),
        verb="qe-compare",
    ):
        return 0
    try:
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            row = {"row": number}
            try:
                pair = json.loads(line)
                fields = ("source", "target", "source_locale", "target_locale")
                if not isinstance(pair, dict) or not all(
                    isinstance(pair.get(k), str) for k in fields
                ):
                    raise ValueError(
                        "each row needs string source, target, source_locale, target_locale"
                    )
                if "gold_pass" in pair and not isinstance(pair["gold_pass"], bool):
                    raise ValueError("gold_pass must be true or false")
                row["gold_pass"] = pair.get("gold_pass")
                values = [pair[k] for k in fields]
                tap_meta = _cli_qe_tap_meta("qe-compare", pair, number)
                for judge in ("llm", "jev"):
                    started = time.perf_counter()
                    try:
                        card = (
                            qe.score_segment(spec, *values, tap_meta=tap_meta)
                            if judge == "llm"
                            else qe_jev.score_segment(*values, tap_meta=tap_meta)
                        )
                    except Exception as exc:
                        card = qe._skipped("provider_error", type(exc).__name__)
                    row[judge] = {
                        **card,
                        "ms": (time.perf_counter() - started) * 1000,
                        "pass": False
                        if card.get("structural_blocked")
                        else None
                        if card["skipped"] or (card.get("unsure") and card["score"] >= threshold)
                        else card["score"] >= threshold,
                    }
            except (ValueError, TypeError) as exc:
                errors.append(f"row {number}: {exc}")
                for judge in ("llm", "jev"):
                    row[judge] = {**qe._skipped("invalid_pair", str(exc)), "ms": 0, "pass": None}
            row["agree"] = (
                row["llm"]["pass"] == row["jev"]["pass"]
                if all(row[j]["pass"] is not None for j in ("llm", "jev"))
                else None
            )
            rows.append(row)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    both = [r for r in rows if r["agree"] is not None]
    numeric = [r for r in both if all(r[j].get("score") is not None for j in ("llm", "jev"))]
    summary = {
        "rows": len(rows),
        "threshold": threshold,
        "both_scored": len(both),
        "gate_agreement_rate": statistics.mean(r["agree"] for r in both) if both else None,
        "mean_absolute_score_difference": statistics.mean(
            abs(r["llm"]["score"] - r["jev"]["score"]) for r in numeric
        )
        if numeric
        else None,
    }
    for judge in ("llm", "jev"):
        latencies = sorted(r[judge]["ms"] for r in rows)
        gold = [
            r for r in rows if isinstance(r.get("gold_pass"), bool) and r[judge]["pass"] is not None
        ]
        summary[judge] = {
            "scored": sum(r[judge]["pass"] is not None for r in rows),
            "p50_ms": statistics.median(latencies) if latencies else None,
            "p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
            "total_cost_usd": sum(r[judge].get("cost_usd", 0) or 0 for r in rows),
            "gold_scored": len(gold),
            "gold_accuracy": statistics.mean(r[judge]["pass"] == r["gold_pass"] for r in gold)
            if gold
            else None,
        }
    report = {"rows": rows, "summary": summary, "errors": errors}
    if args.report_json:
        try:
            Path(args.report_json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            errors.append(f"JSON report: {exc}")
    if emit.as_json:
        emit.json(report)
    else:
        show_unsure = any(r["jev"].get("unsure", []) for r in rows)
        emit.table(
            [
                (
                    r["row"],
                    r["llm"]["score"],
                    r["jev"]["score"],
                    r["llm"]["pass"],
                    r["jev"]["pass"],
                    r["agree"],
                    f"{r['llm']['ms']:.1f}",
                    f"{r['jev']['ms']:.1f}",
                    r["llm"].get("reason") or "",
                    r["jev"].get("rationale")
                    if r["jev"].get("structural_findings")
                    else r["jev"].get("reason") or "",
                )
                + (
                    (",".join(c[0].upper() for c in r["jev"].get("unsure", [])),)
                    if show_unsure
                    else ()
                )
                for r in rows
            ],
            headers=(
                "row",
                "llm score",
                "jev score",
                "llm pass",
                "jev pass",
                "agree",
                "llm ms",
                "jev ms",
                "llm reason",
                "jev reason",
            )
            + (("unsure",) if show_unsure else ()),
        )
        emit.say("summary: " + json.dumps(summary, sort_keys=True))
        for error in errors:
            emit.say("error: " + error)
    return 0


def _jev_refine(args, emit) -> int:
    """Local experiment; per-row input errors exit zero (in the report), refusals exit two."""
    import json
    import statistics
    from pathlib import Path

    from app import config
    from app.kernel import qe_jev, qe_jev_loop
    from app.kernel.providers import ProviderSpec

    if not 1 <= args.max_tries <= qe_jev_loop.MAX_TRIES_CEILING:
        raise CliError(f"--max-tries must be from 1 to {qe_jev_loop.MAX_TRIES_CEILING}", 2)
    if not 0 <= args.target_score <= 100:
        raise CliError("--target-score must be from 0 to 100", 2)
    if args.pairs is not None and (args.source is not None or args.target is not None):
        raise CliError("Use --pairs or --source and --target, not both", 2)
    _refuse_remote_and_clobber(args, "jev-refine")

    rows, errors = [], []
    fields = ("source", "target", "source_locale", "target_locale")
    provider = args.provider or config.DEFAULT_PROVIDER
    spec = ProviderSpec(provider=provider, model=config.DEFAULT_MODELS.get(provider))
    judge = _refine_judge(args, emit)
    read_error = None
    if args.pairs:
        try:
            lines = Path(args.pairs).read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            # Documented behavior: an unreadable --pairs is listed in the report's errors and
            # the run still exits 0 (only refusals exit 2); it spends nothing, so no gate.
            lines, read_error = [], str(exc)
    else:
        lines = [json.dumps({k: getattr(args, k) for k in fields})]
    # Up to --max-tries versions per pair, each one fixed by the LLM and scored by Jev (plus an
    # LLM judge per version with --cross-check): price the ceiling and require --yes before the
    # first paid call (#531).
    n_pairs, toks = _estimate_pairs(lines, None)
    rounds = args.max_tries
    fixer_calls = n_pairs * max(0, rounds - 1)
    judge_calls = n_pairs * rounds if args.cross_check else 0
    if n_pairs and not spend_gate(
        emit,
        args,
        spend_estimate(
            provider,
            spec.model,
            calls=fixer_calls + judge_calls + n_pairs * rounds,
            input_tokens=sum(
                (rounds - 1) * (2 * t + 1500) + (rounds * (t + 600) if args.cross_check else 0)
                for t in toks
            ),
            output_tokens=sum(
                (rounds - 1) * (t // 2 + 100) + (rounds * 200 if args.cross_check else 0)
                for t in toks
            ),
            jev_input_tokens=sum(rounds * (t + 300) for t in toks),
            note=f"{n_pairs} pair(s), at most {rounds} version(s) each "
            "(the loop stops early once the target is reached).",
        ),
        verb="jev-refine",
    ):
        return 0
    try:
        if read_error:
            raise OSError(read_error)
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                pair = json.loads(line)
                if not isinstance(pair, dict) or not all(
                    isinstance(pair.get(k), str) for k in fields
                ):
                    raise ValueError(
                        "each pair needs string source, target, source_locale, target_locale"
                    )
                if "gold_pass" in pair and not isinstance(pair["gold_pass"], bool):
                    raise ValueError("gold_pass must be true or false")
                result = qe_jev_loop.refine(
                    *(pair[k] for k in fields),
                    spec=spec,
                    target_score=args.target_score,
                    max_tries=args.max_tries,
                    cross_check=args.cross_check,
                    tap_meta=_cli_qe_tap_meta("jev-refine", pair, number),
                    scorer=_judge_scorer(judge, pair),
                )
                rows.append({"row": number, **result})
            except (ValueError, TypeError) as exc:
                errors.append(f"row {number}: {exc}")
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(str(exc))
    gains = [
        r["best"]["score"] - r["rounds"][0]["jev"]["score"]
        for r in rows
        if r["best"]["score"] is not None and r["rounds"][0]["jev"].get("score") is not None
    ]
    summary = {
        "pairs": len(rows),
        "reached_target": sum(r["stop_reason"] == "target_reached" for r in rows),
        "mean_rounds": statistics.mean(len(r["rounds"]) for r in rows) if rows else None,
        "mean_score_gain": statistics.mean(gains) if gains else None,
        "total_cost_usd": sum(r["totals"]["cost_usd"] for r in rows),
    }
    report = {"rows": rows, "summary": summary, "errors": errors}
    if args.report_json:
        try:
            Path(args.report_json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"JSON report: {exc}")
    if emit.as_json:
        emit.json(report)
    else:
        for result in rows:
            emit.say(f"Pair {result['row']}")
            table = []
            for row in result["rounds"]:
                card = row["jev"]
                levels = [
                    "unsure"
                    if c in card.get("unsure", [])
                    else qe_jev.LEVELS[card["categories"].get(c, 0)]
                    if not card["skipped"]
                    else "?"
                    for c in ("accuracy", "fluency", "terminology", "style")
                ]
                meaning = (card.get("rationale") or "").partition("meaning_changed=")[2] or "?"
                table.append(
                    (
                        row["round"],
                        card["score"],
                        *levels,
                        meaning,
                        json.dumps(row["delta_vs_previous"]),
                        f"{row['elapsed_ms']:.1f}",
                        f"{row['cost_usd']:.8f}",
                        row["llm"]["score"] if row["llm"] else "",
                    )
                )
                if args.show_prompts and "prompt" in row:
                    emit.say(f"Prompt for version {row['round']}:\n{row['prompt']}")
                if card.get("reason") or row.get("error"):
                    emit.say(f"Version {row['round']}: {row.get('error') or card['reason']}")
                if card.get("structural_findings") or card.get("judge_skipped"):
                    emit.say(f"Version {row['round']}: {card['rationale']}")
            emit.table(
                table,
                headers=(
                    "round",
                    "score",
                    "accuracy",
                    "fluency",
                    "terminology",
                    "style",
                    "meaning",
                    "delta",
                    "ms",
                    "cost",
                    "llm score",
                ),
            )
            for key in ("best", "final"):
                value = result[key]
                emit.say(
                    f"{key} (version {value['round']}, score {value['score']}): "
                    f"{value['translation']}"
                )
            emit.say("stop_reason: " + result["stop_reason"])
            emit.say("totals: " + json.dumps(result["totals"]))
        emit.say("summary: " + json.dumps(summary))
        for error in errors:
            emit.say("error: " + error)
    return 0
