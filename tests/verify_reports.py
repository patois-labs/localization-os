"""Reports v1 (R2-1) verification - hermetic, the 57th suite.

Offline + hermetic (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider;
no provider/connector network is ever touched - Jobs are inserted directly with known
result shapes, so cost/token math is exact and deterministic).

Covers the spec's test section:
  1. COST MATH == pricing.cost_usd (a token job on a priced model; a mock $0 job; an
     NMT chars+nmt_kind job char-bills); TOTALS row sums the detail rows.
  2. GROUPING by project / customer / provider / date buckets correctly.
  3. DATE-RANGE: a job outside [from,to] excluded; boundary inclusive.
  4. SCOPING (the isolation cases - most important):
       (a) a restricted user (limited visible_project_ids) sees only their projects;
       (b) a customer-bound user sees ONLY their customer across EVERY group-by AND a
           foreign --customer filter yields their-own/empty, never the foreign data;
       (c) org isolation under the gate - an org-A caller's report excludes org-B jobs.
  5. CSV: to_csv round-trips (parse back), deterministic columns, TOTALS present,
     filename sanitized.
  6. WEB + CLI + /api parity: the same query yields the same numbers on all three.
  7. DERIVED WORDS: labeled/counted from translated segments; documented as derived.

Final line: "REPORTS OK".
"""

import csv as _csv
import io
import json as _json
import os
import sys
from contextlib import redirect_stdout
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_reports_")

FIXTURE_PW = "fixture-pass-1234!!"  # pregate: allow-secret test fixture

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Connection,
    Customer,
    Document,
    Job,
    JobStatus,
    LQAResult,
    Project,
    Role,
    Segment,
    SegmentPostEdit,
    User,
    Vendor,
)
from app.services import (
    access,
    apikeys,
    content,
    pricing,
    reports,
    security,
    settings,
)


# --- fixtures / helpers ------------------------------------------------------
def _mk_customer(name, org_id=None):
    with Session(engine) as s:
        c = Customer(name=name, org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_project(name, customer_id=None, org_id=None, created_by="admin@x"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            name,
            customer_id=str(customer_id) if customer_id else None,
            target_locales="fr-FR",
            created_by=created_by,
        )
        if org_id is not None:
            row = s.get(Project, p.id)
            row.org_id = org_id
            s.add(row)
            s.commit()
        return p.id


def _add_segments(pid, pairs):
    """pairs: list of (source_text, target_text). A blank target = untranslated."""
    with Session(engine) as s:
        d = Document(project_id=pid, filename="d.txt", fmt="txt", mode="segmented", status="parsed")
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, (src, tgt) in enumerate(pairs):
            s.add(
                Segment(
                    document_id=d.id,
                    source_text=src,
                    target_text=tgt,
                    target_locale="fr-FR",
                    order_index=i,
                )
            )
        s.commit()
        return d.id


def _mk_job(
    pid,
    *,
    provider="claude",
    ti=0,
    to=0,
    cr=0,
    cw=0,
    chars=0,
    nmt_kind=None,
    when=None,
    org_id=None,
):
    """Insert a finished translate job with a known result. `chars`+`nmt_kind` make
    it an NMT (char-billed) job; otherwise it's a token job on `provider`."""
    result = {
        "translated": 1,
        "from_segment_tm": 0,
        "from_full_text_tm": 0,
        "provider_calls": 1,
        "failovers": 0,
        "errors": 0,
        "provider_used": {provider: 1} if provider else {},
        "tokens_in": ti,
        "tokens_out": to,
        "tokens_cache_read": cr,
        "tokens_cache_write": cw,
    }
    if chars:
        result["chars"] = chars
        result["nmt_kind"] = nmt_kind
    payload = {"project_id": pid}
    if org_id is not None:
        payload["org_id"] = org_id
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            status=JobStatus.COMPLETED,
            payload=payload,
            result=result,
            finished_at=when or datetime(2026, 7, 4, 12, 0, 0),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _mk_doc_job(
    doc_id, *, provider="claude", ti=0, to=0, cr=0, cw=0, chars=0, nmt_kind=None, when=None
):
    """Insert a finished translate_document job with the REAL production payload
    shape - {document_id, locale} - and NO project_id, exactly what
    app/web/documents.py:182 and app/web/api.py:706 actually submit. This is the
    shape verify_reports' own _mk_job did NOT cover (it hard-coded
    payload={"project_id": pid}), which is why the CRITICAL project-attribution
    bug (every real job silently dropped) slipped past the original suite."""
    result = {
        "translated": 1,
        "from_segment_tm": 0,
        "from_full_text_tm": 0,
        "provider_calls": 1,
        "failovers": 0,
        "errors": 0,
        "provider_used": {provider: 1} if provider else {},
        "tokens_in": ti,
        "tokens_out": to,
        "tokens_cache_read": cr,
        "tokens_cache_write": cw,
    }
    if chars:
        result["chars"] = chars
        result["nmt_kind"] = nmt_kind
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            status=JobStatus.COMPLETED,
            payload={"document_id": doc_id, "locale": "fr-FR"},
            result=result,
            finished_at=when or datetime(2026, 7, 4, 12, 0, 0),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _mk_connector_job(
    connection_id, *, provider="claude", ti=0, to=0, when=None, result_document_id=None
):
    """Insert a finished connector_translate job with the REAL payload shape
    ({connection_id, actor} - app/services/jobs.py:116 _run_connector_translate;
    submit sites app/web/api.py:170 and app/web/connectors.py:411), never
    project_id or (necessarily) document_id in the payload."""
    result = {
        "message": "ok",
        "ok": True,
        "translated": 1,
        "provider_used": {provider: 1} if provider else {},
        "tokens_in": ti,
        "tokens_out": to,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
    }
    if result_document_id is not None:
        result["document_id"] = result_document_id
    with Session(engine) as s:
        j = Job(
            kind="connector_translate",
            status=JobStatus.COMPLETED,
            payload={"connection_id": connection_id, "actor": "conn@x"},
            result=result,
            finished_at=when or datetime(2026, 7, 4, 12, 0, 0),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _mk_connection(project_id, kind="airtable", name="RepConn"):
    with Session(engine) as s:
        c = Connection(kind=kind, name=name, config={}, secret_enc="", project_id=project_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_workflow_run(pid, doc_id, stepruns, *, when=None, wf_name="TestWF", locale="fr-FR"):
    """Create a Workflow + a completed WorkflowRun on doc_id with the given StepRun
    specs, mirroring what `orchestrator.run_workflow` persists (WorkflowRun + StepRun
    rows, NEVER a Job). stepruns: list of dicts with optional keys provider / cost_usd /
    tokens_in / tokens_out (a NULL cost_usd/tokens = a legacy/untracked step). Returns
    (wf_id, run_id)."""
    from app.models import StepRun, StepType, Workflow, WorkflowRun, WorkflowStep

    when = when or datetime(2026, 7, 4, 12, 0, 0)
    with Session(engine) as s:
        wf = Workflow(name=wf_name)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        step_ids = []
        for i, spec in enumerate(stepruns):
            st = WorkflowStep(
                workflow_id=wf_id,
                order_index=i,
                name=f"S{i}",
                step_type=StepType.LLM_AGENT,
                provider=spec.get("provider", "claude"),
            )
            s.add(st)
            s.commit()
            s.refresh(st)
            step_ids.append(st.id)
        run = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale=locale,
            status="completed",
            started_at=when,
            finished_at=when,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
        for sid, spec in zip(step_ids, stepruns, strict=False):
            s.add(
                StepRun(
                    workflow_run_id=run_id,
                    step_id=sid,
                    status="completed",
                    provider_used=spec.get("provider", "claude"),
                    cost_usd=spec.get("cost_usd"),
                    tokens_in=spec.get("tokens_in"),
                    tokens_out=spec.get("tokens_out"),
                )
            )
        s.commit()
    return wf_id, run_id


def _row_by_key(report, key):
    kc = report["key_column"]
    for r in report["rows"]:
        if r[kc] == key:
            return r
    return None


# --- Wave 2 fixtures (qa / postedit) ----------------------------------------
def _mk_doc_segs(pid, n=1, *, tte=0, locale="fr-FR", src="alpha beta gamma"):
    """A parsed document with n translated segments; returns (doc_id, [seg_ids])."""
    with Session(engine) as s:
        d = Document(project_id=pid, filename="q.txt", fmt="txt", mode="segmented", status="parsed")
        s.add(d)
        s.commit()
        s.refresh(d)
        seg_ids = []
        for i in range(n):
            seg = Segment(
                document_id=d.id,
                source_text=src,
                target_text="t",
                target_locale=locale,
                order_index=i,
                tte_ms=tte,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            seg_ids.append(seg.id)
        return d.id, seg_ids


def _mk_lqa(
    doc_id,
    *,
    seg_id=None,
    dimension="Accuracy/Omission",
    severity="major",
    score=90.0,
    locale="fr-FR",
    source="llm",
    judge="claude/sonnet",
    scoring_id="card-1",
    vendor_id=None,
    when=None,
    actor=None,
    withdrawn_at=None,
):
    with Session(engine) as s:
        s.add(
            LQAResult(
                document_id=doc_id,
                segment_id=seg_id,
                dimension=dimension,
                severity=severity,
                note="n",
                score=score,
                target_locale=locale,
                finding_source=source,
                judge=judge,
                scoring_id=scoring_id,
                vendor_id=vendor_id,
                actor=actor,
                withdrawn_at=withdrawn_at,
                created_at=when or datetime(2026, 7, 4, 12, 0, 0),
            )
        )
        s.commit()


def _mk_pe(
    doc_id,
    seg_id,
    *,
    episode_seq=1,
    actor="",
    actor_kind="human",
    vendor_id=None,
    baseline_kind="mt",
    baseline_chars=10,
    final_chars=12,
    source_words=3,
    dist=(3, 1, 25.0),
    cum=(3, 1, 25.0),
    unit_kind="segment",
    metric_version="pe/1",
    edit_count=1,
    locale="fr-FR",
    when=None,
):
    when = when or datetime(2026, 7, 4, 12, 0, 0)
    with Session(engine) as s:
        s.add(
            SegmentPostEdit(
                segment_id=seg_id,
                document_id=doc_id,
                target_locale=locale,
                episode_seq=episode_seq,
                unit_kind=unit_kind,
                actor=actor,
                actor_kind=actor_kind,
                vendor_id=vendor_id,
                baseline_kind=baseline_kind,
                baseline_chars=baseline_chars,
                final_chars=final_chars,
                source_words=source_words,
                distance_char=(dist[0] if dist else None),
                distance_word=(dist[1] if dist else None),
                distance_pct=(dist[2] if dist else None),
                cum_distance_char=(cum[0] if cum else None),
                cum_distance_word=(cum[1] if cum else None),
                cum_distance_pct=(cum[2] if cum else None),
                metric_version=metric_version,
                edit_count=edit_count,
                first_edited_at=when,
                last_edited_at=when,
            )
        )
        s.commit()


def _mk_vendor(name):
    with Session(engine) as s:
        v = Vendor(name=name)
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


# ============================================================================
# 1. COST MATH
# ============================================================================
def test_cost_math():
    cid = _mk_customer("CostCust")
    pid = _mk_project("CostProj", customer_id=cid)
    # a priced token job (claude -> haiku default), a mock $0 job, an NMT char job.
    _mk_job(pid, provider="claude", ti=1000, to=500)
    _mk_job(pid, provider="mock", ti=9999, to=9999)  # mock -> $0
    _mk_job(pid, provider="deepl", chars=1_000_000, nmt_kind="deepl")

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", customer_id=cid), user=None
        )
    row = rep["rows"][0]
    # Independently compute the expected cost via pricing.cost_usd VERBATIM, using the
    # SAME provider->model resolution the report uses (reports._model_for_provider).
    c_token = pricing.cost_usd(
        "claude",
        reports._model_for_provider("claude"),
        {"input_tokens": 1000, "output_tokens": 500},
    )
    c_mock = pricing.cost_usd(
        "mock", reports._model_for_provider("mock"), {"input_tokens": 9999, "output_tokens": 9999}
    )
    c_nmt = pricing.cost_usd(
        "deepl", reports._model_for_provider("deepl"), {"chars": 1_000_000, "nmt_kind": "deepl"}
    )
    assert c_mock == 0.0, "mock must price to $0"
    assert c_token > 0, "a real-provider token job must have a positive list cost"
    expected = round(c_token + c_mock + c_nmt, 6)
    assert row["cost_usd"] == expected, f"cost {row['cost_usd']} != pricing {expected}"
    assert row["jobs"] == 3
    assert row["tokens_in"] == 1000 + 9999, "token totals sum across jobs"
    # TOTALS row (single bucket) equals the row.
    totals = reports.totals_row(rep)
    assert totals["cost_usd"] == expected and totals["jobs"] == 3
    # And the CSV footer matches.
    text = reports.to_csv(rep)
    parsed = list(_csv.reader(io.StringIO(text)))
    assert parsed[-1][0] == "TOTALS"
    ci = rep["columns"].index("cost_usd")
    assert abs(float(parsed[-1][ci]) - expected) < 1e-9
    print(
        f"  cost math: token={c_token} + mock=0 + nmt={c_nmt} == report {row['cost_usd']}; "
        "TOTALS sums; CSV footer matches OK"
    )


# ============================================================================
# 1b. PROJECT ATTRIBUTION VIA document_id (the CRITICAL fix - real payload shape)
# ============================================================================
def test_attribution_via_document_id():
    """Production translate_document jobs carry {document_id, locale}, NEVER
    project_id (web app/web/documents.py:182, API app/web/api.py:706). Before
    the fix, _job_project_id ONLY read payload.project_id, so every real job
    resolved to project=None and was silently skipped -> cost/token reports
    were EMPTY on a live install. This drives a job through the REAL shape (via
    _mk_doc_job, using an actual Document row - the same ingest seam other
    suites use) and asserts it is attributed and its tokens/cost are counted."""
    cid = _mk_customer("AttrDocCust")
    pid = _mk_project("AttrDocProj", customer_id=cid)
    doc_id = _add_segments(pid, [("hello world", "bonjour monde")])
    _mk_doc_job(doc_id, provider="claude", ti=777, to=333)

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", customer_id=cid), user=None
        )
    assert rep["rows"], "a document_id-shaped job must attribute to its project (FIX 1)"
    row = rep["rows"][0]
    assert row["jobs"] == 1
    assert row["tokens_in"] == 777 and row["tokens_out"] == 333
    expected = pricing.cost_usd(
        "claude", reports._model_for_provider("claude"), {"input_tokens": 777, "output_tokens": 333}
    )
    assert row["cost_usd"] == round(expected, 6)
    # Grouped by project too - the project-level report must show it (not empty).
    with Session(engine) as s:
        repp = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="project", customer_id=cid), user=None
        )
    assert _row_by_key(repp, f"{pid}: AttrDocProj")["tokens_in"] == 777
    print(
        "  attribution(document_id): a real-shaped {document_id,locale} job "
        "(no project_id) attributes correctly - tokens+cost non-empty OK"
    )


# ============================================================================
# 1c. CONNECTOR SPEND ATTRIBUTION (a whole paid channel must not read $0)
# ============================================================================
def test_attribution_connector_job():
    """connector_translate jobs carry {connection_id, actor}, never project_id
    or (reliably) document_id (confluence in particular fans out to MANY
    documents per run, so there is no single result.document_id to key off).
    FIX 2 resolves via Connection.project_id (stable across every connector
    kind, set once at first pull/translate via _ensure_project). This proves a
    connector job's spend is counted, not silently zero."""
    cid = _mk_customer("AttrConnCust")
    pid = _mk_project("AttrConnProj", customer_id=cid)
    conn_id = _mk_connection(pid, kind="airtable", name="ConnAttr")
    _mk_connector_job(conn_id, provider="claude", ti=555, to=222)

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", customer_id=cid), user=None
        )
    assert rep["rows"], "a connector_translate job must attribute via Connection.project_id (FIX 2)"
    row = rep["rows"][0]
    assert row["tokens_in"] == 555 and row["tokens_out"] == 222
    assert row["cost_usd"] > 0, "connector spend must not silently read $0"

    # Also prove the result.document_id fallback path resolves (belt-and-suspenders):
    # a SECOND connection/job pair whose payload has no document_id but whose
    # connection is gone, forcing the result.document_id path to be the one that
    # actually resolves it.
    cid2 = _mk_customer("AttrConnCust2")
    pid2 = _mk_project("AttrConnProj2", customer_id=cid2)
    doc_id2 = _add_segments(pid2, [("x", "y")])
    conn_id2 = _mk_connection(pid2, kind="jira", name="ConnAttr2")
    with Session(engine) as s:
        # Delete the connection row so the connection_id path can't resolve,
        # isolating the result.document_id fallback (step 4 of _job_project_id).
        c = s.get(Connection, conn_id2)
        s.delete(c)
        s.commit()
    _mk_connector_job(conn_id2, provider="claude", ti=111, to=44, result_document_id=doc_id2)
    with Session(engine) as s:
        rep2 = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", customer_id=cid2), user=None
        )
    assert rep2["rows"], "result.document_id fallback must resolve when connection_id can't"
    assert rep2["rows"][0]["tokens_in"] == 111
    print(
        "  attribution(connector): a {connection_id,actor} job attributes via "
        "Connection.project_id, spend > 0 (not silently $0); result.document_id "
        "fallback resolves when the connection is gone OK"
    )


# ============================================================================
# 1d. MALFORMED RESULT ROW - one bad row must not 500 the whole report
# ============================================================================
def test_malformed_result_row_does_not_crash():
    """A legacy/malformed Job.result can carry a non-int token value (e.g. a
    stringly-typed "notanint"). Before the fix, the aggregation loop's bare
    int(...) (and _job_cost's) raised ValueError -> the WHOLE report 500'd for
    every caller, not just the bad row. FIX 3 coerces defensively (-> 0) so the
    malformed row contributes 0 and the report still renders, alongside a good
    row whose totals are unaffected."""
    cid = _mk_customer("MalformedCust")
    pid = _mk_project("MalformedProj", customer_id=cid)
    good_id = _mk_job(pid, provider="claude", ti=100, to=50)
    # A job whose result carries a non-numeric tokens_in (and friends) directly,
    # bypassing _mk_job's clean int fields.
    with Session(engine) as s:
        bad = Job(
            kind="translate_document",
            status=JobStatus.COMPLETED,
            payload={"project_id": pid},
            result={
                "provider_used": {"claude": 1},
                "tokens_in": "notanint",
                "tokens_out": None,
                "tokens_cache_read": "x",
                "tokens_cache_write": [],
                "translated": 1,
            },
            finished_at=datetime(2026, 7, 4, 12, 0, 0),
        )
        s.add(bad)
        s.commit()
        s.refresh(bad)
        bad_id = bad.id

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", customer_id=cid), user=None
        )  # must NOT raise
    row = rep["rows"][0]
    assert row["jobs"] == 2, "both the good and the malformed job are counted"
    assert row["tokens_in"] == 100, "malformed row contributes 0, good row's 100 stands"
    expected = pricing.cost_usd(
        "claude", reports._model_for_provider("claude"), {"input_tokens": 100, "output_tokens": 50}
    )
    assert row["cost_usd"] == round(expected, 6), "malformed row prices as $0, not a crash"
    assert good_id != bad_id
    print(
        "  malformed row: a non-int tokens_in/out/cache field contributes 0 "
        "instead of 500-ing the report OK"
    )


# ============================================================================
# 2. GROUPING
# ============================================================================
def test_grouping():
    # The verify runner drives every test in ONE process and does NOT wipe the DB
    # between tests, so other tests' jobs coexist. We isolate this test's data with a
    # UNIQUE date window (Sept 2027) and range-filter every grouping query to it, so
    # the provider/date/engine buckets count ONLY this test's jobs.
    cA = _mk_customer("GrpA")
    cB = _mk_customer("GrpB")
    pA = _mk_project("GrpProjA", customer_id=cA)
    pB = _mk_project("GrpProjB", customer_id=cB)
    _mk_job(pA, provider="claude", ti=100, to=50, when=datetime(2027, 9, 1))
    _mk_job(pA, provider="gemini", ti=200, to=80, when=datetime(2027, 9, 2))
    _mk_job(pB, provider="claude", ti=400, to=100, when=datetime(2027, 9, 2))
    win = dict(date_from=date(2027, 9, 1), date_to=date(2027, 9, 30))  # noqa: C408

    with Session(engine) as s:
        # by project
        rep = reports.run_report(s, reports.ReportQuery(group_by="project", **win), user=None)
        assert _row_by_key(rep, f"{pA}: GrpProjA")["jobs"] == 2
        assert _row_by_key(rep, f"{pB}: GrpProjB")["jobs"] == 1
        # by customer
        rep = reports.run_report(s, reports.ReportQuery(group_by="customer", **win), user=None)
        assert _row_by_key(rep, f"{cA}: GrpA")["tokens_in"] == 300
        assert _row_by_key(rep, f"{cB}: GrpB")["tokens_in"] == 400
        # by provider (window-isolated -> exactly this test's jobs)
        rep = reports.run_report(s, reports.ReportQuery(group_by="provider", **win), user=None)
        assert _row_by_key(rep, "claude")["jobs"] == 2  # pA-claude + pB-claude
        assert _row_by_key(rep, "gemini")["jobs"] == 1
        # by engine (synonym of provider in v1)
        rep = reports.run_report(s, reports.ReportQuery(group_by="engine", **win), user=None)
        assert _row_by_key(rep, "claude")["jobs"] == 2
        # by date
        rep = reports.run_report(s, reports.ReportQuery(group_by="date", **win), user=None)
        assert _row_by_key(rep, "2027-09-01")["jobs"] == 1
        assert _row_by_key(rep, "2027-09-02")["jobs"] == 2
    print("  grouping: project/customer/provider/engine/date each bucket correctly OK")


# ============================================================================
# 2b. GROUPING by workflow (CLI-parity backlog item 3)
# ============================================================================
def test_grouping_by_workflow():
    """group_by="workflow" is EXACT (feat/workflow-spend): a workflow bucket's
    cost/tokens is the SUM of its WorkflowRuns' stamped StepRun spend (NOT the old
    most-recent-run job-cost approximation). Plain-MT/connector JOBS carry no workflow
    and roll up as "(no workflow)"; runs+jobs never double-count (a `workflow run`
    writes StepRuns, never a Job). A run whose StepRuns are all cost-NULL (legacy,
    pre-tracking) surfaces honestly as "(untracked)", never silently zeroed."""
    from app.models import StepRun, StepType, Workflow, WorkflowRun, WorkflowStep

    cid = _mk_customer("WfGrpCust")
    pid = _mk_project("WfGrpProj", customer_id=cid)

    # doc A: a TRACKED workflow run (2 stamped StepRuns) + ALSO a plain-MT job on the
    # same doc. The run's spend must attribute to the workflow bucket from its
    # StepRuns; the job's spend must land in "(no workflow)" (NOT double-counted onto
    # the workflow).
    doc_a = _add_segments(pid, [("hello", "bonjour")])
    with Session(engine) as s:
        wf = Workflow(name="WfGrp Workflow")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        st1 = WorkflowStep(
            workflow_id=wf_id,
            order_index=0,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="claude",
        )
        st2 = WorkflowStep(
            workflow_id=wf_id,
            order_index=1,
            name="Review",
            step_type=StepType.LLM_AGENT,
            provider="claude",
        )
        s.add(st1)
        s.add(st2)
        s.commit()
        s.refresh(st1)
        s.refresh(st2)
        st1_id, st2_id = st1.id, st2.id
        run = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_a,
            target_locale="fr-FR",
            status="completed",
            started_at=datetime(2027, 9, 1, 10, 0, 0),
            finished_at=datetime(2027, 9, 1, 10, 5, 0),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        # two stamped StepRuns: total cost 0.0100, tokens 1000 in / 200 out.
        s.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=st1_id,
                status="completed",
                provider_used="claude",
                cost_usd=0.006,
                tokens_in=600,
                tokens_out=120,
            )
        )
        s.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=st2_id,
                status="completed",
                provider_used="claude",
                cost_usd=0.004,
                tokens_in=400,
                tokens_out=80,
            )
        )
        s.commit()
    _mk_doc_job(doc_a, provider="claude", ti=100, to=10, when=datetime(2027, 9, 1, 12))

    # doc B: never run through `workflow run` -> its job buckets "(no workflow)".
    doc_b = _add_segments(pid, [("world", "monde")])
    _mk_doc_job(doc_b, provider="claude", ti=200, to=20, when=datetime(2027, 9, 1, 12))

    # doc C: a LEGACY workflow run whose StepRun predates spend tracking (cost NULL) ->
    # "(untracked)", never zeroed into a workflow's real total.
    doc_c = _add_segments(pid, [("bye", "adieu")])
    with Session(engine) as s:
        run_c = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_c,
            target_locale="fr-FR",
            status="completed",
            started_at=datetime(2027, 9, 1, 9, 0, 0),
            finished_at=datetime(2027, 9, 1, 9, 2, 0),
        )
        s.add(run_c)
        s.commit()
        s.refresh(run_c)
        s.add(
            StepRun(
                workflow_run_id=run_c.id, step_id=st1_id, status="completed", provider_used="claude"
            )
        )  # cost_usd/tokens NULL = legacy
        s.commit()

    assert "workflow" in reports.GROUP_BYS
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="workflow", customer_id=cid), user=None
        )
    # workflow bucket = EXACT StepRun sums (from the run, NOT the job).
    row_wf = _row_by_key(rep, f"{wf_id}: WfGrp Workflow")
    assert row_wf is not None, rep["rows"]
    assert row_wf["tokens_in"] == 1000 and row_wf["tokens_out"] == 200, row_wf
    assert abs(row_wf["cost_usd"] - 0.01) < 1e-9, row_wf
    assert row_wf["jobs"] == 1, ("the count column counts RUNS on the workflow axis", row_wf)
    # NO double-count: the doc_a job's 100 tokens are in "(no workflow)" with doc_b's
    # 200 (=300 total), never added onto the workflow bucket's 1000.
    row_none = _row_by_key(rep, "(no workflow)")
    assert row_none is not None and row_none["tokens_in"] == 300, row_none
    # legacy all-NULL run surfaces honestly, counted but zero-cost (not silently zeroed
    # into the workflow's real 0.01).
    row_unt = _row_by_key(rep, "(untracked)")
    assert row_unt is not None, rep["rows"]
    assert row_unt["cost_usd"] == 0.0 and row_unt["jobs"] == 1, row_unt

    # date filter is on the RUN's finished date: a window excluding the runs drops the
    # workflow + untracked rows (jobs are date-filtered separately, unchanged).
    with Session(engine) as s:
        rep_dr = reports.run_report(
            s,
            reports.ReportQuery(
                kind="cost", group_by="workflow", customer_id=cid, date_from=date(2028, 1, 1)
            ),
            user=None,
        )
    assert _row_by_key(rep_dr, f"{wf_id}: WfGrp Workflow") is None, (
        "a run outside the date window must not appear on the workflow axis"
    )

    # every report kind accepts the axis without error.
    for kind in ("tokens", "volume"):
        with Session(engine) as s:
            r = reports.run_report(
                s, reports.ReportQuery(kind=kind, group_by="workflow", customer_id=cid), user=None
            )
        assert r["group_by"] == "workflow" and r["key_column"] == "workflow", r

    # --- CLI parity: `report run cost --group-by workflow` ---
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["--json", "report", "run", "cost", "--group-by", "workflow", "--customer", str(cid)]
        )
    assert rc == 0, buf.getvalue()
    cli_out = _json.loads(buf.getvalue().strip().splitlines()[-1])
    assert cli_out["group_by"] == "workflow"
    assert any(r["workflow"] == f"{wf_id}: WfGrp Workflow" for r in cli_out["rows"]), cli_out

    # Regression: on Python 3.10/3.11 a subcommand's `--to` matched the top-level
    # `--token` / `--token-file` ("ambiguous option") until the top parser set
    # allow_abbrev=False.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "report", "run", "cost", "--to", "2099-01-01"])
    assert rc == 0, buf.getvalue()

    # --- web + /api parity: the group_by control accepts "workflow" end-to-end ---
    client = TestClient(app)
    r = client.get(f"/reports/run?kind=cost&group_by=workflow&customer_id={cid}&format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("workflow,"), r.text.splitlines()[0]
    au = security.create_user("wfgrpapiuser@x", "WfGrpAU", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "wfgrp", scopes="api docs")
    r = client.get(
        f"/api/reports/run?kind=cost&group_by=workflow&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200, r.text
    api_out = r.json()
    assert api_out["group_by"] == "workflow"
    assert any(row["workflow"] == f"{wf_id}: WfGrp Workflow" for row in api_out["rows"]), api_out
    # /reports page's group-by <select> offers the new axis.
    r = client.get("/reports")
    assert r.status_code == 200 and '<option value="workflow"' in r.text, (
        "the /reports group-by select must offer the workflow axis"
    )

    # The results page renders the workflow-axis caveat via the SINGLE _caveats() source
    # (SPEC-wave2 §6: the hand-written prose blocks were removed; the generic caveats list
    # is now the one on-screen source, matching CSV + /api). The caveat appears only when
    # group_by=workflow is the ACTIVE query, and the '(untracked)' data row still renders.
    r = client.get(f"/reports/run?kind=cost&group_by=workflow&customer_id={cid}")
    assert r.status_code == 200
    assert "per-run volume is not attributed on this axis" in r.text and "(untracked)" in r.text, (
        "the workflow-axis results page must render the workflow caveat + untracked row"
    )
    assert "approximated" not in r.text, "the stale approximation wording must be gone"
    r_other = client.get(f"/reports/run?kind=cost&group_by=customer&customer_id={cid}")
    assert r_other.status_code == 200
    assert "per-run volume is not attributed on this axis" not in r_other.text, (
        "the workflow caveat must NOT appear for a different group_by"
    )
    print(
        "  grouping(workflow): EXACT per-workflow spend from StepRun sums; plain-MT "
        "jobs roll up '(no workflow)' with NO double-count; a legacy all-NULL run "
        "surfaces '(untracked)'; run-finished date filter; every kind accepts the "
        "axis; CLI/web/api parity; /reports select + exact-semantics caveat OK"
    )


# ============================================================================
# 3. DATE RANGE
# ============================================================================
def test_date_range():
    cid = _mk_customer("DateCust")
    pid = _mk_project("DateProj", customer_id=cid)
    _mk_job(pid, provider="claude", ti=10, to=5, when=datetime(2026, 6, 30))  # before
    _mk_job(pid, provider="claude", ti=20, to=5, when=datetime(2026, 7, 1))  # lower bnd
    _mk_job(pid, provider="claude", ti=40, to=5, when=datetime(2026, 7, 15))  # inside
    _mk_job(pid, provider="claude", ti=80, to=5, when=datetime(2026, 7, 31))  # upper bnd
    _mk_job(pid, provider="claude", ti=99, to=5, when=datetime(2026, 8, 1))  # after

    with Session(engine) as s:
        # customer_id filter isolates this test's jobs from other tests' (shared DB).
        q = reports.ReportQuery(
            group_by="none", customer_id=cid, date_from=date(2026, 7, 1), date_to=date(2026, 7, 31)
        )
        rep = reports.run_report(s, q, user=None)
        row = rep["rows"][0]
        # Only the 3 in-range jobs (both boundaries inclusive), tokens 20+40+80.
        assert row["jobs"] == 3, f"expected 3 in-range jobs, got {row['jobs']}"
        assert row["tokens_in"] == 140, f"boundary-inclusive token sum wrong: {row['tokens_in']}"
    print("  date-range: outside excluded, both boundaries inclusive OK")


# ============================================================================
# 4a. SCOPING - restricted user (membership visibility)
# ============================================================================
def test_scope_restricted_user():
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    try:
        # A translator who CREATED one project sees only it (project_scoping ON).
        u = security.create_user("restr@x", "R", Role.TRANSLATOR, FIXTURE_PW)
        mine = _mk_project("MineProj", created_by="restr@x")
        theirs = _mk_project("TheirsProj", created_by="someone@x")
        _mk_job(mine, provider="claude", ti=100, to=10)
        _mk_job(theirs, provider="claude", ti=999, to=99)
        with Session(engine) as s:
            ur = s.get(User, u.id)
            vis = access.visible_project_ids(s, ur)
            assert mine in set(vis) and theirs not in set(vis), "membership scope"
            rep = reports.run_report(s, reports.ReportQuery(group_by="project"), user=ur)
            keys = {r["project"] for r in rep["rows"]}
            assert any(str(mine) in k for k in keys), "own project present"
            assert not any(str(theirs) in k for k in keys), "foreign project absent"
            total = reports.totals_row(rep)
            assert total["tokens_in"] == 100, "restricted totals exclude foreign jobs"
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
        settings.invalidate_cache()
    print(
        "  scope(restricted): a membership-scoped user's report shows only their "
        "projects' totals OK"
    )


# ============================================================================
# 4b. SCOPING - customer-bound user (the R2-3 isolation guarantee)
# ============================================================================
def test_scope_customer_bound():
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        cA = _mk_customer("BoundA")
        cB = _mk_customer("BoundB")
        pA = _mk_project("BoundProjA", customer_id=cA)
        pB = _mk_project("BoundProjB", customer_id=cB)
        _mk_job(pA, provider="claude", ti=1000, to=100)
        _mk_job(pB, provider="claude", ti=5000, to=500)  # the FOREIGN data
        ua = security.create_user("bnd@x", "BA", Role.TRANSLATOR, FIXTURE_PW, customer_id=cA)
        with Session(engine) as s:
            ub = s.get(User, ua.id)
            assert access.is_customer_bound(ub)
            # Across EVERY group-by, customer B's data must be absent. The vendor axis
            # (L6) has its own columns (no tokens_in) so its check is blob-only.
            for gb in (
                "none",
                "project",
                "customer",
                "provider",
                "date",
                "workflow",
                "locale",
                "vendor",
            ):
                rep = reports.run_report(s, reports.ReportQuery(group_by=gb), user=ub)
                if gb != "vendor":
                    assert reports.totals_row(rep)["tokens_in"] == 1000, (
                        f"group_by={gb}: bound totals must EXCLUDE customer B (got foreign bleed)"
                    )
                blob = _json.dumps(rep)
                assert "BoundProjB" not in blob and "BoundB" not in blob and "5000" not in blob, (
                    f"group_by={gb}: foreign customer data leaked into the report"
                )
            # A --customer filter naming the FOREIGN customer yields their-own/empty,
            # NEVER the foreign data (the clamp).
            repf = reports.run_report(
                s, reports.ReportQuery(group_by="customer", customer_id=cB), user=ub
            )
            assert reports.totals_row(repf)["tokens_in"] == 1000, (
                "foreign --customer must clamp to own (never the foreign 5000)"
            )
            assert not any("BoundB" in r["customer"] for r in repf["rows"]), (
                "foreign --customer must not surface customer B"
            )
            # own --customer filter is honored (still just A).
            repo = reports.run_report(
                s, reports.ReportQuery(group_by="customer", customer_id=cA), user=ub
            )
            assert reports.totals_row(repo)["tokens_in"] == 1000
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print(
        "  scope(customer-bound): foreign customer absent from EVERY group-by; foreign "
        "--customer clamps to own; own filter honored OK"
    )


# ============================================================================
# 4c. SCOPING - org isolation under the sacred gate
# ============================================================================
def test_scope_org_isolation():
    from app.models import Organization as _Org
    from app.services import access as _access

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        assert _access.multi_org_active()
        # org_id is a real FK (project/job/user.org_id -> organization.id): use the
        # seeded Default org as "org 1" and create a REAL second org rather than a
        # bare org_id=2 sentinel (which never persisted on Postgres).
        with Session(engine) as s:
            org1_id = s.exec(select(_Org.id).order_by(_Org.id).limit(1)).first()
            org2 = _Org(name="Reports Org 2")
            s.add(org2)
            s.commit()
            s.refresh(org2)
            org2_id = org2.id
        # org 1 + org 2 each own a project + a job.
        p1 = _mk_project("Org1Proj", org_id=org1_id)
        p2 = _mk_project("Org2Proj", org_id=org2_id)
        _mk_job(p1, provider="claude", ti=1000, to=100, org_id=org1_id)
        _mk_job(p2, provider="claude", ti=7000, to=700, org_id=org2_id)
        # An admin in org 1 (see-all within org 1).
        u1 = security.create_user(
            "org1admin@x", "O1", Role.ADMIN, FIXTURE_PW, org_id=org1_id
        )  # #181: created IN its org
        with Session(engine) as s:
            row = s.get(User, u1.id)
            row.org_id = org1_id
            s.add(row)
            s.commit()
            u1 = s.get(User, u1.id)
            rep = reports.run_report(s, reports.ReportQuery(group_by="project"), user=u1)
            blob = _json.dumps(rep)
            assert "Org1Proj" in blob and "Org2Proj" not in blob, "org-B project leaked"
            assert reports.totals_row(rep)["tokens_in"] == 1000, "org-B tokens leaked"
            # Even an explicit org_id override for a shared operator confines.
            rep2 = reports.run_report(
                s, reports.ReportQuery(group_by="project"), user=None, org_id=org2_id
            )
            assert reports.totals_row(rep2)["tokens_in"] == 7000, "explicit --org=2 scope"
            assert "Org1Proj" not in _json.dumps(rep2), "explicit --org must exclude org 1"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  scope(org): org-A caller excludes org-B jobs; explicit --org confines a "
        "trusted operator OK"
    )


# ============================================================================
# 5. CSV round-trip + determinism + filename
# ============================================================================
def test_csv_roundtrip_and_filename():
    cid = _mk_customer("CsvCust")
    pid = _mk_project("CsvProj", customer_id=cid)
    _add_segments(pid, [("alpha beta gamma", "x"), ("solo", "y"), ("untranslated", "")])
    _mk_job(pid, provider="claude", ti=300, to=90)

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="customer", customer_id=cid), user=None
        )
    text = reports.to_csv(rep)
    parsed = list(_csv.reader(io.StringIO(text)))
    # deterministic columns == report columns; header row exact.
    assert parsed[0] == rep["columns"], "CSV header must equal deterministic columns"
    # TOTALS present as the last row.
    assert parsed[-1][0] == "TOTALS"
    # round-trip: the single detail row's values parse back to the row dict numbers.
    assert len(parsed) == 3, "one header + one detail (single customer filter) + TOTALS"
    detail = parsed[1]
    seg_i = rep["columns"].index("segments")
    assert detail[seg_i] == "2", "only translated segments count (2 of 3)"
    words_i = rep["columns"].index("words_derived")
    assert detail[words_i] == "4", "derived words = 3 (alpha beta gamma) + 1 (solo)"
    # filename sanitized + stamped, no path separators.
    fn = reports.csv_filename("cost", "customer")
    assert fn.startswith("loc-report-cost-customer-") and fn.endswith(".csv")
    weird = reports.csv_filename("../etc/passwd", "a b/c")
    assert "/" not in weird and "\\" not in weird and ".." not in weird, (
        f"filename not sanitized: {weird}"
    )
    print(
        "  csv: header==columns; TOTALS row; translated-only segments; derived words "
        "4; filename sanitized OK"
    )


# ============================================================================
# 6. WEB + CLI + /api PARITY
# ============================================================================
def test_surface_parity():
    cid = _mk_customer("ParityCust")
    pid = _mk_project("ParityProj", customer_id=cid, created_by="admin")
    _add_segments(pid, [("one two three", "t"), ("four", "t")])
    _mk_job(pid, provider="claude", ti=1234, to=567, cr=0, cw=0)

    # All four surfaces run the SAME query (kind=cost, group_by=customer, filtered to
    # THIS test's customer so cross-test jobs don't perturb the comparison).
    # --- in-process service (the source of truth) ---
    with Session(engine) as s:
        svc = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="customer", customer_id=cid), user=None
        )
    svc_totals = reports.totals_row(svc)
    assert len(svc["rows"]) == 1, "single-customer filter -> one bucket"

    # --- CLI (--json, in-process) ---
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["--json", "report", "run", "cost", "--group-by", "customer", "--customer", str(cid)]
        )
    assert rc == 0
    cli_out = _json.loads(buf.getvalue().strip().splitlines()[-1])
    assert cli_out["rows"] == svc["rows"], "CLI rows must equal the service rows"
    assert cli_out["totals"]["cost_usd"] == svc_totals["cost_usd"], "CLI cost parity"

    # --- web (/reports/run, auth off -> any client) ---
    client = TestClient(app)
    r = client.get(f"/reports/run?kind=cost&group_by=customer&customer_id={cid}&format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    web_rows = list(_csv.reader(io.StringIO(r.text)))
    ci = svc["columns"].index("cost_usd")
    web_total = web_rows[-1][ci]
    assert abs(float(web_total) - svc_totals["cost_usd"]) < 1e-9, "web CSV cost parity"

    # --- /api (docs-scoped lsu_ token) ---
    au = security.create_user("apiuser@x", "AU", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "rep", scopes="api docs")
    r = client.get(
        f"/api/reports/run?kind=cost&group_by=customer&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200, r.text
    api_out = r.json()
    assert api_out["rows"] == svc["rows"], "API rows must equal the service rows"
    assert api_out["totals"]["cost_usd"] == svc_totals["cost_usd"], "API cost parity"
    # /api CSV too.
    r = client.get(
        f"/api/reports/run?kind=cost&group_by=customer&customer_id={cid}&format=csv",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    print("  parity: service == CLI == web CSV == /api (rows + cost) OK")


# ============================================================================
# 7. DERIVED WORDS labeling
# ============================================================================
def test_derived_words_labeled():
    cid = _mk_customer("WordsCust")
    pid = _mk_project("WordsProj", customer_id=cid)
    _add_segments(
        pid,
        [
            ("the quick brown fox", "t"),  # 4 words, translated
            ("lazy dog", "t"),  # 2 words, translated
            ("not counted here at all", ""),
        ],
    )  # untranslated -> excluded
    _mk_job(pid, provider="claude", ti=10, to=5)

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="customer", customer_id=cid), user=None
        )
    row = rep["rows"][0]
    assert "words_derived" in rep["columns"], "the column is explicitly labeled derived"
    assert row["words_derived"] == 6, (
        f"derived words = 4 + 2 (translated only), got {row['words_derived']}"
    )
    assert row["segments"] == 2, "only translated segments counted"
    # The volume kind carries no cost/token cols (kind decides render).
    assert "cost_usd" not in rep["columns"] and "tokens_in" not in rep["columns"]
    print(
        "  derived words: counted from TRANSLATED segment source text (6), labeled "
        "'words_derived'; volume kind omits cost/token cols OK"
    )


# ============================================================================
# 9. TM-LEVERAGE BAR (viz quick-win, VIZ-RESEARCH-2026-07-21)
# ============================================================================
def test_tm_leverage_bands():
    """tm.leverage_bands() buckets segments into exact/fuzzy/new/untranslated
    (the bands the persisted tm_match_pct data actually supports - see the
    function's docstring for why "semantic" isn't a 5th band); every segment
    lands in exactly one band, so counts sum to the total. Both /reports (the
    aggregate) and a project's Overview (project-scoped) render the bar."""
    from app.services import tm

    cid = _mk_customer("LevCust")
    pid = _mk_project("LevProj", customer_id=cid)
    doc_id = _add_segments(
        pid,
        [
            ("exact one", "t1"),  # -> tm_match_pct=100 below
            ("fuzzy one", "t2"),  # -> tm_match_pct=88 below
            ("fresh one", "t3"),  # tm_match_pct stays None, translated -> "new"
            ("untranslated one", ""),  # no target -> "untranslated"
        ],
    )
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id).order_by(Segment.order_index)
        ).all()
        segs[0].tm_match_pct = 100
        segs[1].tm_match_pct = 88
        for row in segs:
            s.add(row)
        s.commit()

    with Session(engine) as s:
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        bands = tm.leverage_bands(segs)
    assert bands == {"exact": 1, "fuzzy": 1, "new": 1, "untranslated": 1}, bands
    assert sum(bands.values()) == len(segs) == 4, "bands must sum to the total"

    client = TestClient(app)
    # /reports: the aggregate bar renders with every band labeled (counts vary -
    # other tests' segments coexist in this shared-DB suite run). The "new" band's
    # label is "No TM match" - see test_tm_leverage_no_tm_match_label_is_honest
    # below for WHY it must never say "New (MT)".
    r = client.get("/reports")
    assert r.status_code == 200
    assert 'class="tmlev-bar"' in r.text and "TM leverage" in r.text
    assert "Exact match" in r.text and "Fuzzy match" in r.text
    assert "No TM match" in r.text and "Untranslated" in r.text
    assert "New (MT)" not in r.text, "the misleading MT-only wording must be gone"

    # project Overview: THIS project is scoped to its own documents, so its bar
    # shows exactly this project's own bands - not the global aggregate.
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert 'class="tmlev-bar"' in r.text
    assert r.text.count('<b class="data-mono">1</b>') >= 4, (
        "each of the 4 bands (count=1 each) must render in the project's legend"
    )
    assert "New (MT)" not in r.text
    print(
        "  tm-leverage: bands partition segments (sum==total, no double-count); "
        "/reports shows the labeled aggregate bar; project Overview shows this "
        "project's own scoped bands; 'New (MT)' wording is gone OK"
    )


def test_tm_leverage_no_tm_match_label_is_honest():
    """REGRESSION GUARD (reviewer finding): tm.leverage_bands() puts ANY translated
    segment with tm_match_pct==None into the same band as net-new MT - but a
    HUMAN-TYPED/edited segment saved through the real CAT editor (POST
    /segments/{id}/save, app/web/documents.py) never gets tm_match_pct written
    either, so the OLD "New (MT)" label announced real translator work as machine
    translation (an accessibility falsehood too, since it was baked into the
    aria-label/sr-only text verbatim). This drives a segment through the ACTUAL
    editor save endpoint (not a direct DB write) and pins that it lands in the
    band labeled "No TM match", never "New (MT)", everywhere the label appears -
    legend, tooltip title, aria-label, and the sr-only summary."""
    from app.services import tm

    cid = _mk_customer("HonestCust")
    pid = _mk_project("HonestProj", customer_id=cid)
    doc_id = _add_segments(pid, [("type this by hand", "")])  # starts untranslated
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).first()
        seg_id = seg.id

    client = TestClient(app)
    r = client.post(
        f"/segments/{seg_id}/save", data={"target_text": "je tape ceci a la main", "tte_ms": "800"}
    )
    assert r.status_code == 200 and r.json()["ok"] is True, r.text

    with Session(engine) as s:
        seg = s.get(Segment, seg_id)
        assert seg.target_text, "the editor save must have written a target"
        assert seg.tm_match_pct is None, (
            "the real editor save path must NOT stamp tm_match_pct (confirms the premise)"
        )
        bands = tm.leverage_bands([seg])
    assert bands == {"exact": 0, "fuzzy": 0, "new": 1, "untranslated": 0}, bands

    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "No TM match" in r.text, "the honest label must render in the legend"
    assert "New (MT)" not in r.text, (
        "a human-typed segment must never be announced as machine-translated"
    )
    # the aria-label + sr-only text (what an accessibility user actually hears)
    # must carry the honest wording too, not just the visible legend chip.
    aria_idx = r.text.index('aria-label="TM leverage:')
    aria_end = r.text.index('"', aria_idx + len('aria-label="'))
    aria_text = r.text[aria_idx:aria_end]
    assert "no TM match" in aria_text, ("aria-label must say 'no TM match'", aria_text)
    assert "machine-translated" not in aria_text.lower(), (
        "aria-label must not flatly assert machine-translated",
        aria_text,
    )
    # the sr-only paragraph MAY name "machine-translated" as one of SEVERAL honest
    # possibilities for the band (alongside human-typed/imported) - that's fine;
    # it must never assert it as the SOLE explanation the way "New (MT)" did.
    sr_idx = r.text.index('<p class="sr-only">')
    sr_text = r.text[sr_idx : r.text.index("</p>", sr_idx)]
    assert "no TM match" in sr_text, ("sr-only summary must say 'no TM match'", sr_text)
    print(
        "  tm-leverage(honest label): a segment saved through the REAL editor "
        "path (no tm_match_pct stamped) lands in the 'No TM match' band, and "
        "the legend/tooltip/aria-label/sr-only text all say so - never "
        "'New (MT)' OK"
    )


# ============================================================================
# 8. REGRESSION - the existing /reports dashboard still renders
# ============================================================================
def test_reports_page_still_renders():
    client = TestClient(app)
    r = client.get("/reports")
    assert r.status_code == 200
    body = r.text
    assert "Pipeline health" in body, "the aggregate dashboard must remain"
    assert "Report builder" in body, "the builder card is added"
    print("  regression: /reports keeps the dashboard AND adds the builder card OK")


def test_human_cost_column_is_admin_only():
    """HIGH-E (final round): human_cost_usd is what YOU pay the vendor -- margin data. The
    cost report must expose the column ONLY to the trusted operator (user=None) or an ADMIN,
    and drop it for every non-admin / customer-bound ident, mirroring run_detail's vendor
    gate. (It already appears only on the workflow axis; this adds the identity gate.)"""
    q_wf = reports.ReportQuery(kind="cost", group_by="workflow")
    admin_u = security.create_user("he-admin@x", "HEAdmin", Role.ADMIN, FIXTURE_PW)
    mgr_u = security.create_user("he-mgr@x", "HEMgr", Role.MANAGER, FIXTURE_PW)
    cust_cid = _mk_customer("HEBound")
    cust_u = security.create_user(
        "he-bound@x", "HEBound", Role.TRANSLATOR, FIXTURE_PW, customer_id=cust_cid
    )
    with Session(engine) as s:
        au = s.get(User, admin_u.id)
        mu = s.get(User, mgr_u.id)
        cu = s.get(User, cust_u.id)
        # Operator (user=None) and ADMIN see the column on the workflow axis.
        assert "human_cost_usd" in reports.run_report(s, q_wf, user=None)["columns"]
        assert "human_cost_usd" in reports.run_report(s, q_wf, user=au)["columns"]
        # A MANAGER (non-admin) and a customer-bound ident do NOT.
        assert "human_cost_usd" not in reports.run_report(s, q_wf, user=mu)["columns"], (
            "a non-admin manager must not read vendor margin"
        )
        assert "human_cost_usd" not in reports.run_report(s, q_wf, user=cu)["columns"], (
            "a customer-bound ident must NEVER read what you pay the vendor"
        )
        # And it is never on a non-workflow axis, even for an admin (M7 still holds).
        assert (
            "human_cost_usd"
            not in reports.run_report(
                s, reports.ReportQuery(kind="cost", group_by="none"), user=au
            )["columns"]
        )
    print(
        "  HIGH-E: human_cost_usd column is operator/admin-only (manager + customer-bound "
        "excluded), workflow axis only OK"
    )


# ============================================================================
# CV-4: the COST report KIND is manager+ (spend data). tokens/volume stay viewer-open.
# ============================================================================
def test_cost_report_kind_is_manager_gated():
    """CV-4: `reports.can_see_cost` is the single gate the web /reports/run and GET
    /api/reports/run both enforce; the CLI local arm is the trusted operator (user=None),
    always allowed. A manager+ may run kind=cost; a viewer/translator/reviewer/linguist is
    refused (403, no audit row). tokens/volume are unaffected. This drives the ACTUAL web
    session + /api key surfaces per role, plus the predicate directly."""
    # --- the predicate (single source of truth) ---
    admin_u = security.create_user("cv4-admin@x", "A", Role.ADMIN, FIXTURE_PW)
    mgr_u = security.create_user("cv4-mgr@x", "M", Role.MANAGER, FIXTURE_PW)
    rev_u = security.create_user("cv4-rev@x", "R", Role.REVIEWER, FIXTURE_PW)
    tr_u = security.create_user("cv4-tr@x", "T", Role.TRANSLATOR, FIXTURE_PW)
    vw_u = security.create_user("cv4-vw@x", "V", Role.VIEWER, FIXTURE_PW)
    with Session(engine) as s:
        assert reports.can_see_cost(None) is True, "trusted operator always sees cost"
        assert reports.can_see_cost(s.get(User, admin_u.id)) is True
        assert reports.can_see_cost(s.get(User, mgr_u.id)) is True
        assert reports.can_see_cost(s.get(User, rev_u.id)) is False, "reviewer < manager"
        assert reports.can_see_cost(s.get(User, tr_u.id)) is False
        assert reports.can_see_cost(s.get(User, vw_u.id)) is False

    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        from tests._csrf import csrf_for, sign_out

        client = TestClient(app)

        def _login(email):
            csrf_for(client)
            r = client.post(
                "/auth/login",
                data={"email": email, "password": FIXTURE_PW, "next": "/"},
                follow_redirects=False,
            )
            assert r.status_code == 303, ("login failed", email, r.status_code)
            csrf_for(client)

        # --- WEB: a manager gets the cost report; a viewer 403s (and the builder hides
        #     the cost option); tokens is open to the viewer. ---
        _login("cv4-mgr@x")
        r = client.get("/reports/run?kind=cost&group_by=none")
        assert r.status_code == 200, ("manager must run the cost report", r.status_code)
        assert 'value="cost"' in r.text  # the option is offered
        sign_out(client)

        _login("cv4-vw@x")
        r = client.get("/reports/run?kind=cost&group_by=none")
        assert r.status_code == 403, ("a viewer must be refused the cost report", r.status_code)
        # the builder page itself hides the cost option for the viewer
        r_page = client.get("/reports")
        assert r_page.status_code == 200
        assert 'value="tokens"' in r_page.text and 'value="cost"' not in r_page.text, (
            "the cost option must be hidden from an under-ranked ident's builder"
        )
        # tokens stays viewer-open
        r = client.get("/reports/run?kind=tokens&group_by=none")
        assert r.status_code == 200, "tokens must stay viewer-readable"
        sign_out(client)

        # --- /api: a viewer key 403s on cost, 200s on tokens; a manager key 200s on cost. ---
        _vrow, vraw = apikeys.create_key(vw_u.id, "cv4v", scopes="api docs")
        _mrow, mraw = apikeys.create_key(mgr_u.id, "cv4m", scopes="api docs")
        r = client.get("/api/reports/run?kind=cost", headers={"X-Loc-Token": vraw})
        assert r.status_code == 403, ("viewer key refused on cost", r.text)
        assert not r.json().get("ok") and "manager" in r.json().get("error", "")
        r = client.get("/api/reports/run?kind=tokens", headers={"X-Loc-Token": vraw})
        assert r.status_code == 200, "viewer key may read tokens"
        r = client.get("/api/reports/run?kind=cost", headers={"X-Loc-Token": mraw})
        assert r.status_code == 200, ("manager key runs cost", r.text)

        # --- refused COST attempts write NO audit row (project convention). The
        #     viewer's ALLOWED tokens run above DOES audit — so scope the check to
        #     cost targets by this viewer. ---
        with Session(engine) as s:
            from app.models import AuditLog

            rows = s.exec(
                select(AuditLog).where(AuditLog.action.in_(["report.run", "report.export"]))
            ).all()
            assert not any(
                "cv4-vw@x" in (a.actor or "") and "kind=cost" in (a.target or "") for a in rows
            ), "a refused cost report must leave NO audit row"
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()

    # --- CLI local arm is the trusted operator: kind=cost runs regardless of role. ---
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "report", "run", "cost", "--group-by", "none"])
    assert rc == 0, "the local CLI operator always runs cost"
    print(
        "  CV-4: cost report is manager+ on web + /api (viewer/translator/reviewer 403, "
        "no audit row); tokens/volume viewer-open; CLI local operator unrestricted OK"
    )


# ============================================================================
# CV-1: 7-band WORD-based project leverage (vendor_cost model, surfaced project-wide)
# ============================================================================
def test_project_leverage_7band():
    """CV-1: vendor_cost.project_leverage classifies a project's segments into the real
    7-band model (repetitions/context/exact/fuzzy hi-mid-lo/new) by their PERSISTED
    tm_match_pct / lock_band, WORD-counted. The project page, GET /api/projects/{id}/
    leverage and `project leverage --json` all read the SAME seam (parity)."""
    from app.services import vendor_cost

    cid = _mk_customer("Lev7Cust")
    pid = _mk_project("Lev7Proj", customer_id=cid)
    # 8 segments, 3 words each, one locale -> one repetition group.
    doc_id = _add_segments(
        pid,
        [
            ("alpha one two", "t"),  # pct=100         -> exact
            ("beta three four", "t"),  # pct=96          -> fuzzy_high
            ("gamma five six", "t"),  # pct=88          -> fuzzy_mid
            ("delta seven eight", "t"),  # pct=80          -> fuzzy_low
            ("epsilon nine ten", "t"),  # pct=None        -> new
            ("epsilon nine ten", "t"),  # duplicate       -> repetitions
            ("zeta eleven twelve", "t"),  # lock_band=context -> context
            ("eta thirteen fourteen", "t"),  # lock_band=100     -> exact
        ],
    )
    pct_by_src = {
        "alpha one two": 100,
        "beta three four": 96,
        "gamma five six": 88,
        "delta seven eight": 80,
    }
    lock_by_src = {"zeta eleven twelve": "context", "eta thirteen fourteen": "100"}
    with Session(engine) as s:
        for seg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if seg.source_text in pct_by_src:
                seg.tm_match_pct = pct_by_src[seg.source_text]
            if seg.source_text in lock_by_src:
                seg.lock_band = lock_by_src[seg.source_text]
            s.add(seg)
        s.commit()

    with Session(engine) as s:
        est = vendor_cost.project_leverage(s, pid)
    words = {b: est["bands"][b]["words"] for b in vendor_cost.BANDS}
    assert words == {
        "repetitions": 3,
        "context": 3,
        "exact": 6,
        "fuzzy_high": 3,
        "fuzzy_mid": 3,
        "fuzzy_low": 3,
        "new": 3,
    }, words
    assert est["total_words"] == 24 and est["segment_count"] == 8, est
    # covered = reps+context+exact = 12; billable (post-edit) = 3 fuzzy + new = 12.
    assert est["covered_words"] == 12 and est["billable_words"] == 12, est

    client = TestClient(app)
    # --- web: the project page renders the accessible 7-band bar ---
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert 'class="lev7-bar"' in r.text and "Word-based leverage (7-band)" in r.text
    assert "Repetitions" in r.text and "Fuzzy 95-99%" in r.text and "New / no match" in r.text

    # --- /api twin: byte-identical band words ---
    au = security.create_user("lev7-api@x", "L7", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "lev7", scopes="api docs")
    r = client.get(f"/api/projects/{pid}/leverage", headers={"X-Loc-Token": raw})
    assert r.status_code == 200, r.text
    api_out = r.json()
    api_words = {b["band"]: b["words"] for b in api_out["bands"]}
    assert api_words == words, (api_words, words)
    assert api_out["total_words"] == 24 and api_out["billable_words"] == 12

    # --- CLI twin (--json, in-process): same bands ---
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "project", "leverage", str(pid)])
    assert rc == 0
    cli_out = _json.loads(buf.getvalue().strip().splitlines()[-1])
    cli_words = {b["band"]: b["words"] for b in cli_out["bands"]}
    assert cli_words == words, (cli_words, words)
    print(
        "  CV-1: project_leverage 7-band WORD model (reps/context/exact/fuzzy/new) "
        "from persisted TM data; web bar + /api + CLI parity OK"
    )


def test_project_leverage_over_150_docs_page_api_identity():
    """N1: past the 150-doc chunk boundary the WEB path (_project_stats, its own doc-id
    order) and the /api+CLI seam (vendor_cost.project_leverage, its own doc-id order) must
    draw BYTE-IDENTICAL bands. A per-chunk ORDER BY diverged (page drew fuzzy_high where
    /api drew new); the fix collects across chunks then sorts ONCE globally. Also proves
    _project_stats chunks its IN() (a >999-doc project would otherwise blow the bind)."""
    from app.services import vendor_cost
    from app.web.queries import _project_stats

    cid = _mk_customer("Big150Cust")
    pid = _mk_project("Big150Proj", customer_id=cid)

    def _rep_doc(sess, order_index, pct):
        d = Document(
            project_id=pid,
            filename=f"rep{order_index}.txt",
            fmt="txt",
            mode="segmented",
            status="parsed",
        )
        sess.add(d)
        sess.commit()
        sess.refresh(d)
        sess.add(
            Segment(
                document_id=d.id,
                source_text="rep target",
                target_text="t",
                target_locale="fr-FR",
                order_index=order_index,
                tm_match_pct=pct,
            )
        )
        sess.commit()

    def _filler(sess, i):
        d = Document(
            project_id=pid, filename=f"f{i}.txt", fmt="txt", mode="segmented", status="parsed"
        )
        sess.add(d)
        sess.commit()
        sess.refresh(d)
        sess.add(
            Segment(
                document_id=d.id,
                source_text=f"filler{i} alpha beta",
                target_text="t",
                target_locale="fr-FR",
                order_index=i + 300,
            )
        )
        sess.commit()

    # The repetition pair must STRADDLE the 150-doc chunk boundary INVERTED (the gate's
    # non-vacuity requirement): the member that must sort FIRST globally goes in a LATER
    # chunk, so a per-chunk (broken) sort orders them the WRONG way.
    #   doc A: created ~#5  -> chunk 1 (small id);  order_index 200, tm_match_pct None
    #   doc B: created last -> chunk 2 (large id);  order_index 1,   tm_match_pct 96
    # GLOBAL sort -> B (order_index 1) is the FIRST 'rep target' => fuzzy_high, A => rep.
    # PER-CHUNK sort -> A (chunk 1) emits before B (chunk 2) => A 'new', B => rep: the
    # exact divergence (fuzzy_high vs new) the fix closes.
    with Session(engine) as s:
        for i in range(4):
            _filler(s, i)  # docs #1..#4
        _rep_doc(s, 200, None)  # doc A (~#5, chunk 1, high order_index, 'new'-if-first)
        for i in range(4, 158):
            _filler(s, i)  # docs #6..#159  (154 more fillers)
        _rep_doc(s, 1, 96)  # doc B (#160, chunk 2, low order_index, fuzzy-if-first)

    with Session(engine) as s:
        # WEB path: doc ids in created_at DESC order (exactly what project_detail passes).
        doc_ids_desc = [  # noqa: C416
            d
            for d in s.exec(
                select(Document.id)
                .where(Document.project_id == pid)
                .order_by(Document.created_at.desc())
            ).all()
        ]
        assert len(doc_ids_desc) == 160, "160 docs -> forces >1 chunk"
        web = _project_stats(s, doc_ids_desc, with_leverage7=True)["leverage7"]
        # /api + CLI seam: builds its own doc-id order (id asc).
        api = vendor_cost.project_leverage(s, pid)
    web_words = {b: web["bands"][b]["words"] for b in vendor_cost.BANDS}
    api_words = {b: api["bands"][b]["words"] for b in vendor_cost.BANDS}
    assert web_words == api_words, (
        "page vs api band divergence past chunk boundary",
        web_words,
        api_words,
    )
    assert web["total_words"] == api["total_words"] == 158 * 3 + 2 + 2
    # global-sort determinism: B (order_index 1, fuzzy) is the FIRST 'rep target', A the rep
    assert api_words["fuzzy_high"] == 2 and api_words["repetitions"] == 2, api_words
    assert api_words["new"] == 158 * 3, "the 158 unique fillers"
    print(
        "  N1: >150-doc project — web (_project_stats) and /api (project_leverage) draw "
        "byte-identical bands via one global sort; _project_stats chunks its IN() OK"
    )


# ============================================================================
# RV-2: the `locale` axis (target-locale attribution: jobs + runs + segment volume)
# ============================================================================
def test_locale_axis():
    """RV-2: group_by=locale attributes cost/tokens to a job's payload.locale and a run's
    target_locale, and segment VOLUME to Segment.target_locale (exact per-locale — no
    project fold). A job with no locale -> '(untracked locale)'; an untracked run ->
    '(untracked)'. web/CLI/api parity."""

    cid = _mk_customer("LocCust")
    pid = _mk_project("LocProj", customer_id=cid)
    when = datetime(2026, 7, 4, 12, 0, 0)
    with Session(engine) as s:
        d = Document(
            project_id=pid, filename="loc.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        doc_id = d.id
        # volume by target locale: fr-FR 2 translated (6 words), de-DE 1 (3 words),
        # plus an untranslated fr-FR (no volume).
        for i, (src, tgt, loc) in enumerate(
            [
                ("aa bb cc", "t", "fr-FR"),
                ("dd ee ff", "t", "fr-FR"),
                ("gg hh ii", "t", "de-DE"),
                ("jj kk ll", "", "fr-FR"),
            ]
        ):
            s.add(
                Segment(
                    document_id=doc_id,
                    source_text=src,
                    target_text=tgt,
                    target_locale=loc,
                    order_index=i,
                )
            )

        # jobs by locale
        def _job(locale, ti, to):
            payload = {"document_id": doc_id}
            if locale is not None:
                payload["locale"] = locale
            return Job(
                kind="translate_document",
                status=JobStatus.COMPLETED,
                payload=payload,
                result={
                    "provider_used": {"claude": 1},
                    "tokens_in": ti,
                    "tokens_out": to,
                    "tokens_cache_read": 0,
                    "tokens_cache_write": 0,
                    "translated": 1,
                },
                finished_at=when,
            )

        s.add(_job("fr-FR", 100, 10))
        s.add(_job("de-DE", 200, 20))
        s.add(_job(None, 7, 1))  # no locale -> (untracked locale)
        s.commit()
    # a tracked workflow run in de-DE (StepRun cost + tokens)
    _mk_workflow_run(
        pid,
        doc_id,
        [{"provider": "claude", "cost_usd": 5.0, "tokens_in": 50, "tokens_out": 5}],
        when=when,
        wf_name="LocWF",
        locale="de-DE",
    )

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="locale", customer_id=cid), user=None
        )
    assert rep["key_column"] == "locale" and rep["group_by"] == "locale", rep
    by = {r["locale"]: r for r in rep["rows"]}
    assert set(by) == {"fr-FR", "de-DE", "(untracked locale)"}, list(by)
    # fr-FR: 1 job, 2 translated segments (6 words later), tokens 100/10
    assert by["fr-FR"]["jobs"] == 1 and by["fr-FR"]["segments"] == 2
    assert by["fr-FR"]["tokens_in"] == 100 and by["fr-FR"]["tokens_out"] == 10
    # de-DE: 1 job + 1 run = 2 units, 1 segment, tokens 200+50 / 20+5
    assert by["de-DE"]["jobs"] == 2 and by["de-DE"]["segments"] == 1
    assert by["de-DE"]["tokens_in"] == 250 and by["de-DE"]["tokens_out"] == 25
    # untracked locale: the no-locale job's spend, no volume
    assert by["(untracked locale)"]["jobs"] == 1
    assert by["(untracked locale)"]["tokens_in"] == 7
    assert by["(untracked locale)"]["segments"] == 0
    # words-derived (volume kind) attributes per locale
    with Session(engine) as s:
        repv = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="locale", customer_id=cid), user=None
        )
    byv = {r["locale"]: r for r in repv["rows"]}
    assert byv["fr-FR"]["words_derived"] == 6 and byv["de-DE"]["words_derived"] == 3

    # --- web + /api parity ---
    client = TestClient(app)
    r = client.get(f"/reports/run?kind=tokens&group_by=locale&customer_id={cid}")
    assert r.status_code == 200 and "locale" in r.text and "fr-FR" in r.text
    au = security.create_user("loc-api@x", "LA", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "loc", scopes="api docs")
    r = client.get(
        f"/api/reports/run?kind=tokens&group_by=locale&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == rep["rows"], "api rows must equal service rows"
    # --- CLI parity ---
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["--json", "report", "run", "tokens", "--group-by", "locale", "--customer", str(cid)]
        )
    assert rc == 0
    cli_out = _json.loads(buf.getvalue().strip().splitlines()[-1])
    assert cli_out["rows"] == rep["rows"], "CLI rows must equal service rows"

    # H1: an OUT-OF-WINDOW date filter zeroes the locale axis on EVERY column, including
    # volume — the same as every other axis (no phantom segment volume from a project
    # with no in-window job/run). The standard axes are already all-zero out of window;
    # this locks the locale axis to match.
    from datetime import date as _date

    oow = reports.ReportQuery(
        kind="volume",
        group_by="locale",
        customer_id=cid,
        date_from=_date(2020, 1, 1),
        date_to=_date(2020, 1, 31),
    )
    with Session(engine) as s:
        repo = reports.run_report(s, oow, user=None)
        # sanity: the standard none axis is also empty out of window (sibling behavior)
        repn = reports.run_report(
            s,
            reports.ReportQuery(
                kind="volume",
                group_by="none",
                customer_id=cid,
                date_from=_date(2020, 1, 1),
                date_to=_date(2020, 1, 31),
            ),
            user=None,
        )
    assert repo["rows"] == [], "out-of-window locale report must have NO rows (no phantom volume)"
    assert reports.totals_row(repn).get("segments", 0) == 0, (
        "sibling none axis also 0 out of window"
    )
    print(
        "  RV-2 locale axis: jobs by payload.locale + runs by target_locale + volume by "
        "segment.target_locale; (untracked locale)/(untracked) honest; out-of-window all-zero "
        "(H1); web/api/cli parity OK"
    )


# ============================================================================
# VR-5-lite: the `vendor` axis (vendor-dispatch scorecard, human spend gated)
# ============================================================================
def test_vendor_axis():
    """VR-5-lite: group_by=vendor is a scorecard from VendorDispatch legs — legs,
    word_count, returned_on_time (returned<=expiry), and estimate vs actual HUMAN spend
    (admin-only columns). Scoped by the leg's document->project like every axis; a
    customer-bound ident never sees a foreign vendor's legs. web/api/cli parity."""
    from app.models import Vendor, VendorDispatch

    cid = _mk_customer("VenCust")
    pid = _mk_project("VenProj", customer_id=cid)
    when = datetime(2026, 7, 4, 12, 0, 0)
    with Session(engine) as s:
        d = Document(
            project_id=pid, filename="ven.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        doc_id = d.id
    _wf, run_id = _mk_workflow_run(
        pid, doc_id, [{"provider": "claude", "cost_usd": 1.0}], when=when, wf_name="VenWF"
    )
    # A FOREIGN customer/project/leg (L6): must never appear in customer A's scorecard.
    fcid = _mk_customer("VenForeignCust")
    fpid = _mk_project("VenForeignProj", customer_id=fcid)
    with Session(engine) as s:
        fd = Document(
            project_id=fpid, filename="f.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(fd)
        s.commit()
        s.refresh(fd)
        fdoc_id = fd.id
    _fwf, frun_id = _mk_workflow_run(
        fpid, fdoc_id, [{"provider": "claude", "cost_usd": 1.0}], when=when, wf_name="VenForeignWF"
    )
    from app.models import VendorDispatchStatus as _VS

    with Session(engine) as s:
        v1 = Vendor(name="Acme LSP")
        v2 = Vendor(name="Beta LSP")
        vf = Vendor(name="Foreign LSP")
        s.add(v1)
        s.add(v2)
        s.add(vf)
        s.commit()
        s.refresh(v1)
        s.refresh(v2)
        s.refresh(vf)
        v1id, v2id, vfid = v1.id, v2.id, vf.id

        def _leg(vid, wc, est, act, expires, returned, status, doc=None, run=None):
            return VendorDispatch(
                vendor_id=vid,
                workflow_run_id=run or run_id,
                document_id=doc or doc_id,
                target_locale="fr-FR",
                word_count=wc,
                estimate_cost_usd=est,
                actual_cost_usd=act,
                dispatched_at=when,
                status=status,
                expires_at=expires,
                returned_at=returned,
            )

        R, D = _VS.RETURNED, _VS.DISPATCHED
        # on-time RETURNED
        s.add(_leg(v1id, 100, 50.0, 48.0, datetime(2026, 7, 10), datetime(2026, 7, 8), R))
        # DISPATCHED (in flight, not returned) -> money excluded
        s.add(_leg(v1id, 200, 80.0, None, datetime(2026, 7, 10), None, D))
        # LATE RETURNED
        s.add(_leg(v1id, 50, 20.0, 25.0, datetime(2026, 7, 5), datetime(2026, 7, 9), R))
        s.add(_leg(v2id, 30, 10.0, None, datetime(2026, 7, 10), None, D))
        # foreign leg (customer B) — must be excluded entirely from A's scorecard
        s.add(
            _leg(
                vfid,
                999,
                999.0,
                999.0,
                datetime(2026, 7, 10),
                datetime(2026, 7, 8),
                R,
                doc=fdoc_id,
                run=frun_id,
            )
        )
        # foreign IN-FLIGHT leg — the open_estimate_usd column must not leak it either
        s.add(
            _leg(vfid, 111, 888.0, None, datetime(2026, 7, 10), None, D, doc=fdoc_id, run=frun_id)
        )
        s.commit()

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="vendor", customer_id=cid), user=None
        )  # trusted operator sees the cost columns
    assert rep["key_column"] == "vendor" and rep["group_by"] == "vendor", rep
    assert "estimate_cost_usd" in rep["columns"] and "actual_cost_usd" in rep["columns"]
    assert "returned_on_time" in rep["columns"] and "legs" in rep["columns"]
    by = {r["vendor"]: r for r in rep["rows"]}
    v1key = f"{v1id}: Acme LSP"
    v2key = f"{v2id}: Beta LSP"
    assert f"{vfid}: Foreign LSP" not in by, "foreign vendor leg leaked into A's scorecard"
    assert "999" not in _json.dumps(rep), "foreign leg figures leaked"
    assert "888" not in _json.dumps(rep), "foreign IN-FLIGHT estimate leaked via open_estimate_usd"
    assert by[v1key]["legs"] == 3 and by[v1key]["word_count"] == 350, (
        "legs/words count all statuses"
    )
    assert by[v1key]["returned_on_time"] == 1, "only the on-time leg counts"
    # M3: money sums ONLY RETURNED legs (the DISPATCHED $80 leg is excluded).
    assert by[v1key]["estimate_cost_usd"] == 70.0, (
        "50 + 20 (returned legs only, not the $80 in-flight)"
    )
    assert by[v1key]["actual_cost_usd"] == 73.0, "48 + 25 (returned legs only)"
    # 2026-08-19: the new money columns exist and read the IN-FLIGHT / APPROVED-quote
    # legs the returned-only columns cannot see — an in-flight-only leg is no longer free.
    assert "open_estimate_usd" in rep["columns"] and "quoted_cost_usd" in rep["columns"]
    assert by[v1key]["open_estimate_usd"] == 80.0, "v1's DISPATCHED $80 in-flight leg"
    assert by[v1key]["quoted_cost_usd"] == 0.0, "no approved quotes in this fixture"
    assert by[v2key]["legs"] == 1 and by[v2key]["returned_on_time"] == 0
    assert by[v2key]["estimate_cost_usd"] == 0.0, "B's only leg is in-flight -> no returned money"
    assert by[v2key]["open_estimate_usd"] == 10.0, "B's in-flight $10 leg reads as OPEN, not free"

    # --- human-spend gating: a customer-bound / non-admin ident sees NO cost columns ---
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        bound = security.create_user(
            "ven-bound@x", "VB", Role.TRANSLATOR, FIXTURE_PW, customer_id=cid
        )
        with Session(engine) as s:
            bu = s.get(User, bound.id)
            repb = reports.run_report(
                s, reports.ReportQuery(kind="volume", group_by="vendor"), user=bu
            )
        assert "estimate_cost_usd" not in repb["columns"], (
            "a customer-bound ident must NOT read vendor margin"
        )
        assert "actual_cost_usd" not in repb["columns"]
        # operational columns stay
        assert "legs" in repb["columns"] and "returned_on_time" in repb["columns"]
        # M1: a ZERO-visibility ident still gets the VENDOR shape, never the job columns.
        empty_scope = security.create_user(
            "ven-empty@x",
            "VE",
            Role.TRANSLATOR,
            FIXTURE_PW,
            customer_id=_mk_customer("VenNoProj"),
        )
        with Session(engine) as s:
            eu = s.get(User, empty_scope.id)
            repe = reports.run_report(
                s, reports.ReportQuery(kind="cost", group_by="vendor"), user=eu
            )
        assert repe["key_column"] == "vendor" and repe["columns"][0] == "vendor", (
            "zero-scope vendor report must keep the vendor shape (M1)"
        )
        assert "cost_usd" not in repe["columns"], "must NOT leak the job cost column set"
        assert "legs" in repe["columns"], "vendor operational columns present at zero scope"
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()

    # --- web + /api + CLI parity (auth off -> operator sees cost cols everywhere) ---
    client = TestClient(app)
    r = client.get(f"/reports/run?kind=volume&group_by=vendor&customer_id={cid}")
    assert r.status_code == 200 and "Acme LSP" in r.text and "returned on time" in r.text
    au = security.create_user("ven-api@x", "VA", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "ven", scopes="api docs")
    r = client.get(
        f"/api/reports/run?kind=volume&group_by=vendor&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == rep["rows"], "api rows must equal service rows"
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["--json", "report", "run", "volume", "--group-by", "vendor", "--customer", str(cid)]
        )
    assert rc == 0
    cli_out = _json.loads(buf.getvalue().strip().splitlines()[-1])
    assert cli_out["rows"] == rep["rows"], "CLI rows must equal service rows"
    print(
        "  VR-5-lite vendor axis: leg scorecard (legs/word_count/returned_on_time + "
        "estimate vs actual + open_estimate/quoted); human-spend cols admin-only; "
        "foreign-leg (incl. in-flight) excluded; zero-scope keeps vendor shape (M1); "
        "web/api/cli parity OK"
    )


def test_vendor_money_counts_only_returned_legs():
    """M3: the money columns (estimate/actual) sum ONLY legs whose return was ACCEPTED
    (status RETURNED). A rejected->rework pair must not double-count; a cancelled or
    dead-letter leg must not read as spend. legs/word_count still count ALL statuses (work
    sent)."""
    from app.models import Vendor, VendorDispatch
    from app.models import VendorDispatchStatus as VS

    cid = _mk_customer("VenMoneyCust")
    pid = _mk_project("VenMoneyProj", customer_id=cid)
    when = datetime(2026, 7, 4, 12, 0, 0)
    with Session(engine) as s:
        d = Document(
            project_id=pid, filename="vm.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        doc_id = d.id
    _wf, run_id = _mk_workflow_run(
        pid, doc_id, [{"provider": "claude", "cost_usd": 1.0}], when=when, wf_name="VenMoneyWF"
    )
    with Session(engine) as s:
        v = Vendor(name="Rework LSP")
        s.add(v)
        s.commit()
        s.refresh(v)
        vid = v.id

        def _leg(wc, est, act, status):
            return VendorDispatch(
                vendor_id=vid,
                workflow_run_id=run_id,
                document_id=doc_id,
                target_locale="fr-FR",
                word_count=wc,
                estimate_cost_usd=est,
                actual_cost_usd=act,
                dispatched_at=when,
                status=status,
            )

        # a rejected return (superseded) + its accepted rework + a cancelled leg.
        s.add(_leg(100, 40.0, 42.0, VS.REJECTED))  # rejected -> money EXCLUDED
        s.add(_leg(100, 40.0, 39.0, VS.RETURNED))  # the accepted rework -> money COUNTS
        s.add(_leg(60, 20.0, None, VS.CANCELLED))  # cancelled -> money EXCLUDED
        s.commit()

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="vendor", customer_id=cid), user=None
        )
    row = {r["vendor"]: r for r in rep["rows"]}[f"{vid}: Rework LSP"]
    assert row["legs"] == 3 and row["word_count"] == 260, "legs/words count all statuses"
    assert row["estimate_cost_usd"] == 40.0, (
        "only the accepted rework's estimate (not the rejected/cancelled)"
    )
    assert row["actual_cost_usd"] == 39.0, (
        "only the accepted rework's actual (no double-count of the rejected 42)"
    )
    print(
        "  M3 vendor money: estimate/actual count ONLY accepted (RETURNED) legs; "
        "rejected/rework/cancelled never double-count; legs/words count all statuses OK"
    )


def test_vendor_open_estimate_and_quoted_columns():
    """2026-08-19: the vendor scorecard gains two admin-only money columns.
    open_estimate_usd sums estimate_cost_usd over IN-FLIGHT legs (dispatched/quoted/
    retry_wait/escalated); quoted_cost_usd sums the operator-APPROVED quote
    (quote_approved_at + quoted_cost_usd), excluding proposed-only quotes and the
    cancelled/dead-letter/rejected-superseded terminals. Each is its OWN column — a leg with
    both a quote and an estimate contributes to each, never blended. The returned-only
    estimate/actual columns are unchanged. An in-flight-only vendor is no longer free."""
    from datetime import datetime as _dt

    from app.models import Vendor, VendorDispatch
    from app.models import VendorDispatchStatus as VS

    cid = _mk_customer("VenOpenCust")
    pid = _mk_project("VenOpenProj", customer_id=cid)
    when = datetime(2026, 7, 4, 12, 0, 0)
    with Session(engine) as s:
        d = Document(
            project_id=pid, filename="vo.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        doc_id = d.id
    _wf, run_id = _mk_workflow_run(
        pid, doc_id, [{"provider": "claude", "cost_usd": 1.0}], when=when, wf_name="VenOpenWF"
    )
    with Session(engine) as s:
        v = Vendor(name="Open LSP")
        v2 = Vendor(name="InFlightOnly LSP")
        s.add(v)
        s.add(v2)
        s.commit()
        s.refresh(v)
        s.refresh(v2)
        vid, v2id = v.id, v2.id

        def _leg(vid_, wc, est, status, *, act=None, quoted=None, approved=False):
            leg = VendorDispatch(
                vendor_id=vid_,
                workflow_run_id=run_id,
                document_id=doc_id,
                target_locale="fr-FR",
                word_count=wc,
                estimate_cost_usd=est,
                actual_cost_usd=act,
                dispatched_at=when,
                status=status,
            )
            if quoted is not None:
                leg.quoted_cost_usd = quoted
                leg.quoted_at = _dt(2026, 7, 5, 9, 0, 0)
                if approved:
                    leg.quote_approved_at = _dt(2026, 7, 5, 10, 0, 0)
            return leg

        # vendor V — one of each membership class:
        s.add(_leg(vid, 100, 50.0, VS.RETURNED, act=48.0))  # returned: estimate/actual only
        s.add(_leg(vid, 200, 80.0, VS.DISPATCHED))  # in-flight: open_estimate 80
        s.add(
            _leg(vid, 50, 30.0, VS.QUOTED, quoted=35.0)
        )  # QUOTED, NOT approved: open 30, quoted 0
        s.add(
            _leg(
                vid,
                40,
                20.0,
                VS.DISPATCHED,  # approved quote (in-flight):
                quoted=25.0,
                approved=True,
            )
        )  #   open 20 AND quoted 25
        s.add(_leg(vid, 10, 15.0, VS.ESCALATED))  # escalated: open_estimate 15
        s.add(
            _leg(
                vid,
                10,
                999.0,
                VS.CANCELLED,  # cancelled+approved-quote: NONE
                quoted=999.0,
                approved=True,
            )
        )
        s.add(
            _leg(
                vid,
                10,
                42.0,
                VS.REJECTED,  # rejected-superseded approved quote:
                quoted=42.0,
                approved=True,
            )
        )  #   excluded from quoted (no double-count)
        # vendor V2 — a SINGLE in-flight leg, no returned money at all.
        s.add(_leg(v2id, 60, 60.0, VS.DISPATCHED))
        s.commit()

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="vendor", customer_id=cid), user=None
        )  # operator sees the money columns
    by = {r["vendor"]: r for r in rep["rows"]}
    row = by[f"{vid}: Open LSP"]
    # returned-only columns are unchanged by the new membership
    assert row["estimate_cost_usd"] == 50.0, "returned estimate only"
    assert row["actual_cost_usd"] == 48.0, "returned actual only"
    # open_estimate over ALL in-flight legs: 80 (dispatched) + 30 (quoted) + 20 (approved
    # dispatched) + 15 (escalated) = 145; returned/cancelled/rejected excluded.
    assert row["open_estimate_usd"] == 145.0, ("in-flight estimate sum", row["open_estimate_usd"])
    # quoted: only the APPROVED, non-terminal quote (25). The proposed-only QUOTED leg (35),
    # the cancelled approved quote (999) and the rejected-superseded approved quote (42) are
    # all excluded.
    assert row["quoted_cost_usd"] == 25.0, (
        "approved non-terminal quote only",
        row["quoted_cost_usd"],
    )
    # the in-flight-only vendor: estimate 0 (nothing returned) but open_estimate > 0 -> NOT free
    row2 = by[f"{v2id}: InFlightOnly LSP"]
    assert row2["estimate_cost_usd"] == 0.0 and row2["actual_cost_usd"] == 0.0
    assert row2["open_estimate_usd"] == 60.0, "an in-flight-only vendor must not read as free"
    assert row2["quoted_cost_usd"] == 0.0
    # columns present + admin-only + summable in TOTALS (not blank, not NON_SUMMABLE)
    for c in ("open_estimate_usd", "quoted_cost_usd"):
        assert c in rep["columns"], c
    tot = reports.totals_row(rep)
    assert tot["open_estimate_usd"] == 205.0, (
        "145 + 60 summed in TOTALS",
        tot["open_estimate_usd"],
    )
    assert tot["quoted_cost_usd"] == 25.0, tot["quoted_cost_usd"]
    # the caveats state each new column's membership
    joined = " ".join(rep["caveats"]).lower()
    assert "open_estimate_usd" in joined and "in-flight" in joined
    assert "quoted_cost_usd" in joined and "approved" in joined
    # a customer-bound / non-admin ident sees NEITHER new money column (leak channel closed)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        bound = security.create_user(
            "ven-open-bound@x", "VOB", Role.TRANSLATOR, FIXTURE_PW, customer_id=cid
        )
        with Session(engine) as s:
            bu = s.get(User, bound.id)
            repb = reports.run_report(
                s, reports.ReportQuery(kind="volume", group_by="vendor"), user=bu
            )
        for c in ("open_estimate_usd", "quoted_cost_usd", "estimate_cost_usd", "actual_cost_usd"):
            assert c not in repb["columns"], (c, "money column leaked to a customer-bound ident")
        # no money figure leaks in the payload either (the cancelled leg's 999 estimate)
        assert "999.0" not in _json.dumps(repb), "vendor money leaked to a customer-bound ident"
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    # web / api / cli parity — the new columns render identically on every surface
    client = TestClient(app)
    au = security.create_user("ven-open-api@x", "VOA", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "venopen", scopes="api docs")
    r = client.get(
        f"/api/reports/run?kind=volume&group_by=vendor&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == rep["rows"], "api rows must equal service rows"
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["--json", "report", "run", "volume", "--group-by", "vendor", "--customer", str(cid)]
        )
    assert rc == 0
    cli_out = _json.loads(buf.getvalue().strip().splitlines()[-1])
    assert cli_out["rows"] == rep["rows"], "CLI rows must equal service rows"
    # CSV carries the columns + the summed TOTALS row
    r = client.get(
        f"/api/reports/run?kind=volume&group_by=vendor&customer_id={cid}&format=csv",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200 and "open_estimate_usd" in r.text and "quoted_cost_usd" in r.text
    print(
        "  vendor money+: open_estimate_usd over in-flight legs (dispatched/quoted/"
        "retry_wait/escalated), quoted_cost_usd over APPROVED non-terminal quotes; "
        "separate columns, admin-only, in-flight-only vendor not free; web/api/cli/CSV OK"
    )


def test_csv_and_totals_row_rounding_agree():
    """M2: the CSV footer (to_csv) and the on-screen/JSON footer (totals_row) apply the
    SAME rounding, so a float sum like 0.1+0.2 reads 0.3 in BOTH — never 0.3 on screen and
    0.30000000000000004 in the download."""
    report = {
        "columns": ["scope", "jobs", "cost_usd"],
        "rows": [
            {"scope": "a", "jobs": 1, "cost_usd": 0.1},
            {"scope": "b", "jobs": 1, "cost_usd": 0.2},
        ],
        "key_column": "scope",
        "kind": "cost",
        "group_by": "none",
        "caveats": [],
    }
    tr = reports.totals_row(report)
    csv_rows = list(_csv.reader(io.StringIO(reports.to_csv(report))))
    total_row = csv_rows[-1] if csv_rows[-1][0] == "TOTALS" else csv_rows[-2]
    ci = report["columns"].index("cost_usd")
    assert str(tr["cost_usd"]) == total_row[ci], (
        f"CSV total {total_row[ci]!r} must equal on-screen total {tr['cost_usd']!r}"
    )
    assert "0.30000000000000004" not in total_row[ci] and tr["cost_usd"] == 0.3
    print("  M2: CSV TOTALS and totals_row apply the same rounding (0.1+0.2 -> 0.3 in both) OK")


# ============================================================================
# 2c. WORKFLOW-RUN SPEND on NON-workflow axes (F2-2: the tokens/cost report was 0/0
#     for a workflow-driven install because StepRun spend fed ONLY group_by=workflow)
# ============================================================================
def test_workflow_run_only_spend_counts():
    """F2-2: an install whose translation happens via WORKFLOW RUNS (no background-
    translate Jobs) must show its StepRun token/cost spend on the DEFAULT group_by=none
    report — the exact /api/reports/run?kind=tokens shape that returned 0/0. Also proves
    the run-only project's segment volume folds in (segments were a false 0 too)."""
    cid = _mk_customer("WfOnlyCust")
    pid = _mk_project("WfOnlyProj", customer_id=cid)
    doc_id = _add_segments(pid, [("hello world here", "bonjour"), ("second one", "deux")])
    _mk_workflow_run(
        pid,
        doc_id,
        [
            {"provider": "claude", "cost_usd": 0.006, "tokens_in": 600, "tokens_out": 120},
            {"provider": "claude", "cost_usd": 0.004, "tokens_in": 400, "tokens_out": 80},
        ],
        wf_name="WfOnly",
    )
    # NO Job exists for this customer at all.
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="none", customer_id=cid), user=None
        )
    assert rep["rows"], "a workflow-run-only install must NOT report an empty tokens report"
    row = rep["rows"][0]
    assert row["tokens_in"] == 1000 and row["tokens_out"] == 200, row
    assert row["jobs"] == 1, ("the run is counted as a unit of work in the count col", row)
    assert row["segments"] == 2, ("segment volume folds for a run-only project", row)
    # cost kind sums the StepRun costs (0.006 + 0.004).
    with Session(engine) as s:
        repc = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", customer_id=cid), user=None
        )
    assert abs(repc["rows"][0]["cost_usd"] - 0.01) < 1e-9, repc["rows"][0]

    # The other project-anchored axes see it too, keyed correctly.
    with Session(engine) as s:
        repp = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="project", customer_id=cid), user=None
        )
        assert _row_by_key(repp, f"{pid}: WfOnlyProj")["tokens_in"] == 1000
        reppr = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="provider", customer_id=cid), user=None
        )
        assert _row_by_key(reppr, "claude")["tokens_in"] == 1000, reppr["rows"]

    # The /api endpoint's exact F2-2 default shape (kind=tokens, NO group_by -> none).
    client = TestClient(app)
    au = security.create_user("wfonlyapi@x", "WfOnlyAU", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "wfonly", scopes="api docs")
    r = client.get(f"/api/reports/run?kind=tokens&customer_id={cid}", headers={"X-Loc-Token": raw})
    assert r.status_code == 200, r.text
    api_out = r.json()
    assert api_out["totals"]["tokens_in"] == 1000 and api_out["totals"]["tokens_out"] == 200, (
        "the /api tokens report must not read 0/0 for a workflow-driven install",
        api_out,
    )
    print(
        "  workflow-run-only: StepRun token/cost spend surfaces on the DEFAULT "
        "group_by=none tokens/cost report (was 0/0); segments fold; project/provider "
        "axes + /api parity OK"
    )


def test_mixed_job_and_run_no_double_count():
    """A project translated by BOTH a plain-MT Job AND a workflow run: the none-axis
    total is the SUM of the two (genuinely separate spend events), never a double-count,
    and never just one source. Segments fold exactly once."""
    cid = _mk_customer("MixedCust")
    pid = _mk_project("MixedProj", customer_id=cid)
    doc_id = _add_segments(pid, [("alpha beta", "x")])
    _mk_doc_job(doc_id, provider="claude", ti=700, to=70)  # plain-MT Job spend
    _mk_workflow_run(
        pid,
        doc_id,
        [
            {"provider": "claude", "cost_usd": 0.01, "tokens_in": 300, "tokens_out": 30},
        ],
        wf_name="MixedWF",
    )  # workflow-run spend
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="none", customer_id=cid), user=None
        )
    row = rep["rows"][0]
    assert row["tokens_in"] == 1000, ("job 700 + run 300, no double-count", row)
    assert row["tokens_out"] == 100, ("job 70 + run 30", row)
    assert row["jobs"] == 2, ("one Job + one run = two units of work", row)
    assert row["segments"] == 1, ("segment volume folds exactly ONCE, not per-source", row)
    print(
        "  mixed job+run: none-axis total is the SUM (job 700 + run 300 = 1000), "
        "counted as 2 units, segments folded once, no double-count OK"
    )


def test_run_spend_scoped_to_tenant():
    """A customer-bound report must count ONLY the scoped tenant's StepRuns — a foreign
    customer's workflow-run spend must never leak (the tenancy guarantee, extended to
    the StepRun overlay), on EVERY axis."""
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        cA = _mk_customer("RunScopeA")
        cB = _mk_customer("RunScopeB")
        pA = _mk_project("RunScopeProjA", customer_id=cA)
        pB = _mk_project("RunScopeProjB", customer_id=cB)
        docA = _add_segments(pA, [("a", "x")])
        docB = _add_segments(pB, [("b", "y")])
        _mk_workflow_run(
            pA,
            docA,
            [{"provider": "claude", "cost_usd": 0.002, "tokens_in": 222, "tokens_out": 22}],
            wf_name="RunScopeWfA",
        )
        _mk_workflow_run(
            pB,
            docB,
            [{"provider": "claude", "cost_usd": 0.009, "tokens_in": 9999, "tokens_out": 999}],
            wf_name="RunScopeWfB",
        )  # the FOREIGN spend
        uA = security.create_user("runscope@x", "RSA", Role.TRANSLATOR, FIXTURE_PW, customer_id=cA)
        with Session(engine) as s:
            ub = s.get(User, uA.id)
            for gb in ("none", "project", "customer", "provider", "date"):
                rep = reports.run_report(
                    s, reports.ReportQuery(kind="tokens", group_by=gb), user=ub
                )
                assert reports.totals_row(rep)["tokens_in"] == 222, (
                    f"group_by={gb}: foreign StepRun spend leaked",
                    reports.totals_row(rep)["tokens_in"],
                )
                blob = _json.dumps(rep)
                assert (
                    "9999" not in blob and "RunScopeProjB" not in blob and "RunScopeWfB" not in blob
                ), f"group_by={gb}: foreign tenant data leaked into a scoped report"
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print(
        "  run-spend scoping: a customer-bound report counts only the scoped tenant's "
        "StepRuns; foreign workflow-run spend absent from every axis OK"
    )


def test_job_run_bucket_merge_invariant():
    """F8(a): a plain-MT Job and a workflow run on the SAME document / date / provider
    must MERGE into ONE row on every project-anchored axis (project/customer/date/
    provider/engine). A _run_group_key drift away from _group_key would split them into
    two rows — which group_by=none (keys collapse to 'all') structurally cannot catch."""
    cid = _mk_customer("MergeCust")
    pid = _mk_project("MergeProj", customer_id=cid)
    when = datetime(2027, 3, 3, 9, 0, 0)
    doc_id = _add_segments(pid, [("merge me now", "x")])
    _mk_doc_job(doc_id, provider="claude", ti=500, to=50, when=when)
    _mk_workflow_run(
        pid,
        doc_id,
        [{"provider": "claude", "cost_usd": 0.01, "tokens_in": 300, "tokens_out": 30}],
        when=when,
        wf_name="MergeWF",
    )
    win = dict(date_from=date(2027, 3, 3), date_to=date(2027, 3, 3))  # noqa: C408
    for gb in ("project", "customer", "date", "provider", "engine"):
        with Session(engine) as s:
            rep = reports.run_report(
                s,
                reports.ReportQuery(kind="tokens", group_by=gb, customer_id=cid, **win),
                user=None,
            )
        assert len(rep["rows"]) == 1, (
            f"{gb}: a job+run on the same key must be ONE row, not two",
            rep["rows"],
        )
        row = rep["rows"][0]
        assert row["jobs"] == 2, (f"{gb}: one job + one run = 2 units of work", row)
        assert row["tokens_in"] == 800 and row["tokens_out"] == 80, (gb, row)
    print(
        "  merge-invariant: a job and a run on the same doc/date/provider land in ONE "
        "row (jobs=2, tokens=job+run) on project/customer/date/provider/engine OK"
    )


def test_untracked_run_and_volume_axes():
    """F8(b) / F3 / F2: (1) an UNTRACKED workflow run (all StepRun costs NULL — legacy or
    pending) surfaces as a counted-but-zero "(untracked)" row on the NON-workflow axes
    too, NEVER merged as a silent $0 into a real bucket (so a real tenant's total is not
    diluted). (2) a run-only project's segment volume folds on the date + engine axes as
    well, so the install-wide volume TOTAL is whole, never a disappearing 0."""
    cid = _mk_customer("UntrackedCust")
    pid = _mk_project("UntrackedProj", customer_id=cid)
    when = datetime(2027, 4, 4, 8, 0, 0)
    doc_t = _add_segments(pid, [("alpha beta gamma", "x")])  # 3 words, translated
    doc_u = _add_segments(pid, [("delta", "y")])  # 1 word, translated
    _mk_workflow_run(
        pid,
        doc_t,
        [{"provider": "claude", "cost_usd": 0.005, "tokens_in": 500, "tokens_out": 50}],
        when=when,
        wf_name="TrackedWF",
    )
    _mk_workflow_run(
        pid,
        doc_u,
        [{"provider": "claude", "cost_usd": None, "tokens_in": None, "tokens_out": None}],
        when=when,
        wf_name="LegacyWF",
    )  # all-NULL == untracked

    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="none", customer_id=cid), user=None
        )
    all_row = _row_by_key(rep, "all")
    unt_row = _row_by_key(rep, "(untracked)")
    assert all_row is not None and all_row["tokens_in"] == 500, all_row
    assert unt_row is not None and unt_row["jobs"] == 1 and unt_row["tokens_in"] == 0, (
        "an untracked run is counted in its OWN '(untracked)' row at spend 0",
        unt_row,
    )
    assert reports.totals_row(rep)["tokens_in"] == 500, (
        "the tracked run's spend must not be diluted by the untracked run's phantom 0"
    )

    with Session(engine) as s:
        repe = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="engine", customer_id=cid), user=None
        )
    assert _row_by_key(repe, "claude")["tokens_in"] == 500, repe["rows"]
    assert _row_by_key(repe, "(untracked)") is not None, (
        "an all-NULL run must not vanish nor read as a real provider's $0",
        repe["rows"],
    )

    for gb in ("date", "engine"):
        with Session(engine) as s:
            repv = reports.run_report(
                s, reports.ReportQuery(kind="volume", group_by=gb, customer_id=cid), user=None
            )
        assert reports.totals_row(repv)["words_derived"] == 4, (
            f"{gb}: run-only volume folds; the total is whole (3+1 words)",
            repv["rows"],
        )
        assert reports.totals_row(repv)["segments"] == 2, (gb, repv["rows"])
    print(
        "  untracked+volume axes: an all-NULL run is a counted '(untracked)' row (never "
        "a silent $0) on none/engine; run-only segment volume folds on date+engine so "
        "the install total is whole OK"
    )


def test_provider_filter_run_count_divergence():
    """F8(b) / F4: a provider filter counts a run on the NON-workflow axes ONLY when it
    has a matching-provider step (job-loop-consistent), but the WORKFLOW axis lists every
    run of the workflow regardless. Pin BOTH sides so the documented divergence can't
    silently flip."""
    cid = _mk_customer("PFilterCust")
    pid = _mk_project("PFilterProj", customer_id=cid)
    doc_id = _add_segments(pid, [("x", "y")])
    wf_id, _run = _mk_workflow_run(
        pid,
        doc_id,
        [{"provider": "gemini", "cost_usd": 0.003, "tokens_in": 300, "tokens_out": 30}],
        wf_name="PFilterWF",
    )  # gemini-only run, no claude step

    # no filter: the run shows on the none axis (tokens 300).
    with Session(engine) as s:
        rep_all = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="none", customer_id=cid), user=None
        )
    assert _row_by_key(rep_all, "all")["tokens_in"] == 300, rep_all["rows"]

    # claude filter: the none axis DROPS the gemini-only run entirely (job-loop-consistent).
    with Session(engine) as s:
        rep_none = reports.run_report(
            s,
            reports.ReportQuery(kind="tokens", group_by="none", customer_id=cid, provider="claude"),
            user=None,
        )
    assert rep_none["rows"] == [], (
        "a claude filter drops a gemini-only run on the none axis",
        rep_none["rows"],
    )

    # claude filter, workflow axis: the run is STILL listed (matching-step spend 0).
    with Session(engine) as s:
        rep_wf = reports.run_report(
            s,
            reports.ReportQuery(
                kind="tokens", group_by="workflow", customer_id=cid, provider="claude"
            ),
            user=None,
        )
    wf_row = _row_by_key(rep_wf, f"{wf_id}: PFilterWF")
    assert wf_row is not None and wf_row["jobs"] == 1 and wf_row["tokens_in"] == 0, (
        "the workflow axis lists every run even under a non-matching provider filter",
        rep_wf["rows"],
    )
    print(
        "  provider-filter divergence: a gemini-only run is DROPPED on the none axis "
        "under a claude filter but STILL listed (spend 0) on the workflow axis OK"
    )


def test_provider_axis_exact_per_step_spend():
    """D2: provider/engine spend must be EXACT per-StepRun, never lumped onto the run's
    dominant (most-frequent) provider. A run with claude $10 in ONE step + local $0 in
    TWO steps must show claude cost=10 and local cost=0 — claude must NOT vanish with its
    $10 misattributed to the cheaper, higher-frequency provider (the finance-report
    corruption). The run is still counted once, on its dominant (local) row."""
    cid = _mk_customer("PerStepCust")
    pid = _mk_project("PerStepProj", customer_id=cid)
    doc_id = _add_segments(pid, [("x", "y")])
    _mk_workflow_run(
        pid,
        doc_id,
        [
            {"provider": "claude", "cost_usd": 10.0, "tokens_in": 1000, "tokens_out": 100},
            {"provider": "local", "cost_usd": 0.0, "tokens_in": 200, "tokens_out": 20},
            {"provider": "local", "cost_usd": 0.0, "tokens_in": 200, "tokens_out": 20},
        ],
        wf_name="PerStepWF",
    )
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="provider", customer_id=cid), user=None
        )
    claude = _row_by_key(rep, "claude")
    local = _row_by_key(rep, "local")
    assert claude is not None and abs(claude["cost_usd"] - 10.0) < 1e-9, (
        "claude's $10 must stay on claude, not vanish onto the cheap high-freq provider",
        rep["rows"],
    )
    assert local is not None and abs(local["cost_usd"] - 0.0) < 1e-9, local
    assert claude["tokens_in"] == 1000 and local["tokens_in"] == 400, (claude, local)
    # the run is ONE unit, counted on its dominant (most-frequent = local) provider row;
    # claude carries real spend with jobs=0 — deliberate (spend exact, count one-per-run).
    assert local["jobs"] == 1 and claude["jobs"] == 0, (claude, local)
    print(
        "  D2 provider-axis exact spend: per-StepRun cost/tokens stay on the true "
        "provider (claude $10 present, not misattributed to local); run counted once "
        "on its dominant row OK"
    )


def test_volume_lands_on_real_project_not_untracked():
    """D1: on kind=volume group_by=project, a real named project whose ONLY run is an
    UNTRACKED legacy run (lowest id, so it wins the fold slot) must show its words on the
    PROJECT row — never park them in "(untracked)" (the false-zero-on-a-real-row class
    this branch exists to kill, and the COMMON case on an install carrying legacy runs)."""
    cid = _mk_customer("VolRealCust")
    pid = _mk_project("VolRealProj", customer_id=cid)
    doc_id = _add_segments(pid, [("one two three four", "x")])  # 4 words, translated
    _mk_workflow_run(
        pid,
        doc_id,
        [{"provider": "claude", "cost_usd": None, "tokens_in": None, "tokens_out": None}],
        wf_name="VolLegacyWF",
    )  # all-NULL == untracked (the only run for this project)
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="project", customer_id=cid), user=None
        )
    proj_row = _row_by_key(rep, f"{pid}: VolRealProj")
    assert proj_row is not None and proj_row["words_derived"] == 4 and proj_row["segments"] == 1, (
        "volume must land on the REAL project row, not in (untracked)",
        rep["rows"],
    )
    unt_row = _row_by_key(rep, "(untracked)")
    assert unt_row is not None and unt_row["jobs"] == 1, unt_row
    assert unt_row["words_derived"] == 0 and unt_row["segments"] == 0, (
        "(untracked) carries the honest run COUNT, never the volume",
        unt_row,
    )
    print(
        "  D1 volume attribution: an untracked-run-only project shows its words on the "
        "REAL project row; (untracked) carries only the run count OK"
    )


def test_caveats_surface_on_csv_and_api():
    """D4: the provider-filter / workflow-axis reconciliation caveats must appear on the
    machine surfaces (CSV export + /api JSON), not only the web results page."""
    cid = _mk_customer("CaveatCust")
    pid = _mk_project("CaveatProj", customer_id=cid)
    doc_id = _add_segments(pid, [("x", "y")])
    _mk_workflow_run(
        pid,
        doc_id,
        [{"provider": "gemini", "cost_usd": 0.003, "tokens_in": 300, "tokens_out": 30}],
        wf_name="CaveatWF",
    )
    # the service dict carries caveats for a provider-filtered report.
    with Session(engine) as s:
        rep = reports.run_report(
            s,
            reports.ReportQuery(
                kind="tokens", group_by="customer", customer_id=cid, provider="claude"
            ),
            user=None,
        )
    assert rep.get("caveats"), "a provider-filtered report must carry a caveat"
    # CSV: the caveat text appears AFTER the TOTALS row.
    csv_text = reports.to_csv(rep)
    assert "provider filter" in csv_text.lower(), csv_text
    assert csv_text.index("TOTALS") < csv_text.lower().index("provider filter"), (
        "the caveat must sit after the TOTALS row"
    )
    # a plain (no filter, non-workflow) report carries NO caveat -> no extra CSV rows.
    with Session(engine) as s:
        plain = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="customer", customer_id=cid), user=None
        )
    assert not plain.get("caveats"), "a plain report must carry no caveat"
    assert "NOTE" not in reports.to_csv(plain), "a plain CSV must gain no NOTE rows"
    # /api JSON exposes a caveats field.
    client = TestClient(app)
    au = security.create_user("caveatapi@x", "CaveatAU", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "caveat", scopes="api docs")
    r = client.get(
        f"/api/reports/run?kind=tokens&group_by=customer&customer_id={cid}&provider=claude",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("caveats"), ("the /api JSON must expose caveats", r.json())
    print(
        "  D4 caveats: provider-filter/workflow caveats surface on CSV (after TOTALS) "
        "and in /api JSON; a plain report carries none OK"
    )


# ============================================================================
# Wave 2 — the qa + postedit kinds (SPEC-wave2 §4 / §9 Train B tests 16-31)
# ============================================================================
def test_wave2_column_sets():  # 17
    cat = dict(reports.kinds_catalog())
    assert set(cat) == set(reports.KINDS) and len(cat) == 5, cat
    assert cat["qa"] == [
        "findings",
        "critical",
        "major",
        "minor",
        "neutral",
        "other",
        "human",
        "cards",
        "avg_card_score",
    ], cat["qa"]
    for sev in ("critical", "major", "minor", "neutral", "other"):
        assert sev in cat["qa"], "qa renders five severity columns"
    assert "human" in cat["qa"], "Wave 3 §6.6: qa gains the reviewer-authored `human` column"
    assert cat["postedit"][0] == "segments_edited" and "tte_ms" in cat["postedit"]
    print("  17 column sets: five kinds; qa five severities; postedit full set OK")


def test_wave2_empty_report_ordering():  # 18 (M1)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        u = security.create_user(
            "qa-empty@x",
            "QE",
            Role.TRANSLATOR,
            FIXTURE_PW,
            customer_id=_mk_customer("QaEmptyCust"),
        )
        with Session(engine) as s:
            uu = s.get(User, u.id)
            rq = reports.run_report(s, reports.ReportQuery(kind="qa", group_by="vendor"), user=uu)
            rp = reports.run_report(
                s, reports.ReportQuery(kind="postedit", group_by="vendor"), user=uu
            )
        assert rq["kind"] == "qa" and rq["key_column"] == "vendor"
        assert "findings" in rq["columns"] and "legs" not in rq["columns"], (
            "zero-scope qa+vendor must keep the QA shape, not the vendor scorecard (M1)"
        )
        assert (
            rp["kind"] == "postedit" and "episodes" in rp["columns"] and "legs" not in rp["columns"]
        )
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  18 empty-report ordering: kind arms before the vendor arm (M1) OK")


def test_wave2_key_column_new_axes():  # 19 (M2)
    cid = _mk_customer("KcCust")
    pid = _mk_project("KcProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    _mk_lqa(doc, seg_id=segs[0], dimension="Fluency/Grammar", severity="minor")
    with Session(engine) as s:
        rd = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="dimension", customer_id=cid), user=None
        )
        ra = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="actor", customer_id=cid), user=None
        )
    assert rd["key_column"] == "dimension" and rd["columns"][0] == "dimension", rd
    assert ra["key_column"] == "actor" and ra["columns"][0] == "actor", ra
    print("  19 key_column: dimension/actor render their own header, never 'scope' OK")


def test_wave2_dimension_axis():  # 20
    cid = _mk_customer("DimCust")
    pid = _mk_project("DimProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 3)
    _mk_lqa(doc, seg_id=segs[0], dimension="Accuracy/Omission", severity="major", scoring_id="c1")
    _mk_lqa(
        doc,
        seg_id=segs[0],
        dimension="Accuracy/Mistranslation",
        severity="critical",
        scoring_id="c1",
    )
    _mk_lqa(doc, seg_id=segs[1], dimension="Fluency/Grammar", severity="minor", scoring_id="c2")
    _mk_lqa(
        doc, seg_id=segs[2], dimension="Terminology/Inconsistent", severity="major", scoring_id="c3"
    )
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="dimension", customer_id=cid), user=None
        )
    by = {r["dimension"]: r for r in rep["rows"]}
    # sub-category preserved, not collapsed to the bare head dimension
    assert "Accuracy/Omission" in by and "Accuracy/Mistranslation" in by, by
    assert by["Accuracy/Omission"]["major"] == 1 and by["Accuracy/Omission"]["findings"] == 1
    assert by["Accuracy/Mistranslation"]["critical"] == 1
    assert by["Fluency/Grammar"]["minor"] == 1
    assert by["Terminology/Inconsistent"]["major"] == 1
    print("  20 dimension axis: dimension x severity cross-tab; sub-category preserved OK")


def test_wave2_actor_axis_rbac():  # 21 (M6)
    cid = _mk_customer("ActCust")
    pid = _mk_project("ActProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    _mk_lqa(doc, seg_id=segs[0], source="structural", judge=None, scoring_id="a1")
    _mk_pe(doc, segs[0], actor="ed@x", actor_kind="human")
    # trusted in-process operator (user=None) is ALLOWED
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="actor", customer_id=cid), user=None
        )
    assert rep["key_column"] == "actor" and "structural" in {r["actor"] for r in rep["rows"]}
    # web + /api refuse a viewer; the web form omits the option
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        from tests._csrf import csrf_for, sign_out

        tr = security.create_user("act-tr@x", "AT", Role.TRANSLATOR, FIXTURE_PW)
        client = TestClient(app)
        csrf_for(client)
        r = client.post(
            "/auth/login",
            data={"email": "act-tr@x", "password": FIXTURE_PW, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, ("login failed", r.status_code)
        csrf_for(client)
        r = client.get(f"/reports/run?kind=postedit&group_by=actor&customer_id={cid}")
        assert r.status_code == 403, ("web viewer refused the actor axis", r.status_code)
        # the builder page omits the actor option for the viewer
        rp = client.get("/reports")
        assert rp.status_code == 200 and 'value="actor"' not in rp.text, (
            "the actor option must be hidden from an under-ranked ident's builder"
        )
        # other axes stay viewer-readable
        r = client.get(f"/reports/run?kind=qa&group_by=dimension&customer_id={cid}")
        assert r.status_code == 200, "the dimension axis stays viewer-readable"
        sign_out(client)
        # /api refusal
        _row, raw = apikeys.create_key(tr.id, "act", scopes="api docs")
        r = client.get(
            f"/api/reports/run?kind=postedit&group_by=actor&customer_id={cid}",
            headers={"X-Loc-Token": raw},
        )
        assert r.status_code == 403 and "actor axis" in r.json().get("error", "").lower()
        r = client.get(
            f"/api/reports/run?kind=qa&group_by=dimension&customer_id={cid}",
            headers={"X-Loc-Token": raw},
        )
        assert r.status_code == 200, "a viewer key may read the dimension axis"
        # a MANAGER key clears the actor gate (auth ON) — baseline for the auth-off case
        mgr = security.create_user("act-mgr@x", "AM", Role.MANAGER, FIXTURE_PW)
        _row2, mraw = apikeys.create_key(mgr.id, "actmgr", scopes="api docs")
        r = client.get(
            f"/api/reports/run?kind=postedit&group_by=actor&customer_id={cid}",
            headers={"X-Loc-Token": mraw},
        )
        assert r.status_code == 200, ("manager key runs the actor axis", r.text)
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    # 2026-08-19: the /api actor gate is ALWAYS-ON — it does NOT condition on
    # auth_required, so a viewer/under-ranked key is refused the editor-email axis even
    # with the login wall OFF, while a manager key still 200s. (_require_doc_scope still
    # resolves the key's real role under auth-off, so the gate has a role to test.)
    assert security.auth_required() is False, "precondition: login wall off"
    client2 = TestClient(app)
    r = client2.get(
        f"/api/reports/run?kind=postedit&group_by=actor&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )  # translator key from above
    assert r.status_code == 403 and "actor axis" in r.json().get("error", "").lower(), (
        "auth-off: an under-ranked key must STILL be refused the actor axis",
        r.text,
    )
    r = client2.get(
        f"/api/reports/run?kind=postedit&group_by=actor&customer_id={cid}",
        headers={"X-Loc-Token": mraw},
    )  # manager key from above
    assert r.status_code == 200, ("auth-off: a manager key still runs the actor axis", r.text)
    # a non-actor axis stays viewer-open under auth-off too (the gate is axis-specific)
    r = client2.get(
        f"/api/reports/run?kind=qa&group_by=dimension&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200, "auth-off: the dimension axis stays viewer-readable"
    # local CLI operator is trusted -> allowed
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["--json", "report", "run", "postedit", "--group-by", "actor", "--customer", str(cid)]
        )
    assert rc == 0, "the local CLI operator always runs the actor axis"
    print("  21 actor axis RBAC: viewer 403 on web/api, form omits it, local operator OK")


def test_wave2_postedit_measure_rule():  # 22 + 22b (H3, N7)
    cid = _mk_customer("PeCust")
    pid = _mk_project("PeProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1, tte=1000)
    seg = segs[0]
    vid = _mk_vendor("PeVendor")
    # episode 1 (vendor, MT baseline) then episode 2 (reviewer, human baseline)
    _mk_pe(
        doc,
        seg,
        episode_seq=1,
        actor_kind="vendor",
        vendor_id=vid,
        baseline_kind="mt",
        baseline_chars=10,
        final_chars=12,
        source_words=5,
        dist=(3, 1, 25.0),
        cum=(3, 1, 25.0),
        edit_count=1,
    )
    _mk_pe(
        doc,
        seg,
        episode_seq=2,
        actor="rev@x",
        actor_kind="human",
        baseline_kind="human",
        baseline_chars=12,
        final_chars=15,
        source_words=5,
        dist=(4, 2, 30.0),
        cum=(6, 3, 40.0),
        edit_count=2,
    )
    # CUMULATIVE (project axis): once per segment from the max-episode row
    with Session(engine) as s:
        rp = reports.run_report(
            s, reports.ReportQuery(kind="postedit", group_by="project", customer_id=cid), user=None
        )
    row = rp["rows"][0]
    assert row["segments_edited"] == 1 and row["episodes"] == 2 and row["edits"] == 3
    assert row["distance_char"] == 6 and row["distance_word"] == 3, row
    assert row["avg_distance_pct"] == 40.0
    assert row["mt_chars"] == 10 and row["final_chars"] == 15, row
    assert row["words_derived"] == 5 and row["tte_ms"] == 1000 and row["avg_tte_ms"] == 1000
    # EPISODE-LOCAL (actor axis): two buckets, neither equals the cumulative figure
    with Session(engine) as s:
        ra = reports.run_report(
            s, reports.ReportQuery(kind="postedit", group_by="actor", customer_id=cid), user=None
        )
    by = {r["actor"]: r for r in ra["rows"]}
    assert "vendor: PeVendor" in by and "rev@x" in by, by
    assert by["vendor: PeVendor"]["distance_char"] == 3
    assert by["rev@x"]["distance_char"] == 4, "episode-local, not cumulative"
    # N7: the per-segment columns are ABSENT (not zeroed) on the multi-bucket axis
    for c in ("words_derived", "tte_ms", "avg_tte_ms"):
        assert c not in ra["columns"], (c, ra["columns"])
    assert "mt_chars" in ra["columns"], "mt_chars kept (episode-scoped)"
    # the segment is counted in segments_edited of BOTH rows -> TOTALS deliberately 2,
    # and no per-segment word/effort column can double-count (they are absent)
    tot = reports.totals_row(ra)
    assert tot["segments_edited"] == 2 and "words_derived" not in tot
    assert any("episode-local" in c.lower() for c in ra["caveats"]), ra["caveats"]
    print("  22 measure rule: cumulative on project, episode-local on actor; N7 columns OK")


def test_wave2_honest_buckets():  # 23
    cid = _mk_customer("HonCust")
    pid = _mk_project("HonProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 2)
    # legacy NULL-locale + NULL-scoring_id LQA row
    _mk_lqa(
        doc, seg_id=segs[0], severity="minor", locale=None, scoring_id=None, source=None, judge=None
    )
    _mk_lqa(doc, seg_id=segs[1], severity="major", locale="fr-FR", scoring_id="c9")
    with Session(engine) as s:
        rl = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="locale", customer_id=cid), user=None
        )
    unt = _row_by_key(rl, "(untracked locale)")
    assert unt is not None and unt["findings"] == 1, rl
    assert unt["cards"] == 0 and unt["avg_card_score"] == "", "NULL scoring_id -> no card"
    assert any("scoring_id" in c or "predate Wave 2" in c for c in rl["caveats"])
    # postedit: baseline_kind='none' counts in episodes/edits but not segments_edited
    pid2 = _mk_project("HonProj2", customer_id=cid)
    doc2, segs2 = _mk_doc_segs(pid2, 1)
    _mk_pe(
        doc2,
        segs2[0],
        baseline_kind="none",
        baseline_chars=0,
        final_chars=5,
        dist=None,
        cum=None,
        edit_count=1,
    )
    with Session(engine) as s:
        rp = reports.run_report(
            s, reports.ReportQuery(kind="postedit", group_by="none", project_id=pid2), user=None
        )
    prow = rp["rows"][0]
    assert prow["episodes"] == 1 and prow["segments_edited"] == 0, prow
    assert prow["distance_char"] == 0 and prow["avg_distance_pct"] == ""
    assert any("no baseline" in c.lower() for c in rp["caveats"])
    print("  23 honest buckets: (untracked locale), no-baseline exclusion, NULL scoring OK")


def test_wave2_null_not_zero_and_totals():  # 24 + 25 (M3)
    cid = _mk_customer("NzCust")
    pid = _mk_project("NzProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    _mk_lqa(doc, seg_id=segs[0], scoring_id=None, source=None, judge=None)
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="none", customer_id=cid), user=None
        )
    row = rep["rows"][0]
    assert row["avg_card_score"] == "", "avg renders '' (not 0.0) in JSON"
    totals = reports.totals_row(rep)
    assert totals["avg_card_score"] == "", "M3: avg column blank in TOTALS (not 0)"
    # CSV agrees cell-for-cell: the avg column is empty in the data row AND the TOTALS row
    csv_text = reports.to_csv(rep)
    reader = list(_csv.reader(io.StringIO(csv_text)))
    ci = rep["columns"].index("avg_card_score")
    header_rows = [r for r in reader if r and r[0] not in ("NOTE",)]
    data_row = header_rows[1]
    totals_row_csv = next(r for r in header_rows if r[0] == "TOTALS")
    assert data_row[ci] == "" and totals_row_csv[ci] == "", (data_row, totals_row_csv)
    print("  24/25 NULL != 0: avg blank in HTML/JSON/CSV and in TOTALS; CSV==totals_row OK")


def test_wave2_cards_and_avg_card_score():  # 26 (H2, N8)
    cid = _mk_customer("CardCust")
    pid = _mk_project("CardProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 2)
    # one card (segment-level), 5 findings sharing scoring_id + score
    for _ in range(5):
        _mk_lqa(
            doc,
            seg_id=segs[0],
            dimension="Accuracy/Omission",
            severity="minor",
            score=80.0,
            scoring_id="cardA",
        )
    # two workflow cards (segment_id=None) from two runs -> two distinct cards
    _mk_lqa(doc, seg_id=None, severity="major", score=70.0, scoring_id="wf1")
    _mk_lqa(doc, seg_id=None, severity="major", score=60.0, scoring_id="wf2")
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="none", customer_id=cid), user=None
        )
    row = rep["rows"][0]
    assert row["findings"] == 7 and row["cards"] == 3, row
    assert row["avg_card_score"] == 70.0, ("(80+70+60)/3", row)  # per distinct card
    # N8: mixing per-segment and per-visit cards -> the granularity/regime caveat fires
    assert any("granularit" in c.lower() for c in rep["caveats"]), rep["caveats"]
    print("  26 cards: 5-finding card counts once; two wf cards count two; N8 caveat OK")


def test_wave3_human_findings_reports():  # Wave 3 §6.6 / Train C test 25
    from datetime import datetime as _dt

    cid = _mk_customer("HumCust")
    pid = _mk_project("HumProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 2)
    # BASELINE: one machine card (2 findings, one scoring_id, score 80).
    _mk_lqa(doc, seg_id=segs[0], severity="minor", score=80.0, scoring_id="cardM")
    _mk_lqa(doc, seg_id=segs[0], severity="major", score=80.0, scoring_id="cardM")
    with Session(engine) as s:
        base = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="none", customer_id=cid), user=None
        )
    brow = base["rows"][0]
    assert brow["findings"] == 2 and brow["cards"] == 1 and brow["avg_card_score"] == 80.0
    assert brow["human"] == 0, brow

    # Add a LIVE human finding + a WITHDRAWN human finding (both on the other segment).
    _mk_lqa(
        doc,
        seg_id=segs[1],
        severity="critical",
        source="human",
        judge=None,
        scoring_id=None,
        actor="rev@x",
    )
    _mk_lqa(
        doc,
        seg_id=segs[1],
        severity="major",
        source="human",
        judge=None,
        scoring_id=None,
        actor="rev@x",
        withdrawn_at=_dt(2026, 7, 5, 9, 0, 0),
    )
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="none", customer_id=cid), user=None
        )
    row = rep["rows"][0]
    # the live human finding is counted; the withdrawn one is NOT (§6.6.5).
    assert row["findings"] == 3, ("2 machine + 1 live human; withdrawn excluded", row)
    assert row["human"] == 1, row
    assert row["critical"] == 1 and brow["critical"] == 0, "human severity counted"
    # cards + avg_card_score are UNCHANGED from the machine-only baseline (§6.6.1).
    assert row["cards"] == 1 and row["avg_card_score"] == 80.0, (
        "a hand-logged bug is not a scored card",
        row,
    )
    # its NULL scoring_id must NOT raise the predate-Wave-2 flag (§6.6.2).
    assert not any("predate Wave 2" in c for c in rep["caveats"]), rep["caveats"]
    assert any("logged by hand" in c for c in rep["caveats"]), (
        "human caveat fires",
        rep["caveats"],
    )

    # actor axis: the human finding buckets on its EMAIL, the machine on its source (§6.6.4).
    with Session(engine) as s:
        ra = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="actor", customer_id=cid), user=None
        )
    keys = {r["actor"] for r in ra["rows"]}
    assert "rev@x" in keys, ("human buckets on email", keys)
    assert not any(k == "rev@x" and False for k in keys)
    by = {r["actor"]: r for r in ra["rows"]}
    assert by["rev@x"]["human"] == 1 and by["rev@x"]["findings"] == 1
    print(
        "  25 human findings: counted in findings+human, NOT in cards/avg; withdrawn "
        "excluded; caveat fires; actor axis buckets on email OK"
    )


def test_wave3_withdrawn_excluded_from_vendor_scorecard():  # Wave 3 §6.6.5 (vendor side)
    from datetime import datetime as _dt

    cid = _mk_customer("VwCust")
    pid = _mk_project("VwProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    vid = _mk_vendor("VwVendor")
    # one LIVE vendor-attributed finding + one WITHDRAWN one on the same vendor.
    _mk_lqa(doc, seg_id=segs[0], severity="major", vendor_id=vid, scoring_id="vc1", score=70.0)
    _mk_lqa(
        doc,
        seg_id=segs[0],
        severity="critical",
        vendor_id=vid,
        source="human",
        judge=None,
        scoring_id=None,
        actor="rev@x",
        withdrawn_at=_dt(2026, 7, 6, 9, 0, 0),
    )
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="vendor", customer_id=cid), user=None
        )
    vrow = [r for r in rep["rows"] if str(vid) in r["vendor"]][0]  # noqa: RUF015
    assert vrow["lqa_findings"] == 1, ("the withdrawn finding must not score the vendor", vrow)
    print("  25b vendor scorecard: a withdrawn finding is excluded OK")


def test_wave2_unsupported_axis_degrades_loudly():  # 27
    cid = _mk_customer("DgCust")
    pid = _mk_project("DgProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    _mk_lqa(doc, seg_id=segs[0])
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="provider", customer_id=cid), user=None
        )
    assert rep["group_by"] == "none", "an unsupported axis degrades to none"
    assert any("not available" in c and "provider" in c for c in rep["caveats"]), rep
    csv_text = reports.to_csv(rep)
    assert "not available" in csv_text and csv_text.index("TOTALS") < csv_text.index(
        "not available"
    )
    client = TestClient(app)
    au = security.create_user("dg-api@x", "DG", Role.ADMIN, FIXTURE_PW)
    _row, raw = apikeys.create_key(au.id, "dg", scopes="api docs")
    r = client.get(
        f"/api/reports/run?kind=qa&group_by=provider&customer_id={cid}",
        headers={"X-Loc-Token": raw},
    )
    assert r.status_code == 200 and any("not available" in c for c in r.json()["caveats"])
    print("  27 unsupported axis degrades LOUDLY on caption/CSV/api OK")


def test_wave2_scoping_new_kinds():  # 28
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    try:
        cA = _mk_customer("ScopeA")
        pA = _mk_project("ScopeAP", customer_id=cA)
        cB = _mk_customer("ScopeB")
        pB = _mk_project("ScopeBP", customer_id=cB)
        dA, sA = _mk_doc_segs(pA, 1)
        dB, sB = _mk_doc_segs(pB, 1)
        _mk_lqa(dA, seg_id=sA[0], scoring_id="sa")
        _mk_lqa(dB, seg_id=sB[0], scoring_id="sb")
        _mk_pe(dA, sA[0])
        _mk_pe(dB, sB[0])
        boundA = security.create_user(
            "scope-a@x", "SA", Role.TRANSLATOR, FIXTURE_PW, customer_id=cA
        )
        with Session(engine) as s:
            uu = s.get(User, boundA.id)
            qa = reports.run_report(s, reports.ReportQuery(kind="qa", group_by="project"), user=uu)
            pe = reports.run_report(
                s, reports.ReportQuery(kind="postedit", group_by="project"), user=uu
            )
            # a FOREIGN --customer clamp cannot reach B's data
            qf = reports.run_report(
                s, reports.ReportQuery(kind="qa", group_by="project", customer_id=cB), user=uu
            )
        assert any("ScopeAP" in r["project"] for r in qa["rows"]), qa
        assert all("ScopeBP" not in r["project"] for r in qa["rows"]), qa
        assert all("ScopeBP" not in r["project"] for r in pe["rows"]), pe
        assert all("ScopeBP" not in r["project"] for r in qf["rows"]), ("foreign clamp", qf)
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
        settings.invalidate_cache()
    print("  28 scoping: qa/postedit confined to the caller's projects; foreign clamp OK")


def test_wave2_date_window_new_kinds():  # 29
    cid = _mk_customer("DwCust")
    pid = _mk_project("DwProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    jan = datetime(2026, 1, 15, 12, 0, 0)
    mar = datetime(2026, 3, 15, 12, 0, 0)
    _mk_lqa(doc, seg_id=segs[0], scoring_id="dj", when=jan)
    _mk_lqa(doc, seg_id=segs[0], scoring_id="dm", when=mar)
    with Session(engine) as s:
        rep = reports.run_report(
            s,
            reports.ReportQuery(
                kind="qa",
                group_by="none",
                customer_id=cid,
                date_from=date(2026, 3, 1),
                date_to=date(2026, 3, 31),
            ),
            user=None,
        )
    assert rep["rows"][0]["findings"] == 1, ("only the March finding", rep)
    # postedit: a segment edited in Jan then Mar reports WHOLLY in the later window
    pid2 = _mk_project("DwProj2", customer_id=cid)
    doc2, segs2 = _mk_doc_segs(pid2, 1)
    _mk_pe(
        doc2,
        segs2[0],
        episode_seq=1,
        baseline_kind="mt",
        dist=(2, 1, 20.0),
        cum=(2, 1, 20.0),
        when=jan,
    )
    _mk_pe(
        doc2,
        segs2[0],
        episode_seq=2,
        actor="e@x",
        baseline_kind="human",
        dist=(3, 1, 30.0),
        cum=(5, 2, 50.0),
        when=mar,
    )
    with Session(engine) as s:
        rmar = reports.run_report(
            s,
            reports.ReportQuery(
                kind="postedit",
                group_by="none",
                project_id=pid2,
                date_from=date(2026, 3, 1),
                date_to=date(2026, 3, 31),
            ),
            user=None,
        )
        rjan = reports.run_report(
            s,
            reports.ReportQuery(
                kind="postedit",
                group_by="none",
                project_id=pid2,
                date_from=date(2026, 1, 1),
                date_to=date(2026, 1, 31),
            ),
            user=None,
        )
    assert rmar["rows"][0]["distance_char"] == 5, ("cumulative in March", rmar)
    assert not rjan["rows"], "the segment reports wholly in the later window, not January"
    print("  29 date window: qa created_at; postedit reports in the latest-edit window OK")


def test_wave2_vendor_scorecard_quality():  # 30 (VR-5)
    from app.models import VendorDispatch, VendorDispatchStatus

    cid = _mk_customer("VqCust")
    pid = _mk_project("VqProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    vid = _mk_vendor("QualityLSP")
    when = datetime(2026, 7, 4, 12, 0, 0)
    _wf, run_id = _mk_workflow_run(
        pid, doc, [{"provider": "claude", "cost_usd": 1.0}], when=when, wf_name="VqWF"
    )
    with Session(engine) as s:
        s.add(
            VendorDispatch(
                vendor_id=vid,
                workflow_run_id=run_id,
                document_id=doc,
                target_locale="fr-FR",
                word_count=100,
                estimate_cost_usd=50.0,
                actual_cost_usd=48.0,
                dispatched_at=when,
                status=VendorDispatchStatus.RETURNED,
                expires_at=datetime(2026, 7, 10),
                returned_at=datetime(2026, 7, 8),
            )
        )
        s.commit()
    _mk_lqa(
        doc,
        seg_id=segs[0],
        severity="critical",
        score=60.0,
        scoring_id="vq1",
        vendor_id=vid,
        when=when,
    )
    _mk_lqa(
        doc,
        seg_id=segs[0],
        severity="minor",
        score=60.0,
        scoring_id="vq1",
        vendor_id=vid,
        when=when,
    )
    _mk_lqa(
        doc,
        seg_id=segs[0],
        severity="major",
        score=60.0,
        scoring_id="vq1",
        vendor_id=vid,
        when=when,
    )
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="vendor", customer_id=cid), user=None
        )
    cols = rep["columns"]
    for c in ("lqa_findings", "lqa_major_critical", "avg_lqa_score"):
        assert c in cols, cols
    assert (
        cols.index("returned_on_time")
        < cols.index("lqa_findings")
        < cols.index("estimate_cost_usd")
    ), "quality cols after legs, before money"
    # the admin-only money group is LAST, in order: returned estimate/actual, then the
    # in-flight open_estimate and the approved-quote column (2026-08-19 additions).
    assert cols[-4:] == [
        "estimate_cost_usd",
        "actual_cost_usd",
        "open_estimate_usd",
        "quoted_cost_usd",
    ], cols
    row = _row_by_key(rep, f"{vid}: QualityLSP")
    assert row["lqa_findings"] == 3 and row["lqa_major_critical"] == 2, row
    assert row["avg_lqa_score"] == 60.0, ("one distinct card at 60", row)
    # a vendor with legs but NO LQA reads 0, 0, "" (never 0.0)
    v2 = _mk_vendor("NoQualityLSP")
    with Session(engine) as s:
        s.add(
            VendorDispatch(
                vendor_id=v2,
                workflow_run_id=run_id,
                document_id=doc,
                target_locale="fr-FR",
                word_count=10,
                estimate_cost_usd=5.0,
                actual_cost_usd=None,
                dispatched_at=when,
                status=VendorDispatchStatus.DISPATCHED,
                expires_at=datetime(2026, 7, 10),
            )
        )
        s.commit()
        rep2 = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="vendor", customer_id=cid), user=None
        )
    row2 = _row_by_key(rep2, f"{v2}: NoQualityLSP")
    assert row2["lqa_findings"] == 0 and row2["avg_lqa_score"] == "", row2
    # the Wave 1 leg assertions are unchanged for the quality vendor
    assert row["legs"] == 1 and row["word_count"] == 100 and row["returned_on_time"] == 1
    print("  30 vendor scorecard quality cols: appended last, correct, blank-not-0 OK")


def test_wave2_surface_parity():  # 16
    cid = _mk_customer("W2ParCust")
    pid = _mk_project("W2ParProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    _mk_lqa(doc, seg_id=segs[0], dimension="Accuracy/Omission", severity="major", scoring_id="p1")
    _mk_pe(doc, segs[0], baseline_kind="mt", dist=(3, 1, 25.0), cum=(3, 1, 25.0))
    for kind in ("qa", "postedit"):
        with Session(engine) as s:
            svc = reports.run_report(
                s, reports.ReportQuery(kind=kind, group_by="project", customer_id=cid), user=None
            )
        from app.cli.__main__ import main as cli_main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(
                ["--json", "report", "run", kind, "--group-by", "project", "--customer", str(cid)]
            )
        assert rc == 0
        cli_out = _json.loads(buf.getvalue().strip().splitlines()[-1])
        assert cli_out["rows"] == svc["rows"], (kind, "CLI == service")
        client = TestClient(app)
        au = security.create_user(f"w2p-{kind}@x", "W", Role.ADMIN, FIXTURE_PW)
        _row, raw = apikeys.create_key(au.id, f"w2p{kind}", scopes="api docs")
        r = client.get(
            f"/api/reports/run?kind={kind}&group_by=project&customer_id={cid}",
            headers={"X-Loc-Token": raw},
        )
        assert r.status_code == 200 and r.json()["rows"] == svc["rows"], (kind, r.text)
        r = client.get(f"/reports/run?kind={kind}&group_by=project&customer_id={cid}&format=csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    print("  16 four-surface parity for qa + postedit OK")


def test_wave2_human_origin_disclosure():  # HIGH-1
    """A segment with NO machine (mt/tm) origin is excluded from the cumulative distance
    (Train A NULLs cum_* for every non-mt/tm origin, human included). That exclusion MUST
    be disclosed by the no-machine-origin caveat, keyed to the real predicate (cum is
    None), and must NOT fire spuriously for a clean mt-origin segment."""
    cid = _mk_customer("HoCust")
    pid = _mk_project("HoProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    _mk_pe(
        doc,
        segs[0],
        episode_seq=1,
        actor="h@x",
        actor_kind="human",
        baseline_kind="human",
        baseline_chars=12,
        final_chars=15,
        dist=(4, 2, 30.0),
        cum=None,
        edit_count=1,
    )
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="postedit", group_by="project", customer_id=cid), user=None
        )
    row = rep["rows"][0]
    assert row["segments_edited"] == 0 and row["distance_char"] == 0, row
    assert any("no machine (mt/tm) origin" in c for c in rep["caveats"]), (
        "human-origin exclusion must be disclosed",
        rep["caveats"],
    )
    # a clean mt-origin segment: neither the no-machine nor the no-baseline caveat fires
    pid2 = _mk_project("HoProj2", customer_id=cid)
    doc2, segs2 = _mk_doc_segs(pid2, 1)
    _mk_pe(doc2, segs2[0], baseline_kind="mt", dist=(3, 1, 25.0), cum=(3, 1, 25.0))
    with Session(engine) as s:
        rep2 = reports.run_report(
            s, reports.ReportQuery(kind="postedit", group_by="project", project_id=pid2), user=None
        )
    assert rep2["rows"][0]["segments_edited"] == 1
    assert not any("no machine" in c for c in rep2["caveats"]), (
        "no spurious no-machine caveat for a clean mt segment",
        rep2["caveats"],
    )
    assert not any("no baseline" in c.lower() for c in rep2["caveats"]), (
        "no spurious no-baseline caveat for a clean mt segment",
        rep2["caveats"],
    )
    print(
        "  HIGH-1 human-origin discloses (no-machine-origin caveat); clean mt no spurious fire OK"
    )


def test_wave2_blank_dimension_bucket():  # MED-2
    cid = _mk_customer("BdCust")
    pid = _mk_project("BdProj", customer_id=cid)
    doc, segs = _mk_doc_segs(pid, 1)
    _mk_lqa(doc, seg_id=segs[0], dimension="", severity="minor", scoring_id="bd1")
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="dimension", customer_id=cid), user=None
        )
    keys = {r["dimension"] for r in rep["rows"]}
    assert "(untracked dimension)" in keys, ("a blank dimension buckets as untracked", keys)
    assert "Other/" not in keys, "a blank dimension must not fabricate the 'Other/' bucket"
    print("  MED-2 blank dimension -> (untracked dimension), no fabricated 'Other/' OK")


def test_wave2_docless_lqa_untracked():  # MED-5
    with Session(engine) as s:
        s.add(
            LQAResult(
                document_id=None,
                segment_id=None,
                dimension="Accuracy/Omission",
                severity="major",
                note="n",
                score=50.0,
                target_locale="de-DE",
                finding_source="llm",
                judge="x/y",
                scoring_id="docless-med5",
                created_at=datetime(2026, 7, 4, 12, 0, 0),
            )
        )
        s.commit()
    # the unrestricted operator (user=None) sees it under (untracked project)...
    with Session(engine) as s:
        rep = reports.run_report(s, reports.ReportQuery(kind="qa", group_by="project"), user=None)
        repl = reports.run_report(s, reports.ReportQuery(kind="qa", group_by="locale"), user=None)
    assert "(untracked project)" in {r["project"] for r in rep["rows"]}, (
        "a doc-less finding must surface for the trusted operator, not vanish"
    )
    assert any("no document" in c for c in rep["caveats"]), (
        "the doc-less absorption must be named in a caveat when the arm is active"
    )
    assert any(r["locale"] == "de-DE" for r in repl["rows"]), (
        "on the locale axis the doc-less row buckets by its own locale"
    )
    # ...but an operator whose report is NARROWED by --project must NOT absorb the
    # install-wide doc-less finding (the gate's silent-merge probe).
    cN = _mk_customer("DlNarrowCust")
    pN = _mk_project("DlNarrowProj", customer_id=cN)
    dN, sN = _mk_doc_segs(pN, 1)
    _mk_lqa(dN, seg_id=sN[0], severity="minor", score=90.0, scoring_id="dl-narrow")
    with Session(engine) as s:
        repn = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="none", project_id=pN), user=None
        )
    nrow = repn["rows"][0]
    assert nrow["findings"] == 1 and nrow["cards"] == 1 and nrow["avg_card_score"] == 90.0, (
        "a --project-narrowed operator report must NOT absorb doc-less findings",
        nrow,
    )
    assert "(untracked project)" not in {r.get("scope") for r in repn["rows"]}
    assert not any("no document" in c for c in repn["caveats"])
    # a --customer-narrowed operator report is likewise clean
    with Session(engine) as s:
        repc = reports.run_report(
            s, reports.ReportQuery(kind="qa", group_by="none", customer_id=cN), user=None
        )
    assert repc["rows"][0]["findings"] == 1, ("--customer narrowed must not absorb", repc)
    # ...and a scope-confined caller NEVER sees it (tenant safety)
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    try:
        c2 = _mk_customer("DlCust")
        p2 = _mk_project("DlProj", customer_id=c2)
        d2, s2 = _mk_doc_segs(p2, 1)
        _mk_lqa(d2, seg_id=s2[0], scoring_id="dl2")
        bound = security.create_user(
            "dl-bound@x", "DB", Role.TRANSLATOR, FIXTURE_PW, customer_id=c2
        )
        with Session(engine) as s:
            uu = s.get(User, bound.id)
            repb = reports.run_report(
                s, reports.ReportQuery(kind="qa", group_by="project"), user=uu
            )
        assert all(r["project"] != "(untracked project)" for r in repb["rows"]), (
            "a scope-confined caller must NEVER see the doc-less row (tenant safety)"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
        settings.invalidate_cache()
    print(
        "  MED-5 doc-less LQA: untracked for operator, own locale on locale axis, hidden from scoped OK"  # noqa: E501
    )


def test_wave2_report_query_shape_no_blobs():  # MED-1 (F1 query-shape lock)
    for tup in (reports._QA_QUERY_COLS, reports._PE_QUERY_COLS, reports._VENDOR_LQA_COLS):
        keys = [c.key for c in tup]
        for blob in reports._REPORT_FORBIDDEN_BLOB_COLS:
            assert blob not in keys, (
                f"a report reader must never project the {blob!r} TEXT blob (F1)",
                keys,
            )
    # WAVE 3 §6.6 / test 24b (HIGH-3): the qa reader deliberately WIDENED to carry `actor`
    # (the human-finding actor axis) and `withdrawn_at` (the retraction exclusion); the
    # vendor reader carries `withdrawn_at` only. Asserted positively so the widening is a
    # decision on the record, not a silently-grown projection — and `note` stays out.
    qa_keys = [c.key for c in reports._QA_QUERY_COLS]
    assert "actor" in qa_keys and "withdrawn_at" in qa_keys, (
        "Wave 3: _QA_QUERY_COLS must project actor + withdrawn_at",
        qa_keys,
    )
    assert "note" not in qa_keys, "the qa reader must still never load the note blob"
    vlqa_keys = [c.key for c in reports._VENDOR_LQA_COLS]
    assert "withdrawn_at" in vlqa_keys, (
        "Wave 3: _VENDOR_LQA_COLS must project withdrawn_at",
        vlqa_keys,
    )
    assert "note" not in vlqa_keys and "actor" not in vlqa_keys, (
        "the vendor lqa reader needs neither note nor actor"
    )
    print("  MED-1 query-shape: no note blob; Wave 3 actor+withdrawn_at present OK")


class _SqlLog:
    """Capture every SQL statement emitted on `engine` while active (the A5 query-shape
    probe pattern, mirrors verify_settings_propagation._TxnLog)."""

    def __init__(self):
        self.stmts = []

    def _hook(self, conn, cursor, statement, params, ctx, many):
        self.stmts.append(" ".join(statement.split()).lower())

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hook)
        return False


def test_issue53_windowed_job_scan_and_projected_segments():
    # Issue #53 (round-2 A5): the cost/volume reader must (1) push the date window into the
    # Job scan (was `select(Job)` with NO WHERE -> every job + its payload/result blobs) and
    # (2) project only the columns _segment_metrics needs (was full `select(Segment)`). This
    # pin proves BOTH the query SHAPE (WHERE on the job scan; no blob columns in the segment
    # scan) AND that output stays correct: in-window jobs on BOTH boundaries plus the
    # finished_at-NULL created_at fallback all count, out-of-window jobs do not, and the
    # multi-doc segment volume sums are exact. If the SQL superset ever wrongly dropped an
    # in-window row the token/segment totals would come up short.
    cid = _mk_customer("I53Cust")
    pid = _mk_project("I53Proj", customer_id=cid)
    # Jobs: boundaries + inside COUNT; before/after do NOT.
    _mk_job(pid, provider="claude", ti=1000, to=0, when=datetime(2026, 6, 30))  # before -> out
    _mk_job(pid, provider="claude", ti=10, to=0, when=datetime(2026, 7, 1))  # lower bnd -> in
    _mk_job(pid, provider="claude", ti=20, to=0, when=datetime(2026, 7, 15))  # inside -> in
    _mk_job(pid, provider="claude", ti=40, to=0, when=datetime(2026, 7, 31))  # upper bnd -> in
    _mk_job(pid, provider="claude", ti=5000, to=0, when=datetime(2026, 8, 5))  # after -> out
    # finished_at NULL: the reader buckets on COALESCE(finished_at, created_at) — the SQL
    # superset MUST fall back to created_at or these rows would be silently dropped.
    with Session(engine) as s:
        base = {
            "translated": 1,
            "provider_used": {"claude": 1},
            "tokens_out": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
        }
        s.add(
            Job(
                kind="translate_document",
                status=JobStatus.COMPLETED,
                payload={"project_id": pid},
                result={**base, "tokens_in": 5},
                finished_at=None,
                created_at=datetime(2026, 7, 10),
            )
        )  # created in-window -> in
        s.add(
            Job(
                kind="translate_document",
                status=JobStatus.COMPLETED,
                payload={"project_id": pid},
                result={**base, "tokens_in": 2000},
                finished_at=None,
                created_at=datetime(2026, 6, 1),
            )
        )  # created out-window -> out
        s.commit()
    # Segments across TWO documents: one translated + one untranslated per doc.
    _add_segments(pid, [("hello world", "bonjour"), ("foo", "")])  # 1 xlated: 11c / 2w
    _add_segments(pid, [("aaa bbb ccc", "x"), ("only source", "")])  # 1 xlated: 11c / 3w

    win = dict(date_from=date(2026, 7, 1), date_to=date(2026, 7, 31))  # noqa: C408
    with _SqlLog() as log, Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(group_by="none", customer_id=cid, **win), user=None
        )
    row = rep["rows"][0]
    # --- OUTPUT correctness (the byte-identical / no-row-dropped proof) ---
    assert row["jobs"] == 4, (
        "in-window jobs (2 boundaries + inside + null-finished "  # noqa: UP031
        "fallback), out excluded: %d" % row["jobs"]
    )
    assert row["tokens_in"] == 75, (
        "token sum 10+20+40+5, out-of-window excluded: %d" % row["tokens_in"]  # noqa: UP031
    )
    assert row["segments"] == 2, "two TRANSLATED segments across two docs: %d" % row["segments"]  # noqa: UP031
    assert row["chars"] == 22, "source-char sum 11+11: %d" % row["chars"]  # noqa: UP031
    assert row["words_derived"] == 5, "derived source words 2+3: %d" % row["words_derived"]  # noqa: UP031

    # --- QUERY SHAPE: the Job scan carries a WHERE (window pushed into SQL) ---
    job_scans = [st for st in log.stmts if "from job" in st and st.startswith("select")]
    assert job_scans, "no job scan captured"
    windowed = [
        st for st in job_scans if " where " in st and ("finished_at" in st or "created_at" in st)
    ]
    assert windowed, f"the Job scan must carry a date WHERE (issue #53 part 1); scans={job_scans!r}"
    # And the OLD unfiltered full-table scan (SELECT ... FROM job with no WHERE) is gone.
    assert not any(" from job " in (" " + st + " ") and " where " not in st for st in job_scans), (
        f"an unfiltered full Job-table scan is still emitted: {job_scans!r}"
    )

    # --- QUERY SHAPE: the segment scan projects columns, not full rows with blobs ---
    seg_scans = [st for st in log.stmts if "from segment" in st and st.startswith("select")]
    assert seg_scans, "no segment scan captured"
    for st in seg_scans:
        for blob in ("qa_flags", "inline_tags", "segment.meta", "mt_confidence"):
            assert blob not in st, (
                f"issue #53 part 2: the segment scan must not load {blob!r}: {st}"
            )
        assert "source_text" in st and "target_text" in st, (
            f"the projected scan still needs source_text + target_text: {st}"
        )

    # The locale axis (_segment_metrics_by_locale) is projected too.
    with _SqlLog() as log2, Session(engine) as s:
        reports.run_report(
            s, reports.ReportQuery(group_by="locale", customer_id=cid, **win), user=None
        )
    loc_seg_scans = [st for st in log2.stmts if "from segment" in st and st.startswith("select")]
    assert loc_seg_scans, "no segment scan on the locale axis"
    for st in loc_seg_scans:
        assert "qa_flags" not in st and "inline_tags" not in st, (
            f"locale-axis segment scan must be projected too: {st}"
        )
    print(
        "  issue #53: job scan windowed in SQL (WHERE present, no unfiltered full scan), "
        "segment scans projected (no blobs); boundary + null-finished rows counted, "
        "volume sums exact OK"
    )


def main():
    init_db()
    # seed_initial() creates admin/linguist/user1 AND turns the login wall ON. The
    # verify model (and the trusted in-process CLI's user=None path) expects auth OFF
    # by default, so reset the wall here; the scoping tests turn it back ON locally
    # for exactly their duration. This keeps the default surface single-tenant, like
    # every other verify_*.py, while still giving us seeded users to bind/scope.
    assert security.seed_initial() is True
    settings.set_many(
        {"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"}
    )
    settings.invalidate_cache()

    test_cost_math()
    test_attribution_via_document_id()
    test_attribution_connector_job()
    test_malformed_result_row_does_not_crash()
    test_grouping()
    test_grouping_by_workflow()
    test_workflow_run_only_spend_counts()
    test_mixed_job_and_run_no_double_count()
    test_run_spend_scoped_to_tenant()
    test_job_run_bucket_merge_invariant()
    test_untracked_run_and_volume_axes()
    test_provider_filter_run_count_divergence()
    test_provider_axis_exact_per_step_spend()
    test_volume_lands_on_real_project_not_untracked()
    test_caveats_surface_on_csv_and_api()
    test_human_cost_column_is_admin_only()
    test_cost_report_kind_is_manager_gated()
    test_project_leverage_7band()
    test_project_leverage_over_150_docs_page_api_identity()
    test_locale_axis()
    test_vendor_axis()
    test_vendor_money_counts_only_returned_legs()
    test_vendor_open_estimate_and_quoted_columns()
    test_csv_and_totals_row_rounding_agree()
    test_date_range()
    test_scope_restricted_user()
    test_scope_customer_bound()
    test_scope_org_isolation()
    test_csv_roundtrip_and_filename()
    test_surface_parity()
    test_derived_words_labeled()
    test_tm_leverage_bands()
    test_tm_leverage_no_tm_match_label_is_honest()
    test_reports_page_still_renders()
    # Wave 2 — qa + postedit kinds (SPEC-wave2 §4 / §9 Train B)
    test_wave2_column_sets()
    test_wave2_empty_report_ordering()
    test_wave2_key_column_new_axes()
    test_wave2_dimension_axis()
    test_wave2_actor_axis_rbac()
    test_wave2_postedit_measure_rule()
    test_wave2_honest_buckets()
    test_wave2_null_not_zero_and_totals()
    test_wave2_cards_and_avg_card_score()
    test_wave3_human_findings_reports()
    test_wave3_withdrawn_excluded_from_vendor_scorecard()
    test_wave2_unsupported_axis_degrades_loudly()
    test_wave2_scoping_new_kinds()
    test_wave2_date_window_new_kinds()
    test_wave2_vendor_scorecard_quality()
    test_wave2_surface_parity()
    # Fix-round regression locks (Train B gate 2026-08-19)
    test_wave2_human_origin_disclosure()
    test_wave2_blank_dimension_bucket()
    test_wave2_docless_lqa_untracked()
    test_wave2_report_query_shape_no_blobs()
    test_issue53_windowed_job_scan_and_projected_segments()
    print("REPORTS OK")


if __name__ == "__main__":
    main()
