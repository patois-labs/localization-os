"""The EXTERNAL dispatch-and-wait workflow step, end to end (PROPOSAL-external-processing-
step §9 Wave A). Hermetic + offline: the outbound dispatch runs through an injected
httpx.MockTransport (vendor_dispatch.set_test_transport) so NO real network is touched, and
the SSRF guard is proven against a hostile endpoint literal.

  [A] DISPATCH end-to-end (segmented): a [translate -> external] run reaches the external
      step, packages the in-flight state into the §3.2 contract (ids + sources +
      placeholders + claim token), signs it (HMAC over the raw body), sends it through the
      SSRF guard, PARKS the leg PAUSED (reusing the human_review pause), records a
      VendorDispatch (claim token + scoping estimate + host-only endpoint), and emits a
      `paused` event whose envelope carries the host + estimate but NEVER the claim token.
  [B] SSRF: a vendor endpoint pointing at the cloud-metadata IP is REFUSED — the step FAILS,
      the run FAILS, and NO dispatch leg is recorded (no phantom leg).
  [C] EXPIRY (decision 3): an expired dispatched leg ESCALATES to a human_review pause
      (never a hard fail); a future / unbounded leg is left parked.
  [D] the FULL_TEXT loop dispatches too (the ONE seam, both loops).
"""

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_extstep_")

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
    StepType,
    TransMode,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import orchestrator, vendor_dispatch, vendors
from app.services import workflows as wf
from app.services.connectors.base import ConnectorError

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


_CAPTURED: list = []


def _mock_transport(status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        _CAPTURED.append(
            {
                "url": str(req.url),
                "headers": dict(req.headers),
                "body": req.content,
                "method": req.method,
            }
        )
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handler)


def _seed(endpoint="https://vendor.example.com/intake", banded=True, mode=TransMode.SEGMENTED):
    """A customer + project + banded segmented doc + a vendor linked to the customer."""
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
        if mode == TransMode.SEGMENTED:
            d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
            s.add(d)
            s.commit()
            s.refresh(d)
            rows = [
                ("Hello ⟦x1⟧ world.", None if banded else None, None),  # noqa: RUF034
                ("The cat sat.", 100 if banded else None, None),
                ("Please sign in.", 88 if banded else None, None),
            ]
            for i, (src, pct, band) in enumerate(rows):
                s.add(
                    Segment(
                        document_id=d.id,
                        target_locale="es-ES",
                        order_index=i,
                        source_text=src,
                        status=SegStatus.NEW,
                        tm_match_pct=pct,
                        lock_band=band,
                    )
                )
        else:
            d = Document(
                project_id=p.id,
                filename="ft.docx",
                fmt="docx",
                mode=TransMode.FULL_TEXT,
                skeleton={"units": [{"text": "Hello world."}], "seg_locale": "en-US"},
            )
            s.add(d)
            s.commit()
            s.refresh(d)
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=0,
                    source_text="Hello world.",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        v = Vendor(
            name=f"V-{_seq()}",
            endpoint_url=endpoint,
            rate_card={"currency": "USD", "bands": {"new": 0.2, "fuzzy_mid": 0.1}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": "topsecret"})
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
        return {"customer_id": cust.id, "doc_id": d.id, "vendor_id": v.id, "wf_id": w.id}


def _translate_step(wid):
    with Session(engine) as s:
        wf.add_step(
            s,
            wid,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )


def _external_step(wid, vid, **kw):
    with Session(engine) as s:
        return wf.add_step(
            s, wid, name="Vendor review", step_type=StepType.EXTERNAL, vendor_id=vid, **kw
        ).id


def _run(wid, doc_id, locale="es-ES"):
    with Session(engine) as s:
        return list(orchestrator.run_workflow_stream(s, wid, doc_id, locale))


# ============================================================================
# [A] DISPATCH end to end
# ============================================================================
def test_external_step_skipped_when_all_units_protected():
    """F6 empty-payload LOW (adversarial re-review): a doc whose ENTIRE dispatchable set is
    human-approved yields an EMPTY vendor contract (the segmented payload builder drops every
    protected segment). The external step must SKIP -- no dispatch, no PARK -- rather than send
    an empty contract and wait for sweep_expired to escalate it. No leg is created and the run
    completes."""
    env = _seed()
    # approve EVERY segment so nothing is dispatchable
    with Session(engine) as s:
        for seg in s.exec(select(Segment).where(Segment.document_id == env["doc_id"])).all():
            seg.target_text = "APROBADO %d" % seg.order_index  # noqa: UP031
            seg.status = SegStatus.APPROVED
            s.add(seg)
        s.commit()
    _external_step(env["wf_id"], env["vendor_id"], external_wait_hours=72)
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        events = _run(env["wf_id"], env["doc_id"])
    finally:
        vendor_dispatch.set_test_transport(None)
    types = [e.get("type") for e in events]
    assert "paused" not in types, ("an all-protected doc must NOT park on an empty contract", types)
    assert not any(e.get("paused_reason") == "external_dispatch" for e in events), events
    assert "done" in types, ("the run must complete", types)
    run_id = next(e["run_id"] for e in events if e.get("type") == "run_started")
    with Session(engine) as s:
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)).all()
        assert not legs, ("no vendor leg may be created for an empty contract", legs)
    print(
        "  [F6] an external step over an all-protected doc SKIPS (no dispatch, no leg, no "
        "park) and the run completes OK"
    )


def test_dispatch_end_to_end():
    _CAPTURED.clear()
    env = _seed()
    _translate_step(env["wf_id"])
    _external_step(env["wf_id"], env["vendor_id"], external_wait_hours=72)
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        events = _run(env["wf_id"], env["doc_id"])
    finally:
        vendor_dispatch.set_test_transport(None)

    paused = [e for e in events if e.get("type") == "paused"]
    assert paused, f"expected a paused event, got {[e.get('type') for e in events]}"
    ext = paused[-1].get("external")
    assert ext is not None, "paused event missing the external envelope"
    assert ext["endpoint_host"] == "vendor.example.com"
    assert ext["segment_count"] == 3
    assert ext["expires_at"] is not None
    # SECRET DISCIPLINE: the claim token is NEVER in the SSE envelope.
    assert "claim_token" not in ext and "topsecret" not in json.dumps(paused[-1])

    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        assert run.status == RunStatus.PAUSED, "the leg parks PAUSED"
        # A PAUSED StepRun exists for the external step (human_review pause machinery).
        from app.models import StepRun

        prs = s.exec(
            select(StepRun).where(
                StepRun.workflow_run_id == run.id, StepRun.status == RunStatus.PAUSED
            )
        ).all()
        assert prs, "external pause must persist a PAUSED StepRun"
        # The dispatch leg is recorded with the claim token + estimate + host.
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED
        assert leg.claim_token, "claim token persisted"
        assert leg.endpoint_host == "vendor.example.com"
        assert leg.segment_count == 3
        assert leg.estimate is not None and "bands" in leg.estimate
        # Banded estimate: the pct-100 segment is covered (exact), so billable < total-words.
        assert leg.estimate["bands"]["exact"]["segments"] == 1
        assert leg.expires_at is not None
        claim = leg.claim_token
        signing = "topsecret"

    # The contract that hit the wire: shape + ids + placeholders + HMAC signature.
    assert len(_CAPTURED) == 1, "exactly one outbound dispatch"
    cap = _CAPTURED[0]
    contract = json.loads(cap["body"])
    assert contract["contract_version"] == vendor_dispatch.CONTRACT_VERSION
    assert contract["claim_token"] == claim
    assert contract["source_locale"] == "en-US" and contract["target_locale"] == "es-ES"
    assert [seg["id"] for seg in contract["segments"]], "segments carry stable ids"
    assert len(contract["segments"]) == 3
    # The masked placeholder inventory rides the contract (⟦x1⟧ on segment 0).
    assert contract["segments"][0]["placeholders"] == ["⟦x1⟧"]
    # HMAC signature over the RAW body matches the vendor's signing secret.
    expect = "sha256=" + hmac.new(signing.encode(), cap["body"], hashlib.sha256).hexdigest()
    assert cap["headers"].get("x-locstudio-signature") == expect, "HMAC signature"
    assert cap["headers"].get("x-locstudio-claim") == claim
    print("  [A] dispatch end-to-end: contract + claim + estimate + PAUSED, secret-safe: OK")


# ============================================================================
# [B] SSRF refusal
# ============================================================================
def test_ssrf_endpoint_refused_end_to_end():
    _CAPTURED.clear()
    env = _seed(endpoint="http://169.254.169.254/latest/meta-data")
    _translate_step(env["wf_id"])
    _external_step(env["wf_id"], env["vendor_id"])
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        events = _run(env["wf_id"], env["doc_id"])
    finally:
        vendor_dispatch.set_test_transport(None)
    errs = [e for e in events if e.get("type") == "error"]
    assert errs, "a hostile endpoint must fail the step"
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        assert run.status == RunStatus.FAILED
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).all()
        assert not legs, "no phantom dispatch leg is recorded on SSRF refusal"
    assert not _CAPTURED, "nothing left the process for a blocked target"
    print("  [B] SSRF metadata-IP endpoint refused; step FAILED, no leg, no egress: OK")


def test_dispatch_direct_ssrf_raises():
    with Session(engine) as s:
        v = Vendor(name="ssrf", endpoint_url="http://10.0.0.1/x")
        s.add(v)
        s.commit()
        s.refresh(v)
        try:
            vendor_dispatch.dispatch(v, {"claim_token": "t"}, transport=_mock_transport())
            raise AssertionError("private-range endpoint must raise ConnectorError")
        except ConnectorError:
            pass
    print("  [B] dispatch() raises ConnectorError on a private-range endpoint: OK")


def test_dispatch_ssrf_numeric_encoding_refused():
    """P-F1: the vendor dispatch path funnels through the shared base._validate_target, so an
    endpoint given as an ALTERNATE numeric encoding of a private/metadata IP (glibc resolves it
    to loopback/metadata at connect time) is refused at dispatch — never egresses. https so it
    clears the https-only allowlist and the SSRF range check is what must catch it."""
    for endpoint in (
        "https://2130706433/x",  # decimal -> 127.0.0.1
        "https://0x7f000001/x",  # hex     -> 127.0.0.1
        "https://2852039166/x",  # decimal -> 169.254.169.254 (metadata)
        "https://100.64.1.1/x",
    ):  # CGNAT literal is_private misses
        with Session(engine) as s:
            v = Vendor(name="ssrf-num", endpoint_url=endpoint)
            s.add(v)
            s.commit()
            s.refresh(v)
            raised = False
            try:
                vendor_dispatch.dispatch(v, {"claim_token": "t"}, transport=_mock_transport())
            except ConnectorError:
                raised = True
            assert raised, (
                f"P-F1: numeric-encoded private endpoint must raise at dispatch: {endpoint}"
            )
    print(
        "  [B] dispatch() refuses alternate-numeric-encoded private/metadata endpoints (P-F1): OK"
    )


# ============================================================================
# [C] EXPIRY escalation (decision 3)
# ============================================================================
def test_expiry_escalates_to_human_review():
    env = _seed()
    _translate_step(env["wf_id"])
    _external_step(env["wf_id"], env["vendor_id"], external_wait_hours=72)
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        _run(env["wf_id"], env["doc_id"])
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        # Force the leg past its expiry.
        leg.expires_at = datetime.utcnow() - timedelta(hours=1)
        s.add(leg)
        s.commit()
        leg_id, run_id = leg.id, run.id
    with Session(engine) as s:
        n = vendor_dispatch.sweep_expired(s)
        assert n == 1, "the expired leg is escalated"
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.ESCALATED
        assert leg.escalated_at is not None
        run = s.get(WorkflowRun, run_id)
        # decision 3: NEVER a hard fail — it stays PAUSED (a human decides).
        assert run.status == RunStatus.PAUSED
        assert "escalated to human review" in (run.notes or "")
        # Idempotent: a second sweep finds nothing.
        assert vendor_dispatch.sweep_expired(s) == 0
    print("  [C] expiry escalates DISPATCHED -> human_review pause, idempotent, no fail: OK")


def test_future_and_unbounded_legs_not_escalated():
    with Session(engine) as s:
        # future expiry
        leg1 = VendorDispatch(
            vendor_id=1,
            workflow_run_id=1,
            status=VendorDispatchStatus.DISPATCHED,
            expires_at=datetime.utcnow() + timedelta(hours=5),
        )
        # unbounded (NULL expiry)
        leg2 = VendorDispatch(
            vendor_id=1, workflow_run_id=1, status=VendorDispatchStatus.DISPATCHED, expires_at=None
        )
        s.add(leg1)
        s.add(leg2)
        s.commit()
        assert vendor_dispatch.expired_legs(s) == []
        assert vendor_dispatch.sweep_expired(s) == 0
    print("  [C] a future / unbounded leg is never escalated: OK")


# ============================================================================
# [D] FULL_TEXT loop dispatches too (the one seam, both loops)
# ============================================================================
def test_full_text_loop_dispatches():
    _CAPTURED.clear()
    env = _seed(mode=TransMode.FULL_TEXT)
    _translate_step(env["wf_id"])
    _external_step(env["wf_id"], env["vendor_id"])
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        events = _run(env["wf_id"], env["doc_id"])
    finally:
        vendor_dispatch.set_test_transport(None)
    assert [e for e in events if e.get("type") == "paused"], "full-text run must pause"
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        assert run.status == RunStatus.PAUSED
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.segment_count == 1
    assert len(_CAPTURED) == 1
    contract = json.loads(_CAPTURED[0]["body"])
    assert len(contract["segments"]) == 1, "full-text = one whole-body contract unit"
    print("  [D] FULL_TEXT loop dispatches through the same seam: OK")


# ============================================================================
# Fix-round regressions (security review)
# ============================================================================
def test_h2_mock_egress_refused_before_egress():
    """H2: a keyless/mock install must NEVER POST MockProvider placeholder text to a REAL
    vendor endpoint. With NO test transport (real egress) + a mock provider, the dispatch
    is REFUSED before any bytes leave -- the step FAILS, no leg, no capture."""
    from app.services import providers, settings

    settings.set_many({"allow_mock_egress": "0"})
    settings.invalidate_cache()
    assert providers.provider_is_live("mock") is False, "precondition: mock is not live"
    _CAPTURED.clear()
    env = _seed()
    _translate_step(env["wf_id"])
    _external_step(env["wf_id"], env["vendor_id"])
    vendor_dispatch.set_test_transport(None)  # REAL egress -> the guard must fire
    events = _run(env["wf_id"], env["doc_id"])
    assert [e for e in events if e.get("type") == "error"], "mock egress must fail the step"
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        assert run.status == RunStatus.FAILED
        assert not s.exec(
            select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)
        ).all(), "no leg on egress refusal"
    assert not _CAPTURED, "nothing left the process (the guard fired before dispatch)"
    print("  [H2] mock-content dispatch refused before egress; step FAILED, no leg: OK")


def test_m2_http_endpoint_refused():
    """M2: http:// is refused at SAVE (vendors.create) AND at dispatch."""
    with Session(engine) as s:
        try:
            vendors.create(s, "HttpVendor", endpoint_url="http://vendor.example/x")
            raise AssertionError("http:// endpoint must be refused at save")
        except vendors.VendorError:
            pass
        # Direct dispatch to an http endpoint (constructed past the save guard) is refused.
        v = Vendor(name="httpdirect", endpoint_url="http://public.example/x")
        s.add(v)
        s.commit()
        s.refresh(v)
        try:
            vendor_dispatch.dispatch(v, {"claim_token": "t"}, transport=_mock_transport())
            raise AssertionError("http:// dispatch must raise ConnectorError")
        except ConnectorError:
            pass
    print("  [M2] http:// endpoint refused at save and at dispatch: OK")


def test_m1_forged_config_headers_cannot_override():
    """M1: a vendor's config.headers can NEVER override Host, our x-locstudio-* provenance
    headers, the claim token, content-type, or the bearer -- only benign extras survive."""
    _CAPTURED.clear()
    with Session(engine) as s:
        v = Vendor(
            name="hdr",
            endpoint_url="https://real.vendor.example/intake",
            config={
                "method": "DELETE",
                "headers": {
                    "Host": "evil.attacker.example",
                    "X-LocStudio-Claim": "forged-token",
                    "Authorization": "Bearer attacker-key",
                    "Content-Type": "text/evil",
                    "X-Account": "acme",
                },
            },
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": "s"})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendor_dispatch.dispatch(v, {"claim_token": "real-token"}, transport=_mock_transport())
    cap = _CAPTURED[-1]
    h = cap["headers"]
    assert h.get("host") == "real.vendor.example", ("Host smuggling", h.get("host"))
    assert h.get("x-locstudio-claim") == "real-token", "config forged the claim token"
    assert h.get("authorization") != "Bearer attacker-key", "config forged the bearer"
    assert h.get("content-type") == "application/json", "config forged content-type"
    assert h.get("x-account") == "acme", "a benign config header should survive"
    assert cap["method"] == "POST", "method forced to POST (no config-driven verb)"
    print("  [M1] forged Host/claim/auth/content-type stripped; method forced POST: OK")


def test_m7_non_2xx_taxonomy():
    """M7 + Wave-C M4 taxonomy: only a 2xx is accepted. A 3xx (not followed -> delivered to
    nobody) or a NON-retryable 4xx is a POLICY refusal (VendorPolicyRefusal -> escalate to a
    human, never park+bill), while a retryable 5xx / 408 / 429 is a TRANSIENT failure
    (VendorDispatchError -> backoff retry)."""
    with Session(engine) as s:
        v = Vendor(name="redir", endpoint_url="https://vendor.example/intake")
        s.add(v)
        s.commit()
        s.refresh(v)
        for code in (301, 302, 303, 307, 401, 403, 404, 413):
            try:
                vendor_dispatch.dispatch(
                    v, {"claim_token": "t"}, transport=_mock_transport(status=code)
                )
                raise AssertionError(f"HTTP {code} must be a policy refusal")
            except vendor_dispatch.VendorPolicyRefusal:
                pass
        for code in (408, 429, 500, 502, 503):
            try:
                vendor_dispatch.dispatch(
                    v, {"claim_token": "t"}, transport=_mock_transport(status=code)
                )
                raise AssertionError(f"HTTP {code} must be a transient failure")
            except vendor_dispatch.VendorDispatchError:
                pass
        # A 2xx (200) IS accepted (sanity that the gate is not a blanket reject).
        assert (
            vendor_dispatch.dispatch(
                v, {"claim_token": "t"}, transport=_mock_transport(status=200)
            ).status_code
            == 200
        )
    print(
        "  [M7/M4] 3xx + non-retryable 4xx = policy refusal; 5xx/408/429 = transient; "
        "only 2xx accepted: OK"
    )


def test_h4_terminal_run_untouched_by_sweep():
    """H4: sweep_expired must NEVER drag a completed/failed run back to PAUSED. An expired
    leg whose run is terminal is CLOSED (cancelled), never escalated."""
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=1,
            document_id=1,
            target_locale="es-ES",
            status=RunStatus.COMPLETED,
            finished_at=datetime.utcnow(),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        leg = VendorDispatch(
            vendor_id=1,
            workflow_run_id=run.id,
            status=VendorDispatchStatus.DISPATCHED,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        s.add(leg)
        s.commit()
        run_id, leg_id = run.id, leg.id
    with Session(engine) as s:
        vendor_dispatch.sweep_expired(s)
        run = s.get(WorkflowRun, run_id)
        leg = s.get(VendorDispatch, leg_id)
        assert run.status == RunStatus.COMPLETED, "sweep resurrected a terminal run!"
        assert run.finished_at is not None, "sweep cleared finished_at on a terminal run!"
        assert leg.status == VendorDispatchStatus.CANCELLED, "stale leg must be CLOSED"
    print("  [H4] a completed run is untouched by sweep_expired; stale leg closed: OK")


def test_m3_dispatch_pause_notifies_assignee():
    """M3: an external step with an assignee EMAILS them when the run parks on the dispatch
    (and, by the same path, when an expired leg escalates)."""
    from app.services import notifications

    sent: list = []
    orig = notifications.mailer.send
    notifications.mailer.send = lambda to, subj, body: sent.append((to, subj)) or True
    try:
        env = _seed()
        _translate_step(env["wf_id"])
        _external_step(env["wf_id"], env["vendor_id"], assignee_email="owner@example.com")
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            _run(env["wf_id"], env["doc_id"])
        finally:
            vendor_dispatch.set_test_transport(None)
    finally:
        notifications.mailer.send = orig
    assert any(to == "owner@example.com" for to, _ in sent), (
        "the external dispatch pause must notify the step's assignee"
    )
    print("  [M3] external dispatch pause emails the assignee: OK")


def test_external_only_run_is_provider_free_without_glossary():
    """RT-2 (live roundtrip): the segmented orchestrator's glossary prepass called
    glossary.match_terms, which resolved a provider (`provider = provider or get_provider()`)
    BEFORE checking whether any glossary entries existed. On prod (default provider has no
    key) an EXTERNAL-only run — which needs no model at all — therefore hard-failed the
    instant the prepass ran. With zero glossary entries the no-terms path must be
    provider-free end to end: patch the glossary seam's get_provider to RAISE and prove an
    external-only run still reaches the dispatch. (Only the glossary seam is patched, so a
    legitimate model call elsewhere would be unaffected — this pins the prepass specifically.)"""
    from app.services import glossary as _gloss

    _CAPTURED.clear()
    env = _seed()  # no glossary entries are ever seeded
    # NO translate step: an external-only workflow genuinely needs no provider.
    _external_step(env["wf_id"], env["vendor_id"], external_wait_hours=72)

    def _boom(*a, **k):
        raise RuntimeError(
            "get_provider() called on a keyless install (prod): the "
            "no-terms glossary path must never resolve a provider"
        )

    orig = _gloss.get_provider
    set_pieces("app.services.glossary", "get_provider", _boom)
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        events = _run(env["wf_id"], env["doc_id"])
    finally:
        vendor_dispatch.set_test_transport(None)
        set_pieces("app.services.glossary", "get_provider", orig)

    assert not [e for e in events if e.get("type") == "error"], (
        "the external-only run must not fail on a provider it never needs",
        [e for e in events if e.get("type") == "error"],
    )
    assert [e for e in events if e.get("type") == "paused"], (
        "external-only run must reach the dispatch pause",
        [e.get("type") for e in events],
    )
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        assert run.status == RunStatus.PAUSED, "the leg parks PAUSED"
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED
    assert len(_CAPTURED) == 1, "exactly one outbound dispatch reached the wire"
    print("  [RT2] external-only run with no glossary is provider-free -> reaches dispatch: OK")


if __name__ == "__main__":
    test_dispatch_end_to_end()
    test_ssrf_endpoint_refused_end_to_end()
    test_dispatch_direct_ssrf_raises()
    test_dispatch_ssrf_numeric_encoding_refused()
    test_expiry_escalates_to_human_review()
    test_future_and_unbounded_legs_not_escalated()
    test_full_text_loop_dispatches()
    test_h2_mock_egress_refused_before_egress()
    test_m2_http_endpoint_refused()
    test_m1_forged_config_headers_cannot_override()
    test_m7_non_2xx_taxonomy()
    test_h4_terminal_run_untouched_by_sweep()
    test_m3_dispatch_pause_notifies_assignee()
    test_external_only_run_is_provider_free_without_glossary()
    test_external_step_skipped_when_all_units_protected()
    print("verify_external_step: all OK")
