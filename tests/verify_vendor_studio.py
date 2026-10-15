"""VENDOR STUDIO roundtrip acceptance — the whole point of the exercise.

Boots BOTH sides and drives the real HTTP wire:
  * Localization OS: app.main:app on an in-process uvicorn thread (same interpreter as the
    seeding + read-back, so there is NO cross-process SQLite contention — one engine).
  * Vendor Studio: launched as a SUBPROCESS (tools/vendor_studio/run.py) on an ephemeral
    port. The suite NEVER imports vstudio — it speaks to it only over HTTP, which is what
    makes this a real proof of the wire-only isolation.

For each scenario a REAL dispatched-and-paused leg is created in Localization OS by running a
workflow to its EXTERNAL step (the dispatch is intercepted by the injected test transport,
and its exact signed contract body is captured). That captured contract is POSTed to
Vendor Studio's /intake/dispatch (signed with the shared secret). Then Vendor Studio is
driven to edit + QA + deliver back into Localization OS's REAL guarded webhook, and the outcome
is asserted on BOTH sides.

Covered (task spec + fix-round additions):
  [1] happy path   — clean edits admitted (MT) through the guarded seam, leg RETURNED, run resumed
  [2] partial      — a 2-then-1 tranche delivery; the leg stays open until complete
  [3] bad id       — an extra undispatched id is REFUSED by Localization OS (400), nothing written
  [4] source echo  — a source-echo return is a NO-OP (prior target intact, needs_review)
  [5] glossary XSS — a glossary VIOLATION is admitted+needs_review on Localization OS (delivered
                     DIRECTLY — QA is advisory), and the XSS glossary term is Vendor Studio
                     advisor DATA rendered inert (H1)
  [9] advisory QA  — a placeholder-broken return is ACCEPTED by Vendor Studio (no QA block;
                     the findings snapshot rides the delivery) and comes back needs_review by
                     Localization OS's TAG gate — the enforcement moved to the buyer side
  [6] poll         — VS marks COMPLETED; Localization OS poll_leg pulls the signed return (M6)
  [7] secrets      — no read surface echoes the claim token or the signing secret
  [8] isolation    — an in-suite import-scan guard (both directions)
  Every editor action runs only after a real SSO (POST) handoff login.

ASCII-safe prints (Windows consoles); the wire payloads carry non-ASCII freely (UTF-8).
"""

import ast
import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_vstudio_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    ProjectGlossary,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import (
    glossary_module,
    orchestrator,
    vendor_dispatch,
    vendor_return,
    vendors,
)
from app.services import workflows as wf

init_db()

# The poll path resolves a hostname + validates every address before egress; map the fake
# vendor host to a public IP so the hermetic suite never touches real DNS.
if hasattr(vendor_return, "set_test_resolver"):
    vendor_return.set_test_resolver(lambda host: ["93.184.216.34"])

# A glossary term whose TARGET carries an XSS payload — it doubles as (a) the Localization OS
# glossary-violation trigger (a vendor target lacking it is admitted+needs_review) and
# (b) the H1 regression payload the Vendor Studio advisor must render inert.
XSS_TERM_SRC = "cat"
XSS_TERM_TGT = "gato<img src=x onerror=alert(1)>"

SIGNING = "vs-roundtrip-signing-secret"
SSO_SECRET = "vs-roundtrip-sso-secret"
SOURCES = ["Hello ⟦x1⟧ world.", "The cat sat on the mat.", "You have 3 messages."]
GOOD = ["Hola ⟦x1⟧ mundo editado.", "El gato se sento en la alfombra.", "Tienes 3 mensajes."]
_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- seeding a real dispatched leg -------------------------------------------
def _mock_transport(capture):
    def handler(req):
        capture.append({"body": req.content, "headers": dict(req.headers)})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def _seed_vendor_and_workflow(s, poll_url=None):
    cust = Customer(name="C-%d" % _seq())  # noqa: UP031
    s.add(cust)
    s.commit()
    s.refresh(cust)
    cfg = {"poll_url": poll_url} if poll_url else None
    v = Vendor(
        name="V-%d" % _seq(),  # noqa: UP031
        endpoint_url="https://vendor.example.com/intake",
        config=cfg,
        rate_card={"currency": "USD", "bands": {"new": 0.2}},
    )
    v.secret_enc = vendors._enc_secret({"signing_secret": SIGNING})
    s.add(v)
    s.commit()
    s.refresh(v)
    vendors.link_customer(s, v.id, cust.id)
    return cust.id, v.id  # plain ids (the ORM instances detach after the session)


def _seed_glossary(s, project_id, customer_id):
    """Create + approve a customer glossary carrying the XSS term and PIN it to the project
    (an untyped project auto-attaches nothing without a pin). Returns the glossary id."""
    g = glossary_module.create(s, name="GL-%d" % _seq(), customer_id=customer_id)  # noqa: UP031
    glossary_module.add_term(
        s,
        g.id,
        source_term=XSS_TERM_SRC,
        target_term=XSS_TERM_TGT,
        source_locale="en-US",
        target_locale="es-ES",
    )
    glossary_module.approve(s, g.id, approver_email="tester@example.com")
    s.add(ProjectGlossary(project_id=project_id, glossary_id=g.id))
    s.commit()
    return g.id


def _seed_leg(cust_id, vendor_id, with_glossary=False):
    """One customer/project/doc/workflow with an EXTERNAL step; run it to the pause and
    capture the dispatched contract. Returns (leg_id, run_id, claim, contract_bytes,
    doc_id, seg_ids)."""
    with Session(engine) as s:
        p = Project(
            name="P-%d" % _seq(),  # noqa: UP031
            source_locale="en-US",
            default_provider="mock",
            customer_id=cust_id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(SOURCES):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        w = Workflow(
            name="W-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",  # noqa: RUF100, UP031
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(
            s,
            w.id,
            name="Vendor review",
            step_type=StepType.EXTERNAL,
            vendor_id=vendor_id,
            external_wait_hours=72,
        )
        wf_id, doc_id = w.id, d.id
        if with_glossary:
            _seed_glossary(s, p.id, cust_id)

    cap = []
    vendor_dispatch.set_test_transport(_mock_transport(cap))
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)

    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wf_id)).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED
        assert run.status == RunStatus.PAUSED
        seg_ids = [sg.id for sg in orchestrator._load_segments(s, doc_id, "es-ES")]
        return leg.id, run.id, leg.claim_token, cap[-1]["body"], doc_id, seg_ids


def _targets(doc_id):
    with Session(engine) as s:
        return [
            (sg.target_text or "", sg.status)
            for sg in orchestrator._load_segments(s, doc_id, "es-ES")
        ]


def _leg_status(leg_id):
    with Session(engine) as s:
        return s.get(VendorDispatch, leg_id).status


def _wait_leg(leg_id, status, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _leg_status(leg_id) == status:
            return True
        time.sleep(0.15)
    return False


def _wait_run(run_id, status=RunStatus.COMPLETED, timeout=20.0):
    """The webhook admit resumes the run on a BACKGROUND thread, so the run reaches its
    terminal status shortly AFTER the leg flips RETURNED. Poll for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            r = s.get(WorkflowRun, run_id)
            if r is not None and r.status in (status, RunStatus.FAILED):
                return r.status
        time.sleep(0.15)
    with Session(engine) as s:
        return s.get(WorkflowRun, run_id).status


# --- Localization OS in-process uvicorn thread ------------------------------------
class _LocServer:
    def __init__(self, port):
        import uvicorn

        self.port = port
        cfg = uvicorn.Config(
            "app.main:app", host="127.0.0.1", port=port, log_level="warning", loop="asyncio"
        )
        self.server = uvicorn.Server(cfg)
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                try:
                    r = httpx.get("http://127.0.0.1:%d/health" % self.port, timeout=2)  # noqa: UP031
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
            time.sleep(0.2)
        raise RuntimeError("Localization OS server did not come up")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


# --- Vendor Studio subprocess ------------------------------------------------
class _VStudio:
    def __init__(self, port, locstudio_url):
        self.port = port
        self.base = "http://127.0.0.1:%d" % port  # noqa: UP031
        self.tool_dir = os.path.join(_ROOT, "tools", "vendor_studio")
        env = os.environ.copy()
        env.update(
            {
                "VSTUDIO_DB_PATH": os.path.join(_TMP, "vstudio.db"),
                "VSTUDIO_SIGNING_SECRET": SIGNING,
                "VSTUDIO_SSO_SECRET": SSO_SECRET,
                "VSTUDIO_SESSION_SECRET": "vs-roundtrip-session",
                "VSTUDIO_LOCSTUDIO_URL": locstudio_url,
                "VSTUDIO_HOST": "127.0.0.1",
                "VSTUDIO_PORT": str(port),
                "VSTUDIO_ADVISOR_MODE": "mock",
                # The roundtrip drives the adversarial /autopilot/run route (default OFF in prod).
                "VSTUDIO_AUTOPILOT_ENABLED": "1",
                # loopback http: a Secure cookie would not round-trip over the wire.
                "VSTUDIO_COOKIE_INSECURE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        self.env = env
        self.proc = None
        self._log = []

    def _drain(self):
        """Continuously read the child's stdout so its PIPE never fills. A full pipe would
        block uvicorn's logging write() and freeze the server mid-request (the deadlock this
        exists to prevent — surfaces only after ~50 requests, once the buffer is full)."""
        for line in iter(self.proc.stdout.readline, b""):
            self._log.append(line.decode("utf-8", "replace"))
            if len(self._log) > 5000:
                del self._log[:2500]

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=self.tool_dir,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Vendor Studio exited early:\n" + "".join(self._log))
            try:
                r = httpx.get(self.base + "/health", timeout=2)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Vendor Studio did not come up")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # -- HTTP helpers --
    def sign(self, raw):
        return "sha256=" + hmac.new(SIGNING.encode(), raw, hashlib.sha256).hexdigest()

    def post_dispatch(self, contract_bytes, claim):
        return httpx.post(
            self.base + "/intake/dispatch",
            content=contract_bytes,
            headers={
                "x-locstudio-signature": self.sign(contract_bytes),
                "x-locstudio-claim": claim,
                "content-type": "application/json",
            },
            timeout=15,
        )

    def sso_client(self):
        """A cookie-jar client authenticated via a real SSO handoff (mints a token in the
        documented format and completes the /sso handshake — a POST with the token in the
        BODY, per the L1 fix / README spec)."""
        c = httpx.Client(base_url=self.base, timeout=20, follow_redirects=False)
        token = _mint_sso("roundtrip-user", SSO_SECRET)
        r = c.post("/sso", data={"token": token})
        assert r.status_code == 303, ("SSO handoff failed", r.status_code, r.text)
        return c


def _mint_sso(user, secret, ttl=60):
    payload = {
        "user": user,
        "exp": int(time.time()) + ttl,
        "iat": int(time.time()),
        "aud": "vendor-studio",
        "jti": uuid.uuid4().hex,
        "role": "admin",
        "org": None,
        "mo": False,
    }
    body = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    sig = hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return body + "." + sig


# ============================================================================
# scenarios
# ============================================================================
def scenario_happy(vs, cust_id, vendor_id):
    leg_id, run_id, claim, contract, doc_id, _seg_ids = _seed_leg(cust_id, vendor_id)
    r = vs.post_dispatch(contract, claim)
    assert r.status_code == 200, ("intake refused", r.text)
    pid = r.json()["project_id"]

    c = vs.sso_client()
    segs = c.get("/projects/%d/segments" % pid).json()["segments"]  # noqa: UP031
    assert len(segs) == 3
    # Edit + confirm each segment over the REAL editor API (a clean post-edit).
    for seg, good in zip(segs, GOOD, strict=False):
        pr = c.request(
            "PATCH",
            "/projects/%d/segments/%d" % (pid, seg["id"]),  # noqa: UP031
            json={"target": good},  # noqa: RUF100, UP031
        )
        assert pr.status_code == 200, ("patch failed", pr.text)
        cf = c.post("/projects/%d/segments/%d/confirm" % (pid, seg["id"]))  # noqa: UP031
        assert cf.status_code == 200
    gate = c.get("/projects/%d/qa" % pid).json()["gate"]  # noqa: UP031
    # QA is advisory-only now: the summary carries counts, never an ok/blocking verdict.
    assert "ok" not in gate and gate["open"] == 0, (
        "clean edits should leave no open advisory findings",
        gate,
    )
    dl = c.post("/projects/%d/deliver" % pid, json={"cost": 30.0})  # noqa: UP031
    c.close()
    assert dl.status_code == 200, ("deliver route refused", dl.text)
    body = dl.json()
    assert body["ok"] and body["status_code"] == 200 and body["complete"], body
    # Localization OS side: the vendor's edits re-entered through the REAL guarded seam
    # (commit_mt_output, all guard lanes on), the leg is RETURNED, the run resumed, and the
    # content landed with the vendor provenance. Clean content commits as MT; the
    # needs_review lane is exercised by the source-echo scenario below.
    assert _wait_leg(leg_id, VendorDispatchStatus.RETURNED), "leg never RETURNED"
    rows = _targets(doc_id)
    for (txt, st), good in zip(rows, GOOD, strict=False):
        assert txt == good, ("edit did not land in Localization OS", txt, good)
        assert st == SegStatus.MT, ("clean vendor content is admitted as MT", st)
    assert _wait_run(run_id) == RunStatus.COMPLETED, "run must resume + finish"
    print(
        "  [1] happy path: SSO login, edit-via-API, QA pass, deliver -> Localization OS "
        "admitted through the guarded seam, leg RETURNED, run COMPLETED OK"
    )


def scenario_partial(vs, cust_id, vendor_id):
    leg_id, run_id, claim, contract, _doc_id, _seg_ids = _seed_leg(cust_id, vendor_id)
    pid = vs.post_dispatch(contract, claim).json()["project_id"]
    c = vs.sso_client()
    segs = c.get("/projects/%d/segments" % pid).json()["segments"]  # noqa: UP031
    # Confirm ONLY the first two, deliver the confirmed subset (a partial tranche).
    for seg, good in list(zip(segs, GOOD, strict=False))[:2]:
        c.request("PATCH", "/projects/%d/segments/%d" % (pid, seg["id"]), json={"target": good})  # noqa: UP031
        c.post("/projects/%d/segments/%d/confirm" % (pid, seg["id"]))  # noqa: UP031
    first = c.post("/projects/%d/deliver-partial" % pid, json={}).json()  # noqa: UP031
    assert first["status_code"] == 200 and not first["complete"], first
    assert _leg_status(leg_id) == VendorDispatchStatus.DISPATCHED, "partial must keep it open"
    # Now confirm + deliver the last one; the leg completes.
    seg3 = segs[2]
    c.request("PATCH", "/projects/%d/segments/%d" % (pid, seg3["id"]), json={"target": GOOD[2]})  # noqa: UP031
    c.post("/projects/%d/segments/%d/confirm" % (pid, seg3["id"]))  # noqa: UP031
    second = c.post("/projects/%d/deliver-partial" % pid, json={"cost": 40.0}).json()  # noqa: UP031
    c.close()
    assert second["status_code"] == 200 and second["complete"], second
    assert _wait_leg(leg_id, VendorDispatchStatus.RETURNED), "leg never completed"
    _wait_run(run_id)  # drain the async resume so it cannot starve later scenarios
    print(
        "  [2] partial tranche: 2-then-1 delivery; leg stays open until the completing "
        "tranche, then RETURNED OK"
    )


def scenario_bad_id(vs, cust_id, vendor_id):
    leg_id, _run_id, claim, contract, doc_id, _seg_ids = _seed_leg(cust_id, vendor_id)
    before = _targets(doc_id)
    pid = vs.post_dispatch(contract, claim).json()["project_id"]
    c = vs.sso_client()
    out = c.post("/autopilot/run", json={"scenario": "bad_id_extra", "project_id": pid}).json()
    c.close()
    resp = out["responses"][0]
    assert resp["status_code"] == 400, ("Localization OS must REFUSE an extra id", resp)
    assert _leg_status(leg_id) == VendorDispatchStatus.DISPATCHED, "leg must stay open"
    assert _targets(doc_id) == before, "a refused bad-id return must write NOTHING"
    print(
        "  [3] bad-id return (extra undispatched id) REFUSED by Localization OS (400), "
        "nothing written, leg still open OK"
    )


def scenario_source_echo(vs, cust_id, vendor_id):
    _leg_id, run_id, claim, contract, doc_id, _seg_ids = _seed_leg(cust_id, vendor_id)
    before = _targets(doc_id)
    pid = vs.post_dispatch(contract, claim).json()["project_id"]
    c = vs.sso_client()
    out = c.post("/autopilot/run", json={"scenario": "source_echo", "project_id": pid}).json()
    c.close()
    resp = out["responses"][0]
    assert resp["status_code"] == 200, ("a source echo is admitted-but-no-op", resp)
    body = resp["body"]
    assert body.get("no_op"), ("every echoed id must be a no-op", body)
    assert not body.get("admitted"), ("a source echo must admit NOTHING", body)
    # Prior (empty) targets are intact and the segments are routed to review.
    rows = _targets(doc_id)
    for (txt, st), (btxt, _bst) in zip(rows, before, strict=False):
        assert txt == btxt, ("a source echo overwrote a target", txt, btxt)
        assert st == SegStatus.NEEDS_REVIEW
    _wait_run(run_id)  # drain the async resume
    print(
        "  [4] source-echo return is a NO-OP: prior targets intact, segments "
        "needs_review, nothing admitted OK"
    )


def scenario_glossary_xss(vs, cust_id, vendor_id):
    """A genuine committed-and-needs_review admission via a Localization OS glossary VIOLATION,
    where the required target term carries an XSS payload that doubles as the H1 regression
    on the Vendor Studio advisor card path."""
    leg_id, run_id, claim, contract, doc_id, _seg_ids = _seed_leg(
        cust_id, vendor_id, with_glossary=True
    )
    # Inject the package glossary into the dispatch bytes (VP-0 assets.glossary) so Vendor
    # Studio carries it; re-sign the exact bytes we send.
    payload = json.loads(contract.decode("utf-8"))
    payload.setdefault("assets", {})["glossary"] = [{"term": XSS_TERM_SRC, "target": XSS_TERM_TGT}]
    body = json.dumps(payload).encode("utf-8")
    r = vs.post_dispatch(body, claim)
    assert r.status_code == 200, ("intake refused", r.text)
    pid = r.json()["project_id"]

    c = vs.sso_client()
    segs = c.get("/projects/%d/segments" % pid).json()["segments"]  # noqa: UP031
    for seg, good in zip(segs, GOOD, strict=False):
        c.request("PATCH", "/projects/%d/segments/%d" % (pid, seg["id"]), json={"target": good})  # noqa: UP031
        c.post("/projects/%d/segments/%d/confirm" % (pid, seg["id"]))  # noqa: UP031
    # H1: the advisor rationale for seg 2 interpolates the XSS glossary term. The endpoint
    # returns it as a JSON STRING (data), and the front-end renders it via textContent.
    adv = c.post("/projects/%d/qa/advice" % pid, json={"segment_id": segs[1]["id"]}).json()  # noqa: UP031
    assert adv["suggestions"], adv
    assert XSS_TERM_TGT in adv["suggestions"][0]["rationale"], (
        "the advisor did not carry the wire glossary term as data",
        adv,
    )
    # seg 2 carries a VS glossary_missing finding (the required XSS target term is absent).
    # QA is advisory-only now: it is just a finding, and delivery proceeds WITHOUT dismissing
    # it (the old flow had to dismiss it to pass the gate — no longer).
    qa = c.get("/projects/%d/qa" % pid).json()  # noqa: UP031
    assert "ok" not in qa["gate"], ("VS QA must be advisory-only (no ok verdict)", qa["gate"])
    assert any(
        f["check"] == "glossary_missing" and f["segment_id"] == segs[1]["id"]
        for f in qa["findings"]
    ), ("the glossary finding must still be computed", qa)
    dl = c.post("/projects/%d/deliver" % pid, json={}).json()  # noqa: UP031
    c.close()
    assert dl["status_code"] == 200 and dl["complete"], (
        "advisory QA must not block delivery of a glossary-flagged package",
        dl,
    )
    assert dl["qa_snapshot"]["open"] >= 1, (
        "the advisory findings snapshot must ride the delivery record",
        dl,
    )
    assert _wait_leg(leg_id, VendorDispatchStatus.RETURNED), "leg never RETURNED"
    # Localization OS side: seg 2's target landed AND was flagged needs_review (glossary_violation).
    with Session(engine) as s:
        seg2 = orchestrator._load_segments(s, doc_id, "es-ES")[1]
        assert seg2.target_text == GOOD[1], ("the vendor edit must land", seg2.target_text)
        assert seg2.status == SegStatus.NEEDS_REVIEW, (
            "a glossary violation must be admitted as needs_review",
            seg2.status,
        )
        assert any(
            isinstance(f, dict) and f.get("type") == "glossary_violation"
            for f in (seg2.qa_flags or [])
        ), seg2.qa_flags
    _wait_run(run_id)  # drain the async resume
    # H1 regression, front-end half: no innerHTML sink for wire-derived advisor strings.
    ejs = open(  # noqa: SIM115
        os.path.join(_ROOT, "tools", "vendor_studio", "static", "editor.js"), encoding="utf-8"
    ).read()
    assert "note.textContent = s.rationale" in ejs, "advisor rationale must be textContent"
    assert ".innerHTML" not in ejs, "editor.js must not use innerHTML (XSS sink)"
    print(
        "  [5] glossary-violation: committed+needs_review on Localization OS; the XSS glossary "
        "term is advisor DATA rendered inert (textContent, no innerHTML) OK"
    )


def scenario_placeholder_advisory(vs, cust_id, vendor_id):
    """THE KEY SYSTEMIC ASSERTION (2026-08-08): vendor-side QA is advisory, and the
    enforcement moved to the BUYER side. A placeholder-broken return (seg 1 with ⟦x1⟧ dropped)
    is ACCEPTED by Vendor Studio — it delivers with NO QA block, the old gate would have
    409'd — and comes back flagged needs_review by LOCALIZATION OS's tag-integrity gate. This
    proves the enforcing seam is Localization OS's admission path, not vendor QA."""
    leg_id, run_id, claim, contract, doc_id, _seg_ids = _seed_leg(cust_id, vendor_id)
    pid = vs.post_dispatch(contract, claim).json()["project_id"]
    c = vs.sso_client()
    segs = c.get("/projects/%d/segments" % pid).json()["segments"]  # noqa: UP031
    # seg 1 drops the ⟦x1⟧ placeholder (a hard finding under the old gate); segs 2/3 clean.
    broken = "Hola mundo editado."  # GOOD[0] minus the ⟦x1⟧ tag
    edits = [broken, GOOD[1], GOOD[2]]
    for seg, tgt in zip(segs, edits, strict=False):
        c.request("PATCH", "/projects/%d/segments/%d" % (pid, seg["id"]), json={"target": tgt})  # noqa: UP031
        c.post("/projects/%d/segments/%d/confirm" % (pid, seg["id"]))  # noqa: UP031
    # VS QA reports the placeholder finding as ADVISORY (present, no ok/blocking verdict).
    qa = c.get("/projects/%d/qa" % pid).json()  # noqa: UP031
    assert "ok" not in qa["gate"], ("VS QA must be advisory-only", qa["gate"])
    assert any(
        f["check"] == "placeholder_mismatch" and f["segment_id"] == segs[0]["id"]
        for f in qa["findings"]
    ), ("the placeholder finding must be computed", qa)
    # Deliver DIRECTLY despite the finding — the old gate would have refused with a 409.
    dl = c.post("/projects/%d/deliver" % pid, json={"cost": 15.0}).json()  # noqa: UP031
    c.close()
    assert dl["status_code"] == 200 and dl["complete"], (
        "advisory QA must not block a placeholder-broken delivery",
        dl,
    )
    assert dl["qa_snapshot"]["open"] >= 1, (
        "the advisory findings snapshot must ride the delivery record",
        dl,
    )
    assert _wait_leg(leg_id, VendorDispatchStatus.RETURNED), "leg never RETURNED"
    # Localization OS side: seg 1's broken target LANDED and was flagged needs_review by the TAG
    # gate (buyer-side enforcement); segs 2/3 admitted clean as MT.
    with Session(engine) as s:
        loaded = orchestrator._load_segments(s, doc_id, "es-ES")
        seg0 = loaded[0]
        assert seg0.target_text == broken, (
            "the broken vendor edit must still land (VS accepted it)",
            seg0.target_text,
        )
        assert seg0.status == SegStatus.NEEDS_REVIEW, (
            "a placeholder-broken return must be admitted needs_review by the tag gate",
            seg0.status,
        )
        assert any(
            isinstance(f, dict) and f.get("type") == "tag_integrity" for f in (seg0.qa_flags or [])
        ), ("Localization OS's tag gate must flag the dropped placeholder", seg0.qa_flags)
        # sanity: the clean rows admitted as MT (not needs_review).
        assert loaded[1].target_text == GOOD[1] and loaded[1].status == SegStatus.MT
    _wait_run(run_id)  # drain the async resume
    print(
        "  [9] placeholder-broken delivery ACCEPTED by Vendor Studio (advisory QA, no "
        "block; snapshot rides the delivery) and admitted needs_review by Localization OS's "
        "tag gate — enforcement is buyer-side OK"
    )


def scenario_poll(vs, cust_id, vendor_id):
    """The POLL transport: Vendor Studio marks a job COMPLETED, and Localization OS's own
    poll_leg PULLS the signed return from Vendor Studio over HTTP and admits it."""
    leg_id, _run_id, claim, contract, doc_id, _seg_ids = _seed_leg(cust_id, vendor_id)
    pid = vs.post_dispatch(contract, claim).json()["project_id"]
    c = vs.sso_client()
    segs = c.get("/projects/%d/segments" % pid).json()["segments"]  # noqa: UP031
    for seg, good in zip(segs, GOOD, strict=False):
        c.request("PATCH", "/projects/%d/segments/%d" % (pid, seg["id"]), json={"target": good})  # noqa: UP031
        c.post("/projects/%d/segments/%d/confirm" % (pid, seg["id"]))  # noqa: UP031
    # Mark READY for a pull-model vendor (no push). The QA gate must pass here.
    cp = c.post("/projects/%d/complete" % pid)  # noqa: UP031
    assert cp.status_code == 200, ("complete refused", cp.text)

    # The REAL pull from Vendor Studio's poll-serve endpoint over HTTP. This serves the
    # signed return AND flips the VS project DELIVERED on first serve (M6).
    served = httpx.get(
        vs.base + "/intake/poll",
        headers={
            "x-locstudio-claim": claim,
            "x-locstudio-contract-version": "1.0",
            "accept": "application/json",
        },
        timeout=15,
    )
    assert served.status_code == 200, ("VS poll must serve a COMPLETED job", served.text)
    ret_body = served.content
    ret_sig = served.headers.get("x-locstudio-signature")
    assert ret_sig, "the poll-served return must be signed"
    status = c.get("/projects/%d/segments" % pid).json()["status"]  # noqa: UP031
    c.close()
    assert status == "DELIVERED", ("a served poll must flip the VS project DELIVERED", status)

    # Now Localization OS's OWN poll_leg admits exactly those signed VS bytes through its guarded
    # pipeline (the transport returns the captured VS response, so poll_leg does its full
    # signature-verify + id-accounting + commit path on real Vendor Studio output).
    def static(req):
        return httpx.Response(200, content=ret_body, headers={"x-locstudio-signature": ret_sig})

    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        res = vendor_return.poll_leg(s, leg, transport=httpx.MockTransport(static), resume="sync")
    assert res is not None and res["complete"], ("poll admit failed", res)
    assert _leg_status(leg_id) == VendorDispatchStatus.RETURNED, "poll must complete the leg"
    for (txt, _st), good in zip(_targets(doc_id), GOOD, strict=False):
        assert txt == good, ("poll-admitted edit must land", txt, good)
    print(
        "  [6] poll transport: VS marks COMPLETED, Localization OS poll_leg PULLS the signed "
        "return over HTTP and admits it; VS project -> DELIVERED OK"
    )


def scenario_secret_hygiene(vs, cust_id, vendor_id):
    """Pinned invariant: no read surface (segments / package / wirelog) ever echoes the
    claim token or the signing secret."""
    _leg_id, _run_id, claim, contract, _doc_id, _seg_ids = _seed_leg(cust_id, vendor_id)
    pid = vs.post_dispatch(contract, claim).json()["project_id"]
    c = vs.sso_client()
    for path in ("/projects/%d/segments" % pid, "/projects/%d/package" % pid, "/api/wirelog"):  # noqa: UP031
        text = c.get(path).text
        assert claim not in text, f"claim token leaked into {path}"
        assert SIGNING not in text, f"signing secret leaked into {path}"
    c.close()
    print(
        "  [7] secret hygiene: claim token + signing secret never appear in segments / "
        "package / wirelog OK"
    )


# --- isolation guard (in-suite, so the repo suite enforces it too) -----------
def scenario_isolation():
    tool_root = os.path.join(_ROOT, "tools", "vendor_studio")
    # `.claude` holds Claude Code's scratch + git WORKTREES (each a full checkout that
    # contains its own tools/vendor_studio copy); walking into them makes this repo-tree
    # guard find phantom "outside" vstudio imports in the nested worktrees (#338). The
    # other entries are local-only / git-ignored trees, likewise never project source.
    # Keep in sync with _SKIP_DIRS in tools/vendor_studio/tests/test_import_isolation.py.
    skip = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "data",
        "data_qa",
        ".claude",
        ".qwen",
        "VIDEO_DEMOS",
        "sample-docs",
        "scratchpad",
        "scratchpad_review",
        ".test-instances",
        "scan-reports",
        "_audit-results",
        "dist",
        "build",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
    }

    def roots(path):
        with open(path, encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read(), filename=path)
            except SyntaxError:
                return set()
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
        return out

    def walk(root):
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in skip]
            for f in fn:
                if f.endswith(".py"):
                    yield os.path.join(dp, f)

    fwd = [p for p in walk(tool_root) if roots(p) & {"app", "integrations"}]
    assert not fwd, ("vendor_studio imports Localization OS code", fwd)
    rev = [
        p
        for p in walk(_ROOT)
        if not os.path.abspath(p).startswith(os.path.abspath(tool_root) + os.sep)
        and "vstudio" in roots(p)
    ]
    assert not rev, ("code outside vendor_studio imports vstudio", rev)
    print(
        "  [8] import-isolation guard: vendor_studio imports no app/integrations, and "
        "nothing outside imports vstudio OK"
    )


def main():
    scenario_isolation()
    loc_port = _free_port()
    vs_port = _free_port()
    loc = _LocServer(loc_port)
    loc.start()
    vs = _VStudio(vs_port, "http://127.0.0.1:%d" % loc_port)  # noqa: UP031
    try:
        vs.start()
        with Session(engine) as s:
            cust_id, vendor_id = _seed_vendor_and_workflow(s)
            poll_cust_id, poll_vendor_id = _seed_vendor_and_workflow(
                s, poll_url="https://vendor.example.com/returns"
            )
        scenario_happy(vs, cust_id, vendor_id)
        scenario_partial(vs, cust_id, vendor_id)
        scenario_bad_id(vs, cust_id, vendor_id)
        scenario_source_echo(vs, cust_id, vendor_id)
        scenario_glossary_xss(vs, cust_id, vendor_id)
        scenario_placeholder_advisory(vs, cust_id, vendor_id)
        scenario_poll(vs, poll_cust_id, poll_vendor_id)
        scenario_secret_hygiene(vs, cust_id, vendor_id)
    finally:
        vs.stop()
        loc.stop()
    print("VENDOR STUDIO ROUNDTRIP OK")


if __name__ == "__main__":
    main()
