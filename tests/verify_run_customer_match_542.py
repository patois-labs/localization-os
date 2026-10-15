"""#542 item 3 (2026-10-07) -- hermetic.

A run whose workflow belongs to a DIFFERENT customer than the document is REFUSED on every
surface that starts a run. Allowed: a global workflow (customer_id NULL) or a workflow of
the document's own customer. The check lives in ONE service seam (`orchestrator._refuse_archived`
for start_run / run_workflow / plan, `batch_runs_hub._validate` for batches, `keys_hub2`
launch for key translate), so a staff user who sees BOTH customers cannot run customer B's
private workflow on customer A's document.

Pinned: web POST /documents/{id}/run, POST /documents/{id}/run-batch, /api run (async and
wait), /api run-batch, the local CLI `workflow run`, `orchestrator.start_run` /
`run_workflow` directly, the batch service, and the plan endpoint warning refuse customer
B's workflow on customer A's document with a clear message and NO run row; the global and
the same-customer workflow still run.

Final line: "RUN CUSTOMER MATCH 542 OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rcm542_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    Segment,
    TargetLocale,
    TransMode,
    WorkflowRun,
)
from app.services import apikeys, orchestrator, security, settings
from app.services import workflows as W

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _doc(customer_id, name):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US", customer_id=customer_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        d = Document(
            project_id=p.id,
            filename="d.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hello world.",
                target_text="",
            )
        )
        s.commit()
        return d.id


def _wf(name, customer_id):
    with Session(engine) as s:
        w = W.create_workflow(s, name)
        W.add_step(
            s,
            w.id,
            name="T",
            provider="mock",
            model="m1",
            prompt_text="Translate {text}",
            chunk_policy="none",
        )
        w = s.get(type(w), w.id)
        w.customer_id = customer_id
        s.add(w)
        s.commit()
        return w.id


def _runs(doc_id):
    with Session(engine) as s:
        return len(s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc_id)).all())


def _cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue() + err.getvalue()


CUST_A, CUST_B = _customer("Cust A 542c"), _customer("Cust B 542c")
WF_GLOBAL = _wf("Global WF 542c", None)
WF_A = _wf("A WF 542c", CUST_A)
WF_B = _wf("B WF 542c", CUST_B)

admin = security.create_user("rcm542@x.io", "rcm542", "admin", "fixture-pass-1234!!")
_row, RAW = apikeys.create_key(admin.id, "rcm542-key", scopes="api docs")
H = {"X-Loc-Token": RAW}
client = TestClient(app)
MSG = "different customer"


def _refused_body(text, label):
    if MSG not in text:
        _fail(f"{label}: refusal must say '{MSG}', got: {text[:300]}")


# --- service seam: start_run / run_workflow ------------------------------------------
doc = _doc(CUST_A, "Proj A 542c")
for fn_name in ("start_run", "run_workflow"):
    fn = getattr(orchestrator, fn_name)
    with Session(engine) as s:
        try:
            fn(s, WF_B, doc, "fr-FR")
        except ValueError as exc:
            _refused_body(str(exc), fn_name)
        else:
            _fail(f"{fn_name}: customer B's workflow on customer A's document must be refused")
if _runs(doc):
    _fail("a refused launch must leave NO run row")
print("  1. service seam (start_run, run_workflow) refuses B's workflow on A's document: OK")

# --- web single run --------------------------------------------------------------------
r = client.post(
    f"/documents/{doc}/run", data={"workflow_id": WF_B, "locale": "fr-FR"}, follow_redirects=False
)
if r.status_code != 400:
    _fail(f"web run: expected 400, got {r.status_code}")
_refused_body(r.text, "web run")
r = client.post(
    f"/documents/{doc}/run-batch",
    data={"workflow_id": WF_B, "locales": ["fr-FR"]},
    follow_redirects=False,
)
if r.status_code < 400:
    _fail(f"web run-batch: expected a refusal, got {r.status_code}")
_refused_body(r.text, "web run-batch")

# --- /api ------------------------------------------------------------------------------
for extra in ({}, {"wait": True}):
    r = client.post(
        f"/api/workflows/{WF_B}/run", headers=H, json={"doc_id": doc, "locale": "fr-FR", **extra}
    )
    if r.status_code != 400:
        _fail(f"/api run {extra}: expected 400, got {r.status_code} {r.text[:200]}")
    _refused_body(r.text, f"/api run {extra}")
r = client.post(f"/api/workflows/{WF_B}/run-batch", headers=H, json={"doc_id": doc, "wait": True})
if r.status_code < 400:
    _fail(f"/api run-batch: expected a refusal, got {r.status_code} {r.text[:200]}")
_refused_body(r.text, "/api run-batch")
r = client.get(f"/api/workflows/{WF_B}/plan", headers=H, params={"doc_id": doc, "locale": "fr-FR"})
if r.status_code == 200 and MSG not in r.text:
    _fail(f"/api plan must warn the run would be refused: {r.text[:300]}")

# --- CLI (local, in-process) -----------------------------------------------------------
code, out = _cli(["--json", "workflow", "run", str(doc), str(WF_B), "--locale", "fr-FR"])
if code == 0:
    _fail("local CLI run must exit non-zero for a foreign-customer workflow")
_refused_body(out, "local CLI")
if _runs(doc):
    _fail("no surface may leave a run row behind a refusal")
print("  2. web run/run-batch, /api run/wait/run-batch/plan, local CLI all refuse: OK")

# --- batch service ---------------------------------------------------------------------
from app.services import batch_runs

with Session(engine) as s:
    try:
        batch_runs.start_batch(
            s, workflow_id=WF_B, doc_id=doc, locales=["fr-FR"], created_by="t", synchronous=True
        )
    except Exception as exc:  # BatchError
        _refused_body(str(exc), "batch service")
    else:
        _fail("batch service must refuse a foreign-customer workflow")

# --- allowed: global and same-customer still run --------------------------------------
for label, wid in (("global", WF_GLOBAL), ("same-customer", WF_A)):
    d = _doc(CUST_A, f"Proj A ok {label} 542c")
    r = client.post(
        f"/api/workflows/{wid}/run", headers=H, json={"doc_id": d, "locale": "fr-FR", "wait": True}
    )
    if r.status_code != 200:
        _fail(f"/api run with the {label} workflow must still run: {r.status_code} {r.text[:200]}")
    d2 = _doc(CUST_A, f"Proj A ok2 {label} 542c")
    r = client.post(
        f"/documents/{d2}/run", data={"workflow_id": wid, "locale": "fr-FR"}, follow_redirects=False
    )
    if r.status_code != 303:
        _fail(f"web run with the {label} workflow must still run: {r.status_code}")
    d3 = _doc(CUST_A, f"Proj A ok3 {label} 542c")
    code, out = _cli(["--json", "workflow", "run", str(d3), str(wid), "--locale", "fr-FR"])
    if code != 0:
        _fail(f"local CLI with the {label} workflow must run: {code} {out[:300]}")

# B's own workflow runs on B's document; a customer workflow on a no-customer project fails closed
d_b = _doc(CUST_B, "Proj B 542c")
r = client.post(
    f"/api/workflows/{WF_B}/run", headers=H, json={"doc_id": d_b, "locale": "fr-FR", "wait": True}
)
if r.status_code != 200:
    _fail(f"B's workflow on B's document must run: {r.status_code} {r.text[:200]}")
d_none = _doc(None, "Proj none 542c")
r = client.post(f"/api/workflows/{WF_B}/run", headers=H, json={"doc_id": d_none, "locale": "fr-FR"})
if r.status_code != 400:
    _fail(f"a customer workflow on a no-customer project must fail closed: {r.status_code}")
print("  3. global + same-customer workflows still run; no-customer document fails closed: OK")

# --- key translate (launch_translation) and batch re-run ------------------------------
from app.services import keys as keys_svc

d_key = _doc(CUST_A, "Proj A keys 542c")
with Session(engine) as s:
    proj_a = s.get(Project, s.get(Document, d_key).project_id)
    try:
        keys_svc.launch_translation(s, proj_a, WF_B, "fr-FR", actor="t", wait=True)
    except ValueError as exc:
        _refused_body(str(exc), "keys launch_translation")
    else:
        _fail("keys launch_translation must refuse a foreign-customer workflow")

d_rr = _doc(CUST_A, "Proj A rerun 542c")
with Session(engine) as s:
    batch = batch_runs.start_batch(
        s, workflow_id=WF_A, doc_id=d_rr, locales=["fr-FR"], created_by="t", synchronous=True
    )
    batch_id = batch.id
with Session(engine) as s:
    from app.models import Workflow

    w = s.get(Workflow, WF_A)
    w.customer_id = CUST_B  # re-assigned after the batch started
    s.add(w)
    s.commit()
with Session(engine) as s:
    b = s.get(type(batch), batch_id)
    try:
        batch_runs.rerun_batch(s, batch=b, locales=["fr-FR"], created_by="t", synchronous=True)
    except batch_runs.BatchError as exc:
        _refused_body(str(exc), "batch rerun")
    else:
        _fail("batch re-run must refuse a workflow that now belongs to another customer")
print("  4. key translate and batch re-run refuse a foreign-customer workflow: OK")

print("RUN CUSTOMER MATCH 542 OK")
