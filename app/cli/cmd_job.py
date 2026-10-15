"""`job` command group — Phase 6 (Jobs / Workflows).

Runs background-job HANDLERS SYNCHRONOUSLY in the one-shot CLI process via
jobs.run_sync(). This is the crux of the phase: a `python -m app.cli` process has
NO daemon worker threads (they die with the process), so jobs.submit() would
write a QUEUED Job row and exit — the job would never run. run_sync() executes
the handler inline in the calling thread and returns the finished Job, so when
`job run` returns the work is really done (COMPLETED/FAILED, never left QUEUED).

  job run <kind> --payload k=v ... | --payload-json FILE
  job status <id>
  job list [--kind K] [--status S]

--remote is reserved for a later phase; every command calls require_local().

Documented in: docs/architecture/jobs-and-scheduler.md
"""

import json
import os

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
    grp = subparsers.add_parser("job", help="Background jobs (run synchronously, status, list).")
    sub = grp.add_subparsers(dest="job_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "run", help="Run a job kind SYNCHRONOUSLY in-process (blocks until it finishes)."
    )
    p.add_argument("kind", help="job kind (translate_document, connector_translate).")
    p.add_argument(
        "--payload",
        action="append",
        default=[],
        metavar="K=V",
        help="a payload field (repeatable). Digit values become ints.",
    )
    p.add_argument(
        "--payload-json",
        metavar="FILE",
        help="read the whole payload from a JSON file (types preserved).",
    )
    p.set_defaults(func=_job_run)

    p = sub.add_parser("status", help="Show one job by id.")
    p.add_argument("job_id", type=bounded_id, help="job id.")
    p.set_defaults(func=_job_status)

    p = sub.add_parser(
        "list",
        help="List execution history (background jobs, and optionally workflow runs) newest first.",
    )
    p.add_argument(
        "--kind",
        help="filter by JOB kind (translate_document, connector_translate); implies jobs only.",
    )
    p.add_argument(
        "--status",
        help="filter by status (queued, running, completed, "
        "failed; runs also pending, paused, skipped).",
    )
    p.add_argument(
        "--actor",
        dest="actor_filter",
        metavar="EMAIL",
        help="filter by created_by / launcher (substring, case-insensitive).",
    )
    p.add_argument(
        "--document",
        type=bounded_id,
        dest="document_id",
        default=None,
        metavar="ID",
        help="filter to work targeting this document id (unknown/out-of-scope id -> empty).",
    )
    p.add_argument(
        "--workflow",
        type=bounded_id,
        dest="workflow_id",
        default=None,
        metavar="ID",
        help="filter to runs/batches of this workflow id "
        "(implies runs/batches only; unknown/out-of-scope -> empty).",
    )
    p.add_argument(
        "--include-runs",
        dest="include_runs",
        action="store_true",
        help="also include WORKFLOW RUNS in the unified history (default: background jobs only).",
    )
    p.add_argument(
        "--runs-only",
        dest="runs_only",
        action="store_true",
        help="show ONLY workflow runs (no background jobs).",
    )
    p.add_argument("--limit", type=int, default=50, help="page size (default 50).")
    p.add_argument("--offset", type=int, default=0, help="rows to skip (pagination).")
    p.set_defaults(func=_job_list)


# --- helpers -----------------------------------------------------------------
def _coerce(value: str):
    """A bare `k=v` value: a pure-digit string becomes an int (document_id,
    connection_id, project_id are integer keys); everything else stays a string.
    --payload-json is the escape hatch for richer/typed payloads."""
    if value.lstrip("-").isdigit():
        try:
            n = int(value)
        except ValueError:
            return value  # a Unicode digit passes isdigit() but int() rejects -> keep raw
        # bound the SQLite-bind overflow edge; an over-range value stays a raw string
        # (bound aligned with api_cli._int/_tm_int: signed-64-bit inclusive lower edge)
        if -(2**63) <= n < 2**63:
            return n
    return value


def _build_payload(args) -> dict:
    if args.payload_json:
        if not os.path.isfile(args.payload_json):
            raise CliError(f"payload file not found: {args.payload_json}", EXIT_USAGE)
        with open(args.payload_json, encoding="utf-8") as fh:
            try:
                payload = json.load(fh)
            except json.JSONDecodeError as exc:
                raise CliError(f"invalid JSON in {args.payload_json}: {exc}", EXIT_USAGE) from exc
        if not isinstance(payload, dict):
            raise CliError("payload JSON must be an object.", EXIT_USAGE)
        return payload
    payload: dict = {}
    for item in args.payload:
        if "=" not in item:
            raise CliError(f"bad --payload {item!r} (expected K=V).", EXIT_USAGE)
        k, v = item.split("=", 1)
        payload[k.strip()] = _coerce(v)
    return payload


_WEBHOOK_ONLY_KEYS = ("webhook_record_ids", "webhook_article_ids", "webhook_ping_pull")


def confine_job_payload(kind: str, payload: dict, org_id) -> dict:
    """#515: scope an in-process `job run` payload to the --org the operator
    named. A strict no-op when `org_id` is None (single-tenant, or the key-verified
    --operator door). Otherwise it applies what /api/jobs/run applies before it runs a job:

      * data_export is refused (its row-derived scope belongs to the original owner);
      * connector_translate refuses the signed-webhook-only payload keys;
      * every connection / project / document the payload names must belong to the org
        (a foreign id reads exactly like a missing one);

    The AI opt-out check needs no copy here: the handlers apply it themselves per document
    and per connection (jobs._run_translate_document, _run_connector_translate)."""
    if org_id is None:
        return payload
    if kind == "data_export":
        raise CliError(
            "data_export cannot be run through `job run` under --org; use `export` "
            "(its scope belongs to the original owner).",
            EXIT_USAGE,
        )
    if kind == "connector_translate" and any(k in payload for k in _WEBHOOK_ONLY_KEYS):
        raise CliError("webhook-only payload keys are not accepted here.", EXIT_USAGE)
    from sqlmodel import Session

    from app.db import engine
    from app.models import Connection, Document, Project

    out = dict(payload)
    with Session(engine) as s:
        for field, label in (
            ("connection_id", "connection"),
            ("project_id", "project"),
            ("document_id", "document"),
        ):
            raw = out.get(field)
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise CliError(f"{field} must be an id.", EXIT_USAGE) from exc
            if field == "connection_id":
                row_org = getattr(s.get(Connection, value), "org_id", None)
                found = s.get(Connection, value) is not None
            elif field == "project_id":
                proj = s.get(Project, value)
                found, row_org = proj is not None, getattr(proj, "org_id", None)
            else:
                doc = s.get(Document, value)
                proj = s.get(Project, doc.project_id) if doc is not None else None
                found, row_org = proj is not None, getattr(proj, "org_id", None)
            if not found or row_org != org_id:
                raise CliError(f"no such {label}: {value}", EXIT_USAGE)
            out[field] = value
    return out


def _job_row(job) -> dict:
    from app.kernel import jobs

    return jobs.status_dict(job)


# --- job run (synchronous) ---------------------------------------------------
def _job_run(args, emit) -> int:
    from app.kernel import jobs
    from app.models import JobStatus

    if args.kind not in jobs.HANDLERS:
        raise CliError(
            f"unknown job kind: {args.kind} (known: {', '.join(sorted(jobs.HANDLERS))}).",
            EXIT_USAGE,
        )
    payload = _build_payload(args)
    if remote_mode(args):
        return _remote_impl.job_run(args, emit, payload)
    require_org_scope(args, emit)
    payload = confine_job_payload(args.kind, payload, op_org_id(args))
    actor = getattr(args, "actor", None) or "cli"
    emit.detail(f"running {args.kind} with payload {payload!r} ...")
    job = jobs.run_sync(args.kind, payload, actor=actor)
    d = _job_row(job)
    ok = job.status == JobStatus.COMPLETED
    human = (
        f"Job #{d['id']} ({d['kind']}) {d['status']}"
        + (f": {d['message']}" if d["message"] else "")
        + (f" [{d['done']}/{d['total']}]" if d["total"] else "")
    )
    if d.get("error"):
        human += f"\n  error: {d['error']}"
    if d.get("result"):
        human += f"\n  result: {json.dumps(d['result'], default=str)}"
    emit.result(human, {"ok": ok, **d})
    # A FAILED job is an operation failure (exit 1); the row is still emitted.
    return 0 if ok else 1


# --- job status --------------------------------------------------------------
def _job_status(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.job_status(args, emit)
    require_org_scope(args, emit)
    from app.kernel import jobs

    job = jobs.get(args.job_id)
    # #515: under --org a job of another org is "no such job" (the org a job belongs to is
    # its creator's, or the row its payload names: jobs.job_org_id, the /api/jobs/{id} rule).
    if job is None or (op_org_id(args) is not None and jobs.job_org_id(job) != op_org_id(args)):
        raise CliError(f"no such job: {args.job_id}", EXIT_USAGE)
    d = _job_row(job)
    if emit.as_json:
        emit.json({"ok": True, **d})
    else:
        emit.say(
            f"Job #{d['id']}: {d['kind']} [{d['status']}]"
            + (f" {d['done']}/{d['total']}" if d["total"] else "")
        )
        if d.get("message"):
            emit.say(f"  message: {d['message']}")
        if d.get("error"):
            emit.say(f"  error: {d['error']}")
        if d.get("result"):
            emit.say(f"  result: {json.dumps(d['result'], default=str)}")
    return 0


# --- job list ----------------------------------------------------------------
def _history_type(args) -> str:
    """Resolve the type filter from the CLI flags. Default = jobs only (backward-
    compatible with the pre-history `job list`); --include-runs -> both; --runs-only
    -> runs only. A --kind filter forces jobs (a run has no job kind)."""
    if getattr(args, "runs_only", False):
        return "run"
    if getattr(args, "include_runs", False):
        return "all"
    return "job"


def _job_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.job_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import job_history

    type_ = _history_type(args)
    # In-process CLI is the TRUSTED local-root operator (docs/CLI.md: no login, no
    # RBAC). trusted_operator=True keeps it seeing every job + run even once `init`
    # turns the login wall ON (else user=None is read as the WEB "no user -> sees
    # nothing" semantic and the operator's own `job list` goes blind — issue #16a).
    # The resolved --org (op_org_id, under the multi-org gate) still hard-confines the
    # operator view to one org; None = unscoped single-tenant / --operator cross-org.
    # A web/api caller NEVER reaches this arm (they pass their own User, no flag).
    with _session() as session:
        rows, total = job_history.unified_history(
            session,
            None,
            type=type_,
            status=args.status or "",
            kind=args.kind or "",
            actor=getattr(args, "actor_filter", "") or "",
            limit=args.limit,
            offset=args.offset,
            trusted_operator=True,
            org_id=op_org_id(args),
            document_id=getattr(args, "document_id", None),
            workflow_id=getattr(args, "workflow_id", None),
        )
    if emit.as_json:
        emit.json({"ok": True, "total": total, "jobs": [r.as_api_dict() for r in rows]})
    else:
        emit.table(
            [
                (
                    r.type,
                    r.id,
                    r.kind,
                    r.status,
                    (
                        f"{r.progress_done}/{r.progress_total}"
                        if r.type == "job" and r.progress_total
                        else (f"{r.duration_s:.1f}s" if r.duration_s is not None else "-")
                    ),
                    r.actor or "",
                )
                for r in rows
            ],
            headers=("type", "id", "kind/workflow", "status", "progress", "by"),
        )
    return 0
