"""`export` command group (Batch 13): the CLI twin of the full-object-graph data export.

  export run [--scope self|customer|project|workspace] [--ref ID]
             [--history current|full] [--audit] [--include-eval] [--include-embeddings]
             [--emitters jsonl[,csv,xlsx]]
  export list
  export status <run>              (a local run id, or a run_uuid)
  export download <run_uuid> [-o FILE] [--force]

In-process = the trusted operator (actor from --actor; --org confines under the multi-org
gate, the reports/databricks precedent). Generation runs synchronously through jobs.run_sync
(the walker streams + stages the artifact to EXPORTS_DIR via the ExportStore port). The kill
switch (data_export_enabled) and the plaintext-staging refuse-gate (DX-208) are enforced
inside data_export.run_export and surfaced here as a clean CliError.

`--remote URL --token lsu_KEY` turns each verb into a thin client of the /api/exports routes
(the launch-and-poll pattern of `job` / `databricks`): `run` starts the async job and polls it
to a terminal state; `download` streams the completed artifact ZIP (Range-aware). The remote
arm and the in-process arm render the BYTE-IDENTICAL artifact (LAW4, the prompt_tap parity law).

Documented in: docs/architecture/data-export.md
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _session,
    digit_ref_to_id,
    op_org_id,
    remote_mode,
    require_org_scope,
)


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "export",
        help="Full-object-graph data export (run/list/status/download) - a RBAC-confined, "
        "self-describing dump of a scope's whole object graph as JSONL-per-entity in a ZIP.",
    )
    sub = grp.add_subparsers(dest="export_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("run", help="Run one export (stage a ZIP + manifest; --remote polls).")
    p.add_argument(
        "--scope",
        default="self",
        choices=["self", "customer", "project", "workspace"],
        help="what to export (default: self = everything the caller can see).",
    )
    p.add_argument(
        "--ref",
        type=int,
        default=None,
        help="the customer id (--scope customer) or project id (--scope project).",
    )
    p.add_argument(
        "--history",
        default="current",
        choices=["current", "full"],
        help="current = current state; full = append-only/bi-temporal history too.",
    )
    p.add_argument(
        "--audit",
        action="store_true",
        help="include the admin audit slice (workspace scope only; Train C emits it).",
    )
    p.add_argument(
        "--include-eval",
        dest="include_eval",
        action="store_true",
        help="include the Evaluations Lab tables (default OFF - copy-in provenance risk).",
    )
    p.add_argument(
        "--include-embeddings",
        dest="include_embeddings",
        action="store_true",
        help="include the opaque SegmentEmbedding vectors (default OFF - large + rarely useful).",
    )
    p.add_argument(
        "--emitters",
        default="jsonl",
        help="comma-separated emitter set: jsonl (always on, the machine-first backbone) plus "
        "optional human views csv,xlsx (e.g. --emitters jsonl,csv,xlsx).",
    )
    p.set_defaults(func=_run)

    p = sub.add_parser("list", help="List past export runs (newest first).")
    p.set_defaults(func=_list)

    p = sub.add_parser("status", help="Show one export run's status + counts.")
    p.add_argument("run", help="the DataExportRun id (local) or run_uuid.")
    p.set_defaults(func=_status)

    p = sub.add_parser("download", help="Download a completed export artifact ZIP to a file.")
    p.add_argument("run_uuid", help="the run_uuid of a completed export.")
    p.add_argument(
        "-o",
        "--out",
        default=None,
        help="output path (default: dataexport_<uuid>.zip).",
    )
    p.add_argument("--force", action="store_true", help="overwrite the destination if it exists.")
    p.set_defaults(func=_download)


def _run_dict(run) -> dict:
    return {
        "id": run.id,
        "run_uuid": run.run_uuid,
        "scope_kind": run.scope_kind,
        "scope_ref": run.scope_ref,
        "history": run.history,
        "status": run.status,
        "artifact_key": run.artifact_key,
        "artifact_bytes": run.artifact_bytes,
        "entity_counts": run.entity_counts or {},
        "dropped_counts": run.dropped_counts or {},
        "manifest_sha256": run.manifest_sha256,
        "org_id": run.org_id,
        "customer_id": run.customer_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "expires_at": run.expires_at.isoformat() if run.expires_at else None,
    }


# --- commands ----------------------------------------------------------------
def _run(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.export_run(args, emit)
    require_org_scope(args, emit)
    from app.kernel import data_export

    emitters = tuple(s.strip() for s in (args.emitters or "jsonl").split(",") if s.strip()) or (
        "jsonl",
    )
    try:
        run = data_export.run_export(
            user=None,  # the trusted in-process operator (host CLI)
            ident=None,
            org_id=op_org_id(args),
            scope_kind=args.scope,
            scope_ref=args.ref,
            history=args.history,
            emitters=emitters,
            include_audit=args.audit,
            include_eval=args.include_eval,
            include_embeddings=args.include_embeddings,
            actor=_actor(args),
        )
    except data_export.NotFound as exc:
        raise CliError(f"not found: {exc}", EXIT_USAGE) from exc
    except data_export.ExportRefused as exc:
        raise CliError(str(exc)) from exc
    d = _run_dict(run)
    ok = d["status"] in ("ok", "partial")
    emit.result(
        "Export run #{} ({}/{}, {}): status={} artifact={} ({} bytes).".format(
            d["id"],
            d["scope_kind"],
            d["history"],
            d["run_uuid"],
            d["status"],
            d["artifact_key"],
            d["artifact_bytes"],
        ),
        {"ok": ok, **d},
    )
    return 0 if ok else 1


def _list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.export_list(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.models import DataExportRun

    org_id = op_org_id(args)
    with _session() as session:
        stmt = select(DataExportRun)
        if org_id is not None:
            stmt = stmt.where(DataExportRun.org_id == org_id)
        runs = session.exec(stmt.order_by(DataExportRun.id.desc()).limit(100)).all()
        payload = [_run_dict(r) for r in runs]
    if emit.as_json:
        emit.json({"ok": True, "runs": payload})
    else:
        emit.table(
            [
                (
                    d["id"],
                    d["scope_kind"],
                    d["history"],
                    d["status"],
                    sum((d["entity_counts"] or {}).values()),
                    d["artifact_bytes"],
                )
                for d in payload
            ],
            headers=("id", "scope", "history", "status", "rows", "bytes"),
        )
    return 0


def _status(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.export_status(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.models import DataExportRun

    org_id = op_org_id(args)
    ref = str(args.run)
    with _session() as session:
        run = None
        run_id = digit_ref_to_id(ref) if ref.isdigit() else None  # U+00B2 passes isdigit()
        if run_id is not None:
            run = session.get(DataExportRun, run_id)
        if run is None:
            run = session.exec(select(DataExportRun).where(DataExportRun.run_uuid == ref)).first()
        if run is None or (org_id is not None and run.org_id != org_id):
            raise CliError(f"no such export run: {ref}", EXIT_USAGE)
        d = _run_dict(run)
    if emit.as_json:
        emit.json({"ok": True, **d})
    else:
        emit.say(
            "Export run #{} ({}/{}, {})".format(d["id"], d["scope_kind"], d["history"], d["status"])
        )
        emit.say(f"  run_uuid:   {d['run_uuid']}")
        emit.say(f"  artifact:   {d['artifact_key']} ({d['artifact_bytes']} bytes)")
        emit.say(f"  counts:     {d['entity_counts'] or {}}")
        if d["dropped_counts"]:
            emit.say(f"  dropped:    {d['dropped_counts']}")
        emit.say(f"  expires_at: {d['expires_at']}")
    return 0


def _download(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.export_download(args, emit)
    require_org_scope(args, emit)
    import os

    from sqlmodel import select

    from app.models import DataExportRun
    from app.ports import exportstore

    org_id = op_org_id(args)
    with _session() as session:
        run = session.exec(
            select(DataExportRun).where(DataExportRun.run_uuid == args.run_uuid)
        ).first()
        if run is None or (org_id is not None and run.org_id != org_id):
            raise CliError(f"no such export run: {args.run_uuid}", EXIT_USAGE)
        if run.status not in ("ok", "partial") or not run.artifact_key:
            raise CliError(
                f"export {args.run_uuid} has no downloadable artifact (status={run.status})."
            )
        key = run.artifact_key
    from app.kernel import data_export as dx

    # files-03: the download-time coverage re-check (web + /api parity).
    if not dx.artifact_servable(args.run_uuid):
        raise CliError(
            f"export {args.run_uuid} has no downloadable artifact (its scope was deleted; "
            "the artifact has been purged)."
        )
    # Resolve the artifact through the registered ExportStore (disk or S3), exactly like the
    # web and /api downloads: a bare EXPORTS_DIR path made every S3-backed export "gone" (#528).
    out = args.out or f"dataexport_{args.run_uuid}.zip"
    if os.path.exists(out) and not args.force:
        raise CliError(f"destination exists: {out} (pass --force to overwrite).", EXIT_USAGE)
    gone = f"artifact file is gone (reaped or purged): {key}"
    try:
        stream = exportstore.get().open_range(key, 0, None)
    except FileNotFoundError as exc:
        raise CliError(gone) from exc
    # Write a sibling temp then rename, so a failed stream never leaves a truncated zip
    # (or clobbers an existing --force target) behind.
    tmp = f"{out}.part"
    try:
        with open(tmp, "wb") as dst:
            for chunk in stream:
                dst.write(chunk)
        os.replace(tmp, out)
    except FileNotFoundError as exc:
        raise CliError(gone) from exc
    except Exception as exc:
        raise CliError(f"download failed: {type(exc).__name__}") from exc
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    n = os.path.getsize(out)
    emit.result(
        f"Downloaded export {args.run_uuid} -> {out} ({n} bytes).",
        {"ok": True, "run_uuid": args.run_uuid, "out": out, "bytes": n},
    )
    return 0
