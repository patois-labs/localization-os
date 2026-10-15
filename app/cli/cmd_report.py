"""`report` command group (R2-1 + Wave 2): CSV cost / token / volume / qa / postedit
reports.

Parity with the web /reports builder + the /api/reports/run endpoint: all three call
app.services.reports.run_report, so `report run <kind>` prints byte-identical numbers.

  report run <kind> [--from][--to][--customer][--project][--provider][--group-by]
                    [-o out.csv][--json]
  report kinds     - list the report kinds + their columns.

In-process it is the TRUSTED-OPERATOR surface (actor=None, no per-user identity) like
the other read commands, BUT under the multi-org gate it threads --org via
require_org_scope so a shared-host operator can't sweep every org unscoped (the P4/P5
precedent). Remote (--remote --token) hits GET /api/reports/run, which is org-bound +
customer-confined server-side by the token.

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    remote_mode,
    require_org_scope,
)


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    rep = subparsers.add_parser(
        "report", help="Reports (cost / tokens / volume / qa / postedit; CSV download)."
    )
    sub = rep.add_subparsers(dest="report_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "run",
        help="Run a report (cost | tokens | volume | qa | postedit) "
        "and print a table, --json, or write CSV with -o.",
    )
    # choices= gates a bad kind at the CLI door with a clean argparse usage error
    # (issue #25: `report run bogus` used to silently run 'cost'). Kept in lockstep
    # with reports.KINDS (the canonical set, which the service also enforces via
    # validate_kind -- defense in depth); hard-coded here to avoid importing the
    # reports service at parser-build time (register runs on every CLI invocation).
    p.add_argument(
        "kind",
        choices=("cost", "tokens", "volume", "qa", "postedit"),
        help="report kind: cost | tokens | volume | qa | postedit.",
    )
    p.add_argument(
        "--from",
        dest="date_from",
        default="",
        help="inclusive start date YYYY-MM-DD (on the job's finished date).",
    )
    p.add_argument("--to", dest="date_to", default="", help="inclusive end date YYYY-MM-DD.")
    p.add_argument(
        "--customer",
        dest="customer_id",
        type=bounded_id,
        default=None,
        help="filter to one customer id.",
    )
    p.add_argument(
        "--project",
        dest="project_id",
        type=bounded_id,
        default=None,
        help="filter to one project id.",
    )
    p.add_argument("--provider", default="", help="filter to one provider name.")
    p.add_argument(
        "--group-by",
        dest="group_by",
        default="none",
        help="project | customer | provider | engine | date | workflow | "
        "locale | vendor | dimension | actor | none. workflow reports "
        "EXACT per-workflow spend: cost/tokens are the sum of each "
        "workflow run's recorded per-step spend (count column = runs). "
        "Plain-MT/connector jobs roll up as '(no workflow)'; runs "
        "finished before spend tracking show as '(untracked)' rather than "
        "a false zero. On provider|engine, a step that made NO provider call "
        "(human review, an orchestration-only step, a skipped or cap-halted "
        "visit) buckets as '(none)' -- distinct from '(untracked)', which is "
        "about spend tracking, not about which engine ran. "
        "locale groups by TARGET locale (jobs, runs and "
        "segment volume all attributed by locale). vendor is a SCORECARD "
        "from vendor dispatch legs (legs, word_count, returned-on-time, "
        "LQA quality columns, and the human-spend money group for admins: "
        "estimate vs actual over RETURNED legs, open_estimate_usd over "
        "IN-FLIGHT legs, and quoted_cost_usd over APPROVED quotes) with its "
        "own columns, not the kind's; a provider filter "
        "does not apply to it. dimension (qa only) is the MQM dimension x "
        "severity cross-tab; actor (qa/postedit) buckets by the machine "
        "source/judge or the human editor and is manager+ on a remote/api "
        "call -- always-on there (a postedit bucket key is an editor email, "
        "so it is refused even when the login wall is off, unlike cost); the "
        "local operator is trusted. An axis a kind does not "
        "support degrades to none with a caveat.",
    )
    p.add_argument("-o", "--out", default="", help="write CSV to this path.")
    p.set_defaults(func=_report_run)

    p = sub.add_parser("kinds", help="List report kinds + their columns.")
    p.set_defaults(func=_report_kinds)


# --- helpers -----------------------------------------------------------------
def _flag_gate():
    """Layer-2 CLI kill switch (the message lives in the registry; see feature_gate)."""
    from ._common import feature_gate

    feature_gate("reports")


def _build_query(args):
    from app.kernel import reports as reports_service

    try:
        date_from = reports_service.parse_date(args.date_from)
        date_to = reports_service.parse_date(args.date_to)
    except ValueError as e:
        raise CliError("bad date - use YYYY-MM-DD.", EXIT_USAGE) from e
    try:
        return reports_service.ReportQuery(
            kind=args.kind,
            group_by=args.group_by,
            date_from=date_from,
            date_to=date_to,
            customer_id=args.customer_id,
            project_id=args.project_id,
            provider=(args.provider or "").strip() or None,
        ).normalized()
    except reports_service.UnknownReportKind as exc:
        # Defense in depth: argparse choices= already rejects a bad kind, but a caller
        # reaching here another way still gets a clean usage error, not a traceback.
        raise CliError(str(exc), EXIT_USAGE) from exc


def _write_out(path, text, emit):
    import os

    if os.path.exists(path):
        raise CliError(f"destination exists: {path} (choose another -o path).", EXIT_USAGE)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _emit_report(args, emit, report):
    """Print a table / --json, and write CSV when -o was given."""
    from app.kernel import reports as reports_service

    if args.out:
        _write_out(args.out, reports_service.to_csv(report), emit)
    caveats = report.get("caveats") or []
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "kind": report["kind"],
                "group_by": report["group_by"],
                "columns": report["columns"],
                "key_column": report["key_column"],
                "rows": report["rows"],
                "totals": reports_service.totals_row(report),
                "caveats": caveats,
                **({"csv_path": args.out} if args.out else {}),
            }
        )
        return 0
    headers = tuple(c.replace("_", " ") for c in report["columns"])
    rows = [tuple(r.get(c, "") for c in report["columns"]) for r in report["rows"]]
    if report["rows"]:
        totals = reports_service.totals_row(report)
        rows.append(tuple(totals.get(c, "") for c in report["columns"]))
    emit.table(rows or [tuple("" for _ in report["columns"])], headers=headers)
    for caveat in caveats:
        emit.say(f"Note: {caveat}")
    if args.out:
        emit.say(f"Wrote CSV -> {args.out}.")
    return 0


# --- commands ----------------------------------------------------------------
def _report_run(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.report_run(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import reports as reports_service

    q = _build_query(args)
    with _session() as session:
        # In-process trusted operator: no per-user identity (user=None). The resolved
        # --org scope (op_org_id) hard-filters to that org's projects under the gate.
        report = reports_service.run_report(session, q, user=None, org_id=op_org_id(args))
    return _emit_report(args, emit, report)


def _report_kinds(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.report_kinds(args, emit)
    _flag_gate()
    from app.kernel import reports as reports_service

    catalog = reports_service.kinds_catalog()
    if emit.as_json:
        emit.json({"ok": True, "kinds": [{"kind": k, "columns": cols} for k, cols in catalog]})
    else:
        emit.table([(k, ", ".join(cols)) for k, cols in catalog], headers=("kind", "columns"))
    return 0
