"""The GUARDED RETURN for the EXTERNAL / vendor dispatch-and-wait step
(PROPOSAL-external-processing-step Wave B). Hermetic + offline: every dispatch and every
poll rides an injected httpx.MockTransport (vendor_dispatch.set_test_transport), so no
byte reaches a real network, and the whole suite runs on a throwaway data dir.

This suite attacks the return path rather than demonstrating it:

  [A] the happy manual re-upload: ids accounted, content admitted ONLY through
      quality.commit_mt_output, leg RETURNED + reconciled, run resumed exactly once
  [B] PARTIAL returns: a valid subset admits + leaves the leg parked; the completing
      return closes it and resumes -- once
  [C] CONTRACT ID ACCOUNTING as the first gate: unknown id / duplicate-in-payload /
      duplicate-of-already-admitted / wrong locale each fail the WHOLE return with
      NOTHING written
  [D] SIGNATURES: good verifies; missing / wrong / WRONG-LENGTH / non-hex rejected; and
      the ordering proof -- a malformed body with a bad signature fails as a SIGNATURE
      error, never a parse error
  [E] CROSS-TENANT: customer A's vendor+secret cannot admit into customer B's run, and a
      claim token cannot name a foreign customer's segment
  [F] the K1 laws: a source-echo return is a NO-OP that leaves the prior target intact,
      and refusal text is quarantined rather than stored
  [G] LATE returns: cancelled / escalated / terminal-run legs drop a return LOUDLY
  [H] POLLING: nothing-ready, ready-and-admitted, SSRF-refused poll_url, opt-in gating,
      throttling, and the scheduler hook sweeping ONLY opted-in vendors
  [I] cost reconciliation incl. NaN/negative refusal; secret discipline on every surface
  [J] CLI parity for all three transports' operator half, ASCII-safe

NOTE ON PRE-FIX PROOFS: every test here covers NEW behaviour (Wave B did not exist at
Wave A's merge commit 9397f80), so none of them is a fail-before proof of a pre-existing
defect and none pins a commit sha.
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

init_env("loctms_vendorret_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    GlossaryEntry,
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
from app.services import orchestrator, vendor_dispatch, vendor_return, vendors
from app.services import workflows as wf
from app.services.connectors.base import ConnectorError

init_db()

# F-MED-1: the poll path now RESOLVES a hostname and validates every address before egress.
# Map every test hostname to a public IP so the hermetic suite never touches real DNS.
# hasattr-guarded so this SAME file also runs against the pre-fix tip (89fce14), where the
# seam does not exist yet -- that is the fail-before proof for the new F-* tests below.
if hasattr(vendor_return, "set_test_resolver"):
    vendor_return.set_test_resolver(lambda host: ["93.184.216.34"])

_SEQ = [0]
SIGNING = "topsecret-signing"


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _mock_transport(status=200, body=None, headers=None, capture=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(
                {
                    "url": str(req.url),
                    "method": req.method,
                    "headers": dict(req.headers),
                    "body": req.content,
                }
            )
        if body is None:
            return httpx.Response(status, json={"ok": True})
        return httpx.Response(status, content=body, headers=headers or {})

    return httpx.MockTransport(handler)


# --- seeding -----------------------------------------------------------------
SOURCES = ["Hello ⟦x1⟧ world.", "The cat sat on the mat.", "Please sign in now."]
GOOD = ["Hola ⟦x1⟧ mundo.", "El gato se sento en la alfombra.", "Por favor inicie sesion ahora."]


def _seed(
    *,
    endpoint="https://vendor.example.com/intake",
    mode=TransMode.SEGMENTED,
    poll_url=None,
    signing=SIGNING,
):
    """A customer + project + doc + a vendor linked to that customer + a workflow."""
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
        cfg = {"poll_url": poll_url} if poll_url else None
        v = Vendor(
            name=f"V-{_seq()}",
            endpoint_url=endpoint,
            config=cfg,
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": signing})
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


def _dispatch(env, locale="es-ES"):
    """Run the workflow to its external step; return (run_id, leg_id, claim_token)."""
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], locale))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED
        assert run.status == RunStatus.PAUSED
        return run.id, leg.id, leg.claim_token


def _seg_ids(doc_id, locale="es-ES"):
    with Session(engine) as s:
        return [sg.id for sg in orchestrator._load_segments(s, doc_id, locale)]


def _targets(doc_id, locale="es-ES"):
    with Session(engine) as s:
        return [
            (sg.target_text or "", sg.status)
            for sg in orchestrator._load_segments(s, doc_id, locale)
        ]


def _payload(ids, targets, *, claim=None, locale="es-ES", cost=None):
    body = {
        "contract_version": "1.0",
        "target_locale": locale,
        "segments": [{"id": i, "target": t} for i, t in zip(ids, targets, strict=False)],
    }
    if claim is not None:
        body["claim_token"] = claim
    if cost is not None:
        body["actual_cost_usd"] = cost
    return body


def _sign(raw: bytes, secret=SIGNING) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


# ============================================================================
# [A] the happy manual re-upload
# ============================================================================
def test_manual_return_admits_reconciles_and_resumes():
    env = _seed()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        # The dispatch froze the contract id set (Wave B ledger) -- this is what the
        # accounting is measured against and it is NOT re-derivable later.
        assert vendor_return.dispatched_ids(leg) == [str(i) for i in ids]
        assert vendor_return.returned_ids(leg) == []
        out = vendor_return.admit_manual(
            s, _payload(ids, GOOD, claim=claim, cost=123.45), resume="sync", actor="tester"
        )
    assert out["complete"] is True and out["resumed"] is True
    assert len(out["admitted"]) == 3, out
    for (txt, _st), want in zip(_targets(env["doc_id"]), GOOD, strict=False):
        assert txt == want, ("the vendor's text must land in target_text", txt, want)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
        assert leg.returned_at is not None
        assert leg.actual_cost_usd == 123.45, "estimate -> actual reconciliation"
        assert vendor_return.outstanding_ids(leg) == []
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.COMPLETED, ("the leg must resume the run", run.status)
    print("  [A] manual return: admitted, reconciled, leg RETURNED, run resumed: OK")


def test_full_text_leg_returns_through_the_same_pipeline():
    """The FULL_TEXT whole-body unit ("full:0", Wave A's string id) returns identically."""
    env = _seed(mode=TransMode.FULL_TEXT)
    _run_id, leg_id, claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert vendor_return.dispatched_ids(leg) == ["full:0"]
        out = vendor_return.admit_manual(
            s, _payload(["full:0"], ["Hola mundo desde el proveedor."], claim=claim), resume="sync"
        )
    assert out["complete"] and len(out["admitted"]) == 1
    assert _targets(env["doc_id"])[0][0] == "Hola mundo desde el proveedor."
    print("  [A] FULL_TEXT 'full:0' unit returns through the same pipeline: OK")


# ============================================================================
# [B] partial returns
# ============================================================================
def test_partial_then_completing_return_resumes_exactly_once():
    env = _seed()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    resumes = []
    real_resume = orchestrator.resume_workflow

    def _counting_resume(session, rid, locale, seed):
        resumes.append(rid)
        return real_resume(session, rid, locale, seed)

    set_pieces("app.services.orchestrator", "resume_workflow", _counting_resume)
    try:
        with Session(engine) as s:
            first = vendor_return.admit_manual(
                s, _payload(ids[:2], GOOD[:2], claim=claim), resume="sync"
            )
        assert first["complete"] is False and first["resumed"] is False
        assert len(first["outstanding"]) == 1
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            assert leg.status == VendorDispatchStatus.DISPATCHED, "partial keeps it open"
            assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
        assert resumes == [], "a PARTIAL return must never resume the run"
        # The subset it DID cover is admitted.
        assert _targets(env["doc_id"])[0][0] == GOOD[0]
        with Session(engine) as s:
            second = vendor_return.admit_manual(
                s, _payload(ids[2:], GOOD[2:], claim=claim), resume="sync"
            )
        assert second["complete"] is True and second["resumed"] is True
        assert resumes == [run_id], ("the leg resumes EXACTLY once", resumes)
        with Session(engine) as s:
            assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
    finally:
        set_pieces("app.services.orchestrator", "resume_workflow", real_resume)
    print(
        "  [B] partial admits its subset + keeps the leg parked; completing return "
        "resumes exactly once: OK"
    )


# ============================================================================
# [C] contract id accounting -- the FIRST gate
# ============================================================================
def _refused(fn, exc=vendor_return.ReturnContractError):
    try:
        fn()
    except exc as e:
        return str(e)
    raise AssertionError(f"expected {exc.__name__}, nothing was raised")


def test_unknown_duplicate_and_wrong_locale_fail_the_whole_return():
    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    before = _targets(env["doc_id"])

    # (1) an id we never dispatched -- even alongside two perfectly good ones.
    with Session(engine) as s:
        _refused(
            lambda: vendor_return.admit_manual(
                s,
                _payload(ids[:2] + [999999], GOOD, claim=claim),  # noqa: RUF005
                resume="none",  # noqa: RUF005, RUF100
            )
        )
    assert _targets(env["doc_id"]) == before, "an unknown id must admit NOTHING at all"

    # (2) the same id twice WITHIN one payload.
    with Session(engine) as s:
        _refused(
            lambda: vendor_return.admit_manual(
                s, _payload([ids[0], ids[0]], GOOD[:2], claim=claim), resume="none"
            )
        )
    assert _targets(env["doc_id"]) == before

    # (3) the wrong target locale.
    with Session(engine) as s:
        _refused(
            lambda: vendor_return.admit_manual(
                s, _payload(ids, GOOD, claim=claim, locale="fr-FR"), resume="none"
            )
        )
    assert _targets(env["doc_id"]) == before

    # (4) a per-segment locale that disagrees with the leg.
    with Session(engine) as s:
        bad = _payload(ids, GOOD, claim=claim)
        bad["segments"][1]["locale"] = "de-DE"
        _refused(lambda: vendor_return.admit_manual(s, bad, resume="none"))
    assert _targets(env["doc_id"]) == before

    # (5) an empty / missing segments array.
    with Session(engine) as s:
        _refused(
            lambda: vendor_return.admit_manual(
                s, {"claim_token": claim, "segments": []}, resume="none"
            )
        )

    # ...and the leg is still open and still fully outstanding after all five.
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED
        assert len(vendor_return.outstanding_ids(leg)) == 3
    print(
        "  [C] unknown / duplicate / wrong-locale / empty each fail the WHOLE return, "
        "nothing written: OK"
    )


def test_duplicate_of_an_already_admitted_id_is_refused():
    """CUMULATIVE accounting: an id a PREVIOUS partial return already admitted may not
    come back a second time -- the ledger is what makes this visible at all."""
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        vendor_return.admit_manual(s, _payload(ids[:1], GOOD[:1], claim=claim), resume="none")
    after_first = _targets(env["doc_id"])
    with Session(engine) as s:
        msg = _refused(
            lambda: vendor_return.admit_manual(
                s,
                _payload(ids[:1], ["Un texto completamente distinto."], claim=claim),
                resume="none",
            )
        )
    assert "SECOND time" in msg, msg
    assert _targets(env["doc_id"]) == after_first, "the re-send must not overwrite"
    print("  [C] a re-send of an already-admitted id is refused (cumulative ledger): OK")


def test_wave_a_leg_without_a_ledger_is_refused():
    """A leg dispatched before the ledger column existed has no recorded contract, so its
    id accounting is UNDECIDABLE -- fail closed rather than guess an id set."""
    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        leg.contract_ids = None  # simulate a Wave-A row
        s.add(leg)
        s.commit()
        msg = _refused(
            lambda: vendor_return.admit_manual(s, _payload(ids, GOOD, claim=claim), resume="none")
        )
    assert "no recorded contract" in msg, msg
    print("  [C] a ledger-less (Wave A) leg refuses every return, fail-closed: OK")


# ============================================================================
# [D] signatures
# ============================================================================
def test_signature_verification_and_ordering():
    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    raw = json.dumps(_payload(ids, GOOD, claim=claim)).encode()
    hdr = {vendor_return.CLAIM_HEADER: claim}

    with Session(engine) as s:
        # good
        leg = vendor_return.authenticate_callback(
            s, {**hdr, vendor_return.SIGNATURE_HEADER: _sign(raw)}, raw
        )
        assert leg.id == leg_id
        # missing signature
        for bad in (
            "",
            "sha256=",
            "sha256=deadbeef",  # wrong LENGTH
            "sha256=" + "zz" * 32,  # non-hex
            _sign(raw, "the-wrong-secret"),  # wrong secret
            _sign(b"a different body"),
        ):  # right secret, wrong body
            try:
                vendor_return.authenticate_callback(
                    s, {**hdr, vendor_return.SIGNATURE_HEADER: bad}, raw
                )
                raise AssertionError(f"signature {bad!r} must be rejected")
            except vendor_return.ReturnAuthError as exc:
                assert str(exc) == vendor_return.GENERIC_AUTH_MESSAGE, (
                    "an auth failure must never say WHICH check failed"
                )
        # an unknown claim token is the SAME generic refusal (no enumeration oracle)
        try:
            vendor_return.authenticate_callback(
                s,
                {
                    vendor_return.CLAIM_HEADER: "no-such-token",
                    vendor_return.SIGNATURE_HEADER: _sign(raw),
                },
                raw,
            )
            raise AssertionError("an unknown claim token must be rejected")
        except vendor_return.ReturnAuthError as exc:
            assert str(exc) == vendor_return.GENERIC_AUTH_MESSAGE

        # THE ORDERING PROOF: a body that is not even JSON, with a BAD signature, must
        # fail as a SIGNATURE error -- if the parser ran first this would be a contract
        # error, which would mean an unauthenticated caller can reach our JSON parser.
        garbage = b"\xff\xfe not json at all {{{"
        try:
            vendor_return.authenticate_callback(
                s, {**hdr, vendor_return.SIGNATURE_HEADER: "sha256=" + "ab" * 32}, garbage
            )
            raise AssertionError("garbage body with a bad signature must be rejected")
        except vendor_return.ReturnContractError as e:
            raise AssertionError("the body was PARSED before the signature was checked") from e
        except vendor_return.ReturnAuthError:
            pass
        # ...and with a VALID signature the same garbage is a clean contract error.
        leg = vendor_return.authenticate_callback(
            s, {**hdr, vendor_return.SIGNATURE_HEADER: _sign(garbage)}, garbage
        )
        _refused(lambda: vendor_return.admit_return(s, leg, garbage, resume="none"))
    print(
        "  [D] HMAC verified constant-time; bad/short/non-hex/wrong-secret all refused "
        "with ONE generic message; signature precedes parse: OK"
    )


def test_non_ascii_claim_tokens_refuse_cleanly_instead_of_crashing():
    """`hmac.compare_digest` raises TypeError on a non-ASCII str, and BOTH sides of every
    claim comparison are attacker-supplied (a header, or a `claim_token` inside a posted
    body). A 500 there would be a free liveness probe on an unauthenticated route, so the
    comparison runs on UTF-8 BYTES and every exotic token is an ordinary clean refusal."""
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    raw = b"{}"
    with Session(engine) as s:
        for weird in ("ééé", "\U0001f600token", "中文"):
            assert vendor_return.leg_for_claim(s, weird) is None
            try:
                vendor_return.authenticate_callback(
                    s,
                    {vendor_return.CLAIM_HEADER: weird, vendor_return.SIGNATURE_HEADER: _sign(raw)},
                    raw,
                )
                raise AssertionError("a non-ASCII claim token must be refused")
            except vendor_return.ReturnAuthError:
                pass
            # ...and the same value INSIDE a body, against a real leg (the manual path).
            body = _payload(ids, GOOD, claim=weird)
            try:
                vendor_return.admit_manual(s, body, claim_token=claim, resume="none")
                raise AssertionError("a mismatched body claim token must be refused")
            except vendor_return.ReturnContractError:
                pass
    print("  [D] non-ASCII claim tokens refuse cleanly (no compare_digest TypeError): OK")


def test_vendor_without_a_signing_secret_cannot_be_called_back():
    """No secret is NOT 'no signature required' -- it is an auth failure."""
    env = _seed(signing="")
    _run_id, _leg_id, claim = _dispatch(env)
    raw = b'{"segments":[]}'
    with Session(engine) as s:
        try:
            vendor_return.authenticate_callback(
                s,
                {vendor_return.CLAIM_HEADER: claim, vendor_return.SIGNATURE_HEADER: _sign(raw, "")},
                raw,
            )
            raise AssertionError("a secretless vendor must not authenticate")
        except vendor_return.ReturnAuthError:
            pass
    print("  [D] a vendor with no signing secret cannot authenticate a callback: OK")


# ============================================================================
# [E] cross-tenant isolation
# ============================================================================
def test_cross_tenant_claim_token_cannot_admit():
    """Customer A's vendor, signing correctly with ITS OWN secret, cannot use customer
    B's claim token -- the signature is verified against the key of the leg the TOKEN
    names, so a token it does not hold is a key it does not hold."""
    a = _seed(signing="secret-of-A")
    b = _seed(signing="secret-of-B")
    _, _, claim_a = _dispatch(a)
    _run_b, _leg_b, claim_b = _dispatch(b)
    ids_b = _seg_ids(b["doc_id"])
    before_b = _targets(b["doc_id"])
    raw = json.dumps(_payload(ids_b, GOOD, claim=claim_b)).encode()
    with Session(engine) as s:
        # A signs B's payload with A's secret and presents B's token.
        try:
            vendor_return.authenticate_callback(
                s,
                {
                    vendor_return.CLAIM_HEADER: claim_b,
                    vendor_return.SIGNATURE_HEADER: _sign(raw, "secret-of-A"),
                },
                raw,
            )
            raise AssertionError("A's secret must not authenticate B's leg")
        except vendor_return.ReturnAuthError:
            pass
    assert _targets(b["doc_id"]) == before_b

    # And even holding its OWN valid token, A cannot name B's segment ids: they are not
    # in A's contract, so the accounting refuses the whole return.
    ids_a_leg_claims_b_segments = _payload(ids_b, GOOD, claim=claim_a)
    with Session(engine) as s:
        _refused(lambda: vendor_return.admit_manual(s, ids_a_leg_claims_b_segments, resume="none"))
    assert _targets(b["doc_id"]) == before_b, "no cross-tenant write"
    print(
        "  [E] a claim token cannot cross a tenant: wrong key AND foreign ids both "
        "refused, nothing written: OK"
    )


def test_org_scoped_manual_return_cannot_reach_a_foreign_org_leg():
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        # The leg's org_id is None (single-tenant seed); an --org 42 operator must not
        # see it, and the refusal must be indistinguishable from "no such token".
        try:
            vendor_return.admit_manual(
                s, _payload(ids, GOOD, claim=claim), org_id=42, resume="none"
            )
            raise AssertionError("a foreign-org leg must not be reachable")
        except vendor_return.ReturnAuthError as exc:
            assert "no open dispatch" in str(exc)
    print("  [E] an org-scoped manual return cannot reach a foreign-org leg: OK")


def test_return_scoping_is_buyer_org_not_vendor_org():
    """CROSS-ORG LINKING (2026-08-08): the RETURN is scoped by the leg's BUYER org (the
    run/customer side), NEVER the vendor's org. With a leg stamped buyer-org B whose vendor
    lives in org A, a caller scoped to the VENDOR's org A cannot reach the leg (refused
    indistinguishably from 'no such token'); a caller scoped to the BUYER's org B admits
    it. Pins that vendor_return does not re-assert vendor-org == leg-org (which cross-org
    linking would break)."""
    from app.models import Organization

    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        # Real org rows (org_id is a FK to organization.id): vendor's org A, buyer's org B.
        oa, ob = Organization(name="ret-vendor-org"), Organization(name="ret-buyer-org")
        s.add(oa)
        s.add(ob)
        s.commit()
        ORG_A, ORG_B = oa.id, ob.id
        leg = s.get(VendorDispatch, leg_id)
        leg.org_id = ORG_B  # stamp the BUYER side (run/customer org)
        s.add(leg)
        v = s.get(Vendor, leg.vendor_id)
        v.org_id = ORG_A  # the vendor lives in a DIFFERENT org
        s.add(v)
        s.commit()
    # A caller scoped to the VENDOR's org A must NOT reach a buyer-B leg (buyer-ring scope).
    with Session(engine) as s:
        try:
            vendor_return.admit_manual(
                s, _payload(ids, GOOD, claim=claim), org_id=ORG_A, resume="none"
            )
            raise AssertionError("a vendor-org caller must not reach a buyer-org leg")
        except vendor_return.ReturnAuthError as exc:
            assert "no open dispatch" in str(exc)
    # A caller scoped to the BUYER's org B admits it (return authority = the buyer org).
    with Session(engine) as s:
        summary = vendor_return.admit_manual(
            s, _payload(ids, GOOD, claim=claim), org_id=ORG_B, resume="none"
        )
        assert summary, "the buyer-org caller must admit the return"
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
    print("  [E] return scoping is BUYER-org, not vendor-org (no cross-org re-assertion): OK")


def test_document_rehomed_to_another_customer_drops_the_return():
    """A claim token must not keep admitting after its document moved tenants."""
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    before = _targets(env["doc_id"])
    with Session(engine) as s:
        other = Customer(name=f"Other-{_seq()}")
        s.add(other)
        s.commit()
        s.refresh(other)
        proj = s.get(Project, env["project_id"])
        proj.customer_id = other.id
        s.add(proj)
        s.commit()
        try:
            vendor_return.admit_manual(s, _payload(ids, GOOD, claim=claim), resume="none")
            raise AssertionError("a re-homed document must drop the return")
        except vendor_return.ReturnStateError as exc:
            assert "tenant boundary" in str(exc), exc
    assert _targets(env["doc_id"]) == before
    print("  [E] a document re-homed to another customer drops the vendor return: OK")


# ============================================================================
# [F] the K1 laws -- source echo + refusal
# ============================================================================
def test_source_echo_is_a_no_op_that_never_overwrites():
    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # Give segment 0 a real prior translation the echo must not destroy.
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        seg.target_text = "Hola ⟦x1⟧ mundo (revisado)."
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s,
            # seg 0: the SOURCE echoed back.  seg 1: blank.  seg 2: a real translation.
            _payload(ids, [SOURCES[0], "", GOOD[2]], claim=claim),
            resume="none",
        )
    assert set(out["no_op"]) == {str(ids[0]), str(ids[1])}, out
    assert out["admitted"] == [str(ids[2])]
    rows = _targets(env["doc_id"])
    assert rows[0][0] == "Hola ⟦x1⟧ mundo (revisado).", (
        "a source echo OVERWROTE a good stored translation (the K1 law)",
        rows[0],
    )
    assert rows[0][1] == SegStatus.NEEDS_REVIEW, "the echo must flag for a human"
    assert rows[1][1] == SegStatus.NEEDS_REVIEW, "a blank return is 'not translated'"
    # ...and the leg still COMPLETES: those ids came back, they just did not land.
    with Session(engine) as s:
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
    print(
        "  [F] source-echo / blank returns are NO-OPS: prior target intact, "
        "needs_review, leg still accounted: OK"
    )


def test_source_echo_is_allowed_for_an_english_target():
    """en-US -> en-GB legitimately leaves strings byte-identical; the law is scoped to
    non-English targets on purpose."""
    assert vendor_return.is_source_echo("Colour", "Colour", "en-GB") is False
    assert vendor_return.is_source_echo("Colour", "Colour", "es-ES") is True
    assert vendor_return.is_source_echo("Colour", "  Colour ", "es-ES") is True
    assert vendor_return.is_source_echo("Colour", "Color", "es-ES") is False
    print("  [F] the source-echo rule is scoped to non-English targets: OK")


def test_refusal_text_is_quarantined_not_stored():
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        seg.target_text = "Hola ⟦x1⟧ mundo."
        s.add(seg)
        s.commit()
    hedge = (
        "I'm sorry, but I can't help with that request. Let me know if there is "
        "anything else I can do for you."
    )
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids, [hedge, GOOD[1], GOOD[2]], claim=claim), resume="none"
        )
    assert str(ids[0]) in out["needs_review"], out
    assert str(ids[0]) not in out["admitted"]
    rows = _targets(env["doc_id"])
    assert rows[0][0] == "Hola ⟦x1⟧ mundo.", ("refusal text was STORED", rows[0])
    assert rows[0][1] == SegStatus.NEEDS_REVIEW
    print("  [F] a refusal-shaped return is quarantined, never stored: OK")


def test_tag_damage_routes_to_review():
    """The inline-tag integrity lane is ON for a vendor return exactly as for MT: a
    target that drops the source's ⟦x1⟧ cannot come back clean."""
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s,
            _payload(ids, ["Hola mundo sin la etiqueta.", GOOD[1], GOOD[2]], claim=claim),
            resume="none",
        )
    assert str(ids[0]) in out["needs_review"], out
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        assert seg.status == SegStatus.NEEDS_REVIEW
        assert any(
            isinstance(f, dict) and f.get("type") == "tag_integrity" for f in (seg.qa_flags or [])
        ), seg.qa_flags
    print("  [F] a return that damages the inline tags routes to review: OK")


# ============================================================================
# [G] late returns
# ============================================================================
def test_late_return_after_cancel_escalate_or_terminal_run_is_dropped():
    ids_seen = []
    for closing in ("cancel", "escalate", "terminal", "second-return"):
        env = _seed()
        run_id, leg_id, claim = _dispatch(env)
        ids = _seg_ids(env["doc_id"])
        before = _targets(env["doc_id"])
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            run = s.get(WorkflowRun, run_id)
            if closing == "cancel":
                leg.status = VendorDispatchStatus.CANCELLED
                s.add(leg)
            elif closing == "escalate":
                leg.expires_at = datetime.utcnow() - timedelta(hours=1)
                s.add(leg)
                s.commit()
                assert vendor_dispatch.sweep_expired(s) == 1
            elif closing == "terminal":
                run.status = RunStatus.COMPLETED
                run.finished_at = datetime.utcnow()
                s.add(run)
            s.commit()
        if closing == "second-return":
            with Session(engine) as s:
                vendor_return.admit_manual(s, _payload(ids, GOOD, claim=claim), resume="none")
            before = _targets(env["doc_id"])
        with Session(engine) as s:
            try:
                vendor_return.admit_manual(
                    s, _payload(ids, ["INTRUSO " + g for g in GOOD], claim=claim), resume="none"
                )
                raise AssertionError(f"a late return after {closing} must be DROPPED")
            except vendor_return.ReturnStateError as exc:
                assert "DROPPED" in str(exc), (closing, str(exc))
                ids_seen.append(closing)
        assert _targets(env["doc_id"]) == before, (
            f"a late return after {closing} changed stored content"
        )
    assert len(ids_seen) == 4
    print(
        "  [G] late returns after cancel / expiry-escalation / terminal run / a first "
        "return are all DROPPED loudly, nothing written: OK"
    )


def test_abort_closes_the_leg_so_its_token_stops_admitting():
    """close_open_legs_for_run (Wave A's finalizer hook) is what kills a claim token's
    admitting power when a run finishes -- prove the return path honours it."""
    env = _seed()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.FAILED
        run.finished_at = datetime.utcnow()
        s.add(run)
        s.commit()
        assert vendor_dispatch.close_open_legs_for_run(s, run_id) == 1
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.CANCELLED
        try:
            vendor_return.admit_manual(s, _payload(ids, GOOD, claim=claim), resume="none")
            raise AssertionError("a closed leg must not admit")
        except vendor_return.ReturnStateError:
            pass
    print("  [G] aborting a run closes the leg and kills the token's admitting power: OK")


# ============================================================================
# [H] polling
# ============================================================================
POLL_URL = "https://vendor.example.com/returns"


def test_poll_nothing_ready_then_ready():
    env = _seed(poll_url=POLL_URL)
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    cap = []
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        # (1) 204 = nothing ready -> None, nothing written.
        assert (
            vendor_return.poll_leg(s, leg, transport=_mock_transport(204, body=b"", capture=cap))
            is None
        )
        assert cap and cap[0]["method"] == "GET"
        assert cap[0]["headers"].get(vendor_return.CLAIM_HEADER) == claim
        assert str(cap[0]["url"]).startswith(POLL_URL)
        # (2) a signed 200 carrying the contract -> admitted.
        raw = json.dumps(_payload(ids, GOOD, claim=claim, cost=42.0)).encode()
        res = vendor_return.poll_leg(
            s,
            leg,
            resume="sync",
            transport=_mock_transport(
                200, body=raw, headers={vendor_return.SIGNATURE_HEADER: _sign(raw)}
            ),
        )
        assert res is not None and res["complete"] is True
    for (txt, _st), want in zip(_targets(env["doc_id"]), GOOD, strict=False):
        assert txt == want
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED and leg.actual_cost_usd == 42.0
    print("  [H] poll: 204 is 'nothing ready'; a signed 200 admits + reconciles: OK")


def test_poll_rejects_a_bad_response_signature():
    env = _seed(poll_url=POLL_URL)
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    before = _targets(env["doc_id"])
    raw = json.dumps(_payload(ids, GOOD, claim=claim)).encode()
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_return.poll_leg(
                s,
                leg,
                transport=_mock_transport(
                    200, body=raw, headers={vendor_return.SIGNATURE_HEADER: _sign(raw, "wrong")}
                ),
            )
            raise AssertionError("a signed-but-wrong poll response must be discarded")
        except vendor_return.ReturnAuthError:
            pass
    assert _targets(env["doc_id"]) == before
    print("  [H] a poll response the vendor signed WRONG is discarded, not admitted: OK")


def test_poll_url_is_ssrf_guarded_and_opt_in():
    # (a) no poll_url configured -> polling is OFF for that vendor, loudly.
    env = _seed()
    _run_id, leg_id, _ = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_return.poll_leg(s, leg, transport=_mock_transport())
            raise AssertionError("a vendor with no poll_url must not be polled")
        except vendor_return.VendorPollError as exc:
            assert "poll_url" in str(exc)
        # ...and it is invisible to the automatic sweep.
        assert vendor_return.pollable_legs(s, vendor_id=env["vendor_id"]) == []
        assert vendor_return.poll_due_legs(s, vendor_id=env["vendor_id"])["polled"] == 0
    # (b) a hostile poll_url is refused by the SHARED SSRF guard, before any request.
    env2 = _seed(poll_url="http://169.254.169.254/latest/meta-data")
    _, leg2, _ = _dispatch(env2)
    cap = []
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg2)
        try:
            vendor_return.poll_leg(s, leg, transport=_mock_transport(capture=cap))
            raise AssertionError("a metadata-IP poll_url must be refused")
        except ConnectorError:
            pass
    assert not cap, "nothing left the process for a blocked poll target"
    # (c) http:// is refused too (the dispatch-side https allowlist applies both ways).
    env3 = _seed(poll_url="http://public.example.com/returns")
    _, leg3, _ = _dispatch(env3)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg3)
        try:
            vendor_return.poll_leg(s, leg, transport=_mock_transport())
            raise AssertionError("an http:// poll_url must be refused")
        except ConnectorError:
            pass
    print(
        "  [H] polling is per-vendor opt-in; a metadata-IP / http poll_url is refused "
        "before any egress: OK"
    )


def test_poll_sweep_is_throttled_and_survives_a_bad_leg():
    env = _seed(poll_url=POLL_URL)
    _run_id, leg_id, _claim = _dispatch(env)
    vid = env["vendor_id"]
    with Session(engine) as s:
        assert [l.id for l in vendor_return.pollable_legs(s, vendor_id=vid)] == [leg_id]  # noqa: E741
        out = vendor_return.poll_due_legs(
            s, vendor_id=vid, transport=_mock_transport(204, body=b"")
        )
        assert out["polled"] == 1 and out["admitted"] == 0
        # Immediately after, the per-leg throttle window suppresses it.
        assert vendor_return.poll_due_legs(s, vendor_id=vid)["polled"] == 0
        assert (
            vendor_return.poll_due_legs(
                s, vendor_id=vid, throttle=False, transport=_mock_transport(500)
            )["failed"]
            == 1
        )
    print("  [H] the poll sweep throttles per leg and one failing vendor never aborts it: OK")


def test_scheduler_tick_polls_only_opted_in_vendors():
    """The scheduler hook is wired (a configured leg IS swept) but INERT for a vendor
    that declared no poll_url -- so an install that configured no polling never makes an
    outbound request on the scheduler's initiative."""
    from app.services import scheduler

    plain = _seed()  # no poll_url
    _dispatch(plain)
    opted = _seed(poll_url=POLL_URL)
    _, opted_leg, opted_claim = _dispatch(opted)
    with Session(engine) as s:
        assert vendor_return.pollable_legs(s, vendor_id=plain["vendor_id"]) == []
        assert [l.id for l in vendor_return.pollable_legs(s, vendor_id=opted["vendor_id"])] == [  # noqa: E741
            opted_leg
        ]
    ids = _seg_ids(opted["doc_id"])
    raw = json.dumps(_payload(ids, GOOD, claim=opted_claim)).encode()
    # Every leg the tick can see rides the injected transport, so the sweep is hermetic;
    # the plain vendor's leg is never requested at all (it has no poll_url).
    vendor_dispatch.set_test_transport(
        _mock_transport(200, body=raw, headers={vendor_return.SIGNATURE_HEADER: _sign(raw)})
    )
    try:
        scheduler.tick()
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        assert s.get(VendorDispatch, opted_leg).status == VendorDispatchStatus.RETURNED, (
            "the scheduler tick must actually sweep an opted-in leg"
        )
    for (txt, _st), want in zip(_targets(opted["doc_id"]), GOOD, strict=False):
        assert txt == want
    print("  [H] scheduler.tick() sweeps an opted-in leg and ignores a vendor with no poll_url: OK")


# ============================================================================
# [I] cost reconciliation + secret discipline
# ============================================================================
def test_bad_actual_cost_is_refused_before_anything_is_written():
    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    before = _targets(env["doc_id"])
    for bad in (float("nan"), float("inf"), -5.0, "not-a-number"):
        with Session(engine) as s:
            body = _payload(ids, GOOD, claim=claim)
            body["actual_cost_usd"] = bad
            _refused(lambda body=body: vendor_return.admit_manual(s, body, resume="none"))
        assert _targets(env["doc_id"]) == before, f"cost {bad!r} admitted content"
    with Session(engine) as s:
        assert s.get(VendorDispatch, leg_id).actual_cost_usd is None
    print(
        "  [I] a NaN / infinite / negative / non-numeric actual cost is refused and "
        "nothing is written: OK"
    )


def test_no_surface_echoes_the_claim_token_or_the_secret():
    env = _seed(poll_url=POLL_URL)
    _run_id, leg_id, claim = _dispatch(env)
    with Session(engine) as s:
        blob = json.dumps(vendor_return.leg_to_dict(s, s.get(VendorDispatch, leg_id)))
    assert claim not in blob, "the claim token leaked into a read surface"
    assert SIGNING not in blob, "the signing secret leaked into a read surface"
    assert "vendor.example.com" in blob, "the host IS shown (it is not a secret)"
    print("  [I] no read surface echoes the claim token or the signing secret: OK")


def test_provenance_tag_is_vendor():
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    hedge = "I'm sorry, but I can't assist with that."
    with Session(engine) as s:
        vendor_return.admit_manual(
            s, _payload(ids, [hedge, GOOD[1], GOOD[2]], claim=claim), resume="none"
        )
        seg = s.get(Segment, ids[0])
        flags = [f for f in (seg.qa_flags or []) if isinstance(f, dict)]
        assert any(f.get("source") == vendor_return.PROVENANCE_VENDOR for f in flags), (
            "a quarantined vendor return must be attributed to the vendor path",
            flags,
        )
    print("  [I] a quarantined return is attributed with the 'vendor' provenance: OK")


# ============================================================================
# [J] CLI parity — the same three transports, headless
# ============================================================================
def test_cli_dispatches_return_and_poll():
    """Web + CLI parity (standing law): the operator half of all three transports exists
    headless and calls the SAME service seam."""
    import json as _json

    from tests._cli_runner import run_cli

    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])

    code, out, err = run_cli(
        ["--json", "vendor", "dispatches", "--vendor-id", str(env["vendor_id"])]
    )
    assert code == 0, (code, err)
    rows = _json.loads(out.strip().splitlines()[-1])["dispatches"]
    assert [d["id"] for d in rows] == [leg_id], rows
    assert rows[0]["outstanding_ids"] == 3
    assert claim not in out and SIGNING not in out, "the CLI echoed a credential"

    # `vendor return` from a file (the manual transport), resuming synchronously.
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="loctms_ret_"), "return.json")
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump(_payload(ids, GOOD, claim=claim, cost=9.5), fh)
    code, out, err = run_cli(
        ["--json", "vendor", "return", "--file", path, "--dispatch-id", str(leg_id)]
    )
    assert code == 0, (code, err)
    res = _json.loads(out.strip().splitlines()[-1])
    assert res["complete"] is True and len(res["admitted"]) == 3, res
    for (txt, _st), want in zip(_targets(env["doc_id"]), GOOD, strict=False):
        assert txt == want
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED and leg.actual_cost_usd == 9.5

    # A re-run of the same file is a clean usage error against a closed leg.
    code, out, err = run_cli(["vendor", "return", "--file", path, "--dispatch-id", str(leg_id)])
    assert code != 0 and "DROPPED" in (out + err), (code, out, err)

    # `vendor poll` refuses a vendor that never opted in.
    code, out, err = run_cli(["vendor", "poll", "--vendor-id", str(env["vendor_id"])])
    assert code == 0 and "Polled 0 leg(s)" in out, (code, out, err)
    # ASCII-safe output (Windows consoles).
    assert (out + err).isascii(), "CLI output must stay ASCII"
    print(
        "  [J] CLI: dispatches / return --file / poll all work headless, ASCII-safe, "
        "no credential echoed: OK"
    )


# ============================================================================
# [K] Wave B fix-round regressions (two-reviewer findings). Each FAILS on the pre-fix
# tip 89fce14 and PASSES after the fix, except the ones the findings themselves call
# SAFE both ways (tag-damage / hard-lock through resume) which guard against regressing.
# ============================================================================
def _seed_external_first(*, signing=SIGNING):
    """A workflow whose FIRST (and only) content step is the EXTERNAL vendor step, so the
    parked working blob is the RAW SOURCE -- the external->publish shape F-CRIT names, where
    an un-admitted slot's resume seed IS the source and a naive re-commit ships it as MT."""
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
            endpoint_url="https://vendor.example.com/intake",
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": signing})
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


def _seed_tm_match_external(*, tm_seed=None, segs=None, glossary=None, signing=SIGNING):
    """A [tm_match -> external] workflow whose project TM makes tm_match fill+LOCK segments
    IN-RUN (not a pre-persisted lock) before the external dispatch.

    `segs` = ordered list of (source, tm_target_or_None): a non-None tm_target seeds a 100%
    TM entry for that source (tm_match fills+locks it); None is a fresh, un-matched line.
    `tm_seed=(source, target)` is a one-hit shorthand -> segs [(source, target), ('A fresh
    line.', None)]. `glossary` = optional list of (source_term, target_term) so a fill can
    MISS a required term (R3: a flagged fill soft-locks, never hard)."""
    from app.services import tm

    if segs is None:
        src_text, tgt_text = tm_seed
        segs = [(src_text, tgt_text), ("A fresh line.", None)]
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
        for _src, _tgt in segs:
            if _tgt is not None:
                tm.write_back(s, p.id, _src, _tgt, "en-US", "es-ES")
        for _st, _tt in glossary or []:
            s.add(
                GlossaryEntry(
                    project_id=p.id,
                    source_locale="en-US",
                    target_locale="es-ES",
                    source_term=_st,
                    target_term=_tt,
                )
            )
        s.commit()
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, (src, _t) in enumerate(segs):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                    target_text="",
                )
            )
        s.commit()
        v = Vendor(
            name=f"V-{_seq()}",
            endpoint_url="https://vendor.example.com/intake",
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": signing})
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
        wf.add_step(s, w.id, name="TM match", step_type=StepType.TM_MATCH)
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


def _wait_run(run_id, status=RunStatus.COMPLETED, timeout=20.0):
    """Poll a run's status until it reaches `status` (or goes terminal), for the ASYNC
    resume path which runs on a background thread."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            r = s.get(WorkflowRun, run_id)
            if r is not None and r.status in (status, RunStatus.FAILED):
                return r.status
        time.sleep(0.1)
    with Session(engine) as s:
        return s.get(WorkflowRun, run_id).status


# --- F-CRIT: the completing resume must not re-commit un-admitted slots -------
def test_source_echo_no_op_survives_a_completing_SYNC_resume():
    """F-CRIT (K1 law #1). A source-echo / blank no-op must keep its PRIOR stored target
    and its needs_review verdict THROUGH the completing resume -- not have the parked SOURCE
    re-committed over it as clean MT by the resume write-back."""
    env = _seed_external_first()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    PRIOR = "Hola mundo (revisado a mano)."
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        seg.target_text = PRIOR
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids, [SOURCES[0], "", GOOD[2]], claim=claim), resume="sync"
        )
    assert out["complete"] and out["resumed"], out
    assert set(out["no_op"]) == {str(ids[0]), str(ids[1])}, out
    rows = _targets(env["doc_id"])
    assert rows[0][0] == PRIOR, (
        "F-CRIT: the completing RESUME re-committed SOURCE over a good stored target",
        rows[0],
    )
    assert rows[0][1] == SegStatus.NEEDS_REVIEW, (
        "F-CRIT: the resume wiped the no-op needs_review verdict to MT",
        rows[0],
    )
    assert rows[1][0] == "" and rows[1][1] == SegStatus.NEEDS_REVIEW, ("blank no-op", rows[1])
    assert rows[2][0] == GOOD[2], ("the admitted slot lands", rows[2])
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
    print("  [F-CRIT] a source-echo/blank no-op survives the completing SYNC resume: OK")


def test_source_echo_no_op_survives_a_completing_ASYNC_resume():
    """The same F-CRIT law over the ASYNC (web/webhook) resume, which runs the write-back
    on a background thread."""
    env = _seed_external_first()
    run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    PRIOR = "Hola mundo async (revisado)."
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        seg.target_text = PRIOR
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids, [SOURCES[0], GOOD[1], GOOD[2]], claim=claim), resume="async"
        )
    assert out["complete"], out
    assert _wait_run(run_id) == RunStatus.COMPLETED, "the async resume never completed"
    rows = _targets(env["doc_id"])
    assert rows[0][0] == PRIOR and rows[0][1] == SegStatus.NEEDS_REVIEW, (
        "F-CRIT (async): a source echo overwrote the prior target / wiped needs_review",
        rows[0],
    )
    assert rows[1][0] == GOOD[1] and rows[2][0] == GOOD[2]
    print("  [F-CRIT] a source-echo no-op survives the completing ASYNC resume: OK")


def test_refusal_quarantine_survives_a_completing_resume():
    """F-CRIT (K1 law #2). A withheld refusal must stay quarantined (prior target intact,
    needs_review) through the completing resume."""
    env = _seed_external_first()
    run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    PRIOR = "Hola tarjeta mundo (previo)."
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        seg.target_text = PRIOR
        s.add(seg)
        s.commit()
    hedge = (
        "I'm sorry, but I can't help with that request. Let me know if there is "
        "anything else I can do for you."
    )
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids, [hedge, GOOD[1], GOOD[2]], claim=claim), resume="sync"
        )
    assert str(ids[0]) in out["needs_review"] and str(ids[0]) not in out["admitted"], out
    rows = _targets(env["doc_id"])
    assert rows[0][0] == PRIOR, (
        "F-CRIT: the resume stored the refusal / source over the prior target",
        rows[0],
    )
    assert rows[0][1] == SegStatus.NEEDS_REVIEW
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    print("  [F-CRIT] a withheld refusal stays quarantined through the completing resume: OK")


def test_tag_damage_stays_flagged_through_a_completing_resume():
    """Admitted-but-flagged (the finding names these SAFE): the vendor's tag-broken text IS
    stored, routed to review, and STAYS needs_review through the resume (the tag guard
    re-trips on the re-commit) -- never laundered to clean MT."""
    env = _seed_external_first()
    run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    damaged = "Hola mundo sin la etiqueta."  # drops the source's inline tag
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids, [damaged, GOOD[1], GOOD[2]], claim=claim), resume="sync"
        )
    assert str(ids[0]) in out["needs_review"], out
    rows = _targets(env["doc_id"])
    assert rows[0][0] == damaged and rows[0][1] == SegStatus.NEEDS_REVIEW, rows[0]
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        assert any(
            isinstance(f, dict) and f.get("type") == "tag_integrity" for f in (seg.qa_flags or [])
        ), seg.qa_flags
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    print("  [F-CRIT] tag-damaged vendor text stays needs_review through the resume: OK")


def test_hard_locked_slot_survives_a_completing_resume():
    """Hard-lock analogue (the finding names these SAFE): commit_mt_output refuses a
    hard-locked slot on admission AND on the resume re-commit, so its locked TM value is
    never overwritten by the completing return's resume."""
    env = _seed_external_first()
    run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    LOCKED = "Hola ⟦x1⟧ mundo (TM bloqueado)."
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        seg.target_text = LOCKED
        seg.status = SegStatus.APPROVED
        seg.lock_band = "exact"
        seg.lock_hard = True
        s.add(seg)
        s.commit()
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s,
            _payload(ids, ["Otra traduccion del proveedor.", GOOD[1], GOOD[2]], claim=claim),
            resume="sync",
        )
    assert str(ids[0]) in out["lock_refused"], out
    rows = _targets(env["doc_id"])
    assert rows[0][0] == LOCKED, (
        "F-CRIT: a hard-locked TM value was overwritten by the resume",
        rows[0],
    )
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    print("  [F-CRIT] a hard-locked slot survives a vendor return + completing resume: OK")


def test_inrun_tm_hard_lock_survives_a_vendor_return():
    """P-A1 (HIGH, data integrity). The hard-lock-vs-vendor guarantee for an IN-RUN lock,
    not a pre-persisted one (test_hard_locked_slot_survives_a_completing_resume covers the
    already-persisted case -- the SAFE contrast the finding names). In a SINGLE
    `tm_match -> external` run, tm_match's 100%/HARD lock lived only in run-working-state, so
    at the external PAUSE the segment row was empty/unlocked and the vendor return overwrote
    it ("0 TM-locked"), bypassing "a hard lock outranks a vendor return". The fix persists the
    in-run hard lock (+ its TM fill) onto the row at the dispatch pause, so vendor_return's
    is_hard_locked seam refuses the overwrite -- exactly as it does for a lock a prior
    completed run left. A NON-locked sibling is still admitted, proving the guard is targeted,
    not a blanket refusal.

    FAIL-BEFORE (pre-fix): at the pause seg0.lock_band is None; the vendor overwrite lands
    (out["lock_refused"] == [], seg0.target_text == the vendor string)."""
    from app.services import settings

    settings.set_many({"tm_hundreds_enter_lqa": "0"})  # 100s -> HARD lock policy
    try:
        env = _seed_tm_match_external(tm_seed=("Delete all data.", "Eliminar todos los datos."))
        run_id, _leg_id, claim = _dispatch(env)
        ids = _seg_ids(env["doc_id"])
        # AT THE PAUSE: the in-run hard lock + its TM fill are now DURABLE on the row (fix).
        with Session(engine) as s:
            seg0 = s.get(Segment, ids[0])
            assert seg0.lock_band == "100" and seg0.lock_hard is True, (
                "P-A1: an in-run tm_match hard lock must be persisted before the run parks",
                seg0.lock_band,
                seg0.lock_hard,
            )
            assert seg0.target_text == "Eliminar todos los datos.", seg0.target_text
            assert seg0.tm_match_pct == 100 and seg0.mt_confidence is None, (
                "a TM lock keeps TM provenance, never fresh-MT confidence",
                seg0,
            )
        # The vendor returns an OVERWRITE for the hard-locked seg + a real translation for
        # the fresh, un-locked sibling.
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s,
                _payload(
                    ids,
                    ["Traduccion del proveedor (sobrescribe).", "[es-ES] A fresh line."],
                    claim=claim,
                ),
                resume="sync",
            )
        assert str(ids[0]) in out["lock_refused"], (
            "P-A1: the vendor overwrite of an in-run hard lock was NOT refused",
            out,
        )
        assert str(ids[1]) in out["admitted"], (
            "the non-locked sibling must still admit (the guard is targeted)",
            out,
        )
        rows = _targets(env["doc_id"])
        assert rows[0][0] == "Eliminar todos los datos.", (
            "P-A1: a vendor silently overwrote a 100%/HARD in-run TM lock",
            rows[0],
        )
        assert rows[0][1] == SegStatus.MT, ("the locked TM value keeps its clean status", rows[0])
        assert rows[1][0] == "[es-ES] A fresh line.", (
            "the un-locked sibling took the vendor text",
            rows[1],
        )
        with Session(engine) as s:
            seg0 = s.get(Segment, ids[0])
            assert seg0.lock_band == "100" and seg0.lock_hard is True, (
                "the hard lock must survive the vendor return + completing resume",
                seg0.lock_band,
                seg0.lock_hard,
            )
            assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    finally:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
    print("  [P-A1] an IN-RUN tm_match hard lock survives a vendor return (targeted): OK")


def test_inrun_flagged_tm_fill_is_not_hard_persisted_at_external_pause():
    """P-A1 savepoint isolation + R3. The pause-persist commits each hard-planned fill through
    the write seam inside a PER-SEGMENT savepoint and keeps it ONLY when it cleanly HARD-locks.
    A fill carrying a soft glossary flag (R3: soft, never hard) must be ROLLED BACK -- its row
    left byte-untouched for the terminal write-back -- WITHOUT discarding a CLEAN sibling's
    persisted hard lock (the savepoints are independent). This pins two failure modes a naive
    single-transaction persist would introduce: (a) a leaked half-commit corrupting the flagged
    row, and (b) a cross-segment rollback dropping the clean segment's lock."""
    from app.services import settings

    settings.set_many({"tm_hundreds_enter_lqa": "0"})  # HARD policy
    try:
        # seg0: a CLEAN 100% fill. seg1: a 100% fill MISSING the required term 'Aparato'.
        env = _seed_tm_match_external(
            segs=[
                ("Delete all data.", "Eliminar todos los datos."),
                ("Buy the Widget.", "Compra el trasto."),
            ],
            glossary=[("Widget", "Aparato")],
        )
        run_id, _leg_id, claim = _dispatch(env)
        ids = _seg_ids(env["doc_id"])
        with Session(engine) as s:
            seg0, seg1 = s.get(Segment, ids[0]), s.get(Segment, ids[1])
            # clean sibling: persisted + HARD-locked at the pause.
            assert seg0.lock_band == "100" and seg0.lock_hard is True, (
                seg0.lock_band,
                seg0.lock_hard,
            )
            assert seg0.target_text == "Eliminar todos los datos.", seg0.target_text
            # flagged fill: savepoint ROLLED BACK -> row byte-untouched (no target, no lock,
            # no persisted flag, status still NEW), NOT a leaked half-commit.
            assert seg1.target_text == "", (
                "R3-flagged row must be UNTOUCHED by the rollback",
                seg1.target_text,
            )
            assert not seg1.lock_band and seg1.lock_hard is False, (seg1.lock_band, seg1.lock_hard)
            assert seg1.status == SegStatus.NEW, seg1.status
            assert not (seg1.qa_flags or []), (
                "no flag leaked from the rolled-back commit",
                seg1.qa_flags,
            )
        # A vendor overwrite: seg0 (hard) refused; seg1 (unlocked soft) admitted, and the
        # terminal write-back settles it to NEEDS_REVIEW with the glossary flag.
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s, _payload(ids, ["Sobrescribe 0.", "Sobrescribe 1."], claim=claim), resume="sync"
            )
        assert str(ids[0]) in out["lock_refused"] and str(ids[1]) in out["admitted"], out
        rows = _targets(env["doc_id"])
        assert rows[0][0] == "Eliminar todos los datos.", ("the clean hard lock held", rows[0])
        assert rows[1][0] == "Sobrescribe 1.", ("the soft/unlocked fill was overwritten", rows[1])
        with Session(engine) as s:
            assert s.get(Segment, ids[0]).lock_hard is True
            assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    finally:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
    print(
        "  [P-A1] a flagged (R3-soft) fill is rolled back at the pause; the clean sibling's "
        "hard lock is untouched (savepoint isolation): OK"
    )


def test_partial_then_completing_resume_preserves_earlier_admitted_slots():
    """F-CRIT, the partial-return face: a slot an EARLIER partial return admitted has its
    vendor text in the DB but is still SOURCE in the completing return's parked seed. The
    completing resume must not re-commit that seed over the earlier good vendor text."""
    env = _seed_external_first()
    run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # Return 1 (partial): admit ids[0], ids[1] with real vendor translations.
    with Session(engine) as s:
        first = vendor_return.admit_manual(
            s, _payload(ids[:2], GOOD[:2], claim=claim), resume="sync"
        )
    assert first["complete"] is False, first
    # Return 2 (completing): admit ids[2]; the resume must keep ids[0]/ids[1] intact.
    with Session(engine) as s:
        second = vendor_return.admit_manual(
            s, _payload(ids[2:], GOOD[2:], claim=claim), resume="sync"
        )
    assert second["complete"] and second["resumed"], second
    rows = _targets(env["doc_id"])
    assert [r[0] for r in rows] == GOOD, (
        "F-CRIT: the completing resume clobbered an earlier partial-return's slots",
        rows,
    )
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    print("  [F-CRIT] the completing resume preserves earlier partial-admitted slots: OK")


# --- F-HIGH: exactly-once DISPATCHED->RETURNED ------------------------------
def test_exactly_once_completing_return_is_a_guarded_compare_and_set():
    # Part A: two concurrent COMPLETING returns (a webhook retry racing the poll sweep --
    # both read the leg before either committed) must not BOTH admit + BOTH close the leg.
    env = _seed()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    s1, s2 = Session(engine), Session(engine)
    err = None
    try:
        leg1 = vendor_return.leg_for_claim(s1, claim)
        leg2 = vendor_return.leg_for_claim(s2, claim)
        s1.get(WorkflowRun, run_id)
        s2.get(WorkflowRun, run_id)
        out1 = vendor_return.admit_return(
            s1, leg1, _payload(ids, GOOD, claim=claim), transport="webhook", resume="none"
        )
        try:
            vendor_return.admit_return(
                s2, leg2, _payload(ids, GOOD, claim=claim), transport="webhook", resume="none"
            )
        except Exception as exc:
            err = exc
    finally:
        s1.close()
        s2.close()
    assert out1["complete"] is True
    assert isinstance(err, vendor_return.ReturnStateError), (
        "F-HIGH: a concurrent duplicate completing return was ADMITTED -- the "
        "DISPATCHED->RETURNED transition is not a guarded compare-and-set",
        err,
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
        assert len(vendor_return.returned_ids(leg)) == len(ids), (
            "the ledger double-counted the duplicate return",
            vendor_return.returned_ids(leg),
        )

    # Part B: with an ASYNC resume the run is still PAUSED when the second request runs its
    # gates -- count resume invocations, which must be exactly one.
    env = _seed()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    resumes: list = []
    real_async = orchestrator.resume_run_async
    set_pieces(
        "app.services.orchestrator",
        "resume_run_async",
        lambda s, rid, loc, txt, actor="": resumes.append(rid),
    )
    try:
        s1, s2 = Session(engine), Session(engine)
        leg1 = vendor_return.leg_for_claim(s1, claim)
        leg2 = vendor_return.leg_for_claim(s2, claim)
        s1.get(WorkflowRun, run_id)
        s2.get(WorkflowRun, run_id)
        vendor_return.admit_return(
            s1, leg1, _payload(ids, GOOD, claim=claim), transport="webhook", resume="async"
        )
        try:  # noqa: SIM105
            vendor_return.admit_return(
                s2, leg2, _payload(ids, GOOD, claim=claim), transport="webhook", resume="async"
            )
        except vendor_return.ReturnStateError:
            pass
        s1.close()
        s2.close()
    finally:
        set_pieces("app.services.orchestrator", "resume_run_async", real_async)
    assert resumes == [run_id], (
        "F-HIGH: the run was resumed %d time(s) by concurrent returns" % len(resumes)  # noqa: UP031
    )
    print(
        "  [F-HIGH] exactly-once: a concurrent duplicate completing return is DROPPED, "
        "one resume: OK"
    )


# --- F-MED-1: poll SSRF (internal names / CGNAT / private resolution) --------
def test_poll_ssrf_rejects_internal_names_cgnat_and_private_resolution():
    cap: list = []
    for bad in (
        "https://metadata.google.internal/return",
        "https://vendor.local/return",
        "https://localhost/return",
        "https://100.64.1.1/return",
    ):  # CGNAT literal: ip.is_private MISSES it
        env = _seed(poll_url=bad)
        _, leg_id, _ = _dispatch(env)
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            try:
                vendor_return.poll_leg(
                    s, leg, transport=_mock_transport(204, body=b"", capture=cap)
                )
                raise AssertionError(f"a hostile poll_url {bad!r} must be refused")
            except ConnectorError:
                pass
    assert not cap, ("F-MED-1: a byte left the process for a blocked SSRF poll target", cap)

    # A hostname that RESOLVES to a private IP is refused too (DNS-rebind / split-horizon),
    # with ZERO egress. Guarded so the pre-fix tip (no resolver seam) still runs the cases
    # above as its fail-before proof.
    if hasattr(vendor_return, "set_test_resolver"):
        cap2: list = []
        env = _seed(poll_url="https://sneaky.example.com/return")
        _, leg_id, _ = _dispatch(env)
        vendor_return.set_test_resolver(lambda host: ["10.0.0.5"])
        try:
            with Session(engine) as s:
                leg = s.get(VendorDispatch, leg_id)
                try:
                    vendor_return.poll_leg(
                        s, leg, transport=_mock_transport(204, body=b"", capture=cap2)
                    )
                    raise AssertionError("a private-resolving host must be refused")
                except ConnectorError:
                    pass
        finally:
            vendor_return.set_test_resolver(lambda host: ["93.184.216.34"])
        assert not cap2, "a byte left the process for a rebind poll target"
    print(
        "  [F-MED-1] poll SSRF: .internal/.local/localhost/CGNAT + private-resolution "
        "refused, zero egress: OK"
    )


# --- F-MED-2: poll response body cap ----------------------------------------
def test_poll_body_over_cap_is_refused_before_admission():
    from app import config

    env = _seed(poll_url=POLL_URL)
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    before = _targets(env["doc_id"])
    valid = _payload(ids, GOOD, claim=claim)  # a fully-valid contract...
    valid["_pad"] = "x" * (config.WEBHOOK_MAX_BODY_BYTES + 4096)  # ...but oversized
    raw = json.dumps(valid).encode()
    assert len(raw) > config.WEBHOOK_MAX_BODY_BYTES
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_return.poll_leg(
                s,
                leg,
                transport=_mock_transport(
                    200, body=raw, headers={vendor_return.SIGNATURE_HEADER: _sign(raw)}
                ),
            )
            raise AssertionError("an over-cap poll body must be refused, not admitted")
        except vendor_return.VendorPollError as exc:
            assert "cap" in str(exc).lower(), exc
    assert _targets(env["doc_id"]) == before, "F-MED-2: an over-cap poll body admitted content"
    print("  [F-MED-2] an over-cap poll response body is refused before admission: OK")


# --- F-MED-3: return body must bind to its leg by claim_token ----------------
def test_signed_return_body_must_bind_to_its_leg_by_claim_token():
    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    before = _targets(env["doc_id"])
    # (1) a validly-signed body that OMITS claim_token must be REFUSED -- otherwise a body
    #     signed for one leg is replayable into a distinct leg of the same vendor just by
    #     dropping the field (the inert-when-absent check the finding names).
    body = _payload(ids, GOOD)  # no claim_token
    assert "claim_token" not in body
    raw = json.dumps(body).encode()
    with Session(engine) as s:
        leg = vendor_return.authenticate_callback(
            s, {vendor_return.CLAIM_HEADER: claim, vendor_return.SIGNATURE_HEADER: _sign(raw)}, raw
        )
        _refused(
            lambda: vendor_return.admit_return(s, leg, raw, transport="webhook", resume="none")
        )
    assert _targets(env["doc_id"]) == before, "a claim-less signed body admitted content"
    # (2) a body carrying a FOREIGN (present-but-wrong) claim_token is refused too.
    body2 = _payload(ids, GOOD, claim="a-different-legs-token")
    raw2 = json.dumps(body2).encode()
    with Session(engine) as s:
        leg = vendor_return.authenticate_callback(
            s,
            {vendor_return.CLAIM_HEADER: claim, vendor_return.SIGNATURE_HEADER: _sign(raw2)},
            raw2,
        )
        _refused(
            lambda: vendor_return.admit_return(s, leg, raw2, transport="webhook", resume="none")
        )
    assert _targets(env["doc_id"]) == before
    print("  [F-MED-3] a signed body with an absent or foreign claim_token is refused: OK")


# ============================================================================
# [R2] Third-reviewer fix round: HIGH-1 step isolation, MEDIUM-3/4/5/6, L1.
# ============================================================================
def _seed_external_then_human(*, signing=SIGNING):
    """A workflow [EXTERNAL, HUMAN_REVIEW]: dispatch parks the external step; a (bypassing)
    resume can then advance the run to the LATER human_review pause while the external leg
    stays DISPATCHED -- the HIGH-1 step-isolation setup."""
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
            endpoint_url="https://vendor.example.com/intake",
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": signing})
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
            name="Vendor review",
            step_type=StepType.EXTERNAL,
            vendor_id=v.id,
            external_wait_hours=72,
        )
        wf.add_step(s, w.id, name="Final human check", step_type=StepType.HUMAN_REVIEW)
        return {
            "customer_id": cust.id,
            "project_id": p.id,
            "doc_id": d.id,
            "vendor_id": v.id,
            "wf_id": w.id,
        }


def test_return_refused_when_run_parked_at_a_different_step():
    """HIGH-1: gate 1 must refuse a return when the run has advanced PAST the external step
    to a later (human_review) pause -- otherwise the return splices into and resumes the
    WRONG step's pause (the human's review, consumed unseen)."""
    env = _seed_external_then_human()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        blob = orchestrator._paused_blob(s, run_id)
    # Advance the externally-parked run to the human_review pause via the resume SERVICE
    # directly (the route guard is tested separately); the external leg stays DISPATCHED.
    with Session(engine) as s:
        orchestrator.resume_workflow(s, run_id, "es-ES", blob)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        leg = s.get(VendorDispatch, leg_id)
        sr = orchestrator._latest_paused_steprun(s, run_id)
        assert run.status == RunStatus.PAUSED and leg.status == VendorDispatchStatus.DISPATCHED
        assert sr.step_id != leg.step_id, "the run must be parked at a DIFFERENT step now"
    before = _targets(env["doc_id"])
    with Session(engine) as s:
        try:
            vendor_return.admit_manual(s, _payload(ids, GOOD, claim=claim), resume="sync")
            raise AssertionError("HIGH-1: a return admitted against a foreign pause")
        except vendor_return.ReturnStateError as exc:
            assert "parked at this leg's step" in str(exc), exc
    assert _targets(env["doc_id"]) == before, "HIGH-1: content changed on a foreign pause"
    print("  [HIGH-1] a return is refused when the run is parked at a different step: OK")


def test_vendor_currency_never_mutates_operator_estimate_currency():
    """MEDIUM-3: the vendor's reported currency must never overwrite the operator's
    estimate_currency, and a bogus/overlong value is not stored anywhere."""
    env = _seed()
    _run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        est_before = s.get(VendorDispatch, leg_id).estimate_currency
    body = _payload(ids[:1], GOOD[:1], claim=claim, cost=50.0)
    body["actual_currency"] = "NOTACURRENCY" * 500  # overlong garbage
    with Session(engine) as s:
        vendor_return.admit_manual(s, body, resume="none")
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.estimate_currency == est_before, (
            "MEDIUM-3: a vendor currency overwrote estimate_currency",
            leg.estimate_currency,
        )
        assert "actual_currency" not in (leg.contract_ids or {}), (
            "MEDIUM-3: a bogus currency was stored",
            leg.contract_ids,
        )
        assert leg.actual_cost_usd == 50.0
    # ...and a VALID ISO-4217 currency lands in the ledger's own key, still not touching est.
    with Session(engine) as s:
        body2 = _payload(ids[1:], GOOD[1:], claim=claim, cost=10.0)
        body2["actual_currency"] = "eur"
        vendor_return.admit_manual(s, body2, resume="none")
        leg = s.get(VendorDispatch, leg_id)
        assert (leg.contract_ids or {}).get("actual_currency") == "EUR", leg.contract_ids
        assert leg.estimate_currency == est_before
    print(
        "  [MEDIUM-3] vendor currency never mutates estimate_currency; bogus dropped, "
        "valid stored in its own ledger key: OK"
    )


def test_failed_resume_is_loud_and_the_leg_is_redrivable():
    """MEDIUM-4: a resume that throws must leave a LOUD audit trail (not a silent wedge) and
    the RETURNED leg must be re-drivable to completion."""
    env = _seed_external_first()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])

    def _boom(*a, **k):
        raise RuntimeError("simulated resume failure")

    import app.services.audit as audit_mod

    real_resume = orchestrator.resume_workflow
    real_audit = audit_mod.log
    actions = []

    def _spy_audit(**kw):
        actions.append(kw.get("action"))
        return real_audit(**kw)

    set_pieces("app.services.orchestrator", "resume_workflow", _boom)
    audit_mod.log = _spy_audit
    try:
        with Session(engine) as s:
            out = vendor_return.admit_manual(s, _payload(ids, GOOD, claim=claim), resume="sync")
    finally:
        set_pieces("app.services.orchestrator", "resume_workflow", real_resume)
        audit_mod.log = real_audit
    assert out["complete"] is True and out["resumed"] is False, out
    assert "vendor.return_resume_failed" in actions, (
        "MEDIUM-4: a wedged resume left no loud audit trail",
        actions,
    )
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
    # Re-drive: the content is already committed; this continues the parked run to done.
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        res = vendor_return.redrive_returned_leg(s, leg, mode="sync")
        assert res["resumed"] is True, res
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
        assert [t for t, _ in _targets(env["doc_id"])] == GOOD
    print("  [MEDIUM-4] a wedged resume is audited + the leg is re-drivable to completion: OK")


def test_poll_sweep_is_per_vendor_not_per_leg():
    """MEDIUM-5: one vendor with several open legs must yield at most ONE GET per sweep."""
    hits: list = []

    def handler(req):
        hits.append(str(req.url))
        return httpx.Response(204)

    tport = httpx.MockTransport(handler)
    env = _seed(poll_url=POLL_URL)
    _dispatch(env)  # leg #1 on this vendor
    for _ in range(3):  # 3 more legs on the SAME vendor
        with Session(engine) as s:
            d = Document(
                project_id=env["project_id"], filename=f"d{_seq()}.txt", mode=TransMode.SEGMENTED
            )
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
            did = d.id
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            with Session(engine) as s:
                list(orchestrator.run_workflow_stream(s, env["wf_id"], did, "es-ES"))
        finally:
            vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        out = vendor_return.poll_due_legs(
            s, vendor_id=env["vendor_id"], transport=tport, resume="none"
        )
    assert len(hits) <= 1, (
        "MEDIUM-5: per-vendor throttle failed -- %d GETs to one vendor in one sweep" % len(hits)  # noqa: UP031
    )
    assert out["polled"] <= 1, out
    print("  [MEDIUM-5] a vendor with several open legs yields at most one GET per sweep: OK")


def _add_leg(env):
    """Another open DISPATCHED leg on the SAME vendor (own document). Returns
    (doc_id, leg_id, claim_token) of the NEWEST leg."""
    with Session(engine) as s:
        d = Document(
            project_id=env["project_id"], filename=f"d{_seq()}.txt", mode=TransMode.SEGMENTED
        )
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
        did = d.id
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], did, "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        leg = s.exec(
            select(VendorDispatch)
            .where(VendorDispatch.vendor_id == env["vendor_id"])
            .order_by(VendorDispatch.id.desc())
        ).first()
        return did, leg.id, leg.claim_token


def test_poll_sweep_rotates_through_a_vendors_open_legs():
    """#447: with two open legs on one vendor the sweep must not always pick the newest; the
    older leg's return is pulled even while the newer one keeps answering 204."""
    env = _seed(poll_url=POLL_URL)
    _run1, leg1, claim1 = _dispatch(env)  # older leg, its own document
    doc1 = env["doc_id"]
    _doc2, leg2, claim2 = _add_leg(env)  # newer leg
    assert leg2 > leg1
    ids1 = _seg_ids(doc1)
    raw1 = json.dumps(_payload(ids1, GOOD, claim=claim1)).encode()
    hits: list = []

    def handler(req):
        claim = req.headers.get(vendor_return.CLAIM_HEADER)
        hits.append(claim)
        if claim == claim1:  # only the OLDER leg has a finished return
            return httpx.Response(
                200, content=raw1, headers={vendor_return.SIGNATURE_HEADER: _sign(raw1)}
            )
        return httpx.Response(204)

    tport = httpx.MockTransport(handler)
    vendor_return._LAST_POLL.clear()
    admitted = 0
    for _ in range(2):  # two sweeps are enough for two legs to each get a turn
        with Session(engine) as s:
            out = vendor_return.poll_due_legs(
                s, vendor_id=env["vendor_id"], transport=tport, throttle=False, resume="none"
            )
        assert out["polled"] == 1, out  # still ONE GET per vendor per sweep (MEDIUM-5)
        admitted += out["admitted"]
    assert claim1 in hits and claim2 in hits, ("both legs must get a turn", hits)
    assert admitted == 1, ("the older leg's return must be pulled", hits)
    with Session(engine) as s:
        assert s.get(VendorDispatch, leg1).status == VendorDispatchStatus.RETURNED
        assert s.get(VendorDispatch, leg2).status == VendorDispatchStatus.DISPATCHED
    print("  [#447] the sweep rotates a vendor's open legs: the older leg's return is pulled: OK")


def test_scheduler_disabled_makes_zero_poll_requests():
    """MEDIUM-6: the poll sweep sits BELOW the global kill switch, so a disabled scheduler
    makes ZERO outbound poll requests."""
    from app.services import scheduler, settings

    hits: list = []

    def handler(req):
        hits.append(str(req.url))
        return httpx.Response(204)

    env = _seed(poll_url=POLL_URL)
    _dispatch(env)
    settings.set_many({"sync_scheduler_enabled": "0"})
    real_poll = vendor_return.poll_leg

    def _spy(session, leg, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_poll(session, leg, **kw)

    set_pieces("app.services.vendor_return", "poll_leg", _spy)
    try:
        scheduler.tick()
    finally:
        set_pieces("app.services.vendor_return", "poll_leg", real_poll)
        settings.set_many({"sync_scheduler_enabled": "1"})
    assert not hits, "MEDIUM-6: the disabled scheduler still fired %d poll(s)" % len(hits)  # noqa: UP031
    print("  [MEDIUM-6] the global kill switch stops all vendor poll egress: OK")


def test_poll_url_host_must_match_endpoint_host():
    """L1: a poll_url whose host differs from the vendor's endpoint host is refused before
    egress -- a config-only edit cannot redirect the bearer credential to a foreign host."""
    from app.services.connectors.base import ConnectorError

    env = _seed(poll_url="https://other-public.example.net/return")  # != endpoint host
    _, leg_id, _ = _dispatch(env)
    cap: list = []
    vendor_return.set_test_resolver(lambda host: ["93.184.216.34"])  # foreign host is public
    try:
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            try:
                vendor_return.poll_leg(
                    s, leg, transport=_mock_transport(204, body=b"", capture=cap)
                )
                raise AssertionError("L1: a foreign-host poll_url must be refused")
            except ConnectorError as exc:
                assert "endpoint host" in str(exc), exc
    finally:
        vendor_return.set_test_resolver(lambda host: ["93.184.216.34"])
    assert not cap, "L1: a byte left the process for a foreign-host poll target"
    print("  [L1] a poll_url whose host differs from the endpoint host is refused: OK")


def test_poll_url_port_must_match_endpoint_origin():
    """#265 / fix-engine-repoint gate round 1 LOW-1: the pin is the endpoint ORIGIN, so a
    poll_url on ANOTHER PORT of the endpoint host is refused too (it used to pass the
    hostname-only check and receive the bearer auth_token). Fails on base 9bc3c6a1."""
    from app.services.connectors.base import ConnectorError

    env = _seed(poll_url="https://vendor.example.com:8443/return")  # endpoint is :443
    _, leg_id, _ = _dispatch(env)
    cap: list = []
    vendor_return.set_test_resolver(lambda host: ["93.184.216.34"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_return.poll_leg(s, leg, transport=_mock_transport(204, body=b"", capture=cap))
            raise AssertionError("LOW-1: a poll_url on another port must be refused")
        except ConnectorError as exc:
            assert "endpoint host" in str(exc) and "8443" in str(exc), exc
    assert not cap, "LOW-1: a byte left the process for another port of the endpoint host"
    print("  [LOW-1] a poll_url on another port of the endpoint host is refused: OK")


def test_poll_pin_refuses_an_unreadable_endpoint_origin():
    """fix-engine-repoint gate round 2 MEDIUM-1: an endpoint stored (before the save door
    refused it) with an origin the pin cannot read (`https://h:+443`: httpx reaches it,
    urlsplit refuses the port) used to make the pin SKIP. It must refuse instead."""
    from sqlalchemy import text

    from app.services.connectors.base import ConnectorError

    env = _seed(poll_url="https://vendor.example.com/return")
    _, leg_id, _ = _dispatch(env)
    with Session(engine) as s:
        vid = s.get(VendorDispatch, leg_id).vendor_id
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE vendor SET endpoint_url = :u WHERE id = :i"),
            {"u": "https://vendor.example.com:+443/intake", "i": vid},
        )
    cap: list = []
    vendor_return.set_test_resolver(lambda host: ["93.184.216.34"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_return.poll_leg(s, leg, transport=_mock_transport(204, body=b"", capture=cap))
            raise AssertionError("MEDIUM-1: an unreadable endpoint origin must refuse the poll")
        except ConnectorError as exc:
            assert "endpoint host" in str(exc), exc
    assert not cap, "MEDIUM-1: a byte left the process with an unreadable endpoint origin"
    print("  [MEDIUM-1] an unreadable endpoint origin refuses the poll: OK")


# ============================================================================
# [R3] Fourth-review HIGH: a WEDGED (RETURNED-but-unresumed) leg must not be human-
# resumable (it would re-clobber vendor content with the pre-dispatch source), plus
# the FULL_TEXT + L5 coverage gaps.
# ============================================================================
def _seed_full_text_external_first(*, signing=SIGNING):
    """A FULL_TEXT doc whose only content step is EXTERNAL -- the whole-body 'full:0' unit,
    parked blob = raw source."""
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
            endpoint_url="https://vendor.example.com/intake",
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": signing})
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


def test_cli_human_resume_of_a_wedged_leg_is_refused_and_preserves_content():
    """HIGH (round-3): after a wedge (leg RETURNED, run still PAUSED at the external step),
    a human CLI resume must be REFUSED and steer to `vendor redrive`. On the pre-fix tip the
    resume PROCEEDS and the sync write-back re-commits the pre-dispatch SOURCE over the
    already-committed vendor content -- the F-CRIT clobber, via a supported action."""
    from tests._cli_runner import run_cli

    env = _seed_external_first()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # Wedge shape without a throwing resume: admit + close the leg but do NOT resume.
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_return.admit_return(
            s, leg, _payload(ids, GOOD, claim=claim), transport="manual", resume="none"
        )
    assert [t for t, _ in _targets(env["doc_id"])] == GOOD, "vendor content must be committed"
    with Session(engine) as s:
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
    code, out, err = run_cli(
        ["workflow", "resume", str(run_id), "--locale", "es-ES", "--edited-text", ""]
    )
    assert code != 0, (
        "HIGH-r3: a CLI human resume of a wedged leg must be refused",
        code,
        out,
        err,
    )
    assert "redrive" in (out + err).lower(), (out, err)
    assert [t for t, _ in _targets(env["doc_id"])] == GOOD, (
        "HIGH-r3: the vendor content must survive the refused resume (not clobbered to source)"
    )
    print("  [HIGH-r3] a CLI human resume of a wedged leg is refused; content preserved: OK")


def test_wedged_leg_is_still_redrivable_after_the_guard():
    """HIGH (round-3) constraint: the guard must NOT block the sanctioned recovery path --
    `vendor redrive` still resolves exactly the wedged state it refuses a human resume on."""
    env = _seed_external_first()
    run_id, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_return.admit_return(
            s, leg, _payload(ids, GOOD, claim=claim), transport="manual", resume="none"
        )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        res = vendor_return.redrive_returned_leg(s, leg, mode="sync")
        assert res["resumed"] is True, res
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
        assert [t for t, _ in _targets(env["doc_id"])] == GOOD, "redrive must not clobber"
    print("  [HIGH-r3] vendor redrive still resolves a wedged leg (no clobber): OK")


def test_normal_human_review_after_resolved_external_is_not_blocked():
    """HIGH (round-3) constraint (d): once the external leg has resumed cleanly and the run
    has advanced to a LATER human_review pause, that pause is human-resumable (no over-
    refusal), because vendor_pause_state keys on THE CURRENT pause step, not any RETURNED leg."""
    from tests._cli_runner import run_cli

    env = _seed_external_then_human()
    run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        vendor_return.admit_manual(s, _payload(ids, GOOD, claim=claim), resume="sync")
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED  # at human_review now
        leg, state = vendor_dispatch.vendor_pause_state(s, run_id)
        assert leg is None and state == "", (
            "no over-refusal: a later human pause is not a vendor pause",
            leg,
            state,
        )
    code, out, err = run_cli(
        ["workflow", "resume", str(run_id), "--locale", "es-ES", "--edited-text", ""]
    )
    assert code == 0, ("a legit human_review resume must NOT be blocked", code, out, err)
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    print(
        "  [HIGH-r3 d] a normal human_review pause after a resolved external leg is NOT blocked: OK"
    )


def test_full_text_source_echo_survives_a_completing_resume():
    """Coverage gap: the FULL_TEXT loop's un-admitted-slot resume-skip (round-1 F-CRIT) --
    a source-echo no-op keeps its prior target + needs_review through the completing resume."""
    env = _seed_full_text_external_first()
    run_id, _leg_id, claim = _dispatch(env)
    PRIOR = "Hola mundo (previo, full-text)."
    with Session(engine) as s:
        seg = orchestrator._load_segments(s, env["doc_id"], "es-ES")[0]
        seg.target_text = PRIOR
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(["full:0"], ["Hello world."], claim=claim), resume="sync"
        )
    assert out["complete"] and out["resumed"], out
    assert out["no_op"] == ["full:0"], out
    rows = _targets(env["doc_id"])
    assert rows[0][0] == PRIOR, (
        "FULL_TEXT: the completing resume clobbered the prior target with source",
        rows[0],
    )
    assert rows[0][1] == SegStatus.NEEDS_REVIEW, rows[0]
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    print("  [gap] FULL_TEXT source-echo no-op survives the completing resume: OK")


def test_manual_upload_over_cap_is_refused_str_and_bytes():
    """Coverage gap (L5): an over-cap manual/upload return is refused on BOTH the str and
    bytes paths, before anything is admitted."""
    from app import config

    env = _seed()
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    before = _targets(env["doc_id"])
    huge = _payload(ids, GOOD, claim=claim)
    huge["_pad"] = "x" * (config.WEBHOOK_MAX_BODY_BYTES + 4096)
    raw_str = json.dumps(huge)
    raw_bytes = raw_str.encode()
    assert len(raw_bytes) > config.WEBHOOK_MAX_BODY_BYTES
    for payload in (raw_str, raw_bytes):
        with Session(engine) as s:
            msg = _refused(
                lambda payload=payload: vendor_return.admit_manual(
                    s, payload, claim_token=claim, resume="none"
                )
            )
            assert "cap" in msg.lower(), msg
    assert _targets(env["doc_id"]) == before, "L5: an over-cap manual return admitted content"
    print("  [gap/L5] an over-cap manual/upload return (str + bytes) is refused: OK")


if __name__ == "__main__":
    test_manual_return_admits_reconciles_and_resumes()
    test_full_text_leg_returns_through_the_same_pipeline()
    test_partial_then_completing_return_resumes_exactly_once()
    test_unknown_duplicate_and_wrong_locale_fail_the_whole_return()
    test_duplicate_of_an_already_admitted_id_is_refused()
    test_wave_a_leg_without_a_ledger_is_refused()
    test_signature_verification_and_ordering()
    test_non_ascii_claim_tokens_refuse_cleanly_instead_of_crashing()
    test_vendor_without_a_signing_secret_cannot_be_called_back()
    test_cross_tenant_claim_token_cannot_admit()
    test_org_scoped_manual_return_cannot_reach_a_foreign_org_leg()
    test_return_scoping_is_buyer_org_not_vendor_org()
    test_document_rehomed_to_another_customer_drops_the_return()
    test_source_echo_is_a_no_op_that_never_overwrites()
    test_source_echo_is_allowed_for_an_english_target()
    test_refusal_text_is_quarantined_not_stored()
    test_tag_damage_routes_to_review()
    test_late_return_after_cancel_escalate_or_terminal_run_is_dropped()
    test_abort_closes_the_leg_so_its_token_stops_admitting()
    test_poll_nothing_ready_then_ready()
    test_poll_rejects_a_bad_response_signature()
    test_poll_url_is_ssrf_guarded_and_opt_in()
    test_poll_sweep_is_throttled_and_survives_a_bad_leg()
    test_scheduler_tick_polls_only_opted_in_vendors()
    test_bad_actual_cost_is_refused_before_anything_is_written()
    test_no_surface_echoes_the_claim_token_or_the_secret()
    test_provenance_tag_is_vendor()
    test_cli_dispatches_return_and_poll()
    test_source_echo_no_op_survives_a_completing_SYNC_resume()
    test_source_echo_no_op_survives_a_completing_ASYNC_resume()
    test_refusal_quarantine_survives_a_completing_resume()
    test_tag_damage_stays_flagged_through_a_completing_resume()
    test_hard_locked_slot_survives_a_completing_resume()
    test_inrun_tm_hard_lock_survives_a_vendor_return()
    test_inrun_flagged_tm_fill_is_not_hard_persisted_at_external_pause()
    test_partial_then_completing_resume_preserves_earlier_admitted_slots()
    test_exactly_once_completing_return_is_a_guarded_compare_and_set()
    test_poll_ssrf_rejects_internal_names_cgnat_and_private_resolution()
    test_poll_body_over_cap_is_refused_before_admission()
    test_signed_return_body_must_bind_to_its_leg_by_claim_token()
    test_return_refused_when_run_parked_at_a_different_step()
    test_vendor_currency_never_mutates_operator_estimate_currency()
    test_failed_resume_is_loud_and_the_leg_is_redrivable()
    test_poll_sweep_is_per_vendor_not_per_leg()
    test_poll_sweep_rotates_through_a_vendors_open_legs()
    test_scheduler_disabled_makes_zero_poll_requests()
    test_poll_url_host_must_match_endpoint_host()
    test_poll_url_port_must_match_endpoint_origin()
    test_poll_pin_refuses_an_unreadable_endpoint_origin()
    test_cli_human_resume_of_a_wedged_leg_is_refused_and_preserves_content()
    test_wedged_leg_is_still_redrivable_after_the_guard()
    test_normal_human_review_after_resolved_external_is_not_blocked()
    test_full_text_source_echo_survives_a_completing_resume()
    test_manual_upload_over_cap_is_refused_str_and_bytes()
    print("verify_vendor_return: all OK")
