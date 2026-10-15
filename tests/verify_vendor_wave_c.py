"""Vendor Wave C: the transport-protocol seam (interop R1), the outbound-dispatch
dead-letter/backoff state machine (interop R5), and the AI-vs-Human spend helper
(decision 5). Hermetic + offline: every dispatch rides an injected httpx.MockTransport
(vendor_dispatch.set_test_transport / an explicit transport arg), so no byte reaches a
real network, and the whole suite runs on a throwaway data dir.

What this attacks (not merely demonstrates):

  [P] PROTOCOL SEAM: `native` + `manual` are the registered protocols; an unknown protocol is
      refused at vendor create AND update (never at dispatch); a legacy NULL protocol
      reads as native; the registry INVARIANT (transport only) holds -- vendor_protocol
      resolves an unregistered stored value defensively to native.
  [C] CLASSIFICATION: a TRANSIENT transport failure (non-2xx / network) parks a RETRY_WAIT
      leg and PAUSES the run (never fails it); a POLICY refusal (SSRF guard / ConnectorError)
      still FAILS the run with no leg -- byte-identical to pre-Wave-C.
  [R] RETRY -> SUCCESS: a due RETRY_WAIT leg re-sent successfully flips to DISPATCHED, the
      run stays parked awaiting the return, and the held package (retry_contract) is cleared.
  [D] RETRY -> EXHAUSTION: the bounded backoff window exhausts to DEAD_LETTER AND escalates
      to a human_review pause (decision 3 -- never a hard fail), with a sanitized last_error.
  [B] BACKOFF SCHEDULE: next_backoff walks the schedule then returns None (terminal).
  [E] ERROR SANITIZATION: a URL / bearer / signature in a transport error never survives
      into the stored last_error.
  [X] CLOSE: a terminal run closes a still-open RETRY_WAIT leg (CANCELLED) and drops its
      held package, so no retry sweep can drag the finished run back to PAUSED.
  [S] SPEND HELPER: AI = sum(StepRun.cost_usd); Human = actual when set else estimate,
      labeled; actual overrides estimate; CANCELLED legs excluded; per-run + aggregate.

Every test covers NEW Wave C behaviour, so none is a fail-before proof of a prior defect.
Final line: "VENDOR WAVE C OK".
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_vendorwavec_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import (
    orchestrator,
    vendor_dispatch,
    vendor_return,
    vendor_spend,
    vendors,
)
from app.services import workflows as wf
from app.services.connectors.base import ConnectorError

init_db()

_SEQ = [0]
SIGNING = "topsecret-signing"
SOURCES = ["Hello world.", "The cat sat on the mat.", "Please sign in now."]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _mock_transport(status=200, body=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, json={"ok": True})
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def _boom_transport(exc=None):
    """A transport that raises an httpx transport error -> VendorDispatchError (transient)."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise (exc or httpx.ConnectError("connection refused"))

    return httpx.MockTransport(handler)


def _seed(*, endpoint="https://vendor.example.com/intake"):
    """A customer + project + doc (3 segments) + a vendor linked to that customer + a
    workflow with an LLM_AGENT step and an EXTERNAL vendor step."""
    with Session(engine) as s:
        cust = Customer(name=f"C-{_seq()}")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(
            name=f"P-{_seq()}", source_locale="en-US", default_provider="mock", customer_id=cust.id
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
        v = Vendor(
            name=f"V-{_seq()}",
            endpoint_url=endpoint,
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": SIGNING})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendors.link_customer(s, v.id, cust.id)
        w = Workflow(
            name=f"W-{_seq()}", scope="global", source_locale="en-US", target_locale="es-ES"
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
            vendor_id=v.id,
            external_wait_hours=72,
        )
        return {
            "customer_id": cust.id,
            "project_id": p.id,
            "doc_id": d.id,
            "vendor_id": v.id,
            "wf_id": w.id,
        }


def _run_to_external(env, transport, locale="es-ES"):
    """Drive the workflow to its external step with `transport` installed. Returns
    (run_id, leg or None)."""
    vendor_dispatch.set_test_transport(transport)
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], locale))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == env["wf_id"])
            .order_by(WorkflowRun.id.desc())
        ).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        return run.id, (leg.id if leg else None)


# ============================================================================
# [P] protocol seam
# ============================================================================
def test_protocol_registry_and_validation():
    # the registered transport protocols: `native` (signed-JSON wire) and `manual`
    # (file-exchange, no wire — feat/vendor-manual-protocol).
    assert vendor_dispatch.protocol_names() == ("native", "manual")
    assert vendor_dispatch.is_known_protocol("native")
    assert vendor_dispatch.is_known_protocol("manual")
    assert vendor_dispatch.is_known_protocol("")  # blank -> native
    assert vendor_dispatch.is_known_protocol(None)  # None -> native
    assert not vendor_dispatch.is_known_protocol("lionbridge")

    with Session(engine) as s:
        cust = Customer(name=f"C-{_seq()}")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        # create: unknown protocol REFUSED at write time, nothing persisted.
        raised = False
        try:
            vendors.create(s, "BadProto", protocol="lionbridge")
        except vendors.VendorError as exc:
            raised = True
            assert "unknown vendor protocol" in str(exc), str(exc)
        assert raised, "create with an unknown protocol must raise VendorError"
        assert s.exec(select(Vendor).where(Vendor.name == "BadProto")).first() is None, (
            "a refused-protocol create must persist nothing"
        )

        # create: native (explicit + default) OK.
        v1 = vendors.create(s, f"OkProto-{_seq()}", protocol="native")
        assert v1.protocol == "native"
        v2 = vendors.create(s, f"DefProto-{_seq()}")  # default
        assert v2.protocol == "native"
        assert vendors.to_dict(s, v2)["protocol"] == "native"

        # update: unknown protocol refused; native accepted.
        raised = False
        try:
            vendors.update(s, v1.id, protocol="trados")
        except vendors.VendorError:
            raised = True
        assert raised, "update with an unknown protocol must raise VendorError"
        s.refresh(v1)
        assert v1.protocol == "native", "a refused update must not change the stored value"

        # defensive: a stored value that is not a registered protocol reads as native at
        # the single dispatch-by-protocol seam (never crashes dispatch).
        v1.protocol = "ghost"  # force an unregistered value past validation
        s.add(v1)
        s.commit()
        assert vendor_dispatch.vendor_protocol(v1) == "native"
        # a legacy NULL/'' protocol also reads native.
        v1.protocol = ""
        assert vendor_dispatch.vendor_protocol(v1) == "native"
    print(
        "  [P] native+manual registry; unknown protocol refused at create+update; "
        "legacy/unknown reads native: OK"
    )


def test_poll_routes_through_protocol():
    """A poll is a transport op: it routes through the LEG's SNAPSHOTTED protocol (F2), not
    the vendor's live one. `native` polls over the poll_url; a leg dispatched under a
    non-native protocol (no poll implementation until an adapter wave) is refused AT the seam,
    BEFORE any DNS/SSRF/admission work. We register a throwaway protocol to exercise the branch
    the sole `native` entry can't reach, and set it on the LEG's snapshot (keying off the
    vendor's live protocol would let a native-dispatched leg stop being pollable the moment a
    vendor is reconfigured -- exactly the phantom F2 forbids)."""
    env = _seed()
    _run_id, leg_id = _run_to_external(env, _boom_transport())  # parks RETRY_WAIT (native)
    fake = "fake_adapter"
    vendor_dispatch._PROTOCOLS[fake] = vendor_dispatch._PROTOCOLS["native"]
    try:
        with Session(engine) as s:
            v = s.get(Vendor, env["vendor_id"])
            v.config = {"poll_url": "https://vendor.example.com/poll"}
            s.add(v)
            leg = s.get(VendorDispatch, leg_id)
            leg.protocol = fake  # F2: the LEG was dispatched under a non-native transport
            s.add(leg)
            s.commit()
            raised = False
            try:
                vendor_return.poll_leg(s, leg, transport=_mock_transport())
            except vendor_return.VendorPollError as exc:
                raised = True
                assert "protocol" in str(exc), str(exc)
            assert raised, "a non-native poll must be refused at the protocol seam"
    finally:
        vendor_dispatch._PROTOCOLS.pop(fake, None)
    print("  [P] poll_leg routes through the LEG's protocol snapshot (non-native refused): OK")


# ============================================================================
# [C] policy-vs-transient classification (end to end via the orchestrator)
# ============================================================================
def test_transient_failure_parks_retry_wait_and_pauses():
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    assert leg_id is not None, "a transient failure must still record a (RETRY_WAIT) leg"
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        run = s.get(WorkflowRun, run_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT, leg.status
        assert leg.attempt_count == 1
        assert leg.next_attempt_at is not None
        assert leg.retry_contract and leg.retry_contract.get("claim_token"), (
            "the RETRY_WAIT leg must hold the exact package to re-send"
        )
        assert leg.last_error, "a sanitized last_error must be recorded"
        assert run.status == RunStatus.PAUSED, (
            "a transient failure PAUSES, never FAILS",
            run.status,
        )
    print("  [C] transient (non-2xx) -> RETRY_WAIT leg + run PAUSED: OK")


def test_policy_refusal_fails_the_run_with_no_leg():
    # A private endpoint trips the SSRF guard -> ConnectorError (policy) at dispatch.
    env = _seed(endpoint="https://127.0.0.1/intake")
    run_id, _leg_id = _run_to_external(env, _mock_transport())
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.FAILED, ("a policy refusal FAILS the run", run.status)
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)).all()
        assert not legs, "a policy refusal must NEVER leave a phantom leg"
    print("  [C] policy refusal (SSRF/ConnectorError) -> run FAILED, no leg: OK")


# ============================================================================
# [R] retry -> success
# ============================================================================
def test_retry_success_flips_to_dispatched_and_clears_package():
    # retry_leg drives ONE leg deterministically (the sweep is global; selection is tested
    # separately in test_due_retry_legs_selection).
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        due = leg.next_attempt_at + timedelta(seconds=1)
        outcome = vendor_dispatch.retry_leg(s, leg, now=due, transport=_mock_transport())
    assert outcome == "dispatched", outcome
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        run = s.get(WorkflowRun, run_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED, leg.status
        assert leg.retry_contract is None, "the held package must be cleared on success"
        assert leg.next_attempt_at is None
        assert leg.last_error == "", "L1: last_error must be cleared on a delivered retry"
        assert run.status == RunStatus.PAUSED, "the run stays parked awaiting the return"
    print(
        "  [R] due RETRY_WAIT leg re-sent OK -> DISPATCHED, package+error cleared, run parked: OK"
    )


def test_retry_stays_waiting_when_still_failing_midwindow():
    env = _seed()
    _run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        due = leg.next_attempt_at + timedelta(seconds=1)
        outcome = vendor_dispatch.retry_leg(s, leg, now=due, transport=_mock_transport(status=502))
    assert outcome == "retry_wait", outcome
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT
        assert leg.attempt_count == 2, "attempt count advances"
        assert leg.next_attempt_at > due, "the next attempt is scheduled further out"
    print("  [R] a still-failing retry stays RETRY_WAIT and advances the backoff: OK")


def test_due_retry_legs_selection():
    """The sweep's SELECTION: only a RETRY_WAIT leg whose next_attempt_at is in the past is
    due; a future one is not; and retry_due_legs wires to retry_leg for a due leg."""
    env = _seed()
    _run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        past = leg.next_attempt_at - timedelta(seconds=1)  # before it is due
        assert leg_id not in {d.id for d in vendor_dispatch.due_retry_legs(s, now=past)}, (
            "a not-yet-due RETRY_WAIT leg must not be selected"
        )
        due = leg.next_attempt_at + timedelta(seconds=1)
        assert leg_id in {d.id for d in vendor_dispatch.due_retry_legs(s, now=due)}, (
            "a due RETRY_WAIT leg must be selected"
        )
        tally = vendor_dispatch.retry_due_legs(s, now=due, transport=_mock_transport())
        assert tally["retried"] >= 1 and tally["dispatched"] >= 1, tally
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.DISPATCHED
    print("  [R] due_retry_legs selects only past-due legs; retry_due_legs drives them: OK")


# ============================================================================
# [D] retry -> exhaustion -> dead-letter + escalate
# ============================================================================
def test_exhaustion_dead_letters_and_escalates():
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    # Fast-forward to the last scheduled attempt so the NEXT failure exhausts the window.
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        leg.attempt_count = len(vendor_dispatch.RETRY_BACKOFF_SCHEDULE)  # 5
        leg.next_attempt_at = datetime.utcnow() - timedelta(minutes=1)
        s.add(leg)
        s.commit()
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        outcome = vendor_dispatch.retry_leg(
            s, leg, now=datetime.utcnow(), transport=_mock_transport(status=503)
        )
    assert outcome == "dead_letter", outcome
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        run = s.get(WorkflowRun, run_id)
        assert leg.status == VendorDispatchStatus.DEAD_LETTER, leg.status
        assert leg.escalated_at is not None
        assert leg.retry_contract is None, "the held package is dropped on dead-letter"
        assert leg.next_attempt_at is None
        assert run.status == RunStatus.PAUSED, "escalation keeps the run paused, never fails it"
        assert run.notes and "dead-letter" in run.notes.lower(), run.notes
    print("  [D] exhausted retry window -> DEAD_LETTER + escalate to human (run paused): OK")


# ============================================================================
# [B] backoff schedule
# ============================================================================
def test_backoff_schedule_walks_then_terminates():
    sched = vendor_dispatch.RETRY_BACKOFF_SCHEDULE
    assert sched == (1, 5, 15, 60, 360)
    assert vendor_dispatch.next_backoff(1) == timedelta(minutes=1)
    assert vendor_dispatch.next_backoff(5) == timedelta(minutes=360)
    assert vendor_dispatch.next_backoff(6) is None, "beyond the window -> terminal"
    assert vendor_dispatch.next_backoff(0) is None
    print("  [B] next_backoff walks the schedule then returns None (terminal): OK")


# ============================================================================
# [E] error sanitization
# ============================================================================
def test_error_sanitization_strips_urls_and_secrets():
    dirty = (
        "connect to https://vendor.example.com/intake?token=abc123 failed; "
        "authorization: Bearer sk-supersecret signature=sha256=deadbeef"
    )
    clean = vendor_dispatch.sanitize_error(dirty)
    assert "https://" not in clean, clean
    assert "abc123" not in clean, clean
    assert "sk-supersecret" not in clean, clean
    assert "deadbeef" not in clean, clean
    assert "[url]" in clean
    # L2: whitespace-separated forms + filesystem paths are scrubbed too.
    dirty2 = (
        "auth_token sk-whitespace-secret failed at C:\\data\\keys\\vendor.key "
        "and \\\\host\\share\\s.txt; x-locstudio-signature sha256=cafebabe"
    )
    clean2 = vendor_dispatch.sanitize_error(dirty2)
    assert "sk-whitespace-secret" not in clean2, clean2
    assert "cafebabe" not in clean2, clean2
    assert "vendor.key" not in clean2 and "share" not in clean2, ("path leaked", clean2)
    assert "[path]" in clean2
    # L-a: POSIX absolute paths (production is Linux) are scrubbed too, conservatively.
    clean3 = vendor_dispatch.sanitize_error(
        "write failed to /data/loc-studio/uploads/secret.bin (disk full)"
    )
    assert "/data/loc-studio/uploads/secret.bin" not in clean3, ("posix path leaked", clean3)
    assert "[path]" in clean3
    # Conservative: a bare word-slash-word (not an absolute path) is NOT scrubbed.
    assert "and/or" in vendor_dispatch.sanitize_error("choose and/or neither")
    print(
        "  [E] sanitize_error strips URLs, bearer/signature (=, :, whitespace), and "
        "Windows/UNC/POSIX paths (conservatively): OK"
    )


# ============================================================================
# [X] close open legs for a terminal run
# ============================================================================
def test_close_open_legs_closes_retry_wait():
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.COMPLETED  # the run moved on/finished
        s.add(run)
        s.commit()
        n = vendor_dispatch.close_open_legs_for_run(s, run_id)
        assert n == 1, "the RETRY_WAIT leg must be closed on a terminal run"
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.CANCELLED
        assert leg.retry_contract is None, "the held package must be dropped on close"
    print("  [X] a terminal run closes its open RETRY_WAIT leg (package dropped): OK")


# ============================================================================
# [S] AI-vs-Human spend helper
# ============================================================================
def _mk_run_with_spend(ai_costs, legs):
    """Create a WorkflowRun with StepRun.cost_usd entries `ai_costs` and VendorDispatch
    legs `legs` (list of (status, estimate, actual)). Returns run_id."""
    with Session(engine) as s:
        cust = Customer(name=f"C-{_seq()}")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(name=f"P-{_seq()}", source_locale="en-US", customer_id=cust.id)
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        w = Workflow(
            name=f"W-{_seq()}", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        step = wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        v = vendors.create(s, f"V-{_seq()}")
        run = WorkflowRun(
            workflow_id=w.id, document_id=d.id, status=RunStatus.COMPLETED, target_locale="es-ES"
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        for c in ai_costs:
            s.add(
                StepRun(
                    workflow_run_id=run.id, step_id=step.id, status=RunStatus.COMPLETED, cost_usd=c
                )
            )
        for status, est, act in legs:
            s.add(
                VendorDispatch(
                    vendor_id=v.id,
                    workflow_run_id=run.id,
                    status=status,
                    estimate_cost_usd=est,
                    actual_cost_usd=act,
                    customer_id=cust.id,
                )
            )
        s.commit()
        return run.id


def test_spend_helper_math():
    # AI = 1.0 + 2.5 = 3.5; two legs: one estimate 10, one actual 8 (est 12 ignored).
    run_id = _mk_run_with_spend(
        [1.0, 2.5, None],
        [
            (VendorDispatchStatus.DISPATCHED, 10.0, None),
            (VendorDispatchStatus.RETURNED, 12.0, 8.0),
            (VendorDispatchStatus.CANCELLED, 99.0, None),
        ],
    )  # CANCELLED excluded
    with Session(engine) as s:
        summ = vendor_spend.spend_summary(s, [run_id])
    assert summ["ai_spend_usd"] == 3.5, summ
    assert summ["human_spend_usd"] == 18.0, ("10 estimate + 8 actual; cancelled excluded", summ)
    assert summ["human_actual_usd"] == 8.0 and summ["human_estimate_usd"] == 10.0, summ
    assert summ["human_spend_source"] == "mixed", summ
    assert summ["total_spend_usd"] == 21.5, summ

    # actual OVERRIDES estimate: a single returned leg with both set counts the actual.
    run2 = _mk_run_with_spend([], [(VendorDispatchStatus.RETURNED, 50.0, 42.0)])
    with Session(engine) as s:
        summ2 = vendor_spend.spend_summary(s, [run2])
    assert summ2["human_spend_usd"] == 42.0 and summ2["human_spend_source"] == "actual", summ2
    assert summ2["ai_spend_usd"] == 0.0

    # all-estimate -> 'estimate'; no legs -> 'none'/0.
    run3 = _mk_run_with_spend([4.0], [(VendorDispatchStatus.DISPATCHED, 7.0, None)])
    run4 = _mk_run_with_spend([9.0], [])
    with Session(engine) as s:
        s3 = vendor_spend.spend_summary(s, [run3])
        s4 = vendor_spend.spend_summary(s, [run4])
        by = vendor_spend.spend_by_run(s, [run3, run4])
        agg = vendor_spend.spend_summary(s, [run3, run4])
    assert s3["human_spend_source"] == "estimate" and s3["human_spend_usd"] == 7.0, s3
    assert s4["human_spend_source"] == "none" and s4["human_spend_usd"] == 0.0, s4
    # per-run buckets carry each run distinctly; the aggregate sums them.
    assert set(by) == {run3, run4}
    assert by[run3]["human_spend_usd"] == 7.0 and by[run4]["human_spend_usd"] == 0.0
    assert agg["ai_spend_usd"] == 13.0 and agg["human_spend_usd"] == 7.0, agg
    # empty input -> zero summary, never a crash.
    with Session(engine) as s:
        assert vendor_spend.spend_summary(s, [])["total_spend_usd"] == 0.0
    print(
        "  [S] spend helper: AI sum, human actual-overrides-estimate, labels, "
        "CANCELLED excluded, per-run + aggregate: OK"
    )


# ============================================================================
# FIX-ROUND pinning tests (the review's core criticism: the suite was silent
# exactly where the state machine broke).
# ============================================================================
def test_retry_wait_blocks_resume_dead_letter_does_not():
    """BLOCKER: vendor_pause_state must flag a RETRY_WAIT leg as a BLOCKING pause (a human
    must not advance past it), and a DEAD_LETTER leg as a NON-blocking, human-resumable
    escalation (mirroring ESCALATED)."""
    # RETRY_WAIT -> blocking.
    env = _seed()
    run_id, _leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        leg, state = vendor_dispatch.vendor_pause_state(s, run_id)
        assert leg is not None and state == "retry_wait", state
        assert vendor_dispatch.blocks_human_resume(state) is True
        assert vendor_dispatch.open_leg_at_current_pause(s, run_id) is not None
    # DEAD_LETTER (via a non-retryable first-dispatch refusal) -> non-blocking.
    env2 = _seed()
    run2, leg2 = _run_to_external(env2, _mock_transport(status=403))
    with Session(engine) as s:
        leg, state = vendor_dispatch.vendor_pause_state(s, run2)
        assert leg is not None and state == "dead_letter", state
        assert vendor_dispatch.blocks_human_resume(state) is False, (
            "a dead-lettered leg has escalated to a human and IS resumable"
        )
        assert vendor_dispatch.open_leg_at_current_pause(s, run2) is None
        assert s.get(WorkflowRun, run2).status == RunStatus.PAUSED
        assert s.get(VendorDispatch, leg2).status == VendorDispatchStatus.DEAD_LETTER
    print("  [BLOCKER] retry_wait blocks human resume; dead_letter escalates + resumable: OK")


def test_retry_leg_cancels_when_run_advanced():
    """HIGH-A: a due RETRY_WAIT leg whose run is no longer PAUSED at THIS step (it advanced /
    is running) must NOT be re-sent to a real vendor -- the leg is CANCELLED, package dropped."""
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.RUNNING  # advanced past the external pause (non-terminal)
        s.add(run)
        s.commit()
        leg = s.get(VendorDispatch, leg_id)
        outcome = vendor_dispatch.retry_leg(
            s, leg, now=datetime.utcnow() + timedelta(hours=1), transport=_mock_transport()
        )  # would SUCCEED if sent
        assert outcome == "closed", outcome
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.CANCELLED
        assert leg.retry_contract is None, "the held package must be dropped, never re-sent"
    print("  [HIGH-A] retry of a run that advanced is CANCELLED, never re-dispatched: OK")


def test_retry_leg_dead_letters_on_endpoint_change():
    """HIGH-B: if the vendor endpoint host changed while the leg was parked, the retry must
    NOT ship content to the new host -- it dead-letters + escalates."""
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        v = s.get(Vendor, env["vendor_id"])
        v.endpoint_url = "https://different-host.example.com/intake"  # host changed
        s.add(v)
        s.commit()
        leg = s.get(VendorDispatch, leg_id)
        # A success transport would deliver if we sent -- but the host-pin must refuse first.
        outcome = vendor_dispatch.retry_leg(
            s, leg, now=leg.next_attempt_at + timedelta(seconds=1), transport=_mock_transport()
        )
        assert outcome == "dead_letter", outcome
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DEAD_LETTER
        assert "endpoint changed" in leg.last_error.lower(), leg.last_error
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
    print("  [HIGH-B] endpoint change while parked -> DEAD_LETTER, content NOT re-sent: OK")


def test_expired_retry_wait_escalates_above_the_gate():
    """HIGH-D: the ABOVE-gate expiry sweep (runs regardless of the sync kill switch) must
    expire a RETRY_WAIT leg whose bounded wait elapsed, clearing its held package -- so a
    retrying leg is never immortal when outbound sync is off."""
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT
        leg.expires_at = datetime.utcnow() - timedelta(hours=1)  # bounded wait elapsed
        s.add(leg)
        s.commit()
        # expired_legs must SEE a RETRY_WAIT leg (was DISPATCHED-only before the fix).
        assert leg_id in {d.id for d in vendor_dispatch.expired_legs(s)}
        n = vendor_dispatch.sweep_expired(s)
        assert n >= 1, n
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.ESCALATED, leg.status
        assert leg.retry_contract is None and leg.next_attempt_at is None
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
    print("  [HIGH-D] an expired RETRY_WAIT leg escalates above the gate, package cleared: OK")


def test_dead_letter_leg_is_unbilled_human_spend():
    """M2: a DEAD_LETTER leg never reached the vendor, so its estimate must NOT count as
    Human spend."""
    run_id = _mk_run_with_spend(
        [1.0],
        [
            (VendorDispatchStatus.DEAD_LETTER, 500.0, None),  # excluded
            (VendorDispatchStatus.DISPATCHED, 7.0, None),
        ],
    )  # counted
    with Session(engine) as s:
        summ = vendor_spend.spend_summary(s, [run_id])
    assert summ["human_spend_usd"] == 7.0, ("dead-lettered leg must be unbilled", summ)
    assert summ["human_leg_count"] == 1, summ
    print("  [M2] a DEAD_LETTER leg is unbilled (excluded from Human spend): OK")


def test_partial_return_actual_reads_as_estimate():
    """M3: a leg with an interim actual but ids still OUTSTANDING must read as 'estimate'
    grade (not settled), not 'actual'."""
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport())  # a normal DISPATCHED leg
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        # Simulate a partial return: an actual is stamped but the ledger still has outstanding
        # ids (nothing marked returned yet).
        leg.actual_cost_usd = 42.0
        s.add(leg)
        s.commit()
        assert vendor_return.outstanding_ids(leg), "precondition: ids still outstanding"
        _cost, grade = vendor_spend.leg_human_cost(leg)
        assert grade == "estimate", "an actual with outstanding tranches is not settled"
        summ = vendor_spend.spend_summary(s, [run_id])
        assert summ["human_spend_source"] == "estimate", summ
    print("  [M3] a partial-return interim actual reads as estimate-grade: OK")


def test_policy_status_escalates_at_first_dispatch():
    """M4: a non-retryable response (403) at first dispatch escalates (DEAD_LETTER + paused),
    it does NOT silently FAIL and does NOT park for retry."""
    env = _seed()
    run_id, leg_id = _run_to_external(env, _mock_transport(status=403))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        run = s.get(WorkflowRun, run_id)
        assert leg.status == VendorDispatchStatus.DEAD_LETTER, leg.status
        assert leg.retry_contract is None, "a policy refusal is never re-sent"
        assert leg.last_error, "a sanitized reason must be recorded"
        assert run.status == RunStatus.PAUSED, "escalated to a human, not FAILED"
    print("  [M4] non-retryable 403 at first dispatch -> DEAD_LETTER + paused (not FAILED): OK")


def test_retry_after_is_honored_capped_and_floored():
    """M4 + L-b: a Retry-After on a retryable (503) response is honored, CAPPED at the next
    backoff step, and FLOORED so `Retry-After: 0` can't skip the backoff."""

    def _retry_after_transport(seconds):
        return httpx.MockTransport(
            lambda req: httpx.Response(503, headers={"retry-after": str(seconds)}, json={})
        )

    floor = vendor_dispatch.RETRY_AFTER_FLOOR_SECONDS
    # A mid-range Retry-After (45s: above the floor, below next_backoff(2)=5min) -> honored.
    env = _seed()
    _run_id, leg_id = _run_to_external(env, _mock_transport(status=503))  # attempt 1, RETRY_WAIT
    now = datetime.utcnow()
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        outcome = vendor_dispatch.retry_leg(s, leg, now=now, transport=_retry_after_transport(45))
        assert outcome == "retry_wait", outcome
        delta = (s.get(VendorDispatch, leg_id).next_attempt_at - now).total_seconds()
        assert 44 <= delta <= 46, ("Retry-After 45s must be honored", delta)
    # A huge Retry-After is CAPPED at the backoff step (never strands the leg).
    env2 = _seed()
    _r2, l2 = _run_to_external(env2, _mock_transport(status=503))
    now2 = datetime.utcnow()
    with Session(engine) as s:
        leg = s.get(VendorDispatch, l2)
        vendor_dispatch.retry_leg(s, leg, now=now2, transport=_retry_after_transport(999999))
        leg = s.get(VendorDispatch, l2)
        cap = vendor_dispatch.next_backoff(leg.attempt_count)  # next_backoff(2) = 5 min
        delta = (leg.next_attempt_at - now2).total_seconds()
        assert delta <= cap.total_seconds() + 1, ("a huge Retry-After must be capped", delta)
    # L-b: Retry-After 0 is FLOORED to RETRY_AFTER_FLOOR_SECONDS (no immediate re-hammer).
    env3 = _seed()
    _r3, l3 = _run_to_external(env3, _mock_transport(status=503))
    now3 = datetime.utcnow()
    with Session(engine) as s:
        leg = s.get(VendorDispatch, l3)
        vendor_dispatch.retry_leg(s, leg, now=now3, transport=_retry_after_transport(0))
        delta = (s.get(VendorDispatch, l3).next_attempt_at - now3).total_seconds()
        assert delta >= floor - 1, ("Retry-After 0 must be floored, not immediate", delta)
    print(
        f"  [M4/L-b] Retry-After honored, capped at the backoff step, and floored at {floor}s: OK"
    )


def test_retry_dead_letters_when_egress_blocked():
    """M5: the mock-egress perimeter is re-checked on retry; if it now blocks, the leg
    dead-letters as policy rather than re-sending offline placeholder text to a real vendor."""
    from app.services.connectors.egress_guard import MockEgressBlocked

    env = _seed()
    _run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    real = vendor_dispatch.assert_dispatch_egress_allowed
    set_pieces(
        "app.services.vendor_dispatch",
        "assert_dispatch_egress_allowed",
        lambda *a, **k: (_ for _ in ()).throw(MockEgressBlocked("mock egress blocked on retry")),
    )
    try:
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            outcome = vendor_dispatch.retry_leg(
                s, leg, now=leg.next_attempt_at + timedelta(seconds=1), transport=_mock_transport()
            )
            assert outcome == "dead_letter", outcome
            assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.DEAD_LETTER
    finally:
        set_pieces("app.services.vendor_dispatch", "assert_dispatch_egress_allowed", real)
    print("  [M5] retry re-runs the egress guard; a block dead-letters (no mock egress): OK")


def test_dispatch_refuses_unregistered_protocol():
    """M6 + MED-1: the dispatch seam FAILS CLOSED on an unregistered protocol (was
    fail-open). It is a LOCAL CONFIG error -> ConnectorError, so first dispatch FAILS the
    run (like the SSRF / non-https sibling refusals), never a DEAD_LETTER pause whose only
    action ships un-post-edited content."""
    env = _seed()
    with Session(engine) as s:
        v = s.get(Vendor, env["vendor_id"])
        v.protocol = "ghost_adapter"  # forced past validation (hand-edited row)
        s.add(v)
        s.commit()
        raised = False
        try:
            vendor_dispatch.dispatch(
                v, {"claim_token": "x", "segments": []}, transport=_mock_transport()
            )
        except ConnectorError:
            raised = True
        assert raised, "dispatch must refuse an unregistered protocol as ConnectorError"
    # End to end: a local-config refusal FAILS the run with NO phantom leg.
    run_id, _leg_id = _run_to_external(env, _mock_transport())
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.FAILED, "unknown protocol FAILs"
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)).all()
        assert not legs, "a local-config FAIL must never leave a phantom leg"
    print("  [M6/MED-1] dispatch seam fails closed on an unregistered protocol -> FAIL: OK")


def test_retry_dead_letters_when_package_missing():
    """L3: the vendor-gone / package-missing branch dead-letters rather than spinning."""
    env = _seed()
    _run_id, leg_id = _run_to_external(env, _mock_transport(status=503))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        leg.retry_contract = None  # the held package is gone
        s.add(leg)
        s.commit()
        outcome = vendor_dispatch.retry_leg(s, leg, now=leg.next_attempt_at + timedelta(seconds=1))
        assert outcome == "dead_letter", outcome
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.DEAD_LETTER
    print("  [L3] a missing retry package dead-letters (never spins): OK")


def test_migration_downgrade_normalizes_new_statuses():
    """M8: the downgrade normalizes retry_wait->dispatched and dead_letter->escalated BEFORE
    dropping the Wave-C columns, so no leg is stranded with a status the old code can't read."""
    import glob as _glob
    import sqlite3
    import subprocess
    import tempfile

    d = tempfile.mkdtemp(prefix="loctms_mig_")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env2 = dict(os.environ, LOC_TMS_DATA_DIR=d)

    def _alembic(*a):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *a],
            env=env2,
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (a, r.stdout[-800:], r.stderr[-800:])

    # Pin to the WAVE-C revision (not "head") so this migration test stays correct when
    # LATER migrations land on top: this must exercise exactly Wave C's up+down, not
    # whatever the current head happens to be. Wave C is b2c3d4e5f6a7; above it now sit
    # Vendor Wave E (c3d4e5f6a7b8) then the residual-sweep archive migration
    # (2a98273c3c99), so a bare "head"/"-1" would undo one of THOSE. The downgrade below is
    # likewise pinned to Wave C's own down_revision (a1b2c3d4e5f6).
    _alembic("upgrade", "b2c3d4e5f6a7")
    dbs = _glob.glob(os.path.join(d, "*.db")) + _glob.glob(os.path.join(d, "*.sqlite*"))
    assert dbs, ("no sqlite db produced under LOC_TMS_DATA_DIR", os.listdir(d))
    dbf = dbs[0]
    con = sqlite3.connect(dbf)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        cols = con.execute("PRAGMA table_info(vendordispatch)").fetchall()
        names = [c[1] for c in cols if not c[5]]  # non-pk columns

        def _val(c, status):
            name, ctype, notnull = c[1], (c[2] or "").upper(), c[3]
            if name == "status":
                return status
            if name in ("vendor_id", "workflow_run_id"):
                return 1
            if not notnull:  # a nullable column -> NULL
                return None
            if "INT" in ctype:
                return 0
            if "FLOAT" in ctype or "REAL" in ctype or "DOUB" in ctype or "NUM" in ctype:
                return 0.0
            if "DATE" in ctype or "TIME" in ctype:
                return "2026-01-01 00:00:00"
            return ""  # VARCHAR / TEXT / JSON etc.

        ph = ",".join("?" for _ in names)
        sql = f"INSERT INTO vendordispatch ({','.join(names)}) VALUES ({ph})"
        con.execute(sql, [_val(c, "retry_wait") for c in cols if not c[5]])
        con.execute(sql, [_val(c, "dead_letter") for c in cols if not c[5]])
        con.commit()
    finally:
        con.close()

    # Downgrade to the pre-Wave-C revision (a1b2c3d4e5f6 = lqa_threshold, the down_revision
    # of the Wave-C revision). This runs the Wave-E downgrade first (a no-op on these Wave-C-
    # status rows) then the Wave-C downgrade under test -- an explicit target rather than a
    # relative "-1", so it stays correct as later waves (Wave E, ...) add revisions on top.
    _alembic("downgrade", "a1b2c3d4e5f6")

    con = sqlite3.connect(dbf)
    try:
        after_cols = {c[1] for c in con.execute("PRAGMA table_info(vendordispatch)").fetchall()}
        assert "retry_contract" not in after_cols and "attempt_count" not in after_cols, (
            "downgrade must drop the Wave-C columns"
        )
        stranded = con.execute(
            "SELECT COUNT(*) FROM vendordispatch WHERE status IN ('retry_wait','dead_letter')"
        ).fetchone()[0]
        assert stranded == 0, "no leg may be stranded with a Wave-C-only status"
        norm = dict(
            con.execute("SELECT status, COUNT(*) FROM vendordispatch GROUP BY status").fetchall()
        )
        assert norm.get("dispatched", 0) >= 1 and norm.get("escalated", 0) >= 1, norm
    finally:
        con.close()
    print("  [M8] downgrade normalizes retry_wait->dispatched, dead_letter->escalated: OK")


def main():
    test_protocol_registry_and_validation()
    test_poll_routes_through_protocol()
    test_transient_failure_parks_retry_wait_and_pauses()
    test_policy_refusal_fails_the_run_with_no_leg()
    test_retry_success_flips_to_dispatched_and_clears_package()
    test_retry_stays_waiting_when_still_failing_midwindow()
    test_due_retry_legs_selection()
    test_exhaustion_dead_letters_and_escalates()
    test_backoff_schedule_walks_then_terminates()
    test_error_sanitization_strips_urls_and_secrets()
    test_close_open_legs_closes_retry_wait()
    test_spend_helper_math()
    # --- fix-round pinning tests ---
    test_retry_wait_blocks_resume_dead_letter_does_not()
    test_retry_leg_cancels_when_run_advanced()
    test_retry_leg_dead_letters_on_endpoint_change()
    test_expired_retry_wait_escalates_above_the_gate()
    test_dead_letter_leg_is_unbilled_human_spend()
    test_partial_return_actual_reads_as_estimate()
    test_policy_status_escalates_at_first_dispatch()
    test_retry_after_is_honored_capped_and_floored()
    test_retry_dead_letters_when_egress_blocked()
    test_dispatch_refuses_unregistered_protocol()
    test_retry_dead_letters_when_package_missing()
    test_migration_downgrade_normalizes_new_statuses()
    print("VENDOR WAVE C OK")


if __name__ == "__main__":
    main()
