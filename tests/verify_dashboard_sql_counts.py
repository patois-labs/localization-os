"""#514: the dashboard, the project list and /reports count in SQL, not by loading rows.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked).

  equality    on seeded data (one project with more documents than the 150-id IN chunk, an
              internal key document, runs, step runs, findings, locales, TM, full-text TM and
              glossary rows) the SQL results equal a straightforward load-everything reference
              for BOTH an unrestricted and a restricted (visible_ids) caller:
              _dashboard_data kpis / status / run_status / recent runs / summary,
              _project_rows (segments, approved_pct, runs) and the /reports aggregate
              (_dashboard_reports_data: status, provider usage, severities, avg LQA, languages,
              run status, TM / full-text / glossary totals, segment total, leverage bands).
  no row load an ORM `loaded_as_persistent` listener proves NO Segment, SegmentTM, FullTextTM,
              GlossaryEntry, StepRun or LQAResult object is hydrated by those calls, and at
              most 8 WorkflowRun rows (the "recent runs" list).
  chunking    the project has 170 documents (> 150) so the IN() is chunked; with SQLite's
              bound-parameter limit lowered the old unchunked IN would fail.

Final line: "DASHBOARD SQL COUNTS OK".
"""

import os
import sys
from datetime import timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_dashsql_")

from sqlalchemy import event
from sqlalchemy.orm import Session as OrmSession
from sqlmodel import Session, select

from app.db import engine, init_db
from app.kernel import access, tm
from app.models import (
    Document,
    FullTextTM,
    GlossaryEntry,
    LQAResult,
    Project,
    Segment,
    SegmentTM,
    StepRun,
    TargetLocale,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    _utcnow,
)
from app.services import settings
from app.web import home
from app.web.queries import _dashboard_data, _project_rows

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_FAILS = []


def _check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


# --- seed ---------------------------------------------------------------------------------
STATUSES = ["new", "mt", "edited", "approved"]
PROVIDERS = ["mock", "claude", None, "", "mock"]
SEVS = ["major", "minor", "critical", ""]
now = _utcnow()
with Session(engine) as s:
    big = Project(name="Big", source_locale="en-US")
    small = Project(name="Small", source_locale="en-US")
    hidden = Project(name="Hidden", source_locale="en-US")
    s.add_all([big, small, hidden])
    s.commit()
    for p in (big, small, hidden):
        s.refresh(p)
    for p, locs in ((big, ("de-DE", "fr-FR")), (small, ("de-DE",)), (hidden, ("es-ES",))):
        for loc in locs:
            s.add(TargetLocale(project_id=p.id, locale=loc))
    wf = Workflow(name="wf")
    s.add(wf)
    s.commit()
    s.refresh(wf)
    step = WorkflowStep(workflow_id=wf.id, order_index=0, name="t")
    s.add(step)
    s.commit()
    s.refresh(step)

    docs = []
    for i in range(170):  # past the 150-id IN chunk
        docs.append(Document(project_id=big.id, filename=f"big{i}.docx"))
    for i in range(3):
        docs.append(Document(project_id=small.id, filename=f"small{i}.docx"))
    for i in range(2):
        docs.append(Document(project_id=hidden.id, filename=f"hid{i}.docx"))
    docs.append(Document(project_id=big.id, filename="keys-internal", fmt="keys"))
    s.add_all(docs)
    s.commit()
    for d in docs:
        s.refresh(d)
    n = 0
    for d in docs:
        for k in range(2 if d.fmt != "keys" else 3):
            n += 1
            st = STATUSES[n % 4]
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="de-DE",
                    order_index=k,
                    source_text=f"s{n}",
                    target_text="" if n % 5 == 0 else f"t{n}",
                    status=st,
                    tm_match_pct=(None, 100, 85, None, 100)[n % 5],
                )
            )
    s.commit()
    run_docs = [*[d for d in docs if d.fmt != "keys"][:60:5], docs[170], docs[173]]
    run_rows = []
    for i, d in enumerate(run_docs):
        r = WorkflowRun(
            workflow_id=wf.id,
            document_id=d.id,
            target_locale="de-DE",
            status=("completed", "paused", "failed", "running")[i % 4],
            started_at=now - timedelta(minutes=i * 7),
        )
        s.add(r)
        run_rows.append(r)
    s.commit()
    for i, r in enumerate(run_rows):
        s.refresh(r)
        for j in range(2):
            s.add(
                StepRun(
                    workflow_run_id=r.id,
                    step_id=step.id,
                    provider_used=PROVIDERS[(i + j) % len(PROVIDERS)],
                )
            )
    for i, d in enumerate(docs[:40] + docs[170:174]):
        s.add(
            LQAResult(
                document_id=d.id,
                severity=SEVS[i % 4],
                score=None if i % 6 == 0 else float(60 + (i * 7) % 41),
            )
        )
    for i in range(25):
        for p in (big, small, hidden):
            s.add(
                SegmentTM(
                    project_id=p.id,
                    source_locale="en-US",
                    target_locale="de-DE",
                    source_text=f"a{i}",
                    target_text=f"b{i}",
                    source_hash=f"h{p.id}-{i}",
                )
            )
            if i % 2:
                s.add(
                    FullTextTM(
                        project_id=p.id,
                        source_locale="en-US",
                        target_locale="de-DE",
                        source_hash=f"f{p.id}-{i}",
                        source_text=f"a{i}",
                        target_text=f"b{i}",
                    )
                )
            if i % 3 == 0:
                s.add(
                    GlossaryEntry(
                        project_id=p.id,
                        source_locale="en-US",
                        target_locale="de-DE",
                        source_term=f"t{i}",
                    )
                )
    s.add(
        SegmentTM(
            project_id=None,
            source_locale="en-US",
            target_locale="de-DE",
            source_text="x",
            target_text="y",
            source_hash="hx",
        )
    )
    s.add(
        GlossaryEntry(
            project_id=None, source_locale="en-US", target_locale="de-DE", source_term="orphan"
        )
    )
    s.commit()
    VISIBLE = [big.id, small.id]
    HIDDEN_ID = hidden.id


# --- load-everything references (the pre-#514 logic, kept here as the oracle) ----------------
def ref_dashboard(s, visible):
    restricted = visible is not None
    dq = select(Document).where(Document.fmt != "keys")
    if restricted:
        dq = dq.where(Document.project_id.in_(visible))
    documents = s.exec(dq).all()
    doc_ids = [d.id for d in documents]
    segs = []
    for i in range(0, len(doc_ids), 100):
        segs += s.exec(select(Segment).where(Segment.document_id.in_(doc_ids[i : i + 100]))).all()
    status = {}
    for g in segs:
        status[g.status] = status.get(g.status, 0) + 1
    runs = s.exec(select(WorkflowRun).order_by(WorkflowRun.started_at.desc())).all()
    if restricted:
        keep = set(doc_ids)
        runs = [r for r in runs if r.document_id in keep]
    run_status = {}
    for r in runs:
        run_status[r.status] = run_status.get(r.status, 0) + 1
    tq, fq = select(SegmentTM), select(FullTextTM)
    if restricted:
        tq = tq.where(SegmentTM.project_id.in_(visible))
        fq = fq.where(FullTextTM.project_id.in_(visible))
    approved = status.get("approved", 0)
    return {
        "documents": len(documents),
        "segments": len(segs),
        "approved_pct": round(100 * approved / len(segs)) if segs else 0,
        "runs": len(runs),
        "tm": len(s.exec(tq).all()) + len(s.exec(fq).all()),
        "status": status,
        "run_status": run_status,
        "recent": [r.id for r in runs[:8]],
    }


def ref_reports(s, visible):
    dq = select(Document)
    if visible is not None:
        dq = dq.where(Document.project_id.in_(visible))
    doc_ids = [d.id for d in s.exec(dq).all()]
    segs = []
    for i in range(0, len(doc_ids), 100):
        segs += s.exec(select(Segment).where(Segment.document_id.in_(doc_ids[i : i + 100]))).all()
    status = {}
    for g in segs:
        status[g.status] = status.get(g.status, 0) + 1
    runs = s.exec(select(WorkflowRun)).all()
    if visible is not None:
        keep = set(doc_ids)
        runs = [r for r in runs if r.document_id in keep]
    run_ids = {r.id for r in runs}
    step_runs = [
        x for x in s.exec(select(StepRun)).all() if visible is None or x.workflow_run_id in run_ids
    ]
    lqa = [
        x
        for x in s.exec(select(LQAResult)).all()
        if visible is None or x.document_id in set(doc_ids)
    ]
    locs = [
        x for x in s.exec(select(TargetLocale)).all() if visible is None or x.project_id in visible
    ]
    usage = {}
    for sr in step_runs:
        if sr.provider_used:
            usage[sr.provider_used] = usage.get(sr.provider_used, 0) + 1
    sev = {}
    for r in lqa:
        sev[r.severity] = sev.get(r.severity, 0) + 1
    scored = [r.score for r in lqa if r.score is not None]
    lang = {}
    for tl in locs:
        lang[tl.locale] = lang.get(tl.locale, 0) + 1
    rs = {}
    for r in runs:
        rs[r.status] = rs.get(r.status, 0) + 1

    def _n(model, col):
        rows = s.exec(select(model)).all()
        if visible is None:
            return len(rows)
        return len([r for r in rows if getattr(r, col) in visible])

    gloss_rows = s.exec(select(GlossaryEntry)).all()
    if visible is None:
        gloss = len([g for g in gloss_rows if g.glossary_id is None])
    else:
        gloss = len([g for g in gloss_rows if g.project_id in visible])
    return {
        "status": status,
        "provider_usage": usage,
        "sev": sev,
        "avg_lqa": round(sum(scored) / len(scored)) if scored else None,
        "lang": lang,
        "run_status": rs,
        "tm_total": _n(SegmentTM, "project_id"),
        "ft_total": _n(FullTextTM, "project_id"),
        "gloss_total": gloss,
        "segments": len(segs),
        "tm_leverage": tm.leverage_bands(segs),
    }


# --- ORM hydration counter --------------------------------------------------------------------
LOADED = {}


@event.listens_for(OrmSession, "loaded_as_persistent")
def _count_loaded(session, instance):
    k = type(instance).__name__
    LOADED[k] = LOADED.get(k, 0) + 1


FORBIDDEN = ("Segment", "SegmentTM", "FullTextTM", "GlossaryEntry", "StepRun", "LQAResult")


def _no_row_load(label):
    bad = {k: LOADED.get(k, 0) for k in FORBIDDEN if LOADED.get(k, 0)}
    _check(f"{label}: no Segment/TM/glossary/step/finding row hydrated", not bad, str(bad))
    _check(
        f"{label}: at most 8 WorkflowRun rows hydrated",
        LOADED.get("WorkflowRun", 0) <= 8,
        str(LOADED.get("WorkflowRun", 0)),
    )


fake_request = SimpleNamespace(session={}, headers={}, state=SimpleNamespace())

for label, visible in (("unrestricted", None), ("restricted", VISIBLE)):
    with Session(engine) as s:
        want = ref_dashboard(s, visible)
        LOADED.clear()
        got = _dashboard_data(s, visible_ids=visible)
        _no_row_load(f"dashboard {label}")
        k = got["kpis"]
        for key in ("documents", "segments", "approved_pct", "runs", "tm"):
            _check(
                f"dashboard {label}: kpis.{key}", k[key] == want[key], f"{k[key]} vs {want[key]}"
            )
        _check(f"dashboard {label}: segment status", got["status"] == want["status"])
        _check(f"dashboard {label}: run status", got["run_status"] == want["run_status"])
        _check(
            f"dashboard {label}: recent runs",
            [r["id"] for r in got["recent_runs"]] == want["recent"],
        )
        _check(f"dashboard {label}: summary runs", got["summary"]["runs"] == want["runs"])

        want_r = ref_reports(s, visible)
        orig = access.visible_project_ids
        access.visible_project_ids = lambda session, user, _v=visible: _v
        try:
            LOADED.clear()
            got_r = home._dashboard_reports_data(fake_request, s)
        finally:
            access.visible_project_ids = orig
        _no_row_load(f"reports {label}")
        for key, val in want_r.items():
            _check(f"reports {label}: {key}", got_r[key] == val, f"{got_r[key]} vs {val}")

# --- project list rows ------------------------------------------------------------------------
with Session(engine) as s:
    projects = s.exec(select(Project).order_by(Project.id)).all()
    LOADED.clear()
    rows = _project_rows(s, projects, {})
    _no_row_load("project rows")
    by_name = {r["project"].name: r for r in rows}
    for p in projects:
        d_ids = [
            d.id
            for d in s.exec(select(Document).where(Document.project_id == p.id)).all()
            if d.fmt != "keys"
        ]
        segs = []
        for i in range(0, len(d_ids), 100):
            segs += s.exec(select(Segment).where(Segment.document_id.in_(d_ids[i : i + 100]))).all()
        run_n = len([r for r in s.exec(select(WorkflowRun)).all() if r.document_id in set(d_ids)])
        appr = len([x for x in segs if x.status == "approved"])
        row = by_name[p.name]
        _check(f"project row {p.name}: segments", row["segments"] == len(segs))
        _check(
            f"project row {p.name}: approved_pct",
            row["approved_pct"] == (round(100 * appr / len(segs)) if segs else 0),
        )
        _check(f"project row {p.name}: runs", row["runs"] == run_n)
        # #563: the "S segments x L languages" split, from SQL, matches the loaded rows.
        src = {(x.document_id, x.order_index, x.key_id, x.plural_category) for x in segs}
        _check(f"project row {p.name}: source_segments", row["source_segments"] == len(src))
        _check(
            f"project row {p.name}: locale_count",
            row["locale_count"] == len({x.target_locale for x in segs}),
        )

# --- the chunked IN really is bounded: a 170-document project works under a low variable cap ----
from sqlalchemy import text

with Session(engine) as s:
    raw = s.connection().connection
    try:
        raw.setlimit(9, 160)  # sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER (sqlite3 module, 3.11+)
        limited = True
    except Exception:
        limited = False
    if limited:
        ok = True
        try:
            _dashboard_data(s, visible_ids=VISIBLE)
        except Exception as exc:
            ok = False
            print("chunk failure:", exc)
        _check("dashboard works with only 160 bound variables allowed", ok)
        raw.setlimit(9, 32766)
    else:
        print("SKIP variable-limit probe (driver has no setlimit)")
    s.exec(text("select 1"))

if _FAILS:
    print("FAILED:", ", ".join(_FAILS))
    sys.exit(1)
print("DASHBOARD SQL COUNTS OK")
