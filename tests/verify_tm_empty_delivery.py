"""The TM-only delivery empty gate (Batch 15 T2, SPEC-tm-match-step-config §9) -- hermetic.

The maintainer's D15-6: a deliverable produced by a tm_match workflow that still carries EMPTY
(untranslated) targets is HELD from delivery until an audited human approves shipping the
empties as EMPTY STRINGS (not source). The hold is a LIVE COMPANION predicate keyed on the
durable WorkflowRun.used_tm_match marker, never armed at run end, and not bypassable with
allow_held.

Pinned (the threats §14 the adversarial gate reproduces):
  T-6/T-9/T-13  the COMPANION decision (PASS/HOLD/RELEASE_EMPTIES) + provenance via the
                DURABLE marker (held after the tm_match STEP is deleted; a pure-LLM or
                backfilled-False run is NOT held; a FAILED/CANCELLED marker run IS held)
  N4            held_locale_reason's str|None contract is UNCHANGED
  T-6           every delivery surface refuses unapproved empties, incl. WITH allow_held,
                the ZIP bundle, and the KEY export
  T-7           approve -> empties ship EMPTY (not source); a non-gated partial still
                source-fills; a fully-empty target delivers on approval AND after the gate
                setting is turned off (N9)
  T-8           the growth guard (current empties must be a SUBSET of the approved set)
  T-12          MCP has no approve tool; the six routes are EXEMPT
  T-10          FK-cascade (project + single-doc delete, both modes) + preview count
  P4            the two partial unique indexes exist with the right WHERE across
                create_all + alembic-head, and the two modes coexist while a dup is rejected
  P7            key-mode audit customer derives from the PROJECT
  P10           tm_delivery does not import keys/key_files on a pure-document install
  P12           a doc-mode approval never covers a key-mode delivery
  data-export   the project_col Extractor round-trips BOTH modes (incl. a NULL document_id)

Hermetic: throwaway LOC_TMS_DATA_DIR + MockProvider. ASCII-only.
Final line: "TM EMPTY DELIVERY OK".
"""

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

from sqlalchemy import create_engine, text

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

_TMP = init_env("loctms_tmempty_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    DeliveryEmptyApproval,
    Document,
    Project,
    ProjectType,
    Role,
    RunStatus,
    Segment,
    StepType,
    User,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    apikeys,
    audit,
    content,
    data_export,
    delivery,
    export,
    gdpr,
    orchestrator,
    tm,
    tm_delivery,
)
from app.services import keys as keys_svc

init_db()
_LOCALE = "fr-FR"
_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _admin_key():
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "tmempty-admin@x.io")).first()
        if not u:
            u = User(email="tmempty-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "tmempty-api", scopes="api docs keys")
    return raw


def _project(name):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": _LOCALE,
            "default_provider": "mock",
            "customer_id": str(_CUST_ID),
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0].split("#")[0])


def _upload(pid):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("hello.txt", b"Hello world. This is a test.", "text/plain")},
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def _segs(doc_id):
    with Session(engine) as s:
        return list(
            s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == _LOCALE)
                .order_by(Segment.order_index)
            ).all()
        )


def _translate(doc_id, n=None):
    """Translate the first `n` segments (all when n is None)."""
    with Session(engine) as s:
        segs = list(
            s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == _LOCALE)
                .order_by(Segment.order_index)
            ).all()
        )
        for i, sg in enumerate(segs):
            if n is not None and i >= n:
                break
            sg.target_text = "traduit"
            sg.status = "mt"
            s.add(sg)
        s.commit()
        return len(segs)


def _mark(doc_id, *, locale=_LOCALE, used=True, status=RunStatus.COMPLETED, step=True):
    """Give a document a WorkflowRun that stamped (or did not stamp) used_tm_match, plus a
    tm_match step on its workflow. Returns (workflow_id, step_id, run_id)."""
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        wf = Workflow(name=f"wf-{_seq()}", project_id=doc.project_id)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        step_id = None
        if step:
            st = WorkflowStep(
                workflow_id=wf.id,
                step_type=StepType.TM_MATCH,
                order_index=0,
            )
            s.add(st)
            s.commit()
            s.refresh(st)
            step_id = st.id
        run = WorkflowRun(
            workflow_id=wf.id,
            document_id=doc_id,
            target_locale=locale,
            status=status,
            used_tm_match=used,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return wf.id, step_id, run.id


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue() + err.getvalue()


client = TestClient(app)
H = {"X-Loc-Token": _admin_key()}


def _customer():
    """One module-level customer bound to every project, so the audit customer_id is
    assertable (A-M6 b) AND every web project-create supplies the required customer field
    consistently (creating any customer makes the web form require one)."""
    from app.models import Customer

    with Session(engine) as s:
        c = s.exec(select(Customer).where(Customer.name == "tmempty-cust")).first()
        if c is None:
            c = Customer(name="tmempty-cust")
            s.add(c)
            s.commit()
            s.refresh(c)
        return c.id


_CUST_ID = _customer()


# ============================================================================ #
# 1. the companion decision + provenance via the durable marker (T-9/T-13, N4)
# ============================================================================ #
def test_decision_and_provenance():
    # (a) empties but NO provenance -> PASS (a non-tm_match deliverable is untouched)
    pid = _project("dec-noprov")
    doc = _upload(pid)
    _translate(doc, n=1)  # partial: seg0 filled, seg1 empty
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.PASS:
            _fail("empties with no tm_match provenance must be PASS")
        # held_locale_reason contract UNCHANGED (N4): partial -> None, str type otherwise
        if export.held_locale_reason(s, d, _LOCALE) is not None:
            _fail("held_locale_reason must stay None for a partial doc (N4 contract)")

    # (b) add the marker -> HOLD
    _wf, step_id, _run = _mark(doc)
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("a provenanced deliverable with empties + no approval must HOLD")

    # (c) T-13: delete the tm_match STEP -> STILL HELD (durable marker, not a mutable join)
    from app.services import workflows as wf_svc

    with Session(engine) as s:
        wf_svc.delete_step(s, step_id, workflow_id=_wf)
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("T-13: deleting the tm_match step must NOT un-hold (durable marker)")

    # (d) fully translate -> PASS (self-heal, T self-heal / no empties)
    _translate(doc)
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.PASS:
            _fail("a fully-translated provenanced deliverable must PASS (self-heal)")

    # (e) pure-LLM run (used_tm_match=False) with empties -> PASS (not retro-held, P5/T-9)
    pid2 = _project("dec-purellm")
    doc2 = _upload(pid2)
    _translate(doc2, n=1)
    _mark(doc2, used=False)
    with Session(engine) as s:
        d = s.get(Document, doc2)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.PASS:
            _fail("a used_tm_match=False (pure-LLM/backfilled) run must NOT be held")

    # (f) T-9: a FAILED marker-bearing run with empties is STILL HELD (no fail-open)
    pid3 = _project("dec-failed")
    doc3 = _upload(pid3)
    _translate(doc3, n=1)
    _mark(doc3, status=RunStatus.FAILED)
    with Session(engine) as s:
        d = s.get(Document, doc3)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("T-9: a FAILED marker-bearing run with empties must be HELD (no fail-open)")
    print("  1. decision + durable-marker provenance (delete-step, pure-LLM, FAILED) OK")


# ============================================================================ #
# 1b. THE PRIMARY-PATH REACHABILITY PIN (Batch 15 fix-train). Every other test in
# this suite CONSTRUCTS the held state (inject empties + fabricate a used_tm_match
# WorkflowRun). This one drives the ACTUAL PRODUCER: a REAL tm_match-only orchestrator
# run over a fresh doc. Before the fix that run source-filled the unmatched segment
# (target = source, status mt), so a COMPLETED run left ZERO empties and the gate could
# never fire on its live path -- the deliverable shipped English silently. This proves
# the completed-run -> HOLD -> approve -> empty-delivery flow is now reachable end-to-end
# from the orchestrator, reconciling the gate premise (§9, "unmatched segments carry
# EMPTY targets") with what the write-back actually leaves.
# ============================================================================ #
def test_real_tm_only_run_reaches_gate():
    pid = _project("real-tmonly")
    doc = _upload(pid)  # "Hello world. This is a test." -> 2 segments
    with Session(engine) as s:
        # a TM entry matching ONLY the first sentence; the second is genuinely unmatched.
        tm.write_back(s, pid, "Hello world.", "Bonjour tout le monde.", "en-US", _LOCALE)
        wf = Workflow(name=f"wf-real-{_seq()}", project_id=pid)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, step_type=StepType.TM_MATCH, order_index=0))
        s.commit()
        summary = orchestrator.run_workflow(s, wf.id, doc, _LOCALE)
        if summary["status"] != "completed":
            _fail(f"the tm_match-only run must complete, got {summary}")

    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(Segment.document_id == doc, Segment.target_locale == _LOCALE)
            ).all(),
            key=lambda x: x.order_index,
        )
        if segs[0].target_text != "Bonjour tout le monde.":
            _fail(f"the matched segment must fill, got {segs[0].target_text!r}")
        # THE FIX: the unmatched segment stays genuinely EMPTY (not source-filled).
        if segs[1].target_text != "":
            _fail(
                "REACHABILITY: a completed tm_match-only run must leave the unmatched target "
                f"EMPTY, not source-fill it -- got {segs[1].target_text!r}"
            )
        if segs[1].status == "mt":
            _fail("a never-produced slot must not carry status=mt (it was never translated)")
        d = s.get(Document, doc)
        # the durable marker was stamped by the REAL run, and the live predicate now HOLDS.
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc)).first()
        if not run.used_tm_match:
            _fail("the real tm_match run must stamp the durable used_tm_match marker")
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("a completed real tm_match-only run with empties must HOLD (gate reachable)")

    # the delivery surface refuses (409) until a human approves -- allow_held cannot bypass.
    r = client.get(
        f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE, "allow_held": 1}
    )
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"real-run empty doc must 409 empty_held (allow_held cannot bypass): {r.status_code}")

    # approve -> deliver WITH empties (empty string, NOT the English source).
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or not r.json().get("approved"):
        _fail(f"deliver-approve must 200 approved, got {r.status_code} {r.text[:160]}")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"an approved empty delivery must download 200, got {r.status_code}")
    if b"This is a test." in r.content:
        _fail("the approved delivery must emit the unmatched target EMPTY, never the source")
    print("  1b. a REAL tm_match-only run leaves empties -> HOLD -> approve -> empty delivery OK")


# ============================================================================ #
# 2. every delivery surface refuses unapproved empties incl. allow_held (T-6)
# ============================================================================ #
def test_surfaces_refuse():
    pid = _project("surf")
    doc = _upload(pid)
    _translate(doc, n=1)  # partial provenanced
    _mark(doc)

    # web export -> 409 empty_held (NOT the classic held; allow_held cannot bypass)
    r = client.get(f"/documents/{doc}/export", params={"locale": _LOCALE})
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"web export must 409 empty_held, got {r.status_code} {r.text[:160]}")
    r = client.get(f"/documents/{doc}/export", params={"locale": _LOCALE, "allow_held": 1})
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail("T-6: allow_held=1 must NOT bypass the empty gate on the web export")

    # /api download -> 409, plain AND with allow_held
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"/api download must 409 empty_held, got {r.status_code}")
    r = client.get(
        f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE, "allow_held": 1}
    )
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail("T-6: allow_held=1 must NOT bypass the empty gate on /api download")

    # CLI doc export --allow-held -> refused, no file written
    out_path = os.path.join(_TMP, "surf_out.txt")
    rc, txt = _run_cli(
        [
            "doc",
            "export",
            str(doc),
            "--locale",
            _LOCALE,
            "--out",
            out_path,
            "--force",
            "--allow-held",
        ]
    )
    if rc == 0 or os.path.exists(out_path):
        _fail(f"T-6: CLI doc export --allow-held must refuse an empty-held doc: rc={rc} {txt}")
    if "deliver-approve" not in txt:
        _fail(f"the CLI refusal must name the deliver-approve way-out: {txt}")

    # project ZIP bundle -> skip-and-report (naming approve), never source-echoed
    with Session(engine) as s:
        data, _n, _c, skipped = delivery.export_project(s, pid, locales=[_LOCALE], zip=True)
    if not any("deliver-approve" in x["reason"] for x in skipped):
        _fail(f"the bundle must skip-and-report the empty-held doc naming approve: {skipped}")
    if b"This is a test." in data:
        _fail("the bundle must NOT source-echo an empty-held doc")
    print("  2. surfaces refuse unapproved empties; allow_held cannot bypass (web/api/cli/zip) OK")


# ============================================================================ #
# 3. approve -> empties ship EMPTY, not source; N9 outlives the setting (T-7)
# ============================================================================ #
def test_approve_emits_empty():
    # a fully-empty provenanced TM-only doc: normally classic-held (all empty). Approve ->
    # delivers as EMPTY strings (not source).
    pid = _project("emit")
    doc = _upload(pid)  # all empty
    _mark(doc)
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("a fully-empty provenanced doc must HOLD before approval")

    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or not r.json().get("approved"):
        _fail(f"deliver-approve must 200 approved, got {r.status_code} {r.text[:160]}")

    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"an approved empty delivery must download 200, got {r.status_code} {r.text[:160]}")
    if b"Hello world" in r.content or b"This is a test." in r.content:
        _fail("T-7: the approved empty delivery must emit EMPTY, not the SOURCE text")

    # A-H1 (EXACT bytes, not merely source-absent): the "Hello world. This is a test." doc is
    # ONE unit of TWO segments. An all-empty approved unit must map to EXACTLY "" -- never the
    # lone-space artifact `" ".join(["",""])` == " ". Assert at the seam (deterministic).
    from app.services import export as _export

    with Session(engine) as s:
        d = s.get(Document, doc)
        mp = _export.paragraph_texts_for(s, d, _LOCALE, deliver_empties=True)
        if any(v != "" for v in mp.values()):
            _fail(f"A-H1: an all-empty unit must map to EXACTLY '' (no lone space), got {mp!r}")
    # And a PARTIALLY-empty unit drops the empty member (no doubled/trailing space).
    _translate(doc, n=1)  # seg0 filled, seg1 empty (still one unit)
    with Session(engine) as s:
        d = s.get(Document, doc)
        mp = _export.paragraph_texts_for(s, d, _LOCALE, deliver_empties=True)
        vals = [v for v in mp.values()]  # noqa: C416
        if vals != ["traduit"]:
            _fail(f"A-H1: a partially-empty unit must be 'traduit' (no trailing space), got {mp!r}")
    # restore the fully-empty shape for the N9 checks below
    with Session(engine) as s:
        for sg in s.exec(
            select(Segment).where(Segment.document_id == doc, Segment.target_locale == _LOCALE)
        ).all():
            sg.target_text = ""
            s.add(sg)
        s.commit()

    # N9: turn the gate OFF -> the covering approval STILL delivers (does not re-hold)
    from app.services import settings

    settings.set_many({"tm_empty_delivery_gate": "0"})
    try:
        with Session(engine) as s:
            d = s.get(Document, doc)
            dec = tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE)
            if dec != tm_delivery.RELEASE_EMPTIES:
                _fail("N9: a covering approval must RELEASE even with the gate turned off")
        r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
        if r.status_code != 200 or b"Hello world" in r.content:
            _fail("N9: the approved fully-empty target must still deliver empties after gate off")
    finally:
        settings.set_many({"tm_empty_delivery_gate": "1"})

    # LAW-TM-18: a NON-gated partial (no marker) still SOURCE-fills (byte-identical to today)
    pid2 = _project("emit-nongated")
    doc2 = _upload(pid2)
    _translate(doc2, n=1)  # partial, NO marker
    r = client.get(f"/api/documents/{doc2}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or b"This is a test." not in r.content:
        _fail("LAW-TM-18: a non-gated partial doc must still source-fill the empty half")
    print("  3. approve emits EMPTY not source; outlives gate-off (N9); non-gated source-fills OK")


# ============================================================================ #
# 4. growth guard + revoke + revoke-no-record no-op (T-8, N12)
# ============================================================================ #
def test_growth_and_revoke():
    pid = _project("grow")
    doc = _upload(pid)
    _translate(doc, n=1)  # seg0 filled, seg1 empty
    _mark(doc)
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail("approve of the partial-empty doc must 200")
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.RELEASE_EMPTIES:
            _fail("the approval must cover the current empty set")

    # GROW the empty set: clear seg0 too -> a NEW empty the approver never saw -> re-HELD
    with Session(engine) as s:
        segs = _segs(doc)
        seg0 = s.get(Segment, segs[0].id)
        seg0.target_text = ""
        s.add(seg0)
        s.commit()
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("T-8: a GROWN empty set (not a subset of covered) must re-HOLD")

    # revoke re-holds explicitly; a second revoke (no live approval) is a 200 no-op (N12)
    r = client.post(f"/api/documents/{doc}/deliver-revoke", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or not r.json().get("revoked"):
        _fail("revoke of a live approval must 200 revoked=true")
    r = client.post(f"/api/documents/{doc}/deliver-revoke", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or r.json().get("revoked"):
        _fail("N12: a revoke with no live approval must be a 200 no-op (revoked=false)")
    print("  4. growth guard re-holds; revoke re-holds; revoke-no-record no-op (N12) OK")


# ============================================================================ #
# 5. KEY/project mode: three empty shapes + approve + audit customer (N1, P7)
# ============================================================================ #
def _key_project():
    with Session(engine) as s:
        p = content.project_create(
            s,
            f"K-{_seq()}",
            source_locale="en-US",
            target_locales=_LOCALE,
            customer_id=_CUST_ID,
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _key_doc_id(pid):
    with Session(engine) as s:
        return s.exec(
            select(Document.id).where(Document.project_id == pid, Document.fmt == "keys")
        ).first()


def test_key_mode():
    from app.models import AuditLog

    pid = _key_project()  # already bound to _CUST_ID
    cust_id = _CUST_ID
    with Session(engine) as s:
        p = s.get(Project, pid)
        # shape 1: no value row for the locale (base only)
        keys_svc.create_key(s, p, "k_novalue", namespace="nav", base_value="Home")
        # shape 2: an empty target row
        k2 = keys_svc.create_key(s, p, "k_empty", namespace="nav", base_value="Save")
        keys_svc.set_value(s, k2, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        # A-H2: a PLURAL key with SOME empty forms is UNTRANSLATED (not "any-form" translated)
        kp = keys_svc.create_key(
            s, p, "k_plural", namespace="nav", base_value="1 item", is_plural=True
        )
        keys_svc.set_value(
            s, kp, _LOCALE, "1 article", origin=keys_svc.ORIGIN_HUMAN, plural_category="one"
        )
        keys_svc.set_value(
            s, kp, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN, plural_category="other"
        )
        # a fully-translated key (must NOT be an empty)
        k3 = keys_svc.create_key(s, p, "k_done", namespace="nav", base_value="Edit")
        keys_svc.set_value(s, k3, _LOCALE, "Modifier", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()

    # provenance: a used_tm_match run on the internal key document
    key_doc = _key_doc_id(pid)
    if not key_doc:
        _fail("expected an internal fmt=='keys' document for the key project")
    _mark(key_doc, locale=_LOCALE)

    with Session(engine) as s:
        p = s.get(Project, pid)
        empties = tm_delivery.key_empties(s, p, _LOCALE)
        names = {e["name"] for e in empties}
        # shapes 1+2 AND the A-H2 partially-empty plural key are all held
        if not ({"nav::k_novalue", "nav::k_empty", "nav::k_plural"} <= names):
            _fail(f"A-H2: key empties must include no-value + empty + partial-plural: {names}")
        if "nav::k_done" in names:
            _fail("a fully-translated key must NOT be reported empty")
        if tm_delivery.empty_gate_decision(s, project=p, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("a provenanced key project with empties + no approval must HOLD")

    # keys export refuses (409) until approved
    r = client.get(f"/api/projects/{pid}/keys/export", headers=H, params={"locale": _LOCALE})
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"keys export must 409 empty_held before approval: {r.status_code} {r.text[:140]}")

    # A-M6(a) shape (3): the gate is PERMISSION, empty_as is RENDERING -- even --empty-as skip
    # (which would OMIT empty keys from the file) is still REFUSED before it can render.
    r = client.get(
        f"/api/projects/{pid}/keys/export",
        headers=H,
        params={"locale": _LOCALE, "empty_as": "skip"},
    )
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"shape (3): keys export --empty-as skip must still 409, got {r.status_code}")

    # approve (project/key mode) -> export 200
    r = client.post(
        f"/api/projects/{pid}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 200 or not r.json().get("approved"):
        _fail(f"keys deliver-approve must 200 approved, got {r.status_code} {r.text[:160]}")
    r = client.get(f"/api/projects/{pid}/keys/export", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"an approved key export must 200, got {r.status_code} {r.text[:160]}")

    # P7: the key-mode approval row is document_id NULL, keyed on (project, locale)
    with Session(engine) as s:
        row = s.exec(
            select(DeliveryEmptyApproval).where(
                DeliveryEmptyApproval.project_id == pid,
                DeliveryEmptyApproval.document_id.is_(None),
            )
        ).first()
        if row is None or (row.covered or {}).get("mode") != "keys":
            _fail("the key approval must be a document_id-NULL, mode='keys' record")
        # P7 (A-M6 b): the approve + delivered audit events fired AND their customer_id was
        # derived from the PROJECT (customer_for_project), never customer_for_document.
        rows = s.exec(select(AuditLog).where(AuditLog.target == f"project {pid} {_LOCALE}")).all()
        actions = {a.action for a in rows}
        if "delivery.empty_approved" not in actions:
            _fail(f"the key-mode approve must audit delivery.empty_approved: {actions}")
        if "delivery.empty_delivered" not in actions:
            _fail(f"the approved key export must audit delivery.empty_delivered: {actions}")
        want_cust = audit.customer_for_project(pid)
        if want_cust != cust_id:
            _fail(
                f"customer_for_project should resolve the bound customer {cust_id}, got {want_cust}"
            )
        for a in rows:
            if a.action in ("delivery.empty_approved", "delivery.empty_delivered") and (
                a.customer_id != cust_id
            ):
                _fail(
                    f"P7: {a.action} customer_id must be the PROJECT customer {cust_id}, "
                    f"got {a.customer_id}"
                )
    print(
        "  5. key mode: 3 empty shapes + plural held; approve->export; audit customer==project OK"
    )


# ============================================================================ #
# 6. FK-cascade: project + single-doc delete, both modes; preview count (T-10)
# ============================================================================ #
def test_cascade():
    # doc-mode approval + single-document delete
    pid = _project("casc-doc")
    doc = _upload(pid)
    _translate(doc, n=1)
    _wf, _st, _run = _mark(doc)
    client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    with Session(engine) as s:
        if not s.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc)
        ).first():
            _fail("expected a doc-mode approval row before delete")
        content.delete_document(s, doc)  # the single-doc surface; must not IntegrityError
        s.commit()
        if s.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc)
        ).first():
            _fail("T-10: the doc-mode approval must be reaped by delete_document")

    # key-mode (document_id NULL) approval + project delete + preview count
    kpid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, kpid)
        k = keys_svc.create_key(s, p, "c_empty", namespace="n", base_value="X")
        keys_svc.set_value(s, k, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    _mark(_key_doc_id(kpid), locale=_LOCALE)
    with Session(engine) as s:
        tm_delivery.approve_keys(s, s.get(Project, kpid), _LOCALE, approved_by="c@x.io")
        preview = content.delete_project_preview(s, kpid)
        if preview["cascade"].get("delivery_empty_approvals", 0) < 1:
            _fail(f"delete_project_preview must count the key-mode approval: {preview['cascade']}")
    with Session(engine) as s:
        content.delete_project(s, kpid)  # must not IntegrityError
        s.commit()
        if s.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.project_id == kpid)
        ).first():
            _fail("T-10: project delete must reap the key-mode approval")
    print("  6. FK-cascade: single-doc + project delete (both modes) clean; preview counts OK")


# ============================================================================ #
# 7. partial unique indexes: WHERE across create_all + alembic; coexist + dup (P4)
# ============================================================================ #
_WANT_WHERE = {
    "ux_dea_doc": "where document_id is not null",
    "ux_dea_keys": "where document_id is null",
}


def _norm(sql):
    return " ".join((sql or "").lower().split())


def _index_sql(url):
    eng = create_engine(url)
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='deliveryemptyapproval'"
            )
        ).fetchall()
    eng.dispose()
    return {n: s or "" for n, s in rows}


def test_partial_indexes():
    # create_all path = the live hermetic DB
    live = _index_sql("sqlite:///" + os.path.join(_TMP, "loc_tms.db").replace("\\", "/"))
    for name, where in _WANT_WHERE.items():
        if where not in _norm(live.get(name, "")):
            _fail(f"P4 create_all: {name} missing partial {where!r}: {live.get(name)!r}")

    # alembic-head path
    from alembic.config import Config

    from alembic import command

    fresh = tempfile.mkdtemp(prefix="loctms_tmempty_mig_")
    url = "sqlite:///" + os.path.join(fresh, "loc_tms.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        command.upgrade(cfg, "head")
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev
    mig = _index_sql(url)
    for name, where in _WANT_WHERE.items():
        if where not in _norm(mig.get(name, "")):
            _fail(f"P4 alembic head: {name} missing partial {where!r}: {mig.get(name)!r}")

    # functional: a doc-mode and a key-mode row for the SAME (project, locale) coexist;
    # a duplicate WITHIN one mode is rejected by the partial unique index.
    pid = _project("pidx")
    doc = _upload(pid)
    from sqlalchemy.exc import IntegrityError

    with Session(engine) as s:
        s.add(DeliveryEmptyApproval(project_id=pid, document_id=doc, locale="xx"))
        s.add(DeliveryEmptyApproval(project_id=pid, document_id=None, locale="xx"))
        s.commit()  # coexist -> OK
    with Session(engine) as s:
        s.add(DeliveryEmptyApproval(project_id=pid, document_id=doc, locale="xx"))
        try:
            s.commit()
        except IntegrityError:
            pass
        else:
            _fail("P4: a duplicate doc-mode (document_id, locale) row must be rejected")
    with Session(engine) as s:
        s.add(DeliveryEmptyApproval(project_id=pid, document_id=None, locale="xx"))
        try:
            s.commit()
        except IntegrityError:
            pass
        else:
            _fail("P4: a duplicate key-mode (project_id, locale) NULL row must be rejected")
    print("  7. partial unique indexes: WHERE across create_all+alembic; coexist + dup reject OK")


# ============================================================================ #
# 8. mode-typed covered: a doc-mode approval never covers a key delivery (P12)
# ============================================================================ #
def test_mode_typed_covered():
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys_svc.create_key(s, p, "m_empty", namespace="n", base_value="X")
        keys_svc.set_value(s, k, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    _mark(_key_doc_id(pid), locale=_LOCALE)
    # forge a doc-mode-typed 'covered' onto a KEY-mode (document_id NULL) row -> must NOT cover
    with Session(engine) as s:
        p = s.get(Project, pid)
        empty_ids = tm_delivery._key_empty_ids(s, p, _LOCALE)
        s.add(
            DeliveryEmptyApproval(
                project_id=pid,
                document_id=None,
                locale=_LOCALE,
                status="approved",
                covered={"mode": "doc", "ids": empty_ids},  # WRONG mode tag
                covered_count=len(empty_ids),
            )
        )
        s.commit()
    with Session(engine) as s:
        p = s.get(Project, pid)
        if tm_delivery.empty_gate_decision(s, project=p, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("P12: a mode='doc' covered set must NOT cover a KEY delivery")
    print("  8. mode-typed covered: a doc-mode set never covers a key delivery (P12) OK")


# ============================================================================ #
# 9. MCP has no approve tool; the six routes are EXEMPT (T-12)
# ============================================================================ #
def test_mcp_exempt():
    spec = importlib.util.spec_from_file_location(
        "loc_mcp_reg_tmempty", os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")
    )
    reg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reg  # so @dataclass can resolve cls.__module__
    spec.loader.exec_module(reg)
    want = {
        ("GET", "/api/documents/{doc_id}/empties"): "human-repair-surface",
        ("POST", "/api/documents/{doc_id}/deliver-approve"): "human-decision",
        ("POST", "/api/documents/{doc_id}/deliver-revoke"): "human-decision",
        ("GET", "/api/projects/{pid}/keys/empties"): "human-repair-surface",
        ("POST", "/api/projects/{pid}/keys/deliver-approve"): "human-decision",
        ("POST", "/api/projects/{pid}/keys/deliver-revoke"): "human-decision",
    }
    for key, cat in want.items():
        if key not in reg.EXEMPT:
            _fail(f"T-12: {key} must be EXEMPT (no MCP approve tool)")
        if reg.EXEMPT[key][0] != cat:
            _fail(f"{key} EXEMPT category must be {cat}, got {reg.EXEMPT[key][0]}")
        if len(reg.EXEMPT[key][1].strip()) < 40:
            _fail(f"{key} EXEMPT reason must be >= 40 chars")
    # no tool name mentions deliver-approve / deliver-revoke
    for t in reg.TOOLS:
        nm = getattr(t, "name", "")
        if "deliver" in nm.lower() and "approve" in nm.lower():
            _fail(f"T-12: no MCP tool may bind an empty-delivery approval: {nm}")
    print("  9. MCP: six routes EXEMPT (human-decision / human-repair-surface); no approve tool OK")


# ============================================================================ #
# 10. keys-inertness: tm_delivery imports no keys machinery on a doc install (P10)
# ============================================================================ #
def test_keys_inertness():
    code = (
        "import sys; import app.services.tm_delivery;"
        "assert 'app.services.keys' not in sys.modules, 'tm_delivery armed keys';"
        "assert 'app.services.key_files' not in sys.modules, 'tm_delivery armed key_files';"
        "print('INERT')"
    )
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = tempfile.mkdtemp(prefix="loctms_tmempty_inert_")
    p = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=_ROOT, env=env
    )
    if p.returncode != 0 or "INERT" not in p.stdout:
        _fail(f"P10: tm_delivery must not import keys/key_files at load: {p.stderr or p.stdout}")
    print("  10. keys-inertness: tm_delivery loads no keys machinery on a doc install (P10) OK")


# ============================================================================ #
# 11. data-export Extractor round-trips BOTH modes (project_col anchor, P3)
# ============================================================================ #
def test_data_export_roundtrip():
    pid = _project("dx")
    doc = _upload(pid)
    with Session(engine) as s:
        s.add(DeliveryEmptyApproval(project_id=pid, document_id=doc, locale="a", approved_by="d@x"))
        s.add(
            DeliveryEmptyApproval(project_id=pid, document_id=None, locale="b", approved_by="d@x")
        )
        s.commit()
    ex = next((e for e in data_export.ENTITY_REGISTRY if e.name == "delivery_empty_approval"), None)
    if ex is None:
        _fail("a delivery_empty_approval Extractor must be registered")
    if ex.strategy != "project_col":
        _fail(f"the Extractor must anchor project_col (NOT doc_col), got {ex.strategy}")
    if getattr(ex, "parent_col", "") != "project_id":
        _fail(f"the Extractor must anchor parent_col='project_id', got {ex.parent_col!r}")
    # project_col confines on m.project_id (present in BOTH modes). A doc_col strategy would
    # be `document_id.in_(subq)`, which NULL never satisfies -> the key-mode row would export
    # by nothing. Prove project_id reaches BOTH rows (incl. the NULL document_id one).
    with Session(engine) as s:
        rows = list(
            s.exec(
                select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.project_id == pid)
            ).all()
        )
    modes = {("doc" if r.document_id is not None else "keys") for r in rows}
    if modes != {"doc", "keys"}:
        _fail(f"P3: the project_col anchor must reach BOTH modes, got {modes}")
    print("  11. data-export Extractor project_col reaches both modes incl. NULL doc_id (P3) OK")


# ============================================================================ #
# 12. GDPR: approved_by swept + exported symmetrically
# ============================================================================ #
def test_gdpr():
    swept = {label for label, _m, _c in gdpr._ERASE_SWEEP}
    if "delivery_empty_approvers" not in swept:
        _fail("approved_by must be in the GDPR erase sweep")
    if "delivery_empty_approvers" not in gdpr._ERASE_EXPORT_SECTION:
        _fail("the swept label must map to an export section (symmetry)")
    print("  12. GDPR: approved_by swept + exported symmetrically OK")


# ============================================================================ #
# 13. A-M3/B-MED-3: archived-key fail-open closed (gate sees the export's key universe)
# ============================================================================ #
def test_am3_archived():
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        # every NON-archived key translated; the ONLY empty is an ARCHIVED key
        k_done = keys_svc.create_key(s, p, "a_done", namespace="n", base_value="Go")
        keys_svc.set_value(s, k_done, _LOCALE, "Aller", origin=keys_svc.ORIGIN_HUMAN)
        k_arch = keys_svc.create_key(
            s, p, "a_empty", namespace="n", base_value="Stop", is_archived=True
        )
        keys_svc.set_value(s, k_arch, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    _mark(_key_doc_id(pid), locale=_LOCALE)
    with Session(engine) as s:
        p = s.get(Project, pid)
        # default export (include_archived=False) does NOT see the archived empty -> PASS
        if tm_delivery.empty_gate_decision(s, project=p, locale=_LOCALE) != tm_delivery.PASS:
            _fail("default (non-archived) key export must PASS when only an archived key is empty")
        # an --archived export DOES see it -> HOLD (the fail-open the gate flagged is closed)
        d = tm_delivery.empty_gate_decision(s, project=p, locale=_LOCALE, include_archived=True)
        if d != tm_delivery.HOLD:
            _fail(
                "A-M3: an include_archived export must HOLD on an archived empty key (no fail-open)"
            )
    # the whole export path: --archived refuses the archived empty un-approved
    r = client.get(
        f"/api/projects/{pid}/keys/export",
        headers=H,
        params={"locale": _LOCALE, "archived": "1"},
    )
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"A-M3: keys export --archived must 409 on an archived empty, got {r.status_code}")
    # the DEFAULT export still ships (the archived empty is not in its universe)
    r = client.get(f"/api/projects/{pid}/keys/export", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"the default key export must 200 (archived empty out of scope), got {r.status_code}")
    print("  13. A-M3: archived-key fail-open CLOSED (gate matches the export key universe) OK")


# ============================================================================ #
# 14. A-M5 uncovered paths: FULL_TEXT, all six CLI verbs, refusals, delivered-once,
#     CANCELLED, shrink, module-off, single-file 409
# ============================================================================ #
def _upload_ft(pid):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "full_text"},
        files={"file": ("body.txt", b"Alpha beta gamma delta.", "text/plain")},
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def test_am5_pins():
    from app.models import AuditLog

    # (a) FULL_TEXT approved-empty delivery: a FULL_TEXT doc with an empty body + marker is
    # held; approve -> download emits EMPTY (not the English source body).
    pid = _project("ft")
    doc = _upload_ft(pid)
    _mark(doc)
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("FULL_TEXT: a provenanced empty body must HOLD")
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"FULL_TEXT approve must 200, got {r.status_code} {r.text[:160]}")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or b"Alpha beta" in r.content:
        _fail(f"FULL_TEXT approved-empty must deliver EMPTY, not source: {r.status_code}")

    # (b) delivered-fires-once-per-delivery, NEVER on the empties read. Count events fresh.
    def _delivered_n(doc_id, loc):
        with Session(engine) as s:
            return len(
                s.exec(
                    select(AuditLog).where(
                        AuditLog.action == "delivery.empty_delivered",
                        AuditLog.target == f"document {doc_id} {loc}",
                    )
                ).all()
            )

    n0 = _delivered_n(doc, _LOCALE)
    client.get(f"/api/documents/{doc}/empties", headers=H, params={"locale": _LOCALE})
    if _delivered_n(doc, _LOCALE) != n0:
        _fail("delivery.empty_delivered must NOT fire on the empties READ route")
    client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if _delivered_n(doc, _LOCALE) != n0 + 1:
        _fail("delivery.empty_delivered must fire exactly once per actual delivery")

    # (c) CANCELLED marker run with empties is HELD (fail-open closed on cancel too)
    pid_c = _project("cancel")
    doc_c = _upload(pid_c)
    _translate(doc_c, n=1)
    _mark(doc_c, status=RunStatus.CANCELLED)
    with Session(engine) as s:
        d = s.get(Document, doc_c)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("a CANCELLED marker-bearing run with empties must be HELD")

    # (d) shrink STAYS covered: approve a 2-empty doc, fill ONE -> current subset -> RELEASE
    pid_s = _project("shrink")
    doc_s = _upload(pid_s)  # both empty
    _mark(doc_s)
    client.post(f"/api/documents/{doc_s}/deliver-approve", headers=H, params={"locale": _LOCALE})
    _translate(doc_s, n=1)  # fill seg0 -> current empties {seg1} subset of covered {seg0,seg1}
    with Session(engine) as s:
        d = s.get(Document, doc_s)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.RELEASE_EMPTIES:
            _fail("a SHRUNK empty set (subset of covered) must STAY covered (RELEASE)")

    # (e) ALL SIX CLI verbs (web+CLI parity is the required bar). doc verbs on a held doc:
    pid_cli = _project("cli")
    doc_cli = _upload(pid_cli)
    _translate(doc_cli, n=1)
    _mark(doc_cli)
    rc, out = _run_cli(["doc", "empties", str(doc_cli), "--locale", _LOCALE])
    if rc != 0 or "empty" not in out.lower():
        _fail(f"`doc empties` must list empties: rc={rc} {out}")
    rc, out = _run_cli(["doc", "deliver-approve", str(doc_cli), "--locale", _LOCALE])
    if rc != 0 or "approved" not in out.lower():
        _fail(f"`doc deliver-approve` must approve: rc={rc} {out}")
    rc, out = _run_cli(["doc", "deliver-revoke", str(doc_cli), "--locale", _LOCALE])
    if rc != 0 or "revoke" not in out.lower():
        _fail(f"`doc deliver-revoke` must revoke: rc={rc} {out}")

    # key verbs on a held key project + `keys export` CLI refusal names the verb
    pid_k = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid_k)
        kk = keys_svc.create_key(s, p, "cli_empty", namespace="n", base_value="X")
        keys_svc.set_value(s, kk, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    _mark(_key_doc_id(pid_k), locale=_LOCALE)
    rc, out = _run_cli(["keys", "empties", "--project", str(pid_k), "--locale", _LOCALE])
    if rc != 0 or "empty" not in out.lower():
        _fail(f"`keys empties` must list empties: rc={rc} {out}")
    rc, out = _run_cli(["keys", "export", "--project", str(pid_k), "--locale", _LOCALE])
    if rc == 0 or "deliver-approve" not in out:
        _fail(f"`keys export` on a held project must refuse naming deliver-approve: rc={rc} {out}")
    rc, out = _run_cli(["keys", "deliver-approve", "--project", str(pid_k), "--locale", _LOCALE])
    if rc != 0 or "approved" not in out.lower():
        _fail(f"`keys deliver-approve` must approve: rc={rc} {out}")
    rc, out = _run_cli(["keys", "export", "--project", str(pid_k), "--locale", _LOCALE])
    if rc != 0:
        _fail(f"`keys export` after approval must succeed: rc={rc} {out}")
    rc, out = _run_cli(["keys", "deliver-revoke", "--project", str(pid_k), "--locale", _LOCALE])
    if rc != 0 or "revoke" not in out.lower():
        _fail(f"`keys deliver-revoke` must revoke: rc={rc} {out}")

    # (f) module-off: the keys verbs ride the keys-module cli_refusal when disabled
    from app.services import settings

    settings.set_many({"key_projects_enabled": "0"})
    try:
        rc, out = _run_cli(["keys", "empties", "--project", str(pid_k), "--locale", _LOCALE])
        if rc == 0 or "key" not in out.lower():
            _fail(f"`keys empties` with key_projects_enabled=0 must refuse: rc={rc} {out}")
    finally:
        settings.set_many({"key_projects_enabled": "1"})

    # (g) /api project-export single-file fast-path 409 empty_held (one doc, one locale)
    pid_pe = _project("pe")
    doc_pe = _upload(pid_pe)
    _translate(doc_pe, n=1)
    _mark(doc_pe)
    r = client.get(f"/api/projects/{pid_pe}/export", headers=H, params={"locales": _LOCALE})
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"/api single-file export must 409 empty_held: {r.status_code} {r.text[:120]}")
    print("  14. A-M5: FULL_TEXT, six CLI verbs, refusals, delivered-once, CANCELLED, shrink OK")


# ============================================================================ #
# 15. B-H1/B-H2: delete_workflow blocks an un-holding delete; reaps approval run_id
# ============================================================================ #
def test_delete_workflow_gate():
    from app.services import workflows as wf_svc

    # B-H1: a HELD (doc,locale) whose ONLY marker is this workflow -> delete BLOCKED
    pid = _project("dw-held")
    doc = _upload(pid)
    _translate(doc, n=1)
    wf_id, _st, _run = _mark(doc)
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("setup: the doc must be empty-held")
        try:
            wf_svc.delete_workflow(s, wf_id)
        except wf_svc.WorkflowError as exc:
            if "un-hold" not in str(exc):
                _fail(f"B-H1: the block must name the un-hold hazard: {exc}")
        else:
            _fail("B-H1: deleting the sole-marker workflow of a held doc must be BLOCKED")
        # still held (the delete did not proceed)
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("B-H1: the deliverable must remain HELD after the blocked delete")

    # B-H2 + interplay: an APPROVED target -> decision RELEASE (provenance-independent); the
    # workflow delete PROCEEDS (not blocked), run_id is NULLED, the approval SURVIVES, and the
    # decision STAYS RELEASE after the marker is gone (no IntegrityError).
    pid2 = _project("dw-appr")
    doc2 = _upload(pid2)
    _translate(doc2, n=1)
    wf_id2, _st2, run_id2 = _mark(doc2)
    with Session(engine) as s:
        d2 = s.get(Document, doc2)
        tm_delivery.approve_doc(s, d2, _LOCALE, approved_by="a@x")
    with Session(engine) as s:
        row = s.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc2)
        ).first()
        if row is None or row.run_id != run_id2:
            _fail("setup: the approval must reference the workflow's run before delete")
        # delete must proceed WITHOUT IntegrityError even though an approval FKs the run
        wf_svc.delete_workflow(s, wf_id2)
        s.commit()
        row2 = s.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc2)
        ).first()
        if row2 is None:
            _fail("B-H2: the approval record must SURVIVE the workflow delete")
        if row2.run_id is not None:
            _fail("B-H2: the approval's run_id must be NULLED (the run is gone)")
        # interplay: no marker survives, yet the covering approval keeps the decision RELEASE
        d2 = s.get(Document, doc2)
        if (
            tm_delivery.empty_gate_decision(s, doc=d2, locale=_LOCALE)
            != tm_delivery.RELEASE_EMPTIES
        ):
            _fail("interplay: an approved target must STAY RELEASE after its marker is erased")
    print("  15. B-H1/B-H2: un-holding delete BLOCKED; approved delete proceeds, run_id nulled OK")


# ============================================================================ #
# 16. B-MED-4 + A-LOW-2/3/4: sensitive gate key, 409 way-out, locale validation
# ============================================================================ #
def test_sensitive_and_lows():
    from app.cli import cmd_config
    from app.services import settings

    # B-MED-4: disarming the gate needs per-key confirm (both sensitive-key sets)
    if "tm_empty_delivery_gate" not in settings.SENSITIVE_KEYS:
        _fail("B-MED-4: tm_empty_delivery_gate must be in settings.SENSITIVE_KEYS")
    if "tm_empty_delivery_gate" not in cmd_config._SENSITIVE_KEYS:
        _fail("B-MED-4: tm_empty_delivery_gate must be in cmd_config._SENSITIVE_KEYS")

    pid = _project("lows")
    doc = _upload(pid)
    _translate(doc, n=1)
    _mark(doc)

    # A-LOW-2: the /api download 409 names the deliver-approve verb + points approve_url at it
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    body = r.json()
    if "deliver-approve" not in (body.get("error") or ""):
        _fail(f"A-LOW-2: the 409 error must name the deliver-approve way-out: {body}")
    if "deliver-approve" not in (body.get("approve_url") or ""):
        _fail(f"A-LOW-2: approve_url must point at deliver-approve, not the empties read: {body}")

    # A-LOW-3/B-LOW-7: approving a bogus locale is a 400, not a junk 200 row
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": "zz-ZZ"})
    if r.status_code != 400:
        _fail(
            f"A-LOW-3: approve of a non-target locale must 400, got {r.status_code} {r.text[:120]}"
        )
    with Session(engine) as s:
        junk = s.exec(
            select(DeliveryEmptyApproval).where(
                DeliveryEmptyApproval.document_id == doc, DeliveryEmptyApproval.locale == "zz-ZZ"
            )
        ).first()
        if junk is not None:
            _fail("A-LOW-3: a rejected approve must NOT write a junk approval row")

    # A-LOW-4: GET /empties with NO locale is a 400 (parity with the download contract)
    r = client.get(f"/api/documents/{doc}/empties", headers=H)
    if r.status_code != 400:
        _fail(f"A-LOW-4: GET /empties with no locale must 400, got {r.status_code}")
    print("  16. B-MED-4 sensitive key; A-LOW-2 409 way-out; A-LOW-3/4 locale validation OK")


# ============================================================================ #
# 17. A-LOW-5/B-LOW-8: Jira attachment -- approved ships empties, unapproved unchanged
# ============================================================================ #
def test_jira_approved_empties():
    # A2-M4: exercise the CONNECTOR SEAM (jira._build_attachment_bytes + _locale_is_held), not
    # export.build_translated_file directly -- so gutting the connector build goes RED.
    from app.services.connectors import jira

    pid = _project("jira")
    doc = _upload(pid)
    _translate(doc, n=1)  # seg0 filled, seg1 empty -> PARTIAL (classic held does NOT hold it)
    _mark(doc)
    # N6 unchanged path: a PARTIAL target is not held; the attachment seam source-fills the empty.
    if jira._locale_is_held(doc, _LOCALE):
        _fail("N6: a PARTIAL target must NOT read as held for the Jira attachment seam")
    data = jira._build_attachment_bytes(doc, _LOCALE)
    if b"This is a test." not in data:
        _fail("N6: an UNAPPROVED Jira attachment must source-fill the empty seg (unchanged)")
    # Approved -> the attachment seam now ships EMPTY for the empty seg (ratified new behavior).
    with Session(engine) as s:
        d = s.get(Document, doc)
        tm_delivery.approve_doc(s, d, _LOCALE, approved_by="j@x")
    data2 = jira._build_attachment_bytes(doc, _LOCALE)
    if b"This is a test." in data2:
        _fail("A-LOW-5: an APPROVED target's Jira attachment (via the seam) must ship empties")
    print(
        "  17. A-LOW-5: Jira _build_attachment_bytes -- unapproved source-fills, approved emits OK"
    )


# ============================================================================ #
# 18. A2-M3: image render is BYTE-IDENTICAL across a covering approval (out of gate scope)
# ============================================================================ #
_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">\n'
    b'  <rect x="2" y="2" width="296" height="116" fill="#347"/>\n'
    b"  <style>.t { font-family: sans-serif; }</style>\n"
    b'  <text class="t" x="16" y="40">Summer sale.</text>\n'
    b'  <text class="t" x="16" y="80">Shop now.</text>\n'
    b"</svg>\n"
)


def test_image_byte_identical():
    from app.services import image_localization as il

    pid = _project("img")
    with Session(engine) as s:
        p = s.get(Project, pid)
        doc = il.ingest_image(s, p, "banner.svg", _SVG, actor="verify")
        doc_id = doc.id
    _translate(doc_id, n=1)  # fill region 0, leave region 1 empty -> PARTIAL
    _mark(doc_id)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("setup: the image doc must be empty-held before approval")
        before, _f = il.render_localized(s, d, _LOCALE)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        tm_delivery.approve_doc(s, d, _LOCALE, approved_by="i@x")
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        after, _f = il.render_localized(s, d, _LOCALE)
    if before != after:
        _fail(
            "A2-M3: image render_localized must be BYTE-IDENTICAL before/after a covering "
            "approval (the image surface is out of empty-gate scope, deliver_empties=False)"
        )
    print("  18. A2-M3: image render byte-identical across approval (out of gate scope) OK")


# ============================================================================ #
# 19. B2-H1: a REVOKED row re-HOLDS (never falls through to an erased marker); re-approvable
# ============================================================================ #
def test_revoke_after_marker_erasure():
    from app.services import workflows as wf_svc

    # DOC mode: approve -> delete_workflow (erase marker) -> revoke -> HOLD (not PASS/source);
    # then re-approve -> RELEASE. Use a fresh workflow so its delete removes the only marker.
    pid = _project("rev-doc")
    doc = _upload(pid)
    _translate(doc, n=1)  # partial
    wf_id, _st, _run = _mark(doc)
    with Session(engine) as s:
        d = s.get(Document, doc)
        tm_delivery.approve_doc(s, d, _LOCALE, approved_by="r@x")
    # A DIFFERENT delete is blocked while HELD, but an APPROVED target's delete proceeds:
    with Session(engine) as s:
        wf_svc.delete_workflow(s, wf_id)
        s.commit()
    with Session(engine) as s:
        d = s.get(Document, doc)
        # revoke -> the row is now `revoked`; B2-H1: must re-HOLD, NOT fall through to the
        # erased marker and deliver source-fill.
        tm_delivery.revoke_doc(s, d, _LOCALE, approved_by="r@x")
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("B2-H1: a REVOKED row after marker erasure must re-HOLD (not PASS/source-fill)")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 409:
        _fail(f"B2-H1: the revoked target must refuse download (409), got {r.status_code}")
    # re-approvable: the mint-confinement now sees HOLD (a row exists) -> approve succeeds
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"B2-H1: re-approve after revoke must 200, got {r.status_code} {r.text[:120]}")
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.RELEASE_EMPTIES:
            _fail("B2-H1: re-approve must restore RELEASE")

    # KEY mode twin (symmetric fix)
    kpid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, kpid)
        k = keys_svc.create_key(s, p, "rev_empty", namespace="n", base_value="X")
        keys_svc.set_value(s, k, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    kdoc = _key_doc_id(kpid)
    kwf, _kst, _krun = _mark(kdoc, locale=_LOCALE)
    with Session(engine) as s:
        tm_delivery.approve_keys(s, s.get(Project, kpid), _LOCALE, approved_by="r@x")
    with Session(engine) as s:
        wf_svc.delete_workflow(s, kwf)
        s.commit()
    with Session(engine) as s:
        tm_delivery.revoke_keys(s, s.get(Project, kpid), _LOCALE, approved_by="r@x")
    with Session(engine) as s:
        p = s.get(Project, kpid)
        if tm_delivery.empty_gate_decision(s, project=p, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("B2-H1 (key): a REVOKED key row after marker erasure must re-HOLD")
    print("  19. B2-H1: revoked row re-HOLDS (doc+key) after marker erasure; re-approvable OK")


# ============================================================================ #
# 20. A2-H1: archived-key approve deadlock closed (archived-only + mixed, widest covered)
# ============================================================================ #
def test_am2h1_archived_approve():
    # archived-ONLY empty: --archived HOLD -> approve 200 -> --archived export 200
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kd = keys_svc.create_key(s, p, "h_done", namespace="n", base_value="Go")
        keys_svc.set_value(s, kd, _LOCALE, "Aller", origin=keys_svc.ORIGIN_HUMAN)
        ka = keys_svc.create_key(s, p, "h_arch", namespace="n", base_value="Stop", is_archived=True)
        keys_svc.set_value(s, ka, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    _mark(_key_doc_id(pid), locale=_LOCALE)
    # --archived export is held; the review render SHOWS the archived empty (widest)
    r = client.get(f"/api/projects/{pid}/keys/empties", headers=H, params={"locale": _LOCALE})
    names = {e["name"] for e in r.json().get("empties", [])}
    if "n::h_arch" not in names:
        _fail(f"A2-H1: the review surface must SHOW the archived empty (widest): {names}")
    r = client.get(
        f"/api/projects/{pid}/keys/export", headers=H, params={"locale": _LOCALE, "archived": "1"}
    )
    if r.status_code != 409:
        _fail(f"A2-H1: --archived export must 409 on an archived empty, got {r.status_code}")
    # approve (covers the WIDEST set incl. the archived empty) -> --archived export now 200
    r = client.post(
        f"/api/projects/{pid}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 200:
        _fail(
            f"A2-H1: approve of an archived-only empty must 200, got {r.status_code} {r.text[:120]}"
        )
    r = client.get(
        f"/api/projects/{pid}/keys/export", headers=H, params={"locale": _LOCALE, "archived": "1"}
    )
    if r.status_code != 200:
        _fail(
            f"A2-H1: --archived export must 200 after approval (no permanent 409): {r.status_code}"
        )

    # MIXED: a non-archived empty AND an archived empty -> approve covers widest [both] ->
    # both a plain AND a --archived export deliver.
    pid2 = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid2)
        ke = keys_svc.create_key(s, p, "m_empty", namespace="n", base_value="A")
        keys_svc.set_value(s, ke, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        ka2 = keys_svc.create_key(s, p, "m_arch", namespace="n", base_value="B", is_archived=True)
        keys_svc.set_value(s, ka2, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    _mark(_key_doc_id(pid2), locale=_LOCALE)
    r = client.post(
        f"/api/projects/{pid2}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 200:
        _fail(f"A2-H1 mixed: approve must 200, got {r.status_code}")
    for arch in ("0", "1"):
        r = client.get(
            f"/api/projects/{pid2}/keys/export",
            headers=H,
            params={"locale": _LOCALE, "archived": arch},
        )
        if r.status_code != 200:
            _fail(
                f"A2-H1 mixed: export archived={arch} must 200 after approval, got {r.status_code}"
            )
    print(
        "  20. A2-H1: archived approve deadlock CLOSED (archived-only + mixed; widest covered) OK"
    )


# ============================================================================ #
# 21. A2-H2: the mint confinement -- approve on a non-gated PASS target is 400 + ZERO rows
# ============================================================================ #
def test_am2h2_mint_confinement():
    # a NEVER-tm_match doc with empties: decision PASS. Approve must refuse on EVERY surface and
    # write NO row (neutering the refuse-on-PASS mutation must go RED here).
    pid = _project("mint")
    doc = _upload(pid)
    _translate(doc, n=1)  # partial, NO marker -> PASS
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.PASS:
            _fail("setup: a non-tm_match partial doc must be PASS")

    def _rows():
        with Session(engine) as s:
            return len(
                s.exec(
                    select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc)
                ).all()
            )

    # /api
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 400 or _rows() != 0:
        _fail(
            f"A2-H2 /api: approve on PASS must 400 + zero rows, got {r.status_code} rows={_rows()}"
        )
    # web
    r = client.post(
        f"/documents/{doc}/deliver-approve", params={"locale": _LOCALE}, follow_redirects=False
    )
    if r.status_code not in (400, 303) or _rows() != 0:
        # web returns an error page (400) or (if a redirect) still no row
        _fail(
            f"A2-H2 web: approve on PASS must not write a row, got {r.status_code} rows={_rows()}"
        )
    # CLI
    rc, _out = _run_cli(["doc", "deliver-approve", str(doc), "--locale", _LOCALE])
    if rc == 0 or _rows() != 0:
        _fail(f"A2-H2 CLI: approve on PASS must fail + zero rows, got rc={rc} rows={_rows()}")
    # direct service call (doc AND key)
    with Session(engine) as s:
        d = s.get(Document, doc)
        try:
            tm_delivery.approve_doc(s, d, _LOCALE, approved_by="x@x")
        except tm_delivery.LocaleNotDeliverable:
            pass
        else:
            _fail("A2-H2 service: approve_doc on a non-gated PASS target must raise")
    if _rows() != 0:
        _fail("A2-H2: a refused approve must write ZERO rows")

    # key mode: a never-tm_match key project with an empty key -> PASS -> approve refuses
    kpid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, kpid)
        k = keys_svc.create_key(s, p, "mint_empty", namespace="n", base_value="X")
        keys_svc.set_value(s, k, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()  # NO _mark -> no provenance -> PASS
    r = client.post(
        f"/api/projects/{kpid}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 400:
        _fail(f"A2-H2 key: approve on a non-gated key project must 400, got {r.status_code}")
    with Session(engine) as s:
        if (
            s.exec(
                select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.project_id == kpid)
            ).first()
            is not None
        ):
            _fail("A2-H2 key: a refused key approve must write ZERO rows")

    # the locale-validation 400 message on its own (A2-H2 tail)
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": "zz-ZZ"})
    if r.status_code != 400 or "not a target locale" not in (r.json().get("error") or ""):
        _fail(f"A2-H2: the locale-validation 400 must name 'not a target locale': {r.text[:120]}")
    print("  21. A2-H2: mint confinement -- approve-on-PASS is 400 + zero rows (all surfaces) OK")


# ============================================================================ #
# 22. B2-M2: the delete_workflow blocker honors WILDCARD ("" locale) markers
# ============================================================================ #
def test_b2m2_wildcard_blocker():
    from app.services import workflows as wf_svc

    pid = _project("wild")
    doc = _upload(pid)
    _translate(doc, n=1)  # partial (held once provenanced)
    # a WILDCARD marker run (target_locale == "") -- holds every locale of the doc
    wf_id, _st, _run = _mark(doc, locale="")
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("setup: a wildcard-marked partial doc must be HELD for its locale")
        try:
            wf_svc.delete_workflow(s, wf_id)
        except wf_svc.WorkflowError as exc:
            if "un-hold" not in str(exc):
                _fail(f"B2-M2: the wildcard block must name the un-hold hazard: {exc}")
        else:
            _fail("B2-M2: deleting the sole WILDCARD-marker workflow of a held doc must be BLOCKED")
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("B2-M2: the doc must remain HELD after the blocked delete")
    print("  22. B2-M2: delete_workflow blocker honors wildcard ('' locale) markers OK")


# ============================================================================ #
# 23. R3-MED-1: the key-mode delete blocker evaluates at the WIDEST universe
# ============================================================================ #
def test_r3med1_archived_key_blocker():
    from app.services import workflows as wf_svc

    # archived-ONLY empty key project: every non-archived key translated, one archived empty.
    # The export/approve/review surfaces see it (widest); the blocker MUST too, or the delete
    # erases the marker and a later ?archived=1 export ships it un-approved.
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kd = keys_svc.create_key(s, p, "r_done", namespace="n", base_value="Go")
        keys_svc.set_value(s, kd, _LOCALE, "Aller", origin=keys_svc.ORIGIN_HUMAN)
        ka = keys_svc.create_key(s, p, "r_arch", namespace="n", base_value="Stop", is_archived=True)
        keys_svc.set_value(s, ka, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    wf_id, _st, _run = _mark(_key_doc_id(pid), locale=_LOCALE)
    # a DEFAULT decision would be PASS (archived empty invisible); the widest one is HOLD
    with Session(engine) as s:
        p = s.get(Project, pid)
        if tm_delivery.empty_gate_decision(s, project=p, locale=_LOCALE) != tm_delivery.PASS:
            _fail("setup: the DEFAULT key decision must be PASS (archived empty invisible)")
        try:
            wf_svc.delete_workflow(s, wf_id)
        except wf_svc.WorkflowError as exc:
            if "un-hold" not in str(exc):
                _fail(f"R3-MED-1: the block must name the un-hold hazard: {exc}")
        else:
            _fail(
                "R3-MED-1: deleting the sole marker of an archived-only-empty key proj must BLOCK"
            )
    print("  23. R3-MED-1: key delete blocker evaluates the WIDEST universe (archived-only) OK")


# ============================================================================ #
# 24. M1: the three key REVIEW surfaces compute `decision` at the widest universe
# ============================================================================ #
def test_m1_review_surface_decision():
    # archived-ONLY empty key project: every non-archived key translated, one archived empty.
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kd = keys_svc.create_key(s, p, "s_done", namespace="n", base_value="Go")
        keys_svc.set_value(s, kd, _LOCALE, "Aller", origin=keys_svc.ORIGIN_HUMAN)
        ka = keys_svc.create_key(s, p, "s_arch", namespace="n", base_value="Stop", is_archived=True)
        keys_svc.set_value(s, ka, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    _mark(_key_doc_id(pid), locale=_LOCALE)
    # /api GET: the LIST shows the archived key AND `decision` agrees (HOLD, not the narrow PASS)
    r = client.get(f"/api/projects/{pid}/keys/empties", headers=H, params={"locale": _LOCALE})
    body = r.json()
    names = {e["name"] for e in body.get("empties", [])}
    if "n::s_arch" not in names:
        _fail(f"M1: the /api review list must show the archived empty: {names}")
    if body.get("decision") != tm_delivery.HOLD:
        _fail(f"M1: /api decision must AGREE with the listed key (HOLD): {body.get('decision')!r}")
    # web panel: 200, renders the archived key AND "Held" (not a pass-decision contradiction)
    r = client.get(f"/projects/{pid}/keys/empties", params={"locale": _LOCALE})
    if r.status_code != 200 or "s_arch" not in r.text or "Held" not in r.text:
        _fail(f"M1: the web panel must show the archived key + 'Held', got {r.status_code}")
    # CLI: prints the key AND decision=hold
    rc, out = _run_cli(["keys", "empties", "--project", str(pid), "--locale", _LOCALE])
    if rc != 0 or "s_arch" not in out or "decision=hold" not in out:
        _fail(f"M1: `keys empties` must list the key + decision=hold: rc={rc} {out}")
    print("  24. M1: key review surfaces compute decision at the WIDEST universe (3 agree) OK")


# ============================================================================ #
# 25. M2: paragraph_texts_for deliver_empties keeps the STRUCTURAL skeleton fallback (A2-L1)
# ============================================================================ #
def test_m2_skeleton_carveout():
    from app.models import Document as _Doc

    # Craft a SEGMENTED doc: 3 skeleton units, but only 2 segments -> unit 2 is zero-segment.
    #   unit0 "Approved." : one segment, target "" (genuine approved-empty)  -> emits ""
    #   unit1 "Damaged."  : one segment, tag-damaged target + NULL source    -> skeleton (blanks)
    #   unit2 "Zeroseg."  : NO segment (chunk empty)                         -> skeleton (not chunk)
    pid = _project("m2")
    with Session(engine) as s:
        doc = _Doc(
            project_id=pid,
            filename="m2.txt",
            fmt="txt",
            mode="segmented",
            skeleton={
                "kind": "text",
                "seg_locale": "",
                "seg_engine": None,
                "units": [
                    {"p_index": 0, "text": "Approved."},
                    {"p_index": 1, "text": "Damaged."},
                    {"p_index": 2, "text": "Zeroseg."},
                ],
            },
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        doc_id = doc.id
        # seg0: genuine approved-empty (target "")
        s.add(
            Segment(
                document_id=doc_id,
                target_locale=_LOCALE,
                order_index=0,
                source_text="Approved.",
                target_text="",
            )
        )
        # seg1: tag-damaged (invented token in target) with a NULL/empty source -> the tag guard
        # source-falls-back to "" and marks it emptied (`blanks`).
        s.add(
            Segment(
                document_id=doc_id,
                target_locale=_LOCALE,
                order_index=1,
                source_text="",
                target_text="⟦g1⟧broken",
            )
        )
        s.commit()

    with Session(engine) as s:
        d = s.get(_Doc, doc_id)
        mp = export.paragraph_texts_for(s, d, _LOCALE, deliver_empties=True)
    if mp.get("0") != "":
        _fail(f"M2: a genuine approved-empty unit must emit '' , got {mp.get('0')!r}")
    if mp.get("1") != "Damaged.":
        _fail(f"M2: a tag-damage NULL-source unit must keep its skeleton, got {mp.get('1')!r}")
    if mp.get("2") != "Zeroseg.":
        _fail(f"M2: a zero-segment unit must keep its skeleton, got {mp.get('2')!r}")
    print("  25. M2: deliver_empties keeps the structural skeleton fallback (A2-L1) OK")


def test_issue105_midrun_approve_refused():
    """ISSUE #105 (stress-0906, sev:high): a deliver-approve issued while a run is STILL
    EXECUTING minted a covering approval over every not-yet-translated segment, flipping the
    honest 409 into a SILENT BLANK deliverable (126 segments -> a 61-byte file, HTTP 200) for
    the rest of the run. Counterfactual: without the in-flight refusal in `approve_doc`, the
    approve below returns 200 and the download returns a blank body with 200.

    Also pins the message half: the 409 no longer asserts "a TM-only workflow" (the reporter's
    workflow was `tm_match -> llm_agent`, mid-flight) -- it names the run that is still going.
    """
    from app.services import workflows as _wf_mod

    # DRIFT GUARD: the gate's in-flight predicate must stay the SAME status set the rest of
    # the product calls "a live run" (a status added to one and not the other fails here).
    if tuple(tm_delivery._ACTIVE_RUN_STATUSES) != tuple(_wf_mod._ACTIVE_RUN_STATUSES):
        _fail(
            "tm_delivery._ACTIVE_RUN_STATUSES drifted from workflows._ACTIVE_RUN_STATUSES: "
            f"{tm_delivery._ACTIVE_RUN_STATUSES} vs {_wf_mod._ACTIVE_RUN_STATUSES}"
        )

    pid = _project("issue105")
    doc = _upload(pid)  # NOTHING translated: every segment is empty, like mid-run
    _wf, _step, run_id = _mark(doc, status=RunStatus.RUNNING)  # tm_match stamped; MT still going
    n_segs = len(_segs(doc))

    def _rows():
        with Session(engine) as s:
            return len(
                s.exec(
                    select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc)
                ).all()
            )

    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.empty_gate_decision(s, doc=d, locale=_LOCALE) != tm_delivery.HOLD:
            _fail("setup: a mid-run provenanced empty doc must HOLD")

    # (a) the 409 tells the truth about the state instead of "a TM-only workflow"
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"#105: mid-run download must 409 empty_held, got {r.status_code}")
    msg = r.json().get("error") or ""
    if "TM-only workflow" in msg:
        _fail(f"#105: the 409 must not claim a TM-only workflow mid-run: {msg}")
    if f"run #{run_id}" not in msg or "STILL IN PROGRESS" not in msg:
        _fail(f"#105: the 409 must name the in-flight run: {msg}")

    # (b) approve REFUSED on all four surfaces, zero rows written
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 400 or _rows() != 0:
        _fail(f"#105 /api: mid-run approve must 400 + 0 rows, got {r.status_code} rows={_rows()}")
    if f"run #{run_id}" not in (r.json().get("error") or ""):
        _fail(f"#105 /api: the refusal must name the run: {r.json()}")
    r = client.post(
        f"/documents/{doc}/deliver-approve", params={"locale": _LOCALE}, follow_redirects=False
    )
    if r.status_code not in (400, 303) or _rows() != 0:
        _fail(f"#105 web: mid-run approve wrote a row ({r.status_code}, rows={_rows()})")
    rc, out = _run_cli(["doc", "deliver-approve", str(doc), "--locale", _LOCALE])
    if rc == 0 or _rows() != 0:
        _fail(f"#105 CLI: mid-run approve must fail, got rc={rc} rows={_rows()} {out[:200]}")
    with Session(engine) as s:
        d = s.get(Document, doc)
        try:
            tm_delivery.approve_doc(s, d, _LOCALE, approved_by="x@x")
        except tm_delivery.ApprovalRunInFlight:
            pass
        else:
            _fail("#105 service: approve_doc must raise ApprovalRunInFlight mid-run")

    # (b2) the WEB review panel does not offer the button the service will refuse (the trap
    # the reporter followed), while REVOKE (the safe direction) is untouched.
    rp = client.get(f"/documents/{doc}/empties", params={"locale": _LOCALE})
    if rp.status_code != 200:
        _fail(f"#105: the empties panel must render mid-run, got {rp.status_code}")
    if "Approve delivering empty targets" in rp.text:
        _fail("#105 web: the panel still offers approve while a run is in flight")
    if f"Run #{run_id} is still in progress" not in rp.text:
        _fail("#105 web: the panel does not say a run is still in progress")

    # (c) THE HARM: the deliverable is still refused, never a silent blank 200
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 409:
        _fail(f"#105: the document must stay held mid-run, got {r.status_code} ({len(r.content)}b)")

    # (d) revoke stays available mid-run (the SAFE direction is never blocked)
    with Session(engine) as s:
        d = s.get(Document, doc)
        if tm_delivery.revoke_doc(s, d, _LOCALE, approved_by="x@x") is not False:
            _fail("#105: a no-record revoke must stay a 200 no-op mid-run")

    # (e) once the run is terminal the approval works exactly as before (no regression)
    with Session(engine) as s:
        rr = s.get(WorkflowRun, run_id)
        rr.status = RunStatus.COMPLETED
        s.add(rr)
        s.commit()
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or r.json().get("covered_count") != n_segs:
        _fail(f"#105: approve after the run ends must succeed, got {r.status_code} {r.text[:160]}")
    # and the post-run message drops the in-flight sentence
    with Session(engine) as s:
        d = s.get(Document, doc)
        m2 = tm_delivery.held_message(
            s, doc=d, locale=_LOCALE, count=1, unit="segment", way_out="X."
        )
    if "STILL IN PROGRESS" in m2 or "tm_match workflow" not in m2:
        _fail(f"#105: the post-run held message must not claim a live run: {m2}")

    # (f) KEY mode: the same refusal over a project's internal key documents
    kpid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, kpid)
        k = keys_svc.create_key(s, p, "issue105_key", namespace="n", base_value="X")
        keys_svc.set_value(s, k, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    kdoc = _key_doc_id(kpid)
    _wf2, _s2, krun = _mark(kdoc, locale=_LOCALE, status=RunStatus.RUNNING)
    r = client.post(
        f"/api/projects/{kpid}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 400 or f"run #{krun}" not in (r.json().get("error") or ""):
        _fail(f"#105 keys: mid-run approve must 400 naming the run, got {r.status_code} {r.text}")
    with Session(engine) as s:
        if s.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.project_id == kpid)
        ).all():
            _fail("#105 keys: a refused mid-run approve must write ZERO rows")
    print("  26. #105: a mid-run deliver-approve is REFUSED (4 surfaces, doc + keys); the")
    print("      409 names the live run instead of claiming a TM-only workflow OK")


def test_issue105_approval_suspended_while_a_run_is_in_flight():
    """ISSUE #105, ROUND-1 HIGH-1: the READ path honours the in-flight rule too.

    Closing only the MINT path left the VIOLATION reachable through the ordinary product
    lifecycle with NO mid-run human action at all: (1) a TM-only run leaves the locale empty;
    (2) the operator approves empty delivery -- legitimate, nothing in flight; (3) an MT
    workflow is later run over the same (document, locale) -- the normal next step; (4) for the
    whole run `GET /api/documents/{id}/download` answered HTTP 200 with a BLANK file, because a
    covering approval still released empties a live run had not reached yet.

    A covering approval is now SUSPENDED (never revoked) while any run is in flight and RESUMES
    on its own afterwards, covering the targets that are still empty then. This is the gate's
    own probe, pinned: approve -> start a run -> the mid-run download must NOT be 200/0 bytes.

    Counterfactual: without the in-flight check in `_doc_decision` the mid-run download below
    is `200` with `b""`, and `empty_gate_decision` says `release_empties`."""
    pid = _project("issue105-suspend")
    doc = _upload(pid)  # every target empty
    _wf, _st, _run1 = _mark(doc, status=RunStatus.COMPLETED)  # provenanced, nothing in flight
    n_segs = len(_segs(doc))

    def _decision():
        with Session(engine) as s:
            return tm_delivery.empty_gate_decision(s, doc=s.get(Document, doc), locale=_LOCALE)

    # (a) the SANCTIONED pre-run state: the approval is legitimate and the empties really ship.
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or r.json().get("covered_count") != n_segs:
        _fail(f"#105 HIGH-1 setup: the pre-run approval must succeed, got {r.status_code} {r.text}")
    if _decision() != tm_delivery.RELEASE_EMPTIES:
        _fail(f"#105 HIGH-1 setup: an approved, run-free doc must release, got {_decision()}")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"#105 HIGH-1 setup: the approved empties must download, got {r.status_code}")

    # (b) THE HARM the gate reproduced: a NEW run starts over the same (doc, locale).
    _wf2, _st2, run2 = _mark(doc, status=RunStatus.RUNNING)
    if _decision() != tm_delivery.HOLD:
        _fail(f"#105 HIGH-1: a covering approval must be SUSPENDED mid-run, got {_decision()}")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code == 200:
        _fail(f"#105 HIGH-1: mid-run download shipped {len(r.content)} bytes with HTTP 200")
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"#105 HIGH-1: the mid-run export must 409 empty_held, got {r.status_code}")
    msg = r.json().get("error") or ""
    if f"run #{run2}" not in msg or "SUSPENDED, not revoked" not in msg:
        _fail(f"#105 HIGH-1: the 409 must name the run + say the approval is suspended: {msg}")
    if "for as long as runs keep starting" not in msg:
        _fail(f"#105 HIGH-1: the 409 must state the continuous-run consequence: {msg}")
    # LOW-1: the in-flight refusal must NOT hand back the instruction that caused the incident.
    if "deliver-approve" in msg:
        _fail(f"#105 LOW-1: the in-flight 409 still prints the deliver-approve way-out: {msg}")

    # ... on every export surface, not just /api: the CLI and the project ZIP bundle.
    rc, out = _run_cli(["doc", "export", str(doc), "--locale", _LOCALE, "--allow-held"])
    if rc == 0 or f"run #{run2}" not in out:
        _fail(f"#105 HIGH-1 CLI: mid-run export must refuse naming the run, rc={rc} {out[:300]}")
    with Session(engine) as s:
        _data, _n, _c, sk = delivery.export_project(s, pid, locales=[_LOCALE], zip=True)
    if len(sk) != 1 or f"run #{run2}" not in (sk[0].get("reason") or ""):
        _fail(f"#105 HIGH-1 bundle: the held document must name the in-flight run: {sk}")

    # (c) SUSPENDED, NOT REVOKED -- the human's decision row is untouched.
    with Session(engine) as s:
        row = s.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc)
        ).one()
        if row.status != "approved" or row.covered_count != n_segs:
            _fail(f"#105 HIGH-1: the approval must survive the suspension untouched: {row}")

    # (d) it RESUMES by itself once nothing is in flight -- no re-approval, no human action --
    #     and covers only what is still empty then (the run filled one segment).
    with Session(engine) as s:
        sg = _segs(doc)[0]
        live = s.get(Segment, sg.id)
        live.target_text = "traduit"
        s.add(live)
        rr = s.get(WorkflowRun, run2)
        rr.status = RunStatus.COMPLETED
        s.add(rr)
        s.commit()
    if _decision() != tm_delivery.RELEASE_EMPTIES:
        _fail(f"#105 HIGH-1: the approval must resume once no run is in flight: {_decision()}")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"#105 HIGH-1: the resumed approval must deliver again, got {r.status_code}")

    # (e) KEY mode rides the same rule.
    kpid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, kpid)
        k = keys_svc.create_key(s, p, "issue105_suspend_key", namespace="n", base_value="X")
        keys_svc.set_value(s, k, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    kdoc = _key_doc_id(kpid)
    _wf3, _st3, krun = _mark(kdoc, locale=_LOCALE, status=RunStatus.COMPLETED)
    r = client.post(
        f"/api/projects/{kpid}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 200:
        _fail(f"#105 HIGH-1 keys: the pre-run approval must succeed, got {r.status_code} {r.text}")

    def _kdecision():
        with Session(engine) as s:
            return tm_delivery.empty_gate_decision(s, project=s.get(Project, kpid), locale=_LOCALE)

    if _kdecision() != tm_delivery.RELEASE_EMPTIES:
        _fail(f"#105 HIGH-1 keys: an approved, run-free project must release: {_kdecision()}")
    with Session(engine) as s:
        rr = s.get(WorkflowRun, krun)
        rr.status = RunStatus.RUNNING
        s.add(rr)
        s.commit()
    if _kdecision() != tm_delivery.HOLD:
        _fail(f"#105 HIGH-1 keys: the approval must be suspended mid-run: {_kdecision()}")
    print("  27. #105 HIGH-1: a covering approval is SUSPENDED (not revoked) while a run is in")
    print("      flight -- no 200/blank export mid-run -- and resumes by itself after OK")


def _advanced_run(project_id, *, units=(), target_locale=_LOCALE, status="running", name="adv"):
    """An Advanced-Workflow mission + a run in `status` over `project_id` (round-2 HIGH-1).

    Rows only: the harness loop is not what the gate reads. What it reads is exactly what a
    live mission has -- a non-terminal `AdvancedRun` on the project and the mission's
    `model_roster` (`target_locale` + the system-authored `units`, each carrying the
    `segment_id` `advanced.tools._document_units_by_key` binds). Returns (mission_id, run_id).
    """
    from app.models import AdvancedMission, AdvancedRun

    with Session(engine) as s:
        m = AdvancedMission(
            project_id=project_id,
            name=f"{name}-{_seq()}",
            brief="b",
            tool_allowlist=["target.propose", "deliver"],
            model_roster={
                "mode": "conductor",
                "target_locale": target_locale,
                "units": [{"unit_key": f"seg:{sid}", "segment_id": sid} for sid in units],
            },
            scripted_actions=[],
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        r = AdvancedRun(mission_id=m.id, project_id=project_id, status=status)
        s.add(r)
        s.commit()
        s.refresh(r)
        return m.id, r.id


def _advanced_status(run_id, status):
    from app.models import AdvancedRun

    with Session(engine) as s:
        r = s.get(AdvancedRun, run_id)
        r.status = status
        s.add(r)
        s.commit()


def test_issue105_high1_advanced_run_and_batch_member_hold():
    """GATE B ROUND-2 HIGH-1: the in-flight rule saw CLASSIC runs only.

    `active_run_id` read `WorkflowRun` alone, so for the whole duration of an Advanced-Workflow
    DOCUMENT or KEYS mission -- committed RUNNING before its loop starts, every target empty
    until its terminal `deliver` -- the classic empty gate answered "no run in flight". The
    gate reproduced #105's literal VIOLATION one tier over: approve legitimately (nothing
    classic running), run an Advanced mission over the same (document, locale), and the
    document downloaded HTTP 200 with ZERO bytes while `deliver-approve` returned 200 and
    minted a covering row over segments an engine was actively translating -- all while this
    train's own copy told the operator "empties cannot ship while a run may still fill them".

    This is the gate's probe, pinned verbatim, plus the four confinement arms that make the
    widening a MATCH and not a blanket: a sibling document, a different locale, a mission that
    binds no segment at all, and the resume once the run is terminal.

    Counterfactual: dropping the `_advanced_run_id` arm from `active_run_ref` turns (b)'s
    decision back into `release_empties` and (c)'s download back into `200` with `b""`.
    """
    from app.models import AdvancedRunStatus

    # DRIFT GUARD: the Advanced tuple is DERIVED from the tier's own lifecycle, so a new
    # non-terminal status joins it by construction. Pin that it stays derived.
    expected = tuple(s for s in AdvancedRunStatus.ALL if s not in AdvancedRunStatus.TERMINAL)
    if tuple(tm_delivery._ACTIVE_ADVANCED_STATUSES) != expected:
        _fail(
            "tm_delivery._ACTIVE_ADVANCED_STATUSES is no longer ALL-minus-TERMINAL: "
            f"{tm_delivery._ACTIVE_ADVANCED_STATUSES} vs {expected}"
        )

    pid = _project("issue105-adv")
    doc = _upload(pid)  # every target empty
    sib = _upload(pid)  # a SIBLING document in the same project
    wf_doc, _st_doc, _r_doc = _mark(doc, status=RunStatus.COMPLETED)  # provenanced, none live
    _mark(sib, status=RunStatus.COMPLETED)
    n_segs = len(_segs(doc))
    doc_sids = [sg.id for sg in _segs(doc)]

    def _decision(d=doc):
        with Session(engine) as s:
            return tm_delivery.empty_gate_decision(s, doc=s.get(Document, d), locale=_LOCALE)

    # (a) THE SANCTIONED PRE-RUN STATE -- verbatim from the gate's probe.
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or r.json().get("covered_count") != n_segs:
        _fail(f"HIGH-1 setup: the pre-run approval must succeed, got {r.status_code} {r.text}")
    if _decision() != tm_delivery.RELEASE_EMPTIES:
        _fail(f"HIGH-1 setup: an approved, run-free doc must release, got {_decision()}")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200 or r.content not in (b"", b"\n"):
        _fail(f"HIGH-1 setup: the approved empties must download, got {r.status_code}")

    # (b) an ADVANCED run goes RUNNING over the same (project, document, locale).
    _mid, arun = _advanced_run(pid, units=doc_sids, target_locale=_LOCALE, status="running")
    if _decision() != tm_delivery.HOLD:
        _fail(f"HIGH-1: an Advanced run in flight must SUSPEND the approval, got {_decision()}")

    # (c) THE HARM: no 200/0-byte download while the mission is live, on every export surface.
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code == 200:
        _fail(f"HIGH-1: mid-Advanced-run download shipped {len(r.content)} bytes with HTTP 200")
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"HIGH-1: the mid-Advanced-run export must 409 empty_held, got {r.status_code}")
    msg = r.json().get("error") or ""
    if f"Advanced-Workflow run #{arun}" not in msg:
        _fail(f"HIGH-1: the 409 must name the ADVANCED run and its tier: {msg}")
    if "SUSPENDED, not revoked" not in msg or "deliver-approve" in msg:
        _fail(f"HIGH-1: the in-flight 409 lost its round-1 shape: {msg}")
    rc, out = _run_cli(["doc", "export", str(doc), "--locale", _LOCALE, "--allow-held"])
    if rc == 0 or f"Advanced-Workflow run #{arun}" not in out:
        _fail(
            f"HIGH-1 CLI: mid-Advanced-run export must refuse naming the run, rc={rc} {out[:300]}"
        )
    with Session(engine) as s:
        _data, _n, _c, sk = delivery.export_project(s, pid, locales=[_LOCALE], zip=True)
    if not any(f"Advanced-Workflow run #{arun}" in (row.get("reason") or "") for row in sk):
        _fail(f"HIGH-1 bundle: the held document must name the in-flight Advanced run: {sk}")

    # (d) THE MINT: deliver-approve is refused too (the round-1 filed defect, one tier over).
    with Session(engine) as s:  # revoke so the mint path is reachable at all
        d = s.get(Document, doc)
        tm_delivery.revoke_doc(s, d, _LOCALE, approved_by="x@x")
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 400 or f"Advanced-Workflow run #{arun}" not in (
        r.json().get("error") or ""
    ):
        _fail(f"HIGH-1: a mid-Advanced-run approve must 400 naming the run, got {r.status_code}")
    rp = client.get(f"/documents/{doc}/empties", params={"locale": _LOCALE})
    if f"Advanced-Workflow run #{arun} is still in progress" not in rp.text:
        _fail("HIGH-1 web: the panel must name the Advanced run, not call it a classic Run")
    if "Approve delivering empty targets" in rp.text:
        _fail("HIGH-1 web: the panel still offers approve while an Advanced run is in flight")

    # (e) CONFINEMENT -- the widening is a MATCH, not a blanket.
    if _decision(sib) not in (tm_delivery.PASS, tm_delivery.HOLD):
        _fail("HIGH-1: unexpected sibling decision")
    with Session(engine) as s:
        if tm_delivery.active_run_ref(s, doc_id=sib, project_id=pid, locale=_LOCALE) is not None:
            _fail("HIGH-1: a mission bound to document A must not hold its SIBLING document")
        if tm_delivery.active_run_ref(s, doc_id=doc, project_id=pid, locale="de-DE") is not None:
            _fail("HIGH-1: a fr-FR mission must not hold a de-DE deliverable")
    _m2, arun2 = _advanced_run(pid, units=(), target_locale=_LOCALE, status="running")
    with Session(engine) as s:
        ref = tm_delivery.active_run_ref(s, doc_id=sib, project_id=pid, locale=_LOCALE)
    if ref is not None:
        _fail(
            "HIGH-1: a mission that binds NO segment cannot write into any document "
            f"(target.propose refuses it), so it must hold none -- got {ref}"
        )
    _advanced_status(arun2, AdvancedRunStatus.COMPLETED)

    # (f) EVERY non-terminal status counts; a terminal one resumes the approval by itself.
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 400:
        _fail("HIGH-1: still in flight, the approve must stay refused")
    for st in (AdvancedRunStatus.PENDING, AdvancedRunStatus.PAUSED):
        _advanced_status(arun, st)
        if _decision() != tm_delivery.HOLD:
            _fail(f"HIGH-1: an Advanced run in {st} must still hold, got {_decision()}")
    for st in AdvancedRunStatus.TERMINAL:
        _advanced_status(arun, st)
        with Session(engine) as s:
            if tm_delivery.active_run_ref(s, doc_id=doc, project_id=pid, locale=_LOCALE):
                _fail(f"HIGH-1: a {st} Advanced run must not hold anything")
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"HIGH-1: once the Advanced run is terminal the approve must work, {r.status_code}")

    # (g) KEY mode rides the same rule -- the tier #108 itself was filed against.
    kpid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, kpid)
        k = keys_svc.create_key(s, p, "issue105_adv_key", namespace="n", base_value="X")
        keys_svc.set_value(s, k, _LOCALE, "", origin=keys_svc.ORIGIN_HUMAN)
        s.commit()
    kdoc = _key_doc_id(kpid)
    _mark(kdoc, locale=_LOCALE, status=RunStatus.COMPLETED)
    r = client.post(
        f"/api/projects/{kpid}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 200:
        _fail(f"HIGH-1 keys: the pre-run approval must succeed, got {r.status_code} {r.text}")

    def _kdecision():
        with Session(engine) as s:
            return tm_delivery.empty_gate_decision(s, project=s.get(Project, kpid), locale=_LOCALE)

    if _kdecision() != tm_delivery.RELEASE_EMPTIES:
        _fail(f"HIGH-1 keys: an approved, run-free project must release: {_kdecision()}")
    _km, karun = _advanced_run(kpid, units=(), target_locale=_LOCALE, status="running", name="advk")
    if _kdecision() != tm_delivery.HOLD:
        _fail("HIGH-1 keys: an Advanced KEYS mission in flight must suspend the approval")
    r = client.post(
        f"/api/projects/{kpid}/keys/deliver-approve", headers=H, params={"locale": _LOCALE}
    )
    if r.status_code != 400 or f"Advanced-Workflow run #{karun}" not in (
        r.json().get("error") or ""
    ):
        _fail(f"HIGH-1 keys: mid-Advanced-run approve must 400 naming the run, {r.status_code}")
    _advanced_status(karun, AdvancedRunStatus.CANCELLED)
    if _kdecision() != tm_delivery.RELEASE_EMPTIES:
        _fail("HIGH-1 keys: a cancelled Advanced run must release the approval again")

    # (h) BATCH MEMBERS need no arm of their own -- they ARE WorkflowRuns. Pin it, because the
    #     fix round was asked to add one only "if they are not already WorkflowRuns".
    from app.models import WorkflowBatch

    with Session(engine) as s:
        batch = WorkflowBatch(workflow_id=wf_doc, document_id=doc, requested_locales=[_LOCALE])
        s.add(batch)
        s.commit()
        s.refresh(batch)
        member = WorkflowRun(
            workflow_id=wf_doc,
            document_id=doc,
            target_locale=_LOCALE,
            status=RunStatus.RUNNING,
            batch_id=batch.id,
        )
        s.add(member)
        s.commit()
        s.refresh(member)
        mid = member.id
    if _decision() != tm_delivery.HOLD:
        _fail("HIGH-1: a live BATCH MEMBER run must hold exactly like a single run")
    with Session(engine) as s:
        ref = tm_delivery.active_run_ref(s, doc_id=doc, project_id=pid, locale=_LOCALE)
    if ref != (tm_delivery.KIND_CLASSIC, mid):
        _fail(f"HIGH-1: a batch member must be found by the CLASSIC arm, got {ref}")
    print("  28. #105 round-2 HIGH-1: an ADVANCED run in flight suspends the approval and")
    print("      refuses the mint (doc + keys, 4 surfaces); siblings/locales unaffected;")
    print("      batch members ride the classic arm OK")


def _project_locales(name, locales):
    """A project with SEVERAL target locales -- the shape a fan-out needs (round-3 HIGH-1)."""
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": ",".join(locales),
            "default_provider": "mock",
            "customer_id": str(_CUST_ID),
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0].split("#")[0])


def _batch(doc_id, wf_id, locales, *, finished=False):
    """A `WorkflowBatch` row in the LAZY shape the dispatcher actually leaves behind: the batch
    is committed with its whole `requested_locales` plan, and members are created one at a time
    inside the launch loop -- so at any moment the locales it has not reached have NO run row."""
    from app.models import WorkflowBatch, _utcnow

    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wf_id,
            document_id=doc_id,
            requested_locales=list(locales),
            finished_at=(_utcnow() if finished else None),
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id


def _member(doc_id, wf_id, locale, batch_id, status=RunStatus.RUNNING):
    with Session(engine) as s:
        m = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale=locale,
            status=status,
            batch_id=batch_id,
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        return m.id


def test_issue105_round3_high1_unlaunched_batch_member_holds():
    """GATE B ROUND-3 HIGH-1: a fan-out's UN-LAUNCHED locale shipped 200 with zero bytes.

    Round 2 decided, in a code comment, NOT to hold on the batch itself, on the premise that
    "nothing reconciles a batch row whose process died" and there is "no operator move to
    un-wedge it". Both are false -- `batch_runs.recover_interrupted_batches` is that
    reconciler, it runs at EVERY startup and it is `loc-tms db recover-jobs` -- and the gap the
    premise protected is #105's literal VIOLATION: `_dispatch` creates members ONE AT A TIME
    inside its launch loop under `sem.acquire()`, so at concurrency 1 the last locale of an
    N-locale fan-out has no `WorkflowRun` for the entire duration of the first N-1 (hours, not
    a race window). During that time the classic arm found nothing, a covering approval
    released, and the document downloaded HTTP 200 with ZERO bytes -- while this train's own
    copy promised "a fan-out's member runs included" and "stays held for as long as runs keep
    starting".

    The round-4 ruling: an unfinished batch holds covered locales with no terminal member,
    exactly like an in-flight run. This is the gate's B7' probe, pinned, plus the confinement
    arms (a foreign document, a locale outside the plan), the NAMING (`batch #N`, and the way
    out, since a batch has no cancel), and the un-wedge proof -- the reconciler the false
    premise said did not exist, run for real, releasing the hold.

    Counterfactual: drop the `_batch_id` arm from `active_run_ref` and (b) returns to
    `release_empties` with a 200/0-byte download.
    """
    from app.services import batch_runs

    fr, de = "fr-FR", "de-DE"
    pid = _project_locales(f"issue105-batch-{_seq()}", [fr, de])
    doc = _upload(pid)
    sib = _upload(pid)  # a SIBLING document in the same project
    wf, _st, _r = _mark(doc, locale=fr, status=RunStatus.COMPLETED)
    _mark(doc, locale=de, status=RunStatus.COMPLETED)  # provenanced in BOTH, none live
    _mark(sib, locale=de, status=RunStatus.COMPLETED)

    def _dec(d, loc):
        with Session(engine) as s:
            return tm_delivery.empty_gate_decision(s, doc=s.get(Document, d), locale=loc)

    def _ref(d, loc):
        with Session(engine) as s:
            return tm_delivery.active_run_ref(s, doc_id=d, project_id=pid, locale=loc)

    # (a) THE SANCTIONED PRE-BATCH STATE -- approve de-DE with nothing running.
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": de})
    if r.status_code != 200 or not r.json().get("covered_count"):
        _fail(
            f"round-3 HIGH-1 setup: the pre-batch approval must succeed, {r.status_code} {r.text}"
        )
    if _dec(doc, de) != tm_delivery.RELEASE_EMPTIES:
        _fail(f"round-3 HIGH-1 setup: an approved, batch-free doc must release, {_dec(doc, de)}")
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": de})
    if r.status_code != 200:
        _fail(f"round-3 HIGH-1 setup: the approved empties must download, got {r.status_code}")

    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": fr})
    assert r.status_code == 200

    # (b) THE SHIPPED LAZY SHAPE: the batch row is written with BOTH locales; only fr-FR's
    #     member exists. de-DE has no run row and will not have one for the whole fr-FR run.
    bid = _batch(doc, wf, [fr, de])
    mid = _member(doc, wf, fr, bid)
    if _dec(doc, de) != tm_delivery.HOLD:
        _fail(
            "round-3 HIGH-1: an approved locale whose batch member has NOT launched must be "
            f"HELD by the batch itself, got {_dec(doc, de)}"
        )
    r = client.get(f"/api/documents/{doc}/download", headers=H, params={"locale": de})
    if r.status_code == 200:
        _fail(
            f"round-3 HIGH-1: the un-launched locale shipped {len(r.content)} bytes with HTTP "
            "200 -- #105's VIOLATION outcome, on a live fan-out"
        )
    if r.status_code != 409 or not r.json().get("empty_held"):
        _fail(f"round-3 HIGH-1: the download must 409 empty_held, got {r.status_code}")
    msg = r.json().get("error") or ""
    if f"batch #{bid}" not in msg:
        _fail(f"round-3 HIGH-1: the 409 must NAME the batch: {msg}")
    if "recover-jobs" not in msg or "cancel it" in msg:
        _fail(
            "round-3 HIGH-1: a batch has no cancel, so the refusal must name the move that "
            f"does end one (`loc-tms db recover-jobs`) and must not offer a cancel: {msg}"
        )
    if _ref(doc, de) != (tm_delivery.KIND_BATCH, bid):
        _fail(f"round-3 HIGH-1: the un-launched locale must ref the BATCH, got {_ref(doc, de)}")

    # (b2) MOST-SPECIFIC-FIRST: the locale whose member IS live still names that RUN, because
    #      that is the thing the operator can wait on or cancel.
    if _ref(doc, fr) != (tm_delivery.KIND_CLASSIC, mid):
        _fail(f"round-3 HIGH-1: a live member must still answer as its own run, {_ref(doc, fr)}")

    # Round-4 MED-2: the actual CLI sweep beside a live batch must not fail its member
    # or finalize the batch, including the not-yet-created de-DE member's hold.
    rc, out = _run_cli(["db", "recover-jobs"])
    assert rc == 0, out
    with Session(engine) as s:
        from app.models import WorkflowBatch

        assert s.get(WorkflowRun, mid).status == RunStatus.RUNNING, (
            "round-4 MED-2: live member failed"
        )
        assert s.get(WorkflowBatch, bid).finished_at is None, "round-4 MED-2: live batch finalized"
    assert _ref(doc, de) == (tm_delivery.KIND_BATCH, bid)

    # Round-4 MED-1: the gate's exact completed-fr / unlaunched-de probe, plus the
    # other terminal statuses. Prior approvals resume without waiting for de-DE.
    for status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        with Session(engine) as s:
            member = s.get(WorkflowRun, mid)
            member.status = status
            s.add(member)
            s.commit()
        assert _ref(doc, fr) is None, f"round-4 MED-1: terminal {status} member held"
        assert _dec(doc, fr) == tm_delivery.RELEASE_EMPTIES
        assert (
            client.get(
                f"/api/documents/{doc}/download", headers=H, params={"locale": fr}
            ).status_code
            == 200
        )
        assert _ref(doc, de) == (tm_delivery.KIND_BATCH, bid)
        assert _dec(doc, de) == tm_delivery.HOLD
    with Session(engine) as s:
        member = s.get(WorkflowRun, mid)
        member.status = RunStatus.RUNNING
        s.add(member)
        s.commit()

    # (c) THE MINT is refused on the un-launched locale too, naming the batch.
    with Session(engine) as s:
        tm_delivery.revoke_doc(s, s.get(Document, doc), de, approved_by="x@x")
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": de})
    err = r.json().get("error") or ""
    if r.status_code != 400 or f"batch #{bid}" not in err:
        _fail(
            f"round-3 HIGH-1: a mid-batch approve must 400 naming the batch, {r.status_code} {err}"
        )
    if "recover-jobs" not in err:
        _fail(f"round-3 HIGH-1: the mint refusal must name the batch's way out: {err}")

    # (d) EVERY export surface says it: CLI (even --allow-held), the ZIP bundle, the web panel.
    rc, out = _run_cli(["doc", "export", str(doc), "--locale", de, "--allow-held"])
    if rc == 0 or f"batch #{bid}" not in out:
        _fail(f"round-3 HIGH-1 CLI: the export must refuse naming the batch, rc={rc} {out[:300]}")
    with Session(engine) as s:
        _data, _n, _c, sk = delivery.export_project(s, pid, locales=[de], zip=True)
    if not any(f"batch #{bid}" in (row.get("reason") or "") for row in sk):
        _fail(f"round-3 HIGH-1 bundle: the held document must name the batch: {sk}")
    rp = client.get(f"/documents/{doc}/empties", params={"locale": de})
    if f"Batch #{bid} is still in progress" not in rp.text:
        _fail("round-3 HIGH-1 web: the panel must name the BATCH, not call it a classic Run")

    # (e) CONFINEMENT -- the hold is the batch's own PLAN over its own DOCUMENT, not a blanket.
    if _ref(sib, de) is not None:
        _fail(f"round-3 HIGH-1: a batch over one document must not hold a SIBLING: {_ref(sib, de)}")
    doc2 = _upload(pid)
    wf2, _s2, _r2 = _mark(doc2, locale=de, status=RunStatus.COMPLETED)
    b2 = _batch(doc2, wf2, [fr])  # a plan that does NOT cover de-DE
    if _ref(doc2, de) is not None:
        _fail(
            "round-3 HIGH-1: a batch whose plan omits the locale must not hold it: "
            f"{_ref(doc2, de)}"
        )
    if _ref(doc2, fr) != (tm_delivery.KIND_BATCH, b2):
        _fail(f"round-3 HIGH-1: the batch must hold the locale it DOES plan, {_ref(doc2, fr)}")
    # Round-4 LOW-2: all four unreadable shapes remain wildcard holds.
    for shape in (None, [], {"fr-FR": 1}, "fr-FR"):
        with Session(engine) as s:
            from app.models import WorkflowBatch as _WB

            _b = s.get(_WB, b2)
            _b.requested_locales = shape
            s.add(_b)
            s.commit()
        if _ref(doc2, de) != (tm_delivery.KIND_BATCH, b2):
            _fail(f"round-4 LOW-2: unreadable plan {shape!r} must HOLD")
    with Session(engine) as s:
        from app.models import WorkflowBatch as _WB

        _b = s.get(_WB, b2)
        _b.requested_locales = [fr]
        s.add(_b)
        s.commit()

    # (f) THE UN-WEDGE, run for real: the reconciler round 2 said did not exist. It fails the
    #     orphaned member, stamps `finished_at`, and the hold releases -- so holding on a batch
    #     cannot wedge a deliverable permanently, which was the whole false premise.
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": de})
    if r.status_code != 400:
        _fail("round-3 HIGH-1: the mint must still be refused before the reconciler runs")
    healed = batch_runs.recover_interrupted_batches(at_boot=True)
    if healed < 1:
        _fail(
            "round-3 HIGH-1: recover_interrupted_batches finalized nothing; the way out is not real"
        )
    if _ref(doc, de) is not None:
        _fail(f"round-3 HIGH-1: a FINALIZED batch must release the hold, got {_ref(doc, de)}")
    r = client.post(f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": de})
    if r.status_code != 200:
        _fail(f"round-3 HIGH-1: approve must succeed once the batch is finalized, {r.status_code}")
    if _dec(doc, de) != tm_delivery.RELEASE_EMPTIES:
        _fail("round-3 HIGH-1: the deliverable must be deliverable again after the reconciler")
    print("  30. #105: an unfinished BATCH holds covered locales without terminal members,")
    print("      including the members it has not launched; named `batch #N` with its own way")
    print("      out on all five surfaces; confined to its plan + document; released by the")
    print("      startup reconciler OK")


def test_issue105_round3_med1_advanced_ring_is_the_documents_own():
    """GATE B ROUND-3 MED-1: `_advanced_run_id` trusted the CALLER's `project_id` as the ring.

    Its own docstring stated the confinement as a fact -- "the ONLY rows read are AdvancedRuns
    whose project_id IS the deliverable's own project" -- while the query used whatever the
    caller asserted and the document was only the segment-membership test. A caller passing a
    mismatched (document, project) pair therefore got a FOREIGN project's run id back, and the
    refusal copy would have NAMED it (`run_ref_text`), which is the disclosure class rounds 1
    and 2 spent their wordings closing. Worse, `active_run_ref`'s memo DROPS `project_id` in
    document mode -- on the (then untrue) premise that it is a lookup hint -- so one mismatched
    call POISONED the cache for the correct one.

    No shipped caller passes a mismatched pair, so this is a guarantee repair, not a live
    exploit: the fix makes the docstring and the memo key true by construction, by deriving the
    ring from the DOCUMENT in document mode.
    """
    pid_x = _project(f"issue105-ring-x-{_seq()}")
    pid_y = _project(f"issue105-ring-y-{_seq()}")
    dx = _upload(pid_x)
    _mark(dx, status=RunStatus.COMPLETED)
    x_sids = [sg.id for sg in _segs(dx)]

    # A mission in project Y whose caller-authored roster names project X's segment ids.
    _mid, yrun = _advanced_run(pid_y, units=x_sids, target_locale=_LOCALE, status="running")

    with Session(engine) as s:
        correct = tm_delivery.active_run_ref(s, doc_id=dx, project_id=pid_x, locale=_LOCALE)
    if correct is not None:
        _fail(f"round-3 MED-1: nothing is live in dX's OWN project; got {correct}")
    with Session(engine) as s:
        mismatched = tm_delivery.active_run_ref(s, doc_id=dx, project_id=pid_y, locale=_LOCALE)
    if mismatched is not None:
        _fail(
            "round-3 MED-1: a mismatched (document, project) pair reached a FOREIGN ring's run "
            f"{mismatched} over document {dx} (foreign Advanced run #{yrun}); the ring must be "
            "derived from the DOCUMENT, never from the caller's hint"
        )
    # ... and the memo cannot be poisoned: the mismatched call first, then the correct one.
    cache = {}
    with Session(engine) as s:
        first = tm_delivery.active_run_ref(
            s, doc_id=dx, project_id=pid_y, locale=_LOCALE, cache=cache
        )
        second = tm_delivery.active_run_ref(
            s, doc_id=dx, project_id=pid_x, locale=_LOCALE, cache=cache
        )
    if first is not None or second is not None:
        _fail(f"round-3 MED-1: the shared memo served a foreign answer: {first} then {second}")
    # The ring still WORKS: a mission in dX's own project holds it, whatever hint is passed.
    _mid2, xrun = _advanced_run(pid_x, units=x_sids, target_locale=_LOCALE, status="running")
    with Session(engine) as s:
        for hint in (pid_x, pid_y, None):
            got = tm_delivery.active_run_ref(s, doc_id=dx, project_id=hint, locale=_LOCALE)
            if got != (tm_delivery.KIND_ADVANCED, xrun):
                _fail(
                    f"round-3 MED-1: the document's OWN run must be found with hint {hint}: {got}"
                )
    print("  31. #105 round-3 MED-1: the Advanced ring is derived from the DOCUMENT, so no")
    print("      caller hint can reach a foreign project's run or poison the memo OK")


#: The ONE sentence that says WHICH runs the empty gate calls "in flight" (round-2 HIGH-1).
#: The gate's FAIL was that every operator-facing string said "a run" while the predicate saw
#: one tier, so the copy promised more than the code enforced. These five surfaces now say the
#: same thing; this is a DELETION ratchet over the enumerated set (a new surface has no
#: obligation until a person adds it here by hand), plus a live check that the code agrees.
_IN_FLIGHT_TIERS_SENTENCE = (
    "A run is a run in either tier: a classic workflow run (a fan-out's member runs included, "
    "and the unfinished fan-out ITSELF only while that locale's current-attempt member is not yet "
    "created or is non-terminal; a re-run holds its selected locales from acceptance until their "
    "fresh members "
    "are terminal, and finished locales not selected still release) or an "
    "Advanced-Workflow run over the same project/document and locale."
)
_IN_FLIGHT_TIERS_FILES = (
    "app/capabilities/tm.md",
    "docs/CLI.md",  # `doc empties`, `doc deliver-approve`, `keys deliver-approve`
    "app/templates/help.html",  # the two document + two key gate routes
    "app/modules.py",  # the `keys deliver-approve` CLI descriptor
)
_IN_FLIGHT_TIERS_MIN = {"docs/CLI.md": 3, "app/templates/help.html": 4}


def test_the_in_flight_tier_sentence_is_pinned_on_every_gate_surface():
    """ROUND-2 HIGH-1, the four-surface half. `held_message` told the operator "empties cannot
    ship while a run may still fill them" and `docs/CLI.md` / `app/capabilities/tm.md` repeated
    it, while `active_run_id` read `WorkflowRun` only -- and nothing could catch the divergence,
    because no test read the copy. Pin the sentence AND the code fact it summarises together, so
    a future narrowing of the predicate cannot leave the promise standing (or vice versa)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = []
    for rel in _IN_FLIGHT_TIERS_FILES:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            body = fh.read()
            got = body.count(_IN_FLIGHT_TIERS_SENTENCE)
        if rel in ("docs/CLI.md", "app/capabilities/tm.md"):
            assert "holds every locale" not in body, "round-4 MED-1: contradictory broad batch rule"
            assert "follows that same per-locale rule" in body
            normalized = " ".join(body.split())
            assert "only if the server died, after confirming no worker is live" in normalized
            assert (
                "Manual recovery finalizes only batches with "
                "confirmed-dead local dispatcher processes" in normalized
            )
        need = _IN_FLIGHT_TIERS_MIN.get(rel, 1)
        if got < need:
            missing.append(f"{rel}: {got} of {need}")
    if missing:
        _fail(
            "[HIGH-1] the in-flight TIER sentence is missing from a gate surface "
            f"({'; '.join(missing)}). The sentence is {_IN_FLIGHT_TIERS_SENTENCE!r} -- if it "
            "was reworded, reword it HERE too so every surface keeps saying one thing."
        )
    # ... and the sentence must stay TRUE of the code: both kinds resolvable, both labelled.
    if set(tm_delivery._RUN_KIND_LABELS) != {
        tm_delivery.KIND_CLASSIC,
        tm_delivery.KIND_ADVANCED,
        tm_delivery.KIND_BATCH,
    }:
        _fail(
            "[HIGH-1] tm_delivery knows a run kind the copy does not name: "
            f"{sorted(tm_delivery._RUN_KIND_LABELS)}"
        )
    if tm_delivery.run_ref_text((tm_delivery.KIND_ADVANCED, 9)) != "Advanced-Workflow run #9":
        _fail("[HIGH-1] the Advanced tier is no longer NAMED in the refusal text")
    if tm_delivery.run_ref_text((tm_delivery.KIND_BATCH, 9)) != "batch #9":
        _fail("[HIGH-1] round-3: the fan-out is no longer NAMED as a batch in the refusal text")
    # ROUND-3 HIGH-1: the way out is part of the promise. A run can be cancelled; a batch
    # cannot, so its move must name the reconciler instead -- and never offer a cancel.
    _batch_move = tm_delivery.run_ref_wait_move((tm_delivery.KIND_BATCH, 9))
    if "recover-jobs" not in _batch_move or "cancel" in _batch_move:
        _fail(f"[HIGH-1] round-3: a batch refusal must name its own way out: {_batch_move}")
    for token in (
        "Wait for the batch to finish",
        "only if the server died",
        "confirming no worker",
        "is live",
        "install-wide",
    ):
        assert token in _batch_move, f"round-4 MED-2: unsafe recovery advice: {_batch_move}"
    # LOW-1: replacing the authority must change held_message for every run kind.
    from unittest.mock import patch

    for kind in (tm_delivery.KIND_BATCH, tm_delivery.KIND_CLASSIC, tm_delivery.KIND_ADVANCED):
        with (
            patch.object(tm_delivery, "active_run_ref", return_value=(kind, 9)),
            patch.object(tm_delivery, "run_ref_wait_move", return_value="WAY-OUT-SENTINEL") as move,
        ):
            msg = tm_delivery.held_message(
                None, doc=None, locale="fr-FR", count=1, unit="segment", way_out="unused"
            )
            assert "WAY-OUT-SENTINEL" in msg, "round-4 LOW-1: held_message bypasses authority"
            move.assert_called_once_with((kind, 9))
    assert "RING: the document set is the ring" in tm_delivery._batch_id.__doc__, (
        "round-4 LOW-3: missing ring statement"
    )
    if "cancel" not in tm_delivery.run_ref_wait_move((tm_delivery.KIND_CLASSIC, 9)):
        _fail("[HIGH-1] round-3: a classic run's refusal lost its cancel instruction")
    print("  32. HIGH-1: the in-flight TIER sentence is pinned on all four gate surfaces OK")


def _retro_fixture():
    fr, de = "fr-FR", "de-DE"
    pid = _project_locales(f"retro-{_seq()}", [fr, de])
    doc = _upload(pid)
    wf, _, _ = _mark(doc, locale=fr, status=RunStatus.COMPLETED)
    _mark(doc, locale=de, status=RunStatus.COMPLETED)
    for loc in (fr, de):
        assert (
            client.post(
                f"/api/documents/{doc}/deliver-approve", headers=H, params={"locale": loc}
            ).status_code
            == 200
        )
    bid = _batch(doc, wf, [fr, de], finished=True)
    _member(doc, wf, fr, bid, RunStatus.COMPLETED)
    old = _member(doc, wf, de, bid, RunStatus.FAILED)
    return doc, wf, bid, old


def test_retro_high1_reopened_locales():
    from contextlib import nullcontext
    from unittest.mock import patch

    from app.cli._remote import RemoteClient
    from app.models import WorkflowBatch
    from app.services import batch_runs

    for chosen in (["de-DE"], ["fr-FR", "de-DE"]):
        doc, wf, bid, old = _retro_fixture()
        with patch_pieces("app.services.batch_runs", "_dispatch"), Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            b.max_parallel = 1
            # Observe acceptance before dispatch; synchronous callers now own finalization.
            batch_runs.rerun_batch(s, batch=b, locales=chosen, synchronous=False)
        if len(chosen) == 2:
            _member(doc, wf, "fr-FR", bid)
        # Fresh sessions prove this is durable, including the pre-member acceptance gap.
        with Session(engine) as s:
            assert s.get(WorkflowBatch, bid).dispatcher_owner == batch_runs._dispatcher_owner()
            assert tm_delivery._batch_id(s, [doc], "de-DE") == bid, (
                f"retro HIGH-1: {chosen}: old member released rerun"
            )
            assert tm_delivery.active_run_ref(s, doc_id=doc, locale="de-DE") == (
                tm_delivery.KIND_BATCH,
                bid,
            )
            assert (
                tm_delivery.empty_gate_decision(s, doc=s.get(Document, doc), locale="de-DE")
                == tm_delivery.HOLD
            )
            if len(chosen) == 1:
                assert tm_delivery.active_run_ref(s, doc_id=doc, locale="fr-FR") is None
        for path in (f"/api/documents/{doc}/download", f"/documents/{doc}/export"):
            r = client.get(path, headers=H, params={"locale": "de-DE"})
            assert r.status_code == 409, (path, r.status_code)
        args = ["doc", "export", str(doc), "--locale", "de-DE", "--allow-held"]
        rc, out = _run_cli(args)
        assert rc != 0 and f"batch #{bid}" in out, out
        # Exercise the real remote command adapter and HTTP response via TestClient.
        with patch.object(
            RemoteClient, "_client", return_value=nullcontext(TestClient(app, headers=H))
        ):
            rc, out = _run_cli(["--remote", "http://127.0.0.1", "--token", H["X-Loc-Token"], *args])
        assert rc != 0 and f"batch #{bid}" in out, out
        fresh = _member(doc, wf, "de-DE", bid, RunStatus.QUEUED)
        assert fresh > old
        with Session(engine) as s:
            assert tm_delivery.active_run_ref(s, doc_id=doc, locale="de-DE") == (
                tm_delivery.KIND_CLASSIC,
                fresh,
            ), "queued fresh members must keep delivery in flight"
            assert (
                f"{len(chosen)} locale(s) still in flight"
                in batch_runs.rerun_unavailable_reason(s, s.get(WorkflowBatch, bid))
            )
        assert (
            client.get(
                f"/api/documents/{doc}/download", headers=H, params={"locale": "de-DE"}
            ).status_code
            == 409
        )
        with Session(engine) as s:
            row = s.get(WorkflowRun, fresh)
            row.status = RunStatus.COMPLETED
            s.add(row)
            s.commit()
        with Session(engine) as s:
            assert tm_delivery.active_run_ref(s, doc_id=doc, locale="de-DE") is None
            assert (
                tm_delivery.empty_gate_decision(s, doc=s.get(Document, doc), locale="de-DE")
                == tm_delivery.RELEASE_EMPTIES
            )
        assert (
            client.get(
                f"/api/documents/{doc}/download", headers=H, params={"locale": "de-DE"}
            ).status_code
            == 200
        )


def test_retro_med1_safe_recovery_default():
    from pathlib import Path

    from app.models import WorkflowBatch
    from app.services import batch_runs

    doc, wf, bid, _ = _retro_fixture()
    mid = _member(doc, wf, "fr-FR", bid)
    with Session(engine) as s:
        b = s.get(WorkflowBatch, bid)
        b.finished_at = None
        s.add(b)
        s.commit()
    assert batch_runs.recover_interrupted_batches() == 0, "retro MED-1: bare recovery swept batches"
    with Session(engine) as s:
        assert s.get(WorkflowRun, mid).status == RunStatus.RUNNING
        assert s.get(WorkflowBatch, bid).finished_at is None
    assert "recover_interrupted_batches(at_boot=True)" in Path("app/main.py").read_text(
        encoding="utf-8"
    )


def test_retro_rerun_claim_rejects_stale_finished_state():
    from datetime import timedelta

    from app.models import WorkflowBatch
    from app.services import batch_runs

    doc, wf, bid, _ = _retro_fixture()

    real_plan = batch_runs._plan

    def intervening_dispatch(*args, **kwargs):
        # Deterministic interleaving: another dispatch finishes AFTER our caller read
        # its member boundary, BEFORE that caller attempts its acceptance update.
        #
        # The hook is `_plan` because that is the one call that sits BETWEEN those two
        # points in `rerun_batch`: the boundary comes from `_members_by_locale_latest`
        # just above it, and the atomic `finished_at == batch.finished_at` claim comes
        # just below. It used to be the FULL_TEXT x tm_match guard, which happened to sit
        # in the same gap until #155 retired that guard; this is the same interleaving
        # against a seam that still exists. It must DELEGATE, not replace: the claim below
        # needs a real plan to dispatch.
        _member(doc, wf, "de-DE", bid, RunStatus.COMPLETED)
        with Session(engine) as other:
            b = other.get(WorkflowBatch, bid)
            b.finished_at += timedelta(seconds=1)
            other.add(b)
            other.commit()
        return real_plan(*args, **kwargs)

    with (
        patch_pieces("app.services.batch_runs", "_plan", intervening_dispatch),
        patch_pieces("app.services.batch_runs", "_dispatch") as dispatch,
        Session(engine) as s,
    ):
        b = s.get(WorkflowBatch, bid)
        try:
            batch_runs.rerun_batch(s, batch=b, locales=["de-DE"], synchronous=True)
        except batch_runs.BatchError:
            pass
        else:
            assert False, "retro HIGH-1: stale finished state accepted an obsolete member boundary"
        dispatch.assert_not_called()


def test_retro_med2_cli_orphan_recovery():
    import socket
    from pathlib import Path

    from app.models import WorkflowBatch

    # A real exited process, not an invented PID or a timeout mistaken for death.
    dead_pid = int(
        subprocess.check_output(
            [sys.executable, "-c", "import os; print(os.getpid())"], text=True, timeout=10
        )
    )
    ids = []
    for owner in (os.getpid(), dead_pid):
        doc, wf, bid, _ = _retro_fixture()
        mid = _member(doc, wf, "fr-FR", bid)
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            b.finished_at = None
            b.dispatcher_owner = {"host": socket.gethostname(), "pid": owner}
            s.add(b)
            s.commit()
        ids.append((bid, mid))
    rc, out = _run_cli(["db", "recover-jobs"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(WorkflowBatch, ids[0][0]).finished_at is None, (
            "retro MED-2: live batch finalized"
        )
        assert s.get(WorkflowRun, ids[0][1]).status == RunStatus.RUNNING
        assert s.get(WorkflowBatch, ids[1][0]).finished_at is not None, (
            "retro MED-2: orphan not finalized"
        )
        assert s.get(WorkflowRun, ids[1][1]).status == RunStatus.FAILED
        reason = orchestrator.run_reason(s.get(WorkflowRun, ids[1][1]))
        assert reason and reason["code"] == "run_crashed", "retro MED-2: recovery reason invisible"
        assert "Dispatcher process exited" in reason["text"]
        assert s.get(WorkflowBatch, ids[1][0]).notes == "dispatcher exited; 1 member(s) failed"
    assert "1 orphaned batch(es)" in out, out
    rc, help_text = _run_cli(["db", "--help"])
    assert rc == 0 and "confirmed-dead" in help_text
    assert "confirmed-dead" in Path("docs/architecture/deployment.md").read_text(encoding="utf-8")


def test_retro_low1_recovery_comment():
    import inspect

    assert "confirmed-dead dispatcher" in inspect.getsource(tm_delivery.approve_doc), (
        "retro LOW-1: stale recovery comment"
    )


def test_retro_dispatcher_ownership():

    from app.models import WorkflowBatch
    from app.services import batch_runs

    doc, wf, _, _ = _retro_fixture()
    with patch_pieces("app.services.batch_runs", "_dispatch"), Session(engine) as s:
        b = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=doc, locales=["de-DE"], synchronous=False
        )
        bid = b.id
    with Session(engine) as s:
        assert s.get(WorkflowBatch, bid).dispatcher_owner == batch_runs._dispatcher_owner()
    child = subprocess.Popen(
        [sys.executable, "-c", "import os, sys; print(os.getpid(), flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    # Use the interpreter's PID, not a possible Windows venv launcher's PID.
    owner = {**batch_runs._dispatcher_owner(), "pid": int(child.stdout.readline())}
    try:
        assert not batch_runs._dispatcher_confirmed_dead(owner)
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            b.dispatcher_owner = owner
            s.add(b)
            s.commit()
        rc, out = _run_cli(["db", "recover-jobs"])
        assert rc == 0, out
        with Session(engine) as s:
            assert s.get(WorkflowBatch, bid).finished_at is None, "live foreign process finalized"
    finally:
        child.communicate(timeout=10)
    assert batch_runs._dispatcher_confirmed_dead(owner)
    for unknown in (None, {}, "bad", {**owner, "host": "foreign-host"}, {**owner, "pid": True}):
        assert not batch_runs._dispatcher_confirmed_dead(unknown)
    # A real accepted initial batch is recoverable once its owning process has exited,
    # including the window in which no member row exists at all.
    rc, out = _run_cli(["db", "recover-jobs"])
    assert rc == 0 and "1 orphaned batch(es)" in out, out
    with Session(engine) as s:
        assert s.get(WorkflowBatch, bid).finished_at is not None


def test_retro_low2_actionable_way_out():
    move = tm_delivery.run_ref_wait_move((tm_delivery.KIND_BATCH, 1))
    assert "for interrupted jobs and restart" not in move, "retro LOW-2: inert batch way-out"
    assert "confirmed-dead" in move and "install-wide" in move


def test_retro_low3_derived_terminal_statuses():
    import inspect

    assert "RunStatus.COMPLETED" not in inspect.getsource(tm_delivery._batch_id), (
        "retro LOW-3: re-listed terminal triple"
    )
    assert set(tm_delivery._TERMINAL_RUN_STATUSES) == {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }


def test_round7_requested_shapes():
    from app.models import WorkflowBatch
    from app.services import batch_runs

    for shape in (None, {}, "de-DE", 7, True, [None, {}, 1, "fr-FR"]):
        doc, wf, bid, _ = _retro_fixture()
        _member(doc, wf, "fr-FR", bid, RunStatus.RUNNING)
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            b.finished_at = None
            b.requested_locales = shape
            try:
                reason = batch_runs.rerun_unavailable_reason(s, b)
            except Exception as exc:
                raise AssertionError(f"requested_locales {shape!r}: {exc}") from exc
            if isinstance(shape, list):
                assert reason.endswith("1 locale(s) still in flight"), reason
            else:
                assert "not been finalized" in reason and "db recover-jobs" in reason, reason


def test_round7_unfinalized_way_out():
    from app.models import WorkflowBatch

    for plan in ([], ["de-DE"]):
        _doc, _wf, bid, _ = _retro_fixture()
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            b.finished_at = None
            b.requested_locales = plan
            s.add(b)
            s.commit()
        page = client.get(f"/batches/{bid}")
        assert page.status_code == 200
        assert "not been finalized" in page.text and "db recover-jobs" in page.text
        assert "0 locale(s) still in flight" not in page.text
        assert f'action="/batches/{bid}/rerun"' not in page.text


def test_round7_monitor_terminal_reload():
    import shutil
    import subprocess
    from pathlib import Path

    template = Path("app/templates/batch_view.html").read_text(encoding="utf-8")
    tick = template.split("  function tick() {", 1)[1].split("  setTimeout(tick, POLL_MS);", 1)[0]
    node = shutil.which("node")
    assert node, "Node is required to exercise the live monitor terminal transition"
    harness = (
        """
let reloads = 0, stopped = 0, timers = 0, changed = false;
let polls = 0, POLL_MAX = 400, POLL_MS = 2500, url = 'status';
let payload, allowReload = true;
function apply(v) { return changed; }
function boundedReload() { reloads++; return allowReload; }
function showStopped() { stopped++; }
function setTimeout() { timers++; }
function fetch() { return Promise.resolve({ok:true, json:()=>payload}); }
"""
        + "function tick() {"
        + tick
        + """
(async function () {
  for (const status of ['completed', 'failed', 'partial']) {
    reloads = stopped = timers = 0; payload = {status}; tick();
    await new Promise(resolve => setImmediate(resolve));
    if (reloads !== 1 || timers !== 0) throw Error('terminal controls stayed stale: ' + status);
  }
  reloads = timers = 0; payload = {status:'running'}; tick();
  await new Promise(resolve => setImmediate(resolve));
  if (reloads !== 0 || timers !== 1) throw Error('running poll must continue');
  allowReload = false; payload = {status:'completed'}; tick();
  await new Promise(resolve => setImmediate(resolve));
  if (stopped !== 1) throw Error('reload cap must offer refresh');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    )
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_round6_reopened_shapes():
    from app.models import WorkflowBatch

    for shape in (None, {}, [], "de-DE", [1], {"de-DE": None}, {"de-DE": "x"}):
        doc, _wf, bid, _ = _retro_fixture()
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            b.finished_at = None
            b.reopened_members = shape
            s.add(b)
            s.commit()
            try:
                assert tm_delivery._batch_id(s, [doc], "de-DE") is None
            except Exception as exc:
                raise AssertionError(f"unreadable reopened_members {shape!r}: {exc}") from exc
        assert (
            client.get(
                f"/api/documents/{doc}/download", headers=H, params={"locale": "de-DE"}
            ).status_code
            == 200
        )


def test_round6_unfinished_controls():
    from contextlib import nullcontext
    from unittest.mock import patch

    from app.cli._remote import RemoteClient
    from app.models import WorkflowBatch, _utcnow

    doc, wf, bid, _ = _retro_fixture()
    _member(doc, wf, "fr-FR", bid, RunStatus.RUNNING)
    with Session(engine) as s:
        b = s.get(WorkflowBatch, bid)
        b.finished_at = None
        b.requested_locales = ["fr-FR", "de-DE", "es-ES"]
        b.notes = "Spend cap reached"
        s.add(b)
        s.commit()
    sentence = "Re-run is available once the batch has finished; 2 locale(s) still in flight"
    page = client.get(f"/batches/{bid}")
    assert page.status_code == 200
    assert page.text.count(sentence) == 1
    assert 'title="not launched - spend cap"' in page.text
    assert f'action="/batches/{bid}/rerun"' not in page.text
    assert f'action="/batches/{bid}/retry-failed"' not in page.text
    for action in ("rerun", "retry-failed"):
        web = client.post(f"/batches/{bid}/{action}", data={"locale": "de-DE"})
        assert web.status_code == 400 and sentence in web.text, web.text
        api = client.post(f"/api/batches/{bid}/{action}", headers=H, json={"locales": ["de-DE"]})
        assert api.status_code == 400 and api.json()["error"] == sentence, api.text
        args = ["workflow", "batch", action, str(bid)]
        if action == "rerun":
            args += ["--locale", "de-DE"]
        rc, out = _run_cli(args)
        assert rc != 0 and sentence in out, out
        with patch.object(
            RemoteClient, "_client", return_value=nullcontext(TestClient(app, headers=H))
        ):
            rc, out = _run_cli(["--remote", "http://127.0.0.1", "--token", H["X-Loc-Token"], *args])
        assert rc != 0 and sentence in out, out
    with Session(engine) as s:
        b = s.get(WorkflowBatch, bid)
        b.finished_at = _utcnow()
        s.add(b)
        s.commit()
    page = client.get(f"/batches/{bid}")
    assert f'action="/batches/{bid}/retry-failed"' in page.text
    assert ">Re-run</button>" in page.text and ">Run now</button>" in page.text
    # An unfinished batch with nothing failed must also refuse retry-failed.
    _member(doc, wf, "de-DE", bid, RunStatus.COMPLETED)
    with Session(engine) as s:
        b = s.get(WorkflowBatch, bid)
        b.finished_at = None
        s.add(b)
        s.commit()
    for prefix in ("", "/api"):
        response = client.post(f"{prefix}/batches/{bid}/retry-failed", headers=H)
        assert response.status_code == 400 and sentence in response.text
    rc, out = _run_cli(["workflow", "batch", "retry-failed", str(bid)])
    assert rc != 0 and sentence in out, out


def test_round6_recovery_reference():
    from pathlib import Path

    row = next(
        line
        for line in Path("docs/CLI.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `db recover-jobs` |")
    )
    for phrase in (
        "jobs.recover_interrupted",
        "orchestrator.recover_interrupted_runs",
        "RUNNING/PENDING",
        "confirmed-dead local dispatcher",
        "finalizes",
        "releases their delivery holds",
        "install-wide",
    ):
        assert phrase in row, f"recover-jobs row missing {phrase}"


def test_retro_low4_retired_ratchet_comment():
    from pathlib import Path

    body = " ".join(Path("tests/verify_modules.py").read_text(encoding="utf-8").split())
    assert "every locale its plan covers while any" not in body, "retro LOW-4: retired broad rule"


def test_retro_older_batch_and_blank_member():
    from app.models import WorkflowBatch

    doc, wf, newest, _ = _retro_fixture()
    older = _batch(doc, wf, ["fr-FR"])
    newest = _batch(doc, wf, ["fr-FR"])
    _member(doc, wf, "fr-FR", newest, RunStatus.COMPLETED)
    with Session(engine) as s:
        assert tm_delivery._batch_id(s, [doc], "fr-FR") == older
    assert (
        client.get(
            f"/api/documents/{doc}/download", headers=H, params={"locale": "fr-FR"}
        ).status_code
        == 409
    )
    with Session(engine) as s:
        s.delete(s.get(WorkflowBatch, older))
        s.commit()
    blank = _batch(doc, wf, ["fr-FR"])
    _member(doc, wf, "", blank, RunStatus.COMPLETED)
    with Session(engine) as s:
        assert tm_delivery._batch_id(s, [doc], "fr-FR") == blank
    assert (
        client.get(
            f"/api/documents/{doc}/download", headers=H, params={"locale": "fr-FR"}
        ).status_code
        == 409
    )


def main():
    ran = set()

    def run(test):
        test()
        ran.add(test.__name__)

    failures = []
    for test in (
        test_round7_requested_shapes,
        test_round7_unfinalized_way_out,
        test_round7_monitor_terminal_reload,
        test_round6_unfinished_controls,
        test_round6_reopened_shapes,
        test_round6_recovery_reference,
        test_retro_high1_reopened_locales,
        test_retro_rerun_claim_rejects_stale_finished_state,
        test_retro_med1_safe_recovery_default,
        test_retro_med2_cli_orphan_recovery,
        test_retro_dispatcher_ownership,
        test_retro_low1_recovery_comment,
        test_retro_low2_actionable_way_out,
        test_retro_low3_derived_terminal_statuses,
        test_retro_low4_retired_ratchet_comment,
        test_retro_older_batch_and_blank_member,
    ):
        try:
            run(test)
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")
    assert not failures, "\n".join(failures)
    run(test_decision_and_provenance)
    run(test_real_tm_only_run_reaches_gate)
    run(test_surfaces_refuse)
    run(test_approve_emits_empty)
    run(test_growth_and_revoke)
    run(test_key_mode)
    run(test_cascade)
    run(test_partial_indexes)
    run(test_mode_typed_covered)
    run(test_mcp_exempt)
    run(test_keys_inertness)
    run(test_data_export_roundtrip)
    run(test_gdpr)
    run(test_am3_archived)
    run(test_am5_pins)
    run(test_delete_workflow_gate)
    run(test_sensitive_and_lows)
    run(test_jira_approved_empties)
    run(test_image_byte_identical)
    run(test_revoke_after_marker_erasure)
    run(test_am2h1_archived_approve)
    run(test_am2h2_mint_confinement)
    run(test_b2m2_wildcard_blocker)
    run(test_r3med1_archived_key_blocker)
    run(test_m1_review_surface_decision)
    run(test_m2_skeleton_carveout)
    run(test_issue105_midrun_approve_refused)
    run(test_issue105_approval_suspended_while_a_run_is_in_flight)
    run(test_issue105_high1_advanced_run_and_batch_member_hold)
    run(test_issue105_round3_high1_unlaunched_batch_member_holds)
    run(test_issue105_round3_med1_advanced_ring_is_the_documents_own)
    run(test_the_in_flight_tier_sentence_is_pinned_on_every_gate_surface)
    assert ran == {
        name for name, value in globals().items() if name.startswith("test_") and callable(value)
    }, "not all tests ran"
    print("TM EMPTY DELIVERY OK")


if __name__ == "__main__":
    main()
