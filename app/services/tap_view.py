"""Prompt Tap RUN-ENVELOPE VIEWER read model (proposal §9).

The Prompt Tap page used to be a FLAT chronological stream of every provider call. The
run-envelope feature reorganizes it into the hierarchy a PM actually reads (D1):

    TapAction  (one translate CLICK)               -- project / doc / locales / status
      └─ TapRun   (one language-pair leg)           -- locale / status / counts / spend
          └─ calls (the file-based records)         -- the request/response/usage/cost

This module is the READ MODEL that assembles that tree for the web viewer and the CLI
(`prompt-tap actions` / `show-action`). It is READ-ONLY and best-effort: a failure here
degrades to an empty/partial view, never a 500 -- the page must always render.

UNENFORCED-REFERENCE SAFETY (the standing lens). An action records project/document ids
as bare provenance ints; resolving a NAME for display tolerates a deleted row (falls back
to the id), and a run whose calls file was TTL-swept simply shows its envelope with no
nested calls. Nothing here joins a stored id in a way that can crash on a dangling one.

Documented in: docs/architecture/connectors.md
"""

import logging
import os
from datetime import datetime

from app.services import access

log = logging.getLogger("loc_tms.tap_view")

# Cap the calls materialized per run so one huge leg cannot blow up the page DOM /
# CLI output. The envelope's own rollup still reports the true total; the viewer says
# "showing N of M" when it bites.
CALLS_PER_RUN_CAP = 250
DEFAULT_ACTION_LIMIT = 50
# How many recent actions a provider-filtered list scans for matches (bounded work).
PROVIDER_SCAN_ACTIONS = 500


def _session():
    from sqlmodel import Session

    from app.db import engine

    return Session(engine)


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _parse_date(value):
    """A yyyy-mm-dd (or ISO) filter bound -> naive datetime, or None. Best-effort: a
    malformed date is ignored rather than erroring the page."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        try:
            return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None


def _calls_by_run(action_id, *, cap=None):
    """({run_id: [call_record, ...]}, {run_id: total_count}) for one action, read from its
    per-action call file, newest first within each run. Records are already redacted on
    write. ({}, {}) on any failure (a swept / missing file just yields no nested calls).

    STREAMING CAP (L1). When `cap` is set the file is streamed into a bounded deque per run
    (maxlen=cap), so at most `cap` -- the NEWEST -- records are MATERIALIZED per run even
    though the true total is still counted. The grain fix (D1) concentrates N locales'
    calls into ONE per-action file, and the list view assembles up to 50 actions, so
    materializing whole files before the cap applied could blow memory; the cap now bites
    WHILE reading, not after.

    RECORD ADDRESS (M3). Each returned record carries `_record_key` -- the content digest
    the per-row Download link needs (`/prompt-tap/download?record=<key>`). It is computed
    over the PRISTINE record BEFORE the key is attached, so prompt_tap.find_record (which
    recomputes the digest over the file record, without this field) matches it exactly.
    N6: the digest is computed ONLY over the SURVIVING (post-cap) records in the
    materialization below -- not inside the streaming loop -- so the cap-discarded records
    are never hashed (round-1 hashed every record including the ones the deque dropped)."""
    from collections import deque

    from app.services import prompt_tap

    buckets = {}
    counts = {}
    try:
        path = prompt_tap._action_path(action_id)
        if path is None or not os.path.exists(path):
            return buckets, counts
        for rec in prompt_tap._iter_file(path):
            rid = (rec.get("meta") or {}).get("run_id")
            counts[rid] = counts.get(rid, 0) + 1
            if cap is not None and cap > 0:
                buckets.setdefault(rid, deque(maxlen=cap)).append(rec)
            else:
                buckets.setdefault(rid, []).append(rec)
    except Exception:
        return buckets, counts
    out = {}
    for rid, seq in buckets.items():
        lst = list(seq)
        lst.reverse()  # newest first within the run
        for rec in lst:  # key ONLY the survivors (N6)
            try:
                rec["_record_key"] = prompt_tap.record_key(rec)
            except Exception:
                rec["_record_key"] = ""
        out[rid] = lst
    return out, counts


def _name_maps(s, actions, *, org_id=None):
    """{project_id: name} and {document_id: filename} for the actions on the page, resolved
    in two batched reads. A deleted project/doc is simply absent (the viewer falls back to
    the bare id) -- dangling-safe.

    ORG SCOPING (H2). When `org_id` is not None (multi-org active), only Projects in that
    org are resolved to a name, and only Documents whose parent Project is in that org --
    so even if a cross-org action slipped through, its project name / document filename
    never renders. Belt-and-suspenders beside list_actions' own org filter."""
    proj_ids = {a.project_id for a in actions if a.project_id is not None}
    doc_ids = {a.document_id for a in actions if a.document_id is not None}
    projects, docs = {}, {}
    try:
        from sqlmodel import select

        from app.models import Document, Project

        if proj_ids:
            q = select(Project).where(Project.id.in_(proj_ids))
            if org_id is not None:
                q = q.where(Project.org_id == org_id)
            for p in s.exec(q).all():
                projects[p.id] = p.name
        if doc_ids:
            q = select(Document).where(Document.id.in_(doc_ids))
            if org_id is not None:
                # scope via the parent Project's org (Document carries no org column).
                q = q.join(Project, Project.id == Document.project_id).where(
                    Project.org_id == org_id
                )
            for d in s.exec(q).all():
                docs[d.id] = d.filename
    except Exception:  # noqa: S110 -- label lookup fails closed (no names render); view still loads
        pass
    return projects, docs


def _vendor_legs_for(s, runs):
    """The compact VENDOR dispatch legs for a set of TapRuns (joined by workflow_run_id --
    a TapRun maps 1:1 to a WorkflowRun, which owns the legs), for the envelope viewer
    (Wave C, interop R5/R6). Host only, never the claim token / secret / full URL
    (leg_to_dict's discipline). [] on none / any failure (the viewer never 500s)."""
    try:
        rids = [r.workflow_run_id for r in runs if getattr(r, "workflow_run_id", None) is not None]
        if not rids:
            return []
        from sqlmodel import select

        from app.models import VendorDispatch
        from app.services import vendor_return

        legs = s.exec(
            select(VendorDispatch)
            .where(VendorDispatch.workflow_run_id.in_(rids))
            .order_by(VendorDispatch.id.asc())
        ).all()
        out = []
        for leg in legs:
            d = vendor_return.leg_to_dict(s, leg)
            out.append(
                {
                    "id": d["id"],
                    "vendor": d["vendor_name"] or d["vendor_id"],
                    "status": d["status"],
                    "endpoint_host": d["endpoint_host"],
                    "protocol": d.get("protocol", "native"),
                    "target_locale": d["target_locale"],
                    "segments": d["segment_count"],
                    "estimate_cost_usd": d["estimate_cost_usd"],
                    "actual_cost_usd": d["actual_cost_usd"],
                    "returned_ids": d["returned_ids"],
                    "dispatched_ids": d["dispatched_ids"],
                }
            )
        return out
    except Exception:
        return []


def _run_summary(run, calls, total_calls=None):
    """The per-run leg summary the viewer shows on the run row. Prefers the enrichment
    rollup (TapRun.detail.rollup, Wave B) and falls back to counting the calls, so a run
    recorded before enrichment still shows honest counts. `total_calls` (the TRUE count
    before any display cap) is used for the fallback so a capped `calls` list does not
    under-report an un-enriched run (L1)."""
    total = total_calls if total_calls is not None else len(calls)
    detail = run.detail or {}
    roll = detail.get("rollup") or {}
    if roll:
        return {
            "calls": roll.get("calls", total),
            "failures": roll.get("failures", 0),
            "tokens_in": roll.get("tokens_in", 0),
            "tokens_out": roll.get("tokens_out", 0),
            "spend_usd": roll.get("spend_usd", 0.0),
            "source": "envelope",
        }
    failures = sum(1 for c in calls if (c.get("outcome") or "") != "ok")
    return {
        "calls": total,
        "failures": failures,
        "tokens_in": None,
        "tokens_out": None,
        "spend_usd": None,
        "source": "calls",
    }


def list_actions(
    *,
    limit=DEFAULT_ACTION_LIMIT,
    project_id=None,
    document_id=None,
    batch_id=None,
    status=None,
    since=None,
    until=None,
    has_failures=False,
    org_id=None,
    provider=None,
):
    """Actions newest-first, each with its nested runs and per-run calls, for the grouped
    viewer (proposal §9). Best-effort: returns [] on any failure (the page never 500s).

    `provider` (the same engine ref / kind filter the flat section takes, e.g. the judge
    Inspect page's `engine:<id>` link) keeps only the calls to that provider, drops runs
    and actions left with none, and caps the RESULT at `limit` (scanning at most
    PROVIDER_SCAN_ACTIONS recent actions to find them). None is byte-identical.

    Filters (all optional): project_id, document_id, batch_id, status, a started-at date
    range (since/until, yyyy-mm-dd), and has_failures (only actions with >=1 failed call
    or a rolled-up failure count). The date range and has_failures are applied honestly
    over the envelope's own recorded facts, not a time-window heuristic.

    ORG SCOPING (H2). `org_id` None (the single-tenant default, and every install today)
    is byte-identical to before -- no org predicate. When it is set (the web route passes
    the caller's org whenever multi_org is active, via TapAction.org_id, which now EXISTS),
    the grouped-actions section is confined to that org: actions AND the resolved name maps
    (so another org's project name / document filename / initiator never renders). The
    admin gate on the route is NOT a tenant boundary (an org-admin passes it), which is why
    the scope is applied here on the data."""
    try:
        from sqlmodel import select

        from app.models import TapAction, TapRun

        since_dt, until_dt = _parse_date(since), _parse_date(until)
        with _session() as s:
            q = select(TapAction)
            if org_id is not None:
                q = q.where(TapAction.org_id == org_id)
            if project_id is not None:
                q = q.where(TapAction.project_id == project_id)
            if document_id is not None:
                q = q.where(TapAction.document_id == document_id)
            if batch_id is not None:
                q = q.where(TapAction.batch_id == batch_id)
            if status:
                q = q.where(TapAction.status == status)
            if since_dt is not None:
                q = q.where(TapAction.started_at >= since_dt)
            if until_dt is not None:
                q = q.where(TapAction.started_at <= until_dt)
            scan = max(limit or 0, PROVIDER_SCAN_ACTIONS) if provider else limit
            q = q.order_by(TapAction.started_at.desc(), TapAction.id.desc()).limit(scan)
            actions = s.exec(q).all()
            projects, docs = _name_maps(s, actions, org_id=org_id)

            out = []
            for a in actions:
                if provider and limit and len(out) >= limit:
                    break
                calls_by_run, call_counts = _calls_by_run(a.id, cap=CALLS_PER_RUN_CAP)
                if provider:
                    from app.services import prompt_tap

                    calls_by_run = {
                        rid: [c for c in calls if prompt_tap._matches(c, provider=provider)]
                        for rid, calls in calls_by_run.items()
                    }
                    calls_by_run = {rid: c for rid, c in calls_by_run.items() if c}
                    if not calls_by_run:
                        continue
                    call_counts = {rid: len(c) for rid, c in calls_by_run.items()}
                runs = s.exec(
                    select(TapRun)
                    .where(TapRun.action_id == a.id)
                    .order_by(TapRun.target_locale.asc(), TapRun.started_at.asc())
                ).all()
                if provider:
                    runs = [r for r in runs if r.id in calls_by_run]
                run_views = []
                action_failures = 0
                for r in runs:
                    rc = calls_by_run.get(r.id, [])
                    total_rc = call_counts.get(r.id, len(rc))
                    summ = _run_summary(r, rc, total_calls=total_rc)
                    action_failures += summ["failures"] or 0
                    run_views.append(
                        {
                            "id": r.id,
                            "target_locale": r.target_locale,
                            "status": r.status,
                            "started_at": r.started_at,
                            "finished_at": r.finished_at,
                            "summary": summ,
                            "calls": rc,
                            "call_count": total_rc,
                            "more": max(0, total_rc - len(rc)),
                        }
                    )
                # Calls stamped for a run that no longer exists (should not happen, but a
                # swept/edited file could): keep them visible under an "unattached" bucket
                # so nothing silently vanishes from the page.
                orphan = calls_by_run.get(None, [])
                totals = a.totals or None
                tot_failures = (totals or {}).get("failures")
                fails = tot_failures if tot_failures is not None else action_failures
                view = {
                    "id": a.id,
                    "kind": a.kind,
                    "status": a.status,
                    "batch_id": a.batch_id,
                    "project_id": a.project_id,
                    "project_name": projects.get(a.project_id),
                    "document_id": a.document_id,
                    "document_name": docs.get(a.document_id),
                    "requested_locales": a.requested_locales or [],
                    "source_locale": a.source_locale,
                    "workflow_id": a.workflow_id,
                    "workflow_name": a.workflow_name,
                    "initiator": a.initiator,
                    "surface": a.surface,
                    "started_at": a.started_at,
                    "finished_at": a.finished_at,
                    "started_iso": _iso(a.started_at),
                    "totals": totals,
                    "runs": run_views,
                    "vendor_legs": _vendor_legs_for(s, runs),  # Wave C
                    "orphan_calls": orphan,
                    "has_failures": bool(fails),
                    "capture_withheld": bool(getattr(a, "capture_withheld", False)),
                }
                if has_failures and not view["has_failures"]:
                    continue
                out.append(view)
            return out
    except Exception:
        log.warning("tap_view.list_actions failed (returning empty)", exc_info=True)
        return []


def action_detail(action_id):
    """One action's full tree (envelope + runs + calls), or None. For `show-action`
    (CLI) and a future per-action export/API. Best-effort."""
    try:
        from sqlmodel import select

        from app.models import TapAction, TapRun

        with _session() as s:
            a = s.get(TapAction, action_id)
            if a is None:
                return None
            projects, docs = _name_maps(s, [a])
            # action_detail is a single-action deep read (CLI show-action / a per-action
            # export): materialize the FULL call set (no display cap).
            calls_by_run, _counts = _calls_by_run(a.id)
            runs = s.exec(
                select(TapRun)
                .where(TapRun.action_id == a.id)
                .order_by(TapRun.target_locale.asc(), TapRun.started_at.asc())
            ).all()
            run_views = []
            for r in runs:
                rc = calls_by_run.get(r.id, [])
                run_views.append(
                    {
                        "id": r.id,
                        "target_locale": r.target_locale,
                        "status": r.status,
                        "started_at": r.started_at,
                        "finished_at": r.finished_at,
                        "detail": r.detail,
                        "summary": _run_summary(r, rc),
                        "calls": rc,
                        "call_count": len(rc),
                    }
                )
            return {
                "id": a.id,
                "kind": a.kind,
                "status": a.status,
                "batch_id": a.batch_id,
                "project_id": a.project_id,
                "project_name": projects.get(a.project_id),
                "document_id": a.document_id,
                "document_name": docs.get(a.document_id),
                "requested_locales": a.requested_locales or [],
                "source_locale": a.source_locale,
                "workflow_id": a.workflow_id,
                "workflow_name": a.workflow_name,
                "initiator": a.initiator,
                "surface": a.surface,
                "started_at": a.started_at,
                "finished_at": a.finished_at,
                "manifest": a.manifest,
                "totals": a.totals,
                "runs": run_views,
                "vendor_legs": _vendor_legs_for(s, runs),
            }  # Wave C
    except Exception:
        return None


# --- run-190 Item C: BODIES-WITHHELD summary (safe over --remote and /api) ---------
def _summarize_calls(metric_rows):
    """Aggregate a run's per-call METRIC rows (tap_enrich._metric_row — metadata only, NO
    prompt/response text) into the LLM-Ledger SUMMARY: counts, statuses, error classes,
    finish reasons, spend, timings, plus a compact per-call SHAPE list (attempt / outcome /
    finish reason / error class / output chars / elapsed / reasoning tokens). This is the
    Ledger-side twin of the run API's per-step `failure_shape` — no bodies, so it is safe to
    serve over `--remote` and `/api`.

    M6 (gate round 1): the numeric totals (calls/failures/tokens_in/tokens_out/spend) are
    taken from `tap_enrich._rollup_calls` — the SAME function the envelope rollup uses — so
    `calls_summary.tokens_in` can never disagree with the envelope's `summary.tokens_in`
    (both fold input + cache_read + cache_write; the earlier hand-rolled sum here dropped
    cache_write and reported a smaller number side by side with the envelope's). §4: an
    envelope cannot lie about its children.

    M4 (gate round 1): the per-call `shape` list is CAPPED at CALLS_PER_RUN_CAP so a run
    with tens of thousands of calls cannot amplify one summary read into an unbounded row
    list; `shape_total` reports the TRUE call count and `shape_truncated` says the sample is
    partial (the aggregate totals above stay honest — they sum every row)."""
    from app.services import tap_enrich

    roll = tap_enrich._rollup_calls(metric_rows)  # shared totals (incl. cache_write) — M6
    finish, errcls, outcomes = {}, {}, {}
    shape = []
    for m in metric_rows:
        oc = m.get("outcome") or ""
        outcomes[oc] = outcomes.get(oc, 0) + 1
        fr = m.get("finish_reason")
        if fr:
            finish[fr] = finish.get(fr, 0) + 1
        ec = m.get("error_class")
        if ec:
            errcls[ec] = errcls.get(ec, 0) + 1
        if len(shape) < CALLS_PER_RUN_CAP:  # M4: bound the per-run shape emission
            shape.append(
                {
                    "seq": m.get("seq"),
                    "attempt": m.get("attempt"),
                    "provider": m.get("provider_kind") or m.get("provider"),
                    "model": m.get("model"),
                    "outcome": oc,
                    "finish_reason": fr,
                    "error_class": ec,
                    "output_chars": m.get("output_chars"),
                    "elapsed_ms": m.get("elapsed_ms"),
                    "reasoning_tokens": m.get("reasoning_tokens"),
                    "temperature": m.get("temperature"),
                    "seed": m.get("seed"),
                }
            )
    return {
        "calls": roll["calls"],
        "failures": roll["failures"],
        "outcomes": outcomes,
        "finish_reasons": finish,
        "error_classes": errcls,
        "tokens_in": roll["tokens_in"],
        "tokens_out": roll["tokens_out"],
        "spend_usd": roll["spend_usd"],
        "shape": shape,
        "shape_total": len(metric_rows),
        "shape_truncated": len(metric_rows) > CALLS_PER_RUN_CAP,
    }


def _summary_envelope(a, projects, docs, run_views):
    """The shared metadata envelope for an action summary (no bodies). `bodies_withheld` is
    ALWAYS True on this shape (D-marker distinct from `capture_withheld`, which means the
    disk-budget backstop dropped raw calls): it says the SUMMARY view deliberately omits the
    prompt/response bodies, which stay HOST-ONLY."""
    return {
        "id": a.id,
        "kind": a.kind,
        "status": a.status,
        "batch_id": a.batch_id,
        "project_id": a.project_id,
        "project_name": projects.get(a.project_id),
        "document_id": a.document_id,
        "document_name": docs.get(a.document_id),
        "requested_locales": a.requested_locales or [],
        "source_locale": a.source_locale,
        "workflow_id": a.workflow_id,
        "workflow_name": a.workflow_name,
        "initiator": a.initiator,
        "surface": a.surface,
        "started_at": _iso(a.started_at),
        "finished_at": _iso(a.finished_at),
        "totals": a.totals or None,
        "capture_withheld": bool(getattr(a, "capture_withheld", False)),
        "bodies_withheld": True,
        "runs": run_views,
    }


def _run_summary_views(s, action_id, runs):
    """Per-run summary views (no bodies) for an action's runs, from the per-action call file
    read ONCE (tap_enrich._call_rows_by_run) and aggregated by _summarize_calls."""
    from app.services import tap_enrich

    rows_by_run = tap_enrich._call_rows_by_run(action_id)
    out = []
    for r in runs:
        mrows = rows_by_run.get(r.id, [])
        out.append(
            {
                "id": r.id,
                "target_locale": r.target_locale,
                "status": r.status,
                "started_at": _iso(r.started_at),
                "finished_at": _iso(r.finished_at),
                # M6 (gate round 1): pass the run's OWN metric rows (not an empty list) so an
                # un-enriched run's `summary.failures` counts the real failures instead of a
                # hard 0 that would contradict `calls_summary.failures` beside it. An enriched
                # run still prefers its stored envelope rollup inside _run_summary.
                "summary": _run_summary(r, mrows, total_calls=len(mrows)),
                "calls_summary": _summarize_calls(mrows),
            }
        )
    return out


def _action_project_visible(session, user, action):
    """Resolve Ledger provenance before applying the common project visibility rule."""
    from app.models import Document

    pid = action.project_id
    if pid is None and action.document_id is not None:
        doc = session.get(Document, action.document_id)
        if doc is None:
            return False
        pid = doc.project_id
    # Engine diagnostics can have neither reference. Their org/customer ring owns them.
    return pid is None or access.can_view_project(session, user, pid)


def action_summary(action_id, *, org_id=None, customer_id=None, user=access.UNSCOPED):
    """One action's BODIES-WITHHELD summary (envelope + per-run counts/statuses/error
    classes/finish reasons/spend/timings), or None. Serves the CLI `show-action --summary`
    and GET /api/prompt-tap/actions/{id}/summary — both safe over `--remote` because it
    NEVER returns a prompt or response body (only tap_enrich._metric_row metadata). `org_id`
    (active multi-org) confines it to the caller's org, else None (fail-closed).

    H3 (gate round 1): `customer_id` is the R2-3 within-org customer ring. When a caller is
    bound to a customer (a customer-scoped `lsu_` key), an action attributed to ANOTHER
    customer resolves as None (a non-enumerable 404), exactly like `_project_in_org` — so a
    bound token can never read another customer's Ledger envelope. None (staff / single
    customer) applies no customer predicate. TapAction.customer_id is the recorded
    provenance the run was against."""
    try:
        from sqlmodel import select

        from app.models import TapAction, TapRun
        from app.services import prompt_tap

        # H5 (gate round 1): the action-id FORMAT guard lives HERE (the viewer already
        # imports prompt_tap, an allowlisted telemetry-contract reader) rather than in the
        # /api route, so a web module never imports prompt_tap directly. A malformed id is a
        # clean None (the route 404s), the same as a nonexistent one.
        if not prompt_tap.is_action_id((action_id or "").strip()):
            return None
        action_id = (action_id or "").strip()
        with _session() as s:
            a = s.get(TapAction, action_id)
            if a is None:
                return None
            if user is not access.UNSCOPED and not _action_project_visible(s, user, a):
                return None
            if org_id is not None and getattr(a, "org_id", None) != org_id:
                return None  # fail closed: another org's action is not visible
            if customer_id is not None and getattr(a, "customer_id", None) != customer_id:
                return None  # H3: fail closed on the within-org customer ring
            projects, docs = _name_maps(s, [a], org_id=org_id)
            runs = s.exec(
                select(TapRun)
                .where(TapRun.action_id == a.id)
                .order_by(TapRun.target_locale.asc(), TapRun.started_at.asc())
            ).all()
            run_views = _run_summary_views(s, a.id, runs)
            return _summary_envelope(a, projects, docs, run_views)
    except Exception:
        return None


def list_action_summaries(
    *,
    limit=DEFAULT_ACTION_LIMIT,
    project_id=None,
    document_id=None,
    batch_id=None,
    status=None,
    since=None,
    until=None,
    has_failures=False,
    org_id=None,
    customer_id=None,
    user=access.UNSCOPED,
):
    """Actions newest-first as BODIES-WITHHELD summaries (the same filters as list_actions).
    Serves the CLI `actions --summary` and GET /api/prompt-tap/actions — safe over `--remote`
    because no prompt/response body is ever returned. Best-effort: [] on any failure.

    H3 (gate round 1): `customer_id` is the R2-3 within-org customer ring. A customer-bound
    caller sees ONLY actions attributed to its own customer (TapAction.customer_id); another
    customer's rows are omitted from the list entirely (non-enumerable), the same confinement
    `_scoped_projects` gives the project list. None (staff / single customer) is unfiltered."""
    try:
        from sqlmodel import select

        from app.models import Document, TapAction, TapRun

        since_dt, until_dt = _parse_date(since), _parse_date(until)
        with _session() as s:
            q = select(TapAction)
            if org_id is not None:
                q = q.where(TapAction.org_id == org_id)
            if customer_id is not None:
                q = q.where(TapAction.customer_id == customer_id)  # H3: within-org ring
            if user is not access.UNSCOPED:
                allowed = access.visible_project_ids(s, user)
                if allowed is not None:
                    # Project-less diagnostic actions remain shared within their outer ring.
                    docs = select(Document.id).where(Document.project_id.in_(allowed))
                    q = q.where(
                        TapAction.project_id.in_(allowed)
                        | (
                            TapAction.project_id.is_(None)
                            & (TapAction.document_id.is_(None) | TapAction.document_id.in_(docs))
                        )
                    )
            if project_id is not None:
                q = q.where(TapAction.project_id == project_id)
            if document_id is not None:
                q = q.where(TapAction.document_id == document_id)
            if batch_id is not None:
                q = q.where(TapAction.batch_id == batch_id)
            if status:
                q = q.where(TapAction.status == status)
            if since_dt is not None:
                q = q.where(TapAction.started_at >= since_dt)
            if until_dt is not None:
                q = q.where(TapAction.started_at <= until_dt)
            q = q.order_by(TapAction.started_at.desc(), TapAction.id.desc()).limit(limit)
            actions = s.exec(q).all()
            projects, docs = _name_maps(s, actions, org_id=org_id)
            out = []
            for a in actions:
                runs = s.exec(
                    select(TapRun)
                    .where(TapRun.action_id == a.id)
                    .order_by(TapRun.target_locale.asc(), TapRun.started_at.asc())
                ).all()
                run_views = _run_summary_views(s, a.id, runs)
                view = _summary_envelope(a, projects, docs, run_views)
                total_fail = (a.totals or {}).get("failures")
                view["has_failures"] = bool(
                    total_fail
                    if total_fail is not None
                    else sum((rv["calls_summary"]["failures"] or 0) for rv in run_views)
                )
                if has_failures and not view["has_failures"]:
                    continue
                out.append(view)
            return out
    except Exception:
        log.warning("tap_view.list_action_summaries failed (returning empty)", exc_info=True)
        return []


# --- flat "All calls" section + record-download org scoping (N3) ------------------
def _action_orgs(action_ids):
    """{action_id: org_id} for the given action ids, one batched read. Best-effort: {}."""
    ids = {a for a in action_ids if a}
    if not ids:
        return {}
    try:
        from sqlmodel import select

        from app.models import TapAction

        with _session() as s:
            return {
                a.id: a.org_id for a in s.exec(select(TapAction).where(TapAction.id.in_(ids))).all()
            }
    except Exception:
        return {}


def filter_records_for_org(records, org_id):
    """Confine the FLAT 'All calls' records to the caller's org, FAIL-CLOSED (N3).

    The flat section (prompt_tap.page_snapshot -> _iter_records) walks the legacy file AND
    EVERY per-action file, so pre-fix it rendered every org's raw source/target/prompts to
    any org-admin (the admin gate is not a tenant boundary). Under ACTIVE multi-org
    (org_id not None) this keeps only records whose ACTION belongs to the caller's org
    (via TapAction.org_id) and HIDES unattributable legacy records (no action_id) -- the
    same fail-closed posture the sibling /prompt-tap/download batch route already takes.
    org_id None (single-tenant / gate off) returns the records unchanged (byte-identical)."""
    if org_id is None:
        return list(records)
    orgs = _action_orgs((r.get("meta") or {}).get("action_id") for r in records)
    out = []
    for r in records:
        aid = (r.get("meta") or {}).get("action_id")
        if aid and orgs.get(aid) == org_id:
            out.append(r)
        # legacy (no action_id) or other-org -> hidden (fail closed)
    return out


def record_visible_to_org(rec, org_id):
    """True iff the flat-section record `rec` may be served to a caller scoped to `org_id`
    (N3, the per-record download route). org_id None -> always True (single-tenant). Under
    active multi-org, only a record whose ACTION belongs to the caller's org is visible;
    a legacy/unattributable record fails closed (False)."""
    if org_id is None:
        return True
    if rec is None:
        return False
    aid = (rec.get("meta") or {}).get("action_id")
    if not aid:
        return False
    return _action_orgs([aid]).get(aid) == org_id
