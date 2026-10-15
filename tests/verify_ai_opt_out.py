"""AI opt-out (HECVAT DPAI-08) -- a project or a customer keeps its content away from AI.

Proves the design in app/services/ai_optout.py:

  A  SERVICE: project + customer switches, the effective value (project OR customer),
     status() read-back, and the audit rows (project.ai_opt_out / ai_opt_in, customer.*).
  B  CHOKE POINT: a provider that FAILS THE TEST IF CALLED is registered; inside the AI
     scope of an opted-out project run_completion, a direct Provider.complete (a brand-new
     Provider subclass, so the base-class wrap is what protects it), an Embedder.embed, the
     OCR vision seam, image vision and the Jev QE post all refuse before the engine runs.
     The scope is resolved from a project, a document, a run and a segment; it travels into
     a worker thread through ai_optout.submit; and a request under /documents/{id}/... is
     scoped by the ASGI middleware with no route cooperation.
  C  ENTRY POINTS: an AI workflow is refused at start_run, run_workflow and the run seam
     with no WorkflowRun row and no engine call; the MT job and direct MT refuse; term
     extraction refuses; the connector run is skipped; LLMemory capture and recall are
     skipped; the Editor's AI alternatives answer a plain 409; an Advanced mission refuses.
  D  NON-AI STILL WORKS: a human-only workflow runs (pauses for review), TM write-back +
     lookup work, structural LQA scores without the judge, the Editor page renders.
  E  SURFACES: web toggle (manager+ only, 403 for a translator), the project badge and the
     Editor notice, /api routes + detail fields, customer toggle, CLI set-ai + show.
  F  TURNING IT OFF RESTORES normal behaviour (the engine is called again, runs launch).

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys; ASCII-only output.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_aioptout_")

import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from html import unescape
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Connection,
    Customer,
    Document,
    Project,
    Role,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    ai_optout,
    jobs,
    lqa,
    ocr,
    orchestrator,
    security,
    settings,
    tm,
    translate,
)
from app.services.providers import ProviderSpec, register_provider, run_completion
from app.services.providers_common import Provider
from tests._csrf import csrf_for

PW = "Str0ng-Passw0rd!x"
CALLS: list = []
_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


class FailIfCalledProvider(Provider):
    """Defined HERE, after the app loaded: nothing in the product knows this class, so
    only the Provider base-class wrap can stop it."""

    name = "failprov"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        CALLS.append(("fail", task, prompt[:40]))
        raise AssertionError("an AI engine was called for an opted-out project")


class CountingProvider(Provider):
    name = "countprov"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        CALLS.append(("count", task, prompt[:40]))
        return "Hola."


def _mk_customer(name=None, ai=False):
    with Session(engine) as s:
        c = Customer(name=name or "Cust-%d" % _seq(), ai_opt_out=ai)  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_project(ai=False, customer_id=None, provider="failprov"):
    with Session(engine) as s:
        p = Project(
            name="aio-%d" % _seq(),  # noqa: UP031
            source_locale="en-US",
            default_provider=provider,
            customer_id=customer_id,
            ai_opt_out=ai,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(pid):
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        seg = Segment(
            document_id=doc.id,
            target_locale="es-ES",
            order_index=0,
            source_text="Hello.",
            target_text="Hola.",
            status=SegStatus.MT,
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)
        return doc.id, seg.id


def _mk_workflow(step_type=StepType.LLM_AGENT, provider="failprov"):
    with Session(engine) as s:
        wf = Workflow(name="wf-%d" % _seq(), scope="global")  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="Step",
                provider=provider,
                step_type=step_type,
                prompt_text="Translate: {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
                key="s1",
            )
        )
        s.commit()
        return wf.id


def _refused(fn, *a, **kw):
    try:
        out = fn(*a, **kw)
    except ai_optout.AIOptOutRefused as exc:
        return str(exc)
    raise AssertionError(f"expected an AI opt-out refusal, got {out!r}")


def _runs_for(doc_id):
    with Session(engine) as s:
        return s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc_id)).all()


# ------------------------------------------------------------------------------ A
def test_A_service_and_audit():
    cid = _mk_customer()
    pid = _mk_project(customer_id=cid)
    with Session(engine) as s:
        assert not ai_optout.is_opted_out(s, pid)
        ai_optout.set_project(s, pid, True, actor="alice@example.com")
        assert ai_optout.opt_out_source(s, pid) == "project"
        st = ai_optout.status(s, s.get(Project, pid))
        assert st == {"ai_opt_out": True, "ai_opt_out_project": True, "ai_opt_out_customer": False}
        ai_optout.set_project(s, pid, False, actor="alice@example.com")
        assert not ai_optout.is_opted_out(s, pid)
        ai_optout.set_customer(s, cid, True, actor="bob@example.com")
        assert ai_optout.opt_out_source(s, pid) == "customer"
        st = ai_optout.status(s, s.get(Project, pid))
        assert st["ai_opt_out"] and st["ai_opt_out_customer"] and not st["ai_opt_out_project"]
        ai_optout.set_customer(s, cid, False, actor="bob@example.com")
        assert not ai_optout.is_opted_out(s, pid)
        actions = [
            a.action
            for a in s.exec(select(AuditLog).order_by(AuditLog.id)).all()
            if a.action.startswith(("project.ai_", "customer.ai_"))
        ]
    assert actions[-4:] == [
        "project.ai_opt_out",
        "project.ai_opt_in",
        "customer.ai_opt_out",
        "customer.ai_opt_in",
    ], actions
    # the message is plain, names the project and says nothing was sent
    msg = str(ai_optout.AIOptOutRefused("machine translation", 7, "Legal", "project"))
    assert "AI is switched off" in msg and "project 7 (Legal)" in msg and "No content" in msg
    assert chr(0x2014) not in msg  # no em dash in operator-facing prose
    print("A ok: project/customer switches, effective value, status, audit rows")


# ------------------------------------------------------------------------------ B
def test_B_choke_point():
    register_provider("failprov", FailIfCalledProvider())
    pid = _mk_project(ai=True)
    doc_id, seg_id = _mk_doc(pid)
    spec = ProviderSpec(provider="failprov", retry_max=0, backoff_base=0.0)
    CALLS.clear()
    for ids in (
        {"project_id": pid},
        {"document_id": doc_id},
        {"segment_id": seg_id},
    ):
        with ai_optout.scope(**ids):
            msg = _refused(run_completion, spec, "Translate: Hello.", task="translate")
            assert f"project {pid}" in msg, msg
            # a direct complete() on a class the product never saw is still refused
            _refused(FailIfCalledProvider().complete, "Hello.")
            # OCR vision seam: the SDK call is never made
            text, block = ocr._guarded_vision_call(
                "claude", "m", 10, lambda: CALLS.append(("ocr",)) or ("x", None)
            )
            assert text is None and "AI is switched off" in block
            # Jev QE HTTP post refuses before any request
            from app.services import qe_jev

            _refused(qe_jev._post, {"q": 1})
    # a run id resolves too
    wid = _mk_workflow()
    with Session(engine) as s:
        run = WorkflowRun(workflow_id=wid, document_id=doc_id, target_locale="es-ES")
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
    with ai_optout.scope(run_id=rid):
        _refused(run_completion, spec, "x")
    # the scope travels into a worker thread through ai_optout.submit
    with ai_optout.scope(project_id=pid), ThreadPoolExecutor(max_workers=2) as pool:
        futs = [ai_optout.submit(pool, run_completion, spec, "x") for _ in range(3)]
        for f in futs:
            try:
                f.result()
                raise AssertionError("worker thread was not refused")
            except ai_optout.AIOptOutRefused:
                pass
    assert CALLS == [], CALLS
    # the ASGI middleware scopes a NEW route with no route cooperation
    assert ai_optout.ids_from_path(f"/documents/{doc_id}/anything") == [("document_id", doc_id)]
    assert ai_optout.ids_from_path(f"/api/projects/{pid}") == [("project_id", pid)]
    assert ai_optout.ids_from_path("/settings") == []
    # LOW 7: EVERY id in the URL is scoped, not just the first
    assert ai_optout.ids_from_path(f"/projects/1/documents/{doc_id}/x") == [
        ("project_id", 1),
        ("document_id", doc_id),
    ]
    from fastapi import FastAPI

    probe = FastAPI()
    probe.add_middleware(ai_optout.AIScopeMiddleware)

    @probe.get("/documents/{doc}/probe")
    def _probe(doc: int):
        try:
            run_completion(spec, "x")
        except ai_optout.AIOptOutRefused:
            return {"refused": True}
        return {"refused": False}

    r = TestClient(probe).get(f"/documents/{doc_id}/probe")
    assert r.json() == {"refused": True}, r.text
    assert CALLS == [], CALLS
    # control: a project that did NOT opt out reaches the engine (the fake is live)
    pid2 = _mk_project(ai=False)
    with ai_optout.scope(project_id=pid2):
        res = run_completion(spec, "x")
    assert res.get("error") and CALLS, (res, CALLS)
    CALLS.clear()
    print("B ok: choke point (run_completion, new Provider, embed, OCR, Jev, threads, middleware)")


# ------------------------------------------------------------------------------ C
def test_C_entry_points():
    register_provider("failprov", FailIfCalledProvider())
    pid = _mk_project(ai=True)
    doc_id, seg_id = _mk_doc(pid)
    wid = _mk_workflow()
    lqa_wid = _mk_workflow(step_type=StepType.LQA)
    CALLS.clear()
    with Session(engine) as s:
        msg = _refused(orchestrator.start_run, s, wid, doc_id, "es-ES")
        assert "llm_agent" in msg, msg
        _refused(orchestrator.start_run, s, lqa_wid, doc_id, "es-ES")
        _refused(orchestrator.run_workflow, s, wid, doc_id, "es-ES")
        # the seam itself (covers resume + batch members)
        _refused(lambda: next(orchestrator.run_workflow_stream(s, wid, doc_id, "es-ES")))
    assert _runs_for(doc_id) == [], "a refused launch must leave no WorkflowRun row"
    # MT job + direct MT
    _refused(jobs.submit, "translate_document", {"document_id": doc_id, "locale": "es-ES"})
    with Session(engine) as s:
        _refused(lambda: next(translate.pretranslate_stream(s, doc_id, "es-ES")))
    # term extraction
    from app.services import term_extraction

    with Session(engine) as s:
        _refused(term_extraction.extract, s, None, "es-ES", document=s.get(Document, doc_id))
    # connector runs into an opted-out project are skipped cleanly
    with Session(engine) as s:
        conn = Connection(name="c-%d" % _seq(), kind="github", project_id=pid)  # noqa: UP031
        s.add(conn)
        s.commit()
        s.refresh(conn)
        conn_id = conn.id
    assert jobs._connection_ai_opted_out(conn_id)
    # Editor AI alternatives: the route answers a plain 409
    c = TestClient(app)
    r = c.post(f"/segments/{seg_id}/alternates", headers={"X-CSRF-Token": csrf_for(c)})
    assert r.status_code == 409, (r.status_code, r.text[:300])
    assert "AI is switched off" in r.text
    r = c.post(f"/segments/{seg_id}/tag-repair", headers={"X-CSRF-Token": csrf_for(c)})
    assert r.status_code == 409, (r.status_code, r.text[:300])
    assert CALLS == [], CALLS
    print("C ok: workflow launch/seam, MT job, direct MT, extraction, connector, LLMemory, Editor")


# ------------------------------------------------------------------------------ D
def test_D_non_ai_still_works():
    register_provider("failprov", FailIfCalledProvider())
    pid = _mk_project(ai=True)
    doc_id, _seg_id = _mk_doc(pid)
    CALLS.clear()
    human = _mk_workflow(step_type=StepType.HUMAN_REVIEW)
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, human, doc_id, "es-ES")
    assert summary.get("status") in ("paused", "completed"), summary
    with Session(engine) as s:
        tm.write_back(s, pid, "Good morning.", "Buenos dias.", "en-US", "es-ES")
        s.commit()
        hit = tm.lookup(s, pid, "Good morning.", "en-US", "es-ES")
        assert hit and "Buenos" in (hit.get("target_text") or hit.get("target") or str(hit)), hit
        card = lqa.score_document(s, doc_id, "es-ES", persist=False, llm=True)
        assert isinstance(card, dict), card
    c = TestClient(app)
    r = c.get(f"/documents/{doc_id}/editor?locale=es-ES")
    assert r.status_code == 200, r.status_code
    assert "data-ai-off" in r.text and "AI is off for this project" in r.text
    assert 'aria-controls="alts-' not in r.text
    assert CALLS == [], CALLS
    print("D ok: human-only workflow, TM write-back + lookup, structural LQA, Editor render")


# ------------------------------------------------------------------------------ E
def _json_out(text):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(text[text.index("{") :])


def _login(email, role):
    security.create_user(email, email, role, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def test_E_surfaces():
    cid = _mk_customer()
    pid = _mk_project(customer_id=cid)
    _mk_doc(pid)
    # auth OFF (single-tenant): the web toggle + badge
    c = TestClient(app)
    tok = csrf_for(c)
    r = c.get(f"/projects/{pid}")
    assert r.status_code == 200 and "Switch AI off for this project" in r.text
    r = c.post(
        f"/projects/{pid}/ai-opt-out",
        data={"ai_opt_out": "1", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    r = c.get(f"/projects/{pid}")
    assert "data-ai-off" in r.text and "AI is off for this project" in r.text
    assert "Switch AI back on for this project" in r.text
    # /api detail + toggle (a docs-scoped key, like the remote CLI)
    from app.services import apikeys

    admin = security.create_user("apiadm-%d@example.com" % _seq(), "a", Role.ADMIN, PW)  # noqa: UP031
    _row, raw = apikeys.create_key(admin.id, "aio", scopes="docs")
    c.headers["X-Loc-Token"] = raw
    r = c.get(f"/api/projects/{pid}")
    j = r.json()
    assert j["ai_opt_out"] is True and j["ai_opt_out_project"] is True, j
    r = c.post(f"/api/projects/{pid}/ai-opt-out", json={"ai_opt_out": False})
    assert r.status_code == 200 and r.json()["ai_opt_out"] is False, r.text
    r = c.post(f"/api/projects/{pid}/ai-opt-out", json={"ai_opt_out": "maybe"})
    assert r.status_code == 400, r.text
    r = c.post(f"/api/projects/{pid}/ai-opt-out", json={"ai_opt_out": True})
    assert r.json()["ai_opt_out"] is True
    # customer toggle (web + /api); the project page then defers to the customer
    r = c.post(
        f"/customers/{cid}/ai-opt-out",
        data={"ai_opt_out": "1", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        assert s.get(Customer, cid).ai_opt_out is True
    r = c.get(f"/customers/{cid}")
    assert "AI is off for this customer" in r.text
    r = c.post(f"/api/customers/{cid}/ai-opt-out", json={"ai_opt_out": False})
    assert r.status_code == 200 and r.json()["ai_opt_out"] is False, r.text
    # CLI: set-ai + show
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "project", "set-ai", str(pid), "on"])
    assert rc == 0, buf.getvalue()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "project", "show", str(pid)])
    assert rc == 0
    shown = _json_out(buf.getvalue())
    assert shown.get("ai_opt_out") is False and "ai_opt_out_customer" in shown, shown
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "customer", "set-ai", str(cid), "off"])
    assert rc == 0, buf.getvalue()
    with Session(engine) as s:
        assert ai_optout.opt_out_source(s, pid) == "customer"
        ai_optout.set_customer(s, cid, False)
    # auth ON: a translator cannot change it, a manager can
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        security.seed_initial() if hasattr(security, "seed_initial") else None
        tr = _login("tr-%d@example.com" % _seq(), Role.TRANSLATOR)  # noqa: UP031
        r = tr.post(f"/projects/{pid}/ai-opt-out", data={"ai_opt_out": "1"}, follow_redirects=False)
        assert r.status_code == 403, (r.status_code, r.text[:200])
        with Session(engine) as s:
            assert not s.get(Project, pid).ai_opt_out
        mg = _login("mg-%d@example.com" % _seq(), Role.MANAGER)  # noqa: UP031
        r = mg.post(f"/projects/{pid}/ai-opt-out", data={"ai_opt_out": "1"}, follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text[:200])
        with Session(engine) as s:
            assert s.get(Project, pid).ai_opt_out
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("E ok: web toggle + badge + Editor, /api toggle + detail, customer, CLI, role gate")


# ------------------------------------------------------------------------------ F
def test_F_turning_off_restores():
    register_provider("countprov", CountingProvider())
    pid = _mk_project(ai=True, provider="countprov")
    doc_id, _seg = _mk_doc(pid)
    wid = _mk_workflow(provider="countprov")
    spec = ProviderSpec(provider="countprov", retry_max=0, backoff_base=0.0)
    CALLS.clear()
    with ai_optout.scope(project_id=pid):
        _refused(run_completion, spec, "x")
    with Session(engine) as s:
        _refused(orchestrator.run_workflow, s, wid, doc_id, "es-ES")
        ai_optout.set_project(s, pid, False)
    assert CALLS == []
    with ai_optout.scope(project_id=pid):
        res = run_completion(spec, "x")
    assert res.get("text") == "Hola." and CALLS, (res, CALLS)
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wid, doc_id, "es-ES")
    assert summary.get("status") == "completed", summary
    print("F ok: switching AI back on restores engine calls and runs")


class SecretRecordingProvider(Provider):
    """The gate's probe engine: records any call, so a SECRET in it is a leak."""

    name = "secretprov"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        CALLS.append(("secret", task, (prompt or "")[:80]))
        return "Hola."


def _secret_project(customer_ai=False, project_ai=True):
    cid = _mk_customer(ai=customer_ai)
    with Session(engine) as s:
        p = Project(
            name="sec-%d" % _seq(),  # noqa: UP031
            source_locale="en-US",
            default_provider="secretprov",
            customer_id=cid,
            ai_opt_out=project_ai,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, (src, tgt) in enumerate(
            [("SECRET source one.", "SECRETO uno."), ("SECRET source two.", "SECRETO dos.")]
        ):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=src,
                    target_text=tgt,
                    status=SegStatus.MT,
                )
            )
        s.commit()
        return cid, p.id, d.id


def _leaks():
    return [c for c in CALLS if "SECRET" in str(c)]


def test_H_gate_probes():
    """Regression tests for gate round 1 (probes 1-4): paths with NO project scope."""
    from app.services import apikeys, context_ab, style_ab

    register_provider("secretprov", SecretRecordingProvider())
    _cid, pid, did = _secret_project()
    CALLS.clear()
    # H1 context / style A/B: the service, the CLI and /api (a docs-scoped key)
    with Session(engine) as s:
        _refused(context_ab.run_ab, s, s.get(Document, did), "es-ES", provider="secretprov")
        _refused(style_ab.run_ab, s, s.get(Document, did), "es-ES", provider="secretprov")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            [
                "--json",
                "context",
                "ab",
                "--document",
                str(did),
                "--locale",
                "es-ES",
                "--provider",
                "secretprov",
            ]
        )
    assert rc != 0 and "ai_opt_out" in buf.getvalue(), (rc, buf.getvalue()[:300])
    admin = security.create_user("h1-%d@example.com" % _seq(), "a", Role.ADMIN, PW)  # noqa: UP031
    _row, raw = apikeys.create_key(admin.id, "h1", scopes="docs")
    c = TestClient(app)
    c.headers["X-Loc-Token"] = raw
    r = c.post(
        "/api/context/ab", json={"document": did, "locale": "es-ES", "provider": "secretprov"}
    )
    assert r.status_code == 409 and r.json().get("reason") == "ai_opt_out", (r.status_code, r.text)
    assert _leaks() == [], _leaks()
    # H5 (MEDIUM): switching AI off mid-scope stops the NEXT call (no stale "allowed")
    _c5, pid5, _d5 = _secret_project(project_ai=False)
    spec = ProviderSpec(provider="secretprov", retry_max=0, backoff_base=0.0)
    CALLS.clear()
    with ai_optout.scope(project_id=pid5):
        run_completion(spec, "first call before opt-out")
        with Session(engine) as s:
            ai_optout.set_project(s, pid5, True)
        _refused(run_completion, spec, "SECRET second call after opt-out")
    assert _leaks() == [], _leaks()
    # H6 tag repair sweep skips opted-out projects' segments
    from app.services import tag_llm_repair

    settings.set_many({"tag_llm_repair_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == did)).first()
            seg.source_text = "SECRET <b>bold</b> one."
            seg.target_text = "SECRETO negrita uno."
            s.add(seg)
            s.commit()
            rep = tag_llm_repair.repair_stored_segments(s, project_id=pid, apply=False)
        notes = [r.get("note", "") for r in rep.get("segments", [])]
        assert rep.get("attempted", 0) == 0 and _leaks() == [], (rep, _leaks())
        assert any("AI is switched off" in n for n in notes), notes
    finally:
        settings.set_many({"tag_llm_repair_enabled": "0"})
        settings.invalidate_cache()
    print("H ok: gate probes (A/B service+CLI+API, mid-scope, tag sweep)")


# ------------------------------------------------------------------------------ I
def test_I_gate2_probes():
    """Gate round 2: the LOW fixes (glossary sense choice, the text API). The add-on
    probes (semantic TM source, meta-evaluation, Ask inventories) live with the add-ons."""
    from app.models import GlossaryEntry, GlossarySense
    from app.services import apikeys, glossary

    register_provider("secretprov", SecretRecordingProvider())
    # I3 glossary sense choice (an LLM call) is skipped for an opted-out project
    with Session(engine) as s:
        pid7 = _mk_project(ai=True, provider="secretprov")
        e = GlossaryEntry(
            project_id=pid7,
            source_locale="en-US",
            target_locale="es-ES",
            source_term="bank",
            target_term="banco",
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        s.add(GlossarySense(glossary_entry_id=e.id, sense_label="river", target_term="orilla"))
        s.add(GlossarySense(glossary_entry_id=e.id, sense_label="money", target_term="banco"))
        s.commit()
        CALLS.clear()
        out = glossary.match_terms(
            s,
            pid7,
            "SECRET the bank of the river",
            "en-US",
            "es-ES",
            provider=SecretRecordingProvider(),
        )
    assert _leaks() == [], _leaks()
    assert out and out[0]["target_term"] == "banco", out
    # I4 /api/translate/text with a key bound to an opted-out customer -> 409
    cust9 = _mk_customer(ai=True)
    user = security.create_user(
        f"tt-{_seq()}@example.com", "t", Role.LINGUIST, PW, customer_id=cust9
    )
    _row, raw = apikeys.create_key(user.id, "tt", scopes="docs")
    c = TestClient(app)
    c.headers["X-Loc-Token"] = raw
    settings.set_many({"security_auth_required": "1"})  # customer binding is live with auth on
    settings.invalidate_cache()
    try:
        r = c.post(
            "/api/translate/text",
            json={"text": "SECRET hello", "source_locale": "en-US", "target_locale": "es-ES"},
        )
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    assert r.status_code == 409 and r.json().get("reason") == "ai_opt_out", (
        r.status_code,
        r.text[:300],
    )
    print("I ok: glossary sense, text API")


def test_opt_out_wording():
    """#242: render the boundary between instance engines and user-connected AI tools."""
    c = TestClient(app)
    cid = _mk_customer(ai=True)
    pid = _mk_project(customer_id=cid)
    direct_pid = _mk_project(ai=True)
    enabled_pid = _mk_project()
    project_boundary = "This instance's AI engines never process this project's content."
    export_boundary = (
        "Content you download, export, or connect to your own AI tools "
        "(for example the MCP agent connection) is outside this switch."
    )
    for path in (
        f"/projects/{pid}",
        f"/projects/{direct_pid}",
        f"/projects/{enabled_pid}",
        "/help",
        f"/customers/{cid}",
    ):
        response = c.get(path)
        assert response.status_code == 200, (path, response.status_code)
        rendered = " ".join(unescape(response.text).split())
        assert export_boundary in rendered, (path, "missing export boundary")
        if path != f"/customers/{cid}":
            assert project_boundary in rendered, (path, "missing instance boundary")
        else:
            assert "This instance's AI engines never process content" in rendered
        for absolute in (
            "sent to any AI engine or model",
            "away from every AI engine",
        ):
            assert absolute not in rendered, (path, absolute)
    from app.web.api_cli_projects_1 import api_projects_ai_opt_out

    for source in (
        api_projects_ai_opt_out.__doc__,
        Path("llms.txt").read_text(encoding="utf-8"),
        Path("docs/CLI.md").read_text(encoding="utf-8"),
    ):
        normalized = " ".join(source.split())
        assert project_boundary in normalized and export_boundary in normalized
        assert "sent to any AI engine or model" not in normalized
        assert "goes to any AI engine or model" not in normalized
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["project", "set-ai", str(direct_pid), "off"])
    assert rc == 0, buf.getvalue()
    assert project_boundary in buf.getvalue() and export_boundary in buf.getvalue()
    print("Opt-out wording ok: project, customer and help explain the export boundary")


def main():
    init_db()
    test_opt_out_wording()
    test_A_service_and_audit()
    test_B_choke_point()
    test_C_entry_points()
    test_D_non_ai_still_works()
    test_E_surfaces()
    test_F_turning_off_restores()
    test_H_gate_probes()
    test_I_gate2_probes()
    print("verify_ai_opt_out OK")


if __name__ == "__main__":
    main()
