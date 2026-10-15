"""Vendors + MSA cost model + scoping estimate + external-step config parity
(PROPOSAL-external-processing-step §9 Wave A; PROPOSAL-vendor-human-review-handoff).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; no network
— the dispatch client is exercised in tests/verify_external_step.py through an injected
httpx.MockTransport). This suite is the SEND-side substrate WITHOUT the wire:

  [A] VENDOR CRUD + secrets: create/update/archive; the Fernet secret round-trips and is
      NEVER echoed (to_dict carries only has_secret); delete FAILS CLOSED while a step
      selects the vendor.
  [B] CUSTOMER LINKING: link is idempotent + unique; linking is UNRESTRICTED across orgs
      (2026-08-08 — a vendor is a globally linkable asset, full many-to-many); the
      fail-closed scoping seam (vendor_for_customer) resolves a linked vendor (incl.
      cross-org) and refuses an unlinked / archived / no-customer one.
  [C] RATE CARD storage + normalization (unknown bands dropped, missing -> 0.0).
  [D] SCOPING ESTIMATE against a seeded TM-banded document: every band counted correctly,
      and locked / 100% / repetitions EXCLUDED from the billable scope.
  [E] EXTERNAL step-config parity at the SERVICE seam (web + CLI + api all call it): the
      external type accepts a vendor + wait override; a missing/unlinked vendor is refused;
      vendor_id is a DEAD field on every non-external step; a type switch clears it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_vendors_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    Scope,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    VendorCustomer,
    Workflow,
    WorkflowStep,
)
from app.services import vendor_cost, vendors
from app.services import workflows as wf

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _customer(name="Cust", org_id=None):
    with Session(engine) as s:
        c = Customer(name=f"{name}-{_seq()}", org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


RATE_CARD = {
    "currency": "USD",
    "bands": {
        "new": 0.20,
        "fuzzy_high": 0.10,
        "fuzzy_mid": 0.12,
        "fuzzy_low": 0.08,
        "exact": 0.0,
        "context": 0.0,
        "repetitions": 0.0,
    },
}


# ============================================================================
# [A] VENDOR CRUD + secrets
# ============================================================================
def test_crud_and_secret_never_echoed():
    with Session(engine) as s:
        v = vendors.create(
            s,
            "Acme LSP",
            description="desc",
            endpoint_url="https://vendor.example.com/intake",
            secret={"signing_secret": "shhh", "auth_token": "tok"},
            rate_card=RATE_CARD,
        )
        assert v.id is not None
        # Secret is stored ENCRYPTED (ciphertext != plaintext, decrypts back).
        assert v.secret_enc and "shhh" not in v.secret_enc, "secret must be ciphertext"
        assert vendors.secret_dict(v)["signing_secret"] == "shhh"
        # to_dict NEVER carries the secret value — only a presence flag.
        d = vendors.to_dict(s, v)
        blob = repr(d)
        assert "shhh" not in blob and "tok" not in blob, "secret leaked into to_dict"
        assert d["has_secret"] is True
        assert d["endpoint_url"] == "https://vendor.example.com/intake"
        # Rate card normalized on the way out (all 7 bands present).
        assert set(d["rate_card"]["bands"]) == set(vendor_cost.BANDS)

        # Update is partial; a blank name is refused.
        vendors.update(s, v.id, description="new desc")
        assert vendors.get(s, v.id).description == "new desc"
        try:
            vendors.update(s, v.id, name="   ")
            raise AssertionError("blank name must be refused")
        except vendors.VendorError:
            pass

        # set_secret MERGES (a missing field keeps the old value; "" clears one).
        vendors.set_secret(s, v.id, {"auth_token": ""})
        sd = vendors.secret_dict(vendors.get(s, v.id))
        assert sd.get("signing_secret") == "shhh" and "auth_token" not in sd
    print("  [A] CRUD + Fernet secret round-trip, never echoed, partial update: OK")


def test_archive_and_delete_fail_closed():
    with Session(engine) as s:
        v = vendors.create(s, "Retire Me")
        vendors.archive(s, v.id)
        assert vendors.get(s, v.id).archived_at is not None
        assert vendors.selectable(s, v.id) is None, "archived vendor not selectable"
        # Double archive is a clean error.
        try:
            vendors.archive(s, v.id)
            raise AssertionError("double archive must raise")
        except vendors.VendorError:
            pass
        vendors.unarchive(s, v.id)
        assert vendors.selectable(s, v.id) is not None

    # Delete FAILS CLOSED while a workflow step selects the vendor.
    cid = _customer("DelCust")
    with Session(engine) as s:
        v = vendors.create(s, "Referenced")
        vendors.link_customer(s, v.id, cid)
        w = Workflow(name=f"W-{_seq()}", scope=Scope.GLOBAL)
        s.add(w)
        s.commit()
        s.refresh(w)
        wid, vid = w.id, v.id
    with Session(engine) as s:
        wf.add_step(s, wid, name="Vendor", step_type=StepType.EXTERNAL, vendor_id=vid)
    with Session(engine) as s:
        try:
            vendors.delete(s, vid)
            raise AssertionError("delete must fail closed while a step references it")
        except vendors.VendorDeleteBlocked as exc:
            assert exc.refs["workflow_steps"] == 1
    print("  [A] archive/unarchive idempotent-safe + delete fail-closed: OK")


# ============================================================================
# [B] CUSTOMER LINKING + the fail-closed scoping seam
# ============================================================================
def test_linking_idempotent_and_scoped():
    cid = _customer("LinkCust")
    with Session(engine) as s:
        v = vendors.create(s, "Linkable")
        linked, _ = vendors.link_customer(s, v.id, cid)
        assert linked is True
        # Idempotent: a second link is a no-op, never a duplicate row.
        linked2, _ = vendors.link_customer(s, v.id, cid)
        assert linked2 is False
        rows = s.exec(
            select(VendorCustomer).where(
                VendorCustomer.vendor_id == v.id, VendorCustomer.customer_id == cid
            )
        ).all()
        assert len(rows) == 1, "unique (vendor, customer) — no duplicate link"
        assert vendors.linked_customer_ids(s, v.id) == [cid]
        # vendor_for_customer resolves the linked vendor, refuses an unlinked customer,
        # and refuses a NULL customer (an external step needs a customer-scoped vendor).
        assert vendors.vendor_for_customer(s, v.id, cid) is not None
        other = _customer("Other")
        assert vendors.vendor_for_customer(s, v.id, other) is None
        assert vendors.vendor_for_customer(s, v.id, None) is None
        # Unlink restores the "no vendor" state.
        removed, _ = vendors.unlink_customer(s, v.id, cid)
        assert removed is True
        assert vendors.vendor_for_customer(s, v.id, cid) is None
    print("  [B] linking idempotent+unique; fail-closed scoping seam: OK")


def _org(name):
    from app.models import Organization

    with Session(engine) as s:
        o = Organization(name=f"{name}-{_seq()}")
        s.add(o)
        s.commit()
        s.refresh(o)
        return o.id


def test_cross_org_link_allowed():
    """CROSS-ORG LINKING (2026-08-08): vendor<->customer linking is unrestricted
    across orgs — a vendor is a globally linkable asset. This INVERTS the Wave A same-org
    refusal: a vendor in org B links to a customer in org A and the link succeeds. A
    duplicate is still an idempotent no-op (pair-uniqueness), and the fail-closed scoping
    seam resolves the cross-org-linked vendor for that customer."""
    org_a, org_b = _org("A"), _org("B")
    org_a_cust = _customer("OrgA", org_id=org_a)
    with Session(engine) as s:
        v = vendors.create(s, "OrgB vendor", org_id=org_b)
        linked, _ = vendors.link_customer(s, v.id, org_a_cust)
        assert linked is True, "a cross-org link must now succeed (unrestricted)"
        # Idempotent: pair-uniqueness still refuses a duplicate (a second link is a no-op).
        linked2, _ = vendors.link_customer(s, v.id, org_a_cust)
        assert linked2 is False, "duplicate link is a no-op, never a second row"
        rows = s.exec(
            select(VendorCustomer).where(
                VendorCustomer.vendor_id == v.id, VendorCustomer.customer_id == org_a_cust
            )
        ).all()
        assert len(rows) == 1, "unique (vendor, customer) — no duplicate cross-org row"
        # The cross-org-linked vendor is now resolvable for the customer (the LINK is the
        # authorization; the org mismatch no longer refuses).
        assert vendors.vendor_for_customer(s, v.id, org_a_cust) is not None
    print("  [B] cross-org vendor<->customer link allowed (unrestricted), dup refused: OK")


def test_cross_org_many_to_many_and_dispatch_resolution():
    """Full many-to-many across orgs + dispatch-time resolution.
    (1) ONE vendor linked to customers in TWO different orgs — both links stand.
    (2) TWO vendors linked to ONE customer — both stand.
    (3) A cross-org-linked vendor RESOLVES at dispatch time: `vendor_for_customer` (the
        seam `resolve_vendor` calls) succeeds even though the run passes the CUSTOMER's org
        (the buyer side), which differs from the vendor's org."""
    org_a, org_b, org_c = _org("MA"), _org("MB"), _org("MC")
    cust_a = _customer("MOrgA", org_id=org_a)
    cust_b = _customer("MOrgB", org_id=org_b)
    with Session(engine) as s:
        # (1) one vendor (org C) -> customers in org A AND org B.
        v = vendors.create(s, "Global LSP", org_id=org_c)
        assert vendors.link_customer(s, v.id, cust_a)[0] is True
        assert vendors.link_customer(s, v.id, cust_b)[0] is True
        assert set(vendors.linked_customer_ids(s, v.id)) == {cust_a, cust_b}

        # (2) two vendors (each a different org) -> the SAME customer (org A).
        v2 = vendors.create(s, "Second LSP", org_id=org_b)
        assert vendors.link_customer(s, v2.id, cust_a)[0] is True
        # Both vendors resolve for that one customer.
        assert vendors.vendor_for_customer(s, v.id, cust_a) is not None
        assert vendors.vendor_for_customer(s, v2.id, cust_a) is not None

        # (3) dispatch-time resolution passes the CUSTOMER's org (buyer side); a cross-org
        # vendor must still resolve. This is the exact call resolve_vendor makes.
        cust_a_org = s.get(Customer, cust_a).org_id
        assert cust_a_org == org_a and getattr(v, "org_id", None) == org_c
        resolved = vendors.vendor_for_customer(s, v.id, cust_a, org_id=cust_a_org)
        assert resolved is not None, "cross-org vendor must resolve at dispatch time"
    print("  [B] cross-org many-to-many + dispatch resolution across orgs: OK")


def test_scoped_link_authority_is_customer_side():
    """AUTHORITY = the CUSTOMER's org (buyer consent), NOT the vendor's. A scoped caller
    may link ANY active vendor install-wide to a customer IN THEIR OWN org, but NOT to a
    customer in another org — and that refusal is NON-ENUMERABLE (identical message to the
    unknown-id case, so a foreign customer id can't be probed). Both directions:
      * org-A scoped caller -> org-B customer: REFUSED (buyer authority is org B's).
      * org-B scoped caller -> org-B's OWN customer, using org-A's vendor: ALLOWED (the
        vendor is unrestricted as a link target)."""
    org_a, org_b = _org("AuthA"), _org("AuthB")
    cust_b = _customer("AuthCustB", org_id=org_b)
    with Session(engine) as s:
        vendor_a = vendors.create(s, "Auth vendor A", org_id=org_a)
        # org-A scoped caller cannot link a customer whose org is B (buyer authority = B).
        try:
            vendors.link_customer(s, vendor_a.id, cust_b, org_id=org_a)
            raise AssertionError("a scoped caller must not link a foreign-org customer")
        except vendors.VendorError as exc:
            # Non-enumerable: identical to the unknown-id message (no existence oracle).
            assert str(exc) == f"customer {cust_b} does not exist.", exc
        # org-B scoped caller links org-A's vendor to org-B's OWN customer -> allowed.
        linked, _ = vendors.link_customer(s, vendor_a.id, cust_b, org_id=org_b)
        assert linked is True, "any active vendor is a valid target for the buyer's customer"
    print("  [B] scoped link authority is customer-side (buyer consent), non-enumerable: OK")


def test_either_party_unlink():
    """Either party may sever a link: an admin of the CUSTOMER's org (buyer) OR of the
    VENDOR's org. A scoped caller who administers NEITHER is refused non-enumerably
    (removed=False, the link survives)."""
    org_a, org_b, org_c = _org("UlA"), _org("UlB"), _org("UlC")
    cust_a = _customer("UlCustA", org_id=org_a)
    with Session(engine) as s:
        v = vendors.create(s, "Ul vendor", org_id=org_b)  # vendor in org B
        vendors.link_customer(s, v.id, cust_a)  # cross-org link (install admin)
        vid = v.id
    # A caller scoped to org C (neither the vendor's B nor the customer's A) cannot unlink.
    with Session(engine) as s:
        removed, _ = vendors.unlink_customer(s, vid, cust_a, org_id=org_c)
        assert removed is False, "a caller administering neither party must not unlink"
        assert vendors.is_linked(s, vid, cust_a) is True, "the link must survive"
    # The CUSTOMER's-org admin (org A, the buyer) may sever the foreign vendor's link.
    with Session(engine) as s:
        removed, _ = vendors.unlink_customer(s, vid, cust_a, org_id=org_a)
        assert removed is True, "the customer-org admin (buyer) may sever the link"
        assert vendors.is_linked(s, vid, cust_a) is False
    # The VENDOR's-org admin (org B) may also sever it (re-link first).
    with Session(engine) as s:
        vendors.link_customer(s, vid, cust_a)
        removed, _ = vendors.unlink_customer(s, vid, cust_a, org_id=org_b)
        assert removed is True, "the vendor-org admin may also sever the link"
        assert vendors.is_linked(s, vid, cust_a) is False
    print("  [B] either-party unlink: neither refused; customer-org + vendor-org may sever: OK")


# ============================================================================
# [C] RATE CARD normalization
# ============================================================================
def test_rate_card_normalization():
    with Session(engine) as s:
        v = vendors.create(s, "Priced")
        # A VALID card stores + normalizes (missing bands -> 0.0, currency upper).
        vendors.set_rate_card(
            s, v.id, {"currency": "eur", "bands": {"new": "0.25", "fuzzy_low": 0.05}}
        )
        card = vendors.rate_card_for(vendors.get(s, v.id))
        assert card["currency"] == "EUR"
        assert card["bands"]["new"] == 0.25
        assert card["bands"]["fuzzy_low"] == 0.05
        assert card["bands"]["exact"] == 0.0, "missing band defaults to 0.0"
        assert set(card["bands"]) == set(vendor_cost.BANDS)
    print("  [C] rate-card normalization (missing -> 0.0, currency upper): OK")


def test_rate_card_rejects_bad_values_h3():
    """H3: a non-finite / negative / non-numeric rate or an unknown band key is REFUSED
    at write time (nothing stored)."""
    with Session(engine) as s:
        v = vendors.create(s, "H3 Vendor")
        vid = v.id
    bad_cards = [
        {"bands": {"new": float("nan")}},
        {"bands": {"new": float("inf")}},
        {"bands": {"new": -0.01}},
        {"bands": {"new": "not-a-number"}},
        {"bands": {"bogus_band": 0.1}},  # unknown band key refused
        {"bands": "not-an-object"},
    ]
    with Session(engine) as s:
        for bad in bad_cards:
            try:
                vendors.set_rate_card(s, vid, bad)
                raise AssertionError(f"bad rate card must be refused: {bad}")
            except vendors.VendorError:
                pass
        # Nothing was stored (the vendor still has no rate card -> the zero default).
        assert vendors.get(s, vid).rate_card is None
    # A create with a bad card is refused too (nothing persisted).
    with Session(engine) as s:
        try:
            vendors.create(s, "H3 Create", rate_card={"bands": {"new": float("nan")}})
            raise AssertionError("create with a NaN rate card must be refused")
        except vendors.VendorError:
            pass
    print("  [C] H3: nan/inf/negative/non-numeric/unknown-band all refused, nothing stored: OK")


# ============================================================================
# [D] SCOPING ESTIMATE against a seeded TM-banded document
# ============================================================================
def _banded_doc():
    """A document whose segments span every band, incl. a repetition (seg3 == seg0's
    source) and a hard-locked segment (seg4)."""
    with Session(engine) as s:
        p = Project(name=f"Est-{_seq()}", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="e.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        rows = [
            ("Alpha beta gamma", None, None),  # 3w new (billable)
            ("Delta epsilon", 100, None),  # 2w exact (covered)
            ("Zeta eta theta iota", 88, None),  # 4w fuzzy_mid (billable)
            ("Alpha beta gamma", None, None),  # 3w repetition of seg0 (covered)
            ("Kappa", None, "100"),  # 1w locked-100 -> exact (covered)
            ("Lambda mu", 96, None),  # 2w fuzzy_high (billable)
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
        s.commit()
        return d.id


def test_scoping_estimate_bands_and_exclusions():
    doc_id = _banded_doc()
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        v = vendors.create(s, "Estimator", rate_card=RATE_CARD)
        est = vendor_cost.scoping_estimate(s, doc, ["es-ES"], v)
        b = est["bands"]
        # Every band counted correctly.
        assert b["new"]["words"] == 3 and b["new"]["segments"] == 1
        assert b["fuzzy_mid"]["words"] == 4
        assert b["fuzzy_high"]["words"] == 2
        # exact = the pct-100 seg (2w) + the hard-locked seg (1w).
        assert b["exact"]["words"] == 3 and b["exact"]["segments"] == 2
        assert b["repetitions"]["words"] == 3 and b["repetitions"]["segments"] == 1
        # Covered bands are priced 0 (excluded from the billable human-work scope).
        assert b["exact"]["cost"] == 0.0 and b["repetitions"]["cost"] == 0.0
        # Billable = new(3*0.20) + fuzzy_mid(4*0.12) + fuzzy_high(2*0.10) = 1.28.
        assert est["billable_cost"] == 1.28, est
        assert est["billable_words"] == 9
        assert est["covered_words"] == 6  # exact 3 + repetitions 3
        assert est["total_words"] == 15
        assert est["total_cost"] == 1.28  # covered rates are 0
        assert est["segment_count"] == 6
    print("  [D] scoping estimate: bands correct, locked/100%/reps excluded: OK")


def test_estimate_repetition_is_per_locale():
    """The same source across two locales is NOT a repetition — it is two billable units.
    Proven via _sum_estimates (the multi-locale path)."""
    with Session(engine) as s:
        p = Project(name=f"ML-{_seq()}", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="ml.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for loc in ("es-ES", "fr-FR"):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=loc,
                    order_index=0,
                    source_text="One two three",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        v = vendors.create(s, "MLVendor", rate_card=RATE_CARD)
        est = vendor_cost.scoping_estimate(s, s.get(Document, d.id), ["es-ES", "fr-FR"], v)
        # Both locales price their "One two three" as a NEW unit (3 words each), never
        # one repetition across locales.
        assert est["bands"]["new"]["segments"] == 2
        assert est["bands"]["repetitions"]["segments"] == 0
        assert est["bands"]["new"]["words"] == 6
    print("  [D] repetition is per-locale, not cross-locale: OK")


# ============================================================================
# [E] EXTERNAL step-config parity (the service seam web/CLI/api share)
# ============================================================================
def _wf(scope=Scope.GLOBAL, customer_id=None, org_id=None):
    with Session(engine) as s:
        w = Workflow(
            name=f"XW-{_seq()}",
            scope=scope,
            customer_id=customer_id,
            org_id=org_id,
            source_locale="en-US",
            target_locale="es-ES",
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def test_list_vendors_for_picker_union_dedupe_and_link_gate():
    """`list_vendors_for_picker` is the UNION of own-org active vendors + any active vendor
    LINKED to the given customer (cross-org), de-duped. customer_id=None returns own-org
    only; a foreign vendor appears ONLY when linked to that customer."""
    org_a, org_b = _org("PickA"), _org("PickB")
    cust_a = _customer("PickCustA", org_id=org_a)
    with Session(engine) as s:
        own1 = vendors.create(s, "Pick own 1", org_id=org_a)
        own2 = vendors.create(s, "Pick own 2", org_id=org_a)
        foreign = vendors.create(s, "Pick foreign", org_id=org_b)
        foreign_unlinked = vendors.create(s, "Pick foreign unlinked", org_id=org_b)
        # customer_id=None -> own-org only (early return; no foreign vendors).
        base_ids = {
            v.id for v in vendors.list_vendors_for_picker(s, org_id=org_a, customer_id=None)
        }
        assert {own1.id, own2.id} <= base_ids
        assert foreign.id not in base_ids and foreign_unlinked.id not in base_ids
        # Link the foreign vendor (and an own vendor) to cust_a.
        vendors.link_customer(s, foreign.id, cust_a)  # unscoped install admin
        vendors.link_customer(s, own1.id, cust_a)
        picked = vendors.list_vendors_for_picker(s, org_id=org_a, customer_id=cust_a)
        picked_ids = [v.id for v in picked]
        assert foreign.id in picked_ids, "a LINKED foreign vendor must appear for the customer"
        assert foreign_unlinked.id not in picked_ids, "an UNLINKED foreign vendor must not"
        assert picked_ids.count(own1.id) == 1, "own+linked vendor must appear exactly once"
        assert own2.id in picked_ids, "own-org vendors still present"
    print("  [E] picker union + de-dupe + None early-return + link-gated foreign vendor: OK")


def test_validate_step_vendor_global_rings_by_org():
    """The GLOBAL-workflow save-time branch still rings the vendor by the workflow's org
    (closes a vendor-existence oracle) — a foreign vendor is refused. The CUSTOMER-scoped
    branch accepts a cross-org LINKED vendor."""
    org_a, org_b = _org("VSA"), _org("VSB")
    cust_a = _customer("VSCustA", org_id=org_a)
    with Session(engine) as s:
        fid = vendors.create(s, "VS foreign vendor", org_id=org_b).id
    # GLOBAL workflow in org A + a foreign (org B) vendor -> refused at save (org ring).
    gwid = _wf(scope=Scope.GLOBAL, org_id=org_a)
    with Session(engine) as s:
        try:
            wf.add_step(s, gwid, name="Ext", step_type=StepType.EXTERNAL, vendor_id=fid)
            raise AssertionError("a foreign vendor on a GLOBAL wf must be refused at save")
        except wf.WorkflowError:
            pass
    # CUSTOMER-scoped workflow in org A, the foreign vendor LINKED to its customer -> ok.
    with Session(engine) as s:
        vendors.link_customer(s, fid, cust_a)  # cross-org link
    cwid = _wf(scope=Scope.CUSTOMER, customer_id=cust_a, org_id=org_a)
    with Session(engine) as s:
        st = wf.add_step(s, cwid, name="Ext", step_type=StepType.EXTERNAL, vendor_id=fid)
        assert st.vendor_id == fid, "a cross-org LINKED vendor must attach to its customer's wf"
    print("  [E] _validate_step_vendor: GLOBAL rings by org; customer-scoped accepts link: OK")


def test_external_step_add_requires_linked_vendor():
    cid = _customer("XCust")
    with Session(engine) as s:
        v = vendors.create(s, "XVendor")
        vendors.link_customer(s, v.id, cid)
        vid = v.id
    # GLOBAL workflow: a selectable vendor is enough at save time (the run-time branch
    # re-checks the customer link against the doc's actual customer).
    gwid = _wf()
    with Session(engine) as s:
        st = wf.add_step(
            s,
            gwid,
            name="Send out",
            step_type=StepType.EXTERNAL,
            vendor_id=vid,
            external_wait_hours=48,
        )
        assert st.vendor_id == vid and st.external_wait_hours == 48
        assert st.step_type == StepType.EXTERNAL
    # No vendor -> refused.
    with Session(engine) as s:
        try:
            wf.add_step(s, gwid, name="No vendor", step_type=StepType.EXTERNAL)
            raise AssertionError("external step with no vendor must be refused")
        except wf.WorkflowError:
            pass
    # CUSTOMER-scoped workflow: the vendor MUST be linked to that customer.
    other = _customer("Unlinked")
    cwid = _wf(scope=Scope.CUSTOMER, customer_id=other)
    with Session(engine) as s:
        try:
            wf.add_step(s, cwid, name="Wrong cust", step_type=StepType.EXTERNAL, vendor_id=vid)
            raise AssertionError("vendor not linked to the wf's customer must be refused")
        except wf.WorkflowError:
            pass
    print("  [E] external add-step: vendor required + customer-scoped link enforced: OK")


def test_vendor_id_is_dead_on_non_external_steps():
    with Session(engine) as s:
        v = vendors.create(s, "DeadFieldVendor")
        vid = v.id
    wid = _wf()
    # A vendor_id on an llm_agent step is refused (dead field), not silently accepted.
    with Session(engine) as s:
        try:
            wf.add_step(
                s,
                wid,
                name="Translate",
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="{text}",
                vendor_id=vid,
            )
            raise AssertionError("vendor_id on an llm_agent step must be refused")
        except wf.WorkflowError:
            pass
    print("  [E] vendor_id is a dead field on non-external steps: OK")


def test_m6_malformed_ids_do_not_500():
    """M6: a hostile 10**30 vendor_id / external_wait_hours must be a CLEAN error, never a
    raw OverflowError -> 500 at DB commit."""
    wid = _wf()
    huge = "1" + "0" * 30
    with Session(engine) as s:
        # A huge vendor_id coerces to None -> the external step is refused for "no vendor",
        # a clean WorkflowError (not an OverflowError).
        try:
            wf.add_step(s, wid, name="Huge vendor", step_type=StepType.EXTERNAL, vendor_id=huge)
            raise AssertionError("expected a WorkflowError (no valid vendor)")
        except wf.WorkflowError:
            pass
    # A huge external_wait_hours coerces to None (inherit) -> no crash on a valid vendor.
    cid = _customer("M6Cust")
    with Session(engine) as s:
        v = vendors.create(s, "M6Vendor")
        vendors.link_customer(s, v.id, cid)
        vid = v.id
    gwid = _wf()
    with Session(engine) as s:
        st = wf.add_step(
            s, gwid, name="Ok", step_type=StepType.EXTERNAL, vendor_id=vid, external_wait_hours=huge
        )
        assert st.external_wait_hours is None, "an absurd wait must coerce to None (inherit)"
    print("  [E] M6: malformed huge vendor_id / wait_hours are clean, never a 500: OK")


def test_type_switch_clears_vendor():
    cid = _customer("SwitchCust")
    with Session(engine) as s:
        v = vendors.create(s, "SwitchVendor")
        vendors.link_customer(s, v.id, cid)
        vid = v.id
    wid = _wf()
    with Session(engine) as s:
        st = wf.add_step(s, wid, name="Ext", step_type=StepType.EXTERNAL, vendor_id=vid)
        sid = st.id
    # Switch away from external -> vendor_id force-cleared (reported).
    with Session(engine) as s:
        report: dict = {}
        wf.update_step(s, sid, step_type=StepType.HUMAN_REVIEW, report=report)
        st = s.get(WorkflowStep, sid)
        assert st.vendor_id is None, "type switch off external must clear vendor_id"
        assert "vendor_id" in report.get("type_switch_cleared", [])
    print("  [E] type switch off external force-clears vendor: OK")


if __name__ == "__main__":
    test_crud_and_secret_never_echoed()
    test_archive_and_delete_fail_closed()
    test_linking_idempotent_and_scoped()
    test_cross_org_link_allowed()
    test_cross_org_many_to_many_and_dispatch_resolution()
    test_scoped_link_authority_is_customer_side()
    test_either_party_unlink()
    test_rate_card_normalization()
    test_rate_card_rejects_bad_values_h3()
    test_scoping_estimate_bands_and_exclusions()
    test_estimate_repetition_is_per_locale()
    test_list_vendors_for_picker_union_dedupe_and_link_gate()
    test_validate_step_vendor_global_rings_by_org()
    test_external_step_add_requires_linked_vendor()
    test_vendor_id_is_dead_on_non_external_steps()
    test_m6_malformed_ids_do_not_500()
    test_type_switch_clears_vendor()
    print("verify_vendors: all OK")
