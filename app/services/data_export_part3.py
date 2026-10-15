"""data_export: part3

Documented in: docs/architecture/data-export.md
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlmodel import Session, select

from app.db import engine
from app.models import (
    Connection,
    DataExportRun,
    DataExportStatus,
    EvalDataset,
    Glossary,
    User,
    Workflow,
)
from app.services.data_export_part1 import (
    ResolvedScope,
    _audit,
    _check_refuse_gates,
    _fail_dead_running,
    _grace_minutes,
    _opts_hash,
    _org_val,
    _parse_iso_naive,
    _resolve_scope,
    _retention_hours,
    _row_covers,
    _sweep_due,
    _unlink_run_tmp,
)
from app.services.data_export_part2 import (
    _build_payload,
    _cust_clause,
    _cust_or_proj_clause,
    _eval_dataset_clause,
    _new_run_row,
    _org_clause,
    _read_reap_status,
    _reap_tmp_orphans,
    _write_reap_status,
)
from app.services.maintenance import admitted_work


def _glossaries_subq(scope):
    return select(Glossary.id).where(_cust_clause(Glossary, scope, global_bearing=True))


def _workflows_subq(scope):
    return select(Workflow.id).where(_cust_or_proj_clause(Workflow, scope, global_bearing=True))


def _global_workflows_subq(scope):
    """App-global workflows (no customer, no project) visible to the scope's org."""
    o = _org_val(scope)
    q = select(Workflow.id).where(Workflow.customer_id.is_(None), Workflow.project_id.is_(None))
    if o is not None:
        q = q.where(or_(Workflow.org_id.is_(None), Workflow.org_id == o))
    return q


def _connections_subq(scope):
    return select(Connection.id).where(_cust_or_proj_clause(Connection, scope, global_bearing=True))


def _org_users_subq(scope):
    return select(User.id).where(_org_clause(User, scope))


def _eval_datasets_subq(scope):
    return select(EvalDataset.id).where(_eval_dataset_clause(scope))


@admitted_work
def run_export(
    *,
    user,
    ident=None,
    org_id=None,
    scope_kind="self",
    scope_ref=None,
    history="current",
    emitters=("jsonl",),
    include_audit=False,
    include_eval=False,
    include_embeddings=False,
    actor=None,
) -> DataExportRun:
    """The synchronous host-CLI entry (§6): resolve + confine the scope under the caller,
    create the DataExportRun audit row, and drive generation through ``jobs.run_sync`` (the
    handler streams + stages the artifact atomically). Returns the finished DataExportRun.

    Raises ``NotFound`` for an out-of-scope / non-admin-workspace request (LAW2) and
    ``ExportRefused`` for the kill switch / plaintext-staging gate (LAW9/DX-208)."""
    # A real user's actor MUST be its email so the handler can re-resolve the principal from the
    # persisted row (TA-01); the trusted operator (user=None) keeps whatever actor was given.
    if user is not None:
        actor = actor or getattr(user, "email", None)
    _check_refuse_gates()
    with Session(engine) as session:
        scope = _resolve_scope(
            session,
            user=user,
            ident=ident,
            org_id=org_id,
            scope_kind=scope_kind,
            scope_ref=scope_ref,
        )
    ia = bool(include_audit and scope.scope_kind == "workspace")
    run_uuid = uuid.uuid4().hex
    opts_hash = _opts_hash(history, emitters, ia, include_eval, include_embeddings)
    _new_run_row(
        scope,
        run_uuid,
        history,
        emitters,
        ia,
        include_eval,
        include_embeddings,
        actor,
        trusted_operator=(user is None),
    )
    payload = _build_payload(scope, run_uuid, opts_hash, actor)
    from app.services import jobs

    job = jobs.run_sync("data_export", payload, actor=actor or "cli")
    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row.job_id is None and job is not None:
            row.job_id = job.id
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def submit_export(
    *,
    user,
    ident=None,
    org_id=None,
    scope_kind="self",
    scope_ref=None,
    history="current",
    emitters=("jsonl",),
    include_audit=False,
    include_eval=False,
    include_embeddings=False,
    actor=None,
) -> tuple:
    """The ASYNC enqueue path (§5.2 -- the Train-B /api POST calls this): resolve + confine the
    scope, create the DataExportRun row, then ``jobs.submit_unique`` on the FIELD-NAME dedupe
    key ``("scope_kind","scope_ref","actor","opts_hash")`` (DX-04: the elements are payload FIELD
    NAMES the helper looks up, never literal values) so one active export per
    (scope, actor, opts) is enforced while a DIFFERENT option set gets its OWN job (DX-210).
    The post-dedupe ``created_by == actor`` guard (DX-01) never hands a caller another caller's
    job. Returns ``(run_uuid, job_id, created)``."""
    if user is not None:
        actor = actor or getattr(user, "email", None)  # TA-01: row-derivable principal
    _check_refuse_gates()
    with Session(engine) as session:
        scope = _resolve_scope(
            session,
            user=user,
            ident=ident,
            org_id=org_id,
            scope_kind=scope_kind,
            scope_ref=scope_ref,
        )
    ia = bool(include_audit and scope.scope_kind == "workspace")
    run_uuid = uuid.uuid4().hex
    opts_hash = _opts_hash(history, emitters, ia, include_eval, include_embeddings)
    _new_run_row(
        scope,
        run_uuid,
        history,
        emitters,
        ia,
        include_eval,
        include_embeddings,
        actor,
        trusted_operator=(user is None),
    )
    payload = _build_payload(scope, run_uuid, opts_hash, actor)
    from app.services import jobs

    job_id, created = jobs.submit_unique(
        "data_export",
        payload,
        actor or "",
        dedupe_key=("scope_kind", "scope_ref", "actor", "opts_hash"),
    )
    if not created:
        existing = jobs.get(job_id)
        if existing is None or existing.created_by != (actor or ""):
            # DX-01/DX-04: never return another caller's job -- start a fresh one.
            job_id = jobs.submit("data_export", payload, actor or "")
            created = True
    with Session(engine) as s:
        r = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if r is not None:
            r.job_id = job_id
            s.add(r)
            s.commit()
    return run_uuid, job_id, created


def reap_scheduled_sweep(now: datetime | None = None) -> bool:
    """Piggybacked scheduler sweep (§2.3): reap expired staged artifacts + orphan `.tmp` files
    when due. Self-throttles to once per `data_export_retention_hours` via a MAC'd status row (0
    disables). DB + filesystem only, no network egress -- so it sits ABOVE the connector-sync
    kill switch like the audit-chain / llmemory sweeps. Returns True iff a sweep ran this call.
    Best-effort: the caller (scheduler.tick) wraps it."""
    hours = _retention_hours()
    if hours <= 0:
        return False  # retention disabled -> no reaping cadence
    now = now or datetime.utcnow()
    st = _read_reap_status()
    last = _parse_iso_naive(st.get("last")) if isinstance(st, dict) else None
    if not _sweep_due(last, now, hours):
        return False
    _reap_once(now)
    _write_reap_status({"last": now.isoformat()})
    return True


def _reap_once(now: datetime) -> int:
    """The reaper CORE (testable directly, cadence-independent): delete each staged artifact past
    its `expires_at` + the DX-206 hard grace ceiling, fail-out dead `running` rows, and unlink
    orphan `dataexport_*.tmp` siblings (TA-07). Returns the number of artifacts reaped."""
    from app.ports import exportstore

    store = exportstore.get()
    grace_min = _grace_minutes()
    hours = _retention_hours()
    # DX-206: the hard, NON-RENEWABLE deletion ceiling is measured FROM expires_at (never from
    # last_downloaded_at), so a 1-byte Range request every grace-1 min cannot keep an artifact
    # alive past expires_at + max(grace, one sweep interval), nor pin the active-slot cap.
    ceiling_delta = max(timedelta(minutes=grace_min), timedelta(hours=hours))
    reaped = 0
    with Session(engine) as s:
        rows = s.exec(
            select(DataExportRun).where(
                DataExportRun.status.in_([DataExportStatus.OK, DataExportStatus.PARTIAL]),
                DataExportRun.expires_at.is_not(None),
                DataExportRun.expires_at < now,
            )
        ).all()
        for row in rows:
            hard_ceiling = row.expires_at + ceiling_delta
            downloaded_recently = row.last_downloaded_at is not None and (
                now - row.last_downloaded_at
            ) < timedelta(minutes=grace_min)
            if downloaded_recently and now < hard_ceiling:
                continue  # DX-16 grace: skip an in-flight Range-resume, but ONLY under the ceiling
            if row.artifact_key and store.delete(row.artifact_key):
                # Mark `expired` ONLY on a real removal (the port's delete returns False on a
                # missing blob / OSError and NEVER raises, §8.3), so a False leaves the row
                # un-`expired` for a retry rather than falsely claiming removal.
                row.status = DataExportStatus.EXPIRED
                s.add(row)
                reaped += 1
        # TC-01 BACKSTOP: a `purged` row whose artifact_key is STILL set means a prior purge's
        # delete() returned False -- most importantly a CLI-context GDPR erase whose process
        # resolved the WRONG exportstore adapter (the default disk store instead of the S3 staging
        # adapter), so the S3 object was never removed. The reaper runs in the SERVER context,
        # which registers the S3 adapter, so re-attempt the delete here and clear the key once the
        # blob is finally gone -- an erased subject's staged export can never persist indefinitely,
        # even if some future erase context lacks the right adapter.
        stuck = s.exec(
            select(DataExportRun).where(
                DataExportRun.status == DataExportStatus.PURGED,
                DataExportRun.artifact_key != "",
            )
        ).all()
        for row in stuck:
            if store.delete(row.artifact_key):
                row.artifact_key = ""
                s.add(row)
        _fail_dead_running(s, now)
        s.commit()
    _reap_tmp_orphans(now, ceiling_delta)
    return reaped


def purge_artifacts_covering(
    *,
    org_id,
    customer_ids=(),
    project_ids=(),
    actors=(),
    reason: str = "",
    actor: str | None = "gdpr",
) -> dict:
    """DX-207: content-removal for STAGED export artifacts that a GDPR erase would otherwise miss
    (EXPORTS_DIR sits outside every existing erasure sweep). Finds live DataExportRun rows in the
    subject's org whose resolved scope COVERS the subject, deletes each staged artifact via the
    ExportStore port, cancels an in-flight run (so it cannot re-stage erased content -- the
    handler cooperates by dropping its output on a `purged` row), unlinks any stranded `.tmp`, and
    marks the row `purged` (kept for audit, LAW8 -- never hard-deleted).

    ORG-CONFINED (DX-311): matches ONLY rows whose `org_id` == the subject's org, so it never
    reaches another org's artifacts. Fail-safe OVER-deletion by design: erasing a user (who owns
    no project/customer of their own) matches every live WORKSPACE-scope artifact in the org.

    `actors` (#539, 2026-10-05): requester identities (email and tombstone) whose OWN
    exports are purged whatever their scope. A project/customer export is no longer purged
    just because it sits in the subject's org: only when the subject requested it."""
    with Session(engine) as s:
        marked = mark_covering_purged(
            s, org_id=org_id, customer_ids=customer_ids, project_ids=project_ids, actors=actors
        )
        s.commit()
    return finish_purge(marked, org_id=org_id, reason=reason, actor=actor)


_LIVE_STATUSES = (
    DataExportStatus.OK,
    DataExportStatus.PARTIAL,
    DataExportStatus.RUNNING,
    DataExportStatus.QUEUED,
)


def mark_covering_purged(session, *, org_id, customer_ids=(), project_ids=(), actors=()) -> list:
    """files-03: the TRANSACTIONAL half of the DX-207 purge. Flip every live DataExportRun in the
    subject's org whose scope covers the subject to `purged` IN THE CALLER'S SESSION (no commit),
    so a content delete and the invalidation of every export holding that content commit
    together: there is no window after the delete commits in which a stale artifact is still
    downloadable, and no best-effort failure that leaves one served. Returns the
    `(run_uuid, artifact_key)` pairs whose staged bytes `finish_purge` removes after the commit.

    ORG-CONFINED (DX-311): only rows whose org_id is the subject's org, PLUS rows with a NULL
    org_id. A NULL-org run is an UNRESTRICTED dump: every real-user export on a single-tenant
    install (multi-org off, where `access.current_org_id` is None) and a see-all operator run
    without `--org`. It can hold any org's content, so it covers every subject. Matching only
    `org_id == subject_org` missed all of them once the boot backfill stamped the Default org
    on projects/customers/users, so on a default install no erasure ever reached a staged
    export. A row of ANOTHER real org is still never touched."""
    customer_ids = set(customer_ids or ())
    project_ids = set(project_ids or ())
    actors = {a for a in (actors or ()) if a}
    org_match = (
        DataExportRun.org_id.is_(None)
        if org_id is None
        else or_(DataExportRun.org_id == org_id, DataExportRun.org_id.is_(None))
    )
    rows = session.exec(
        select(DataExportRun).where(
            DataExportRun.status.in_(_LIVE_STATUSES),
            org_match,  # org confinement -- NEVER another real org (DX-311)
        )
    ).all()
    marked = []
    for row in rows:
        if not (_row_covers(row, customer_ids, project_ids) or (row.actor and row.actor in actors)):
            continue
        row.status = DataExportStatus.PURGED  # non-downloadable + audit-kept; cancels the run
        row.finished_at = row.finished_at or datetime.utcnow()
        session.add(row)
        marked.append((row.run_uuid, row.artifact_key or ""))
    return marked


def finish_purge(marked, *, org_id, reason: str = "", actor: str | None = "gdpr") -> dict:
    """The post-commit half of the DX-207 purge: delete each marked run's staged artifact via
    the ExportStore port, unlink any stranded `.tmp`, clear `artifact_key` on a REAL removal,
    and write one `data_export.purged` audit row. Best-effort and never raises: a row is
    already `purged` (non-downloadable), and the reaper backstop retries a key left set."""
    from app.ports import exportstore

    store = exportstore.get()
    cleared = []
    for run_uuid, key in marked or ():
        if not key:
            _unlink_run_tmp(run_uuid)
            continue
        removed = False
        try:
            removed = store.delete(key)  # best-effort (never raises)
        except Exception:
            removed = False
        _unlink_run_tmp(run_uuid)
        # TC-01: clear the key ONLY on a real removal. A False (missing blob OR a WRONG-store
        # delete -- a process whose active exportstore adapter differs from the one the artifact
        # was staged through, e.g. a CLI-context erase on an S3 install where only the server
        # registered the S3 adapter) LEAVES the key set, so the reaper backstop (_reap_once)
        # re-attempts the delete in a context that has the right adapter.
        if removed:
            cleared.append(run_uuid)
    if cleared:
        try:
            with Session(engine) as s:
                for row in s.exec(
                    select(DataExportRun).where(DataExportRun.run_uuid.in_(cleared))
                ).all():
                    row.artifact_key = ""
                    s.add(row)
                s.commit()
        except Exception:  # noqa: S110 -- the reaper backstop clears a stale key later
            pass
    purged = len(marked or ())
    if purged:
        _audit(
            "data_export.purged",
            ResolvedScope("workspace", None, org_id, None, frozenset(), clamp=None),
            actor,
            detail=f"gdpr erase: {reason} purged={purged}",
        )
    return {"purged": purged}


def artifact_servable(run_uuid: str) -> bool:
    """files-03: the DOWNLOAD-TIME coverage re-check every artifact door (web, /api, CLI) runs
    after its ownership check. An artifact is served only when its run is `ok`/`partial` with
    a staged key AND the scope target it was built for still exists. A project- or
    customer-scope run whose project/customer has since been deleted is refused, and is purged
    on the spot (fail-safe), so a stale artifact the delete-time purge missed (a purge that
    raised, a pre-fix artifact) can never be handed out."""
    from app.models import Customer, Project

    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row is None:
            return False
        if row.status not in (DataExportStatus.OK, DataExportStatus.PARTIAL):
            return False
        if not row.artifact_key:
            return False
        # #539: the non-renewable expiry ceiling is enforced at SERVE time, not only when the
        # (at most once per window) sweep reaps the row; a failed blob delete used to leave an
        # expired artifact downloadable indefinitely.
        if row.expires_at is not None and row.expires_at <= datetime.utcnow():
            return False
        gone = False
        if row.scope_kind == "project" and row.scope_ref is not None:
            gone = s.get(Project, row.scope_ref) is None
        elif row.scope_kind == "customer" and row.scope_ref is not None:
            gone = s.get(Customer, row.scope_ref) is None
        if not gone:
            return True
        org_id = row.org_id
        row.status = DataExportStatus.PURGED
        row.finished_at = row.finished_at or datetime.utcnow()
        s.add(row)
        marked = [(row.run_uuid, row.artifact_key or "")]
        s.commit()
    finish_purge(marked, org_id=org_id, reason="download-time: scope target deleted")
    return False
