"""Vendors WEB surface (/vendors) — a small hermetic TestClient walk over the router
(app/web/vendors.py) that Wave A adds. Offline + auth-OFF (the single-operator dev
default), so _admin_block is a no-op and no login/CSRF is needed — the same posture
every other offline web verify runs under.

Proves the thin web layer end to end:
  * POST /vendors creates a vendor; GET /vendors lists it.
  * GET /vendors/{id} renders the detail page.
  * POST /vendors/{id}/rate-card stores a per-band MSA rate card.
  * POST /vendors/{id}/secret stores write-only credentials, and the plaintext NEVER
    appears in ANY response body (list, detail, or the redirect target).
  * POST /vendors/{id}/link-customer links a seeded customer and the detail page shows it.

ASCII-only output; standalone one-process-per-suite script.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_vendorsweb_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    Role,
    RunStatus,
    TransMode,
    User,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import security, settings, vendors
from tests._csrf import csrf_for

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


SECRET_SIGNING = "sup3r-secret-signing-value-XYZ"
SECRET_TOKEN = "auth-token-plaintext-ABC123"


def _customer(name="WebCust") -> int:
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _bodies(client, vid):
    """Every response body a secret could plausibly leak into."""
    return [client.get("/vendors").text, client.get(f"/vendors/{vid}").text]


def main():
    c = TestClient(app)

    # --- create + list -------------------------------------------------------
    r = c.post(
        "/vendors",
        data={
            "name": "Acme Loc",
            "description": "MSA vendor",
            "endpoint_url": "https://vendor.example.com/intake",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    dest = r.headers["location"]
    assert dest.startswith("/vendors/") and dest.endswith("?saved=1"), dest
    vid = int(dest.split("/vendors/")[1].split("?")[0])

    r = c.get("/vendors")
    assert r.status_code == 200, r.status_code
    assert "Acme Loc" in r.text, "created vendor not listed"

    # --- detail --------------------------------------------------------------
    r = c.get(f"/vendors/{vid}")
    assert r.status_code == 200, r.status_code
    assert "Acme Loc" in r.text and "MSA rate card" in r.text, "detail page malformed"

    # --- rate card -----------------------------------------------------------
    r = c.post(
        f"/vendors/{vid}/rate-card",
        data={
            "currency": "USD",
            "band_new": "0.20",
            "band_fuzzy_high": "0.10",
            "band_fuzzy_mid": "0.12",
            "band_fuzzy_low": "0.08",
            "band_exact": "0.0",
            "band_context": "0.0",
            "band_repetitions": "0.0",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        v = s.get(Vendor, vid)
        card = vendors.rate_card_for(v)
        assert card["currency"] == "USD", card
        assert abs(card["bands"]["new"] - 0.20) < 1e-9, card["bands"]
        assert abs(card["bands"]["fuzzy_mid"] - 0.12) < 1e-9, card["bands"]

    # --- link a customer -----------------------------------------------------
    cust_id = _customer()
    r = c.post(
        f"/vendors/{vid}/link-customer", data={"customer_id": str(cust_id)}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        assert vendors.is_linked(s, vid, cust_id), "customer link not persisted"
    r = c.get(f"/vendors/{vid}")
    assert "WebCust" in r.text, "linked customer not shown on detail page"

    # --- write-only secret: NEVER echoed ------------------------------------
    r = c.post(
        f"/vendors/{vid}/secret",
        data={"signing_secret": SECRET_SIGNING, "auth_token": SECRET_TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        v = s.get(Vendor, vid)
        assert vendors.has_secret(v), "secret not stored"
        blob = vendors.secret_dict(v)
        assert blob.get("signing_secret") == SECRET_SIGNING, "signing secret round-trip"
        assert blob.get("auth_token") == SECRET_TOKEN, "auth token round-trip"
    for body in _bodies(c, vid):
        assert SECRET_SIGNING not in body, "signing secret leaked into a response body"
        assert SECRET_TOKEN not in body, "auth token leaked into a response body"
    # The detail page shows only the presence flag, never the value.
    detail = c.get(f"/vendors/{vid}").text
    assert "Credentials are set" in detail, "has_secret presence flag missing"

    print("verify_vendors_web OK")


def test_customer_panel_link_unlink_auth_off():
    """CUSTOMER-360 "Connected vendors" panel end to end, auth OFF (single-operator dev
    default — _admin_block is a no-op, so the admin-gated panel renders). Covers the panel
    render + dropdown offering, link via the new POST, idempotent re-link, unlink, and the
    archived-vendor rules (retired from the dropdown, still unlinkable)."""
    c = TestClient(app)
    cid = _customer("PanelCust")

    # An ACTIVE own vendor is offered in the connect dropdown; an ARCHIVED one is not.
    with Session(engine) as s:
        pick_vid = vendors.create(s, f"PanelPick-{_seq()}").id
        arch_name = f"PanelArchived-{_seq()}"
        arch_vid = vendors.create(s, arch_name).id
        vendors.archive(s, arch_vid)

    page = c.get(f"/customers/{cid}").text
    assert "Connected vendors" in page, "the vendors panel did not render for an admin"
    assert "PanelPick-" in page, "an active own vendor is not offered in the connect dropdown"
    assert arch_name not in page, "an archived vendor must not be offered in the dropdown"

    # LINK an own-org vendor via the new POST.
    r = c.post(
        f"/customers/{cid}/link-vendor", data={"vendor_pick": str(pick_vid)}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert vendors.is_linked(s, pick_vid, cid), "own-org link not persisted"
    page = c.get(f"/customers/{cid}").text
    assert f'href="/vendors/{pick_vid}"' in page, (
        "a linked in-ring vendor should link to its detail page"
    )

    # IDEMPOTENT re-link: the same pick again is a clean saved=1, not an error.
    r = c.post(
        f"/customers/{cid}/link-vendor", data={"vendor_pick": str(pick_vid)}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], (
        "idempotent re-link must not error",
        r.headers.get("location"),
    )

    # UNLINK an own-org vendor via the new POST.
    r = c.post(
        f"/customers/{cid}/unlink-vendor", data={"vendor_id": str(pick_vid)}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert not vendors.is_linked(s, pick_vid, cid), "unlink did not sever the link"

    # ARCHIVED vendor is STILL UNLINKABLE: link one active, archive it, unlink it.
    with Session(engine) as s:
        al_vid = vendors.create(s, f"PanelArchLinked-{_seq()}").id
    r = c.post(
        f"/customers/{cid}/link-vendor", data={"vendor_pick": str(al_vid)}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        vendors.archive(s, al_vid)
    page = c.get(f"/customers/{cid}").text
    assert "(archived)" in page, "an archived-but-linked vendor should be flagged archived"
    r = c.post(
        f"/customers/{cid}/unlink-vendor", data={"vendor_id": str(al_vid)}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert not vendors.is_linked(s, al_vid, cid), "an archived vendor must be unlinkable"

    # F1: submitting BOTH a dropdown pick and a DIFFERING free-text id is refused (the
    # typed id must not silently override the pick, the review probe).
    with Session(engine) as s:
        a_vid = vendors.create(s, f"PanelBoth-A-{_seq()}").id
        b_vid = vendors.create(s, f"PanelBoth-B-{_seq()}").id
    r = c.post(
        f"/customers/{cid}/link-vendor",
        data={"vendor_pick": str(a_vid), "vendor_id": str(b_vid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], (
        "both-fields-differ must be refused",
        r.headers.get("location"),
    )
    with Session(engine) as s:
        assert not vendors.is_linked(s, a_vid, cid) and not vendors.is_linked(s, b_vid, cid), (
            "an ambiguous both-fields link must create nothing"
        )
    # ...but the SAME id in both fields is not a conflict.
    r = c.post(
        f"/customers/{cid}/link-vendor",
        data={"vendor_pick": str(a_vid), "vendor_id": str(a_vid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert vendors.is_linked(s, a_vid, cid), "matching both-fields link should succeed"

    # F2: once the customer is archived the connect FORM is hidden and the POST is refused
    # server-side (forged-POST guard), while unlink stays available (done last — archives cid).
    from app.models import _utcnow

    with Session(engine) as s:
        row = s.get(Customer, cid)
        row.archived_at = _utcnow()
        s.add(row)
        s.commit()
    page = c.get(f"/customers/{cid}").text
    assert "Connect vendor" not in page, "the connect form must be hidden on an archived customer"
    with Session(engine) as s:
        arch_target = vendors.create(s, f"PanelArchCust-{_seq()}").id
    r = c.post(
        f"/customers/{cid}/link-vendor",
        data={"vendor_pick": str(arch_target)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], (
        "a link onto an archived customer must be refused",
        r.headers.get("location"),
    )
    with Session(engine) as s:
        assert not vendors.is_linked(s, arch_target, cid), "link created on an archived customer"
    print(
        "  customer-360 vendors panel: link/unlink/idempotent + archived rules + "
        "both-fields guard + archived-customer guard (auth off): OK"
    )


def _open_legs(customer_id, vendor_id, statuses, *, org_id=None):
    """Create a minimal run for `customer_id` and one VendorDispatch leg per status in
    `statuses`, all stamped (vendor_id, customer_id) — the buyer side. Returns the run id.
    Used to exercise the panel's per-vendor OPEN-LEGS fact (incl. Wave E QUOTED)."""
    with Session(engine) as s:
        p = Project(
            name=f"VP-{_seq()}", source_locale="en-US", customer_id=customer_id, org_id=org_id
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        w = Workflow(
            name=f"VW-{_seq()}",
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",
            org_id=org_id,
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        run = WorkflowRun(
            workflow_id=w.id, document_id=d.id, status=RunStatus.COMPLETED, target_locale="es-ES"
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        for st in statuses:
            s.add(
                VendorDispatch(
                    vendor_id=vendor_id, workflow_run_id=run.id, status=st, customer_id=customer_id
                )
            )
        s.commit()
        return run.id


def test_customer_panel_vendor_facts():
    """The panel's per-vendor FACTS: open-legs count (Wave E-aware) + MSA/cost-model
    presence, both for IN-RING vendors. Auth OFF. Proves the count includes a Wave E
    QUOTED leg and excludes terminal legs, is scoped to THIS customer, and that the MSA
    column reflects rate-card presence."""
    c = TestClient(app)
    cid = _customer(f"FactsCust-{_seq()}")
    other_cid = _customer(f"FactsOther-{_seq()}")

    # A vendor WITH an MSA rate card, linked to the customer.
    with Session(engine) as s:
        carded = vendors.create(
            s, f"FactsCarded-{_seq()}", rate_card={"currency": "USD", "bands": {"new": "0.20"}}
        ).id
        bare = vendors.create(s, f"FactsBare-{_seq()}").id
        vendors.link_customer(s, carded, cid)
        vendors.link_customer(s, bare, cid)

    # Open legs for `carded`: 1 DISPATCHED + 1 QUOTED (Wave E, OPEN) + 1 RETRY_WAIT = 3 open;
    # a RETURNED + a REJECTED (Wave E, terminal) + a CANCELLED are all excluded.
    _open_legs(
        cid,
        carded,
        [
            VendorDispatchStatus.DISPATCHED,
            VendorDispatchStatus.QUOTED,
            VendorDispatchStatus.RETRY_WAIT,
            VendorDispatchStatus.RETURNED,
            VendorDispatchStatus.REJECTED,
            VendorDispatchStatus.CANCELLED,
        ],
    )
    # A leg for a DIFFERENT customer must NOT inflate this customer's count.
    _open_legs(other_cid, carded, [VendorDispatchStatus.DISPATCHED, VendorDispatchStatus.QUOTED])

    # Service read-model: 3 open for THIS customer, MSA present on `carded`, absent on `bare`.
    with Session(engine) as s:
        rows = {r["id"]: r for r in vendors.linked_vendor_views(s, cid)}
        assert rows[carded]["open_legs"] == 3, (
            "open-legs must count DISPATCHED+QUOTED+RETRY_WAIT for THIS customer only, "
            "excluding RETURNED/REJECTED/CANCELLED",
            rows[carded]["open_legs"],
        )
        assert rows[carded]["has_rate_card"] is True, "carded vendor must show an MSA on file"
        assert rows[bare]["open_legs"] == 0, "a vendor with no legs must read 0 open"
        assert rows[bare]["has_rate_card"] is False, "bare vendor must show no MSA"
        # Direct Wave E pin: open_leg_count excludes REJECTED, includes QUOTED.
        assert vendors.open_leg_count(s, carded, customer_id=cid) == 3, "count seam drifted"
        # F2: fail CLOSED on an unparseable customer_id — never drop the scope and count
        # across every customer (a cross-tenant count leak).
        assert vendors.open_leg_count(s, carded, customer_id="abc") == 0, (
            "an unparseable customer_id must fail closed, not count across tenants"
        )

    # HTTP render: the panel shows the Open legs + MSA columns and the count for `carded`.
    page = c.get(f"/customers/{cid}").text
    assert "Open legs" in page and ">MSA<" in page, "the facts columns did not render"
    assert "On file" in page, "the MSA-present marker did not render for the carded vendor"
    print(
        "  customer-360 vendors panel facts: open-legs (Wave E QUOTED counted, terminal "
        "excluded, customer-scoped) + MSA presence: OK"
    )


def _org_admin_client(email, org_id):
    """A logged-in ADMIN web session (auth ON, multi-org ON) scoped to `org_id`."""
    return _org_user_client(email, org_id, Role.ADMIN)


def _org_user_client(email, org_id, role):
    """A logged-in web session (auth ON, multi-org ON) scoped to `org_id` with `role`."""
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    u = security.create_user(
        email, email, role, "vw-scope-password-123", org_id=org_id
    )  # #181: created IN its org
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.org_id = org_id
        s.add(row)
        s.commit()
    c = TestClient(app)
    csrf_for(c)  # login POST carries a CSRF token
    r = c.post(
        "/auth/login",
        data={"email": email, "password": "vw-scope-password-123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    csrf_for(c)  # login rotates the session -> refresh the token
    return c


def test_customer_panel_cross_org_and_gating():
    """CUSTOMER-360 panel under auth ON + multi-org ON: a scoped org-B admin links a
    FOREIGN org-A vendor to org-B's OWN customer BY ID (the only web path that can initiate
    a cross-org link), the panel then shows a ring-safe minimal row (name+status, no
    endpoint/secret, no detail link), the either-party unlink works for the buyer admin, a
    foreign CUSTOMER is refused non-enumerably, and a non-admin is blocked from both the
    panel and the POST."""
    FOREIGN_HOST = "cp-foreign-secret-host.example"
    FOREIGN_NAME = f"CP foreign vendor-{_seq()}"
    FOREIGN_DESC = "cp-foreign-description-marker-DESC"
    FOREIGN_SECRET = "cp-foreign-signing-secret-XYZ"
    OWN_NAME = f"CP own vendor-{_seq()}"
    with Session(engine) as s:
        oa, ob = Organization(name=f"CPOrgA-{_seq()}"), Organization(name=f"CPOrgB-{_seq()}")
        s.add(oa)
        s.add(ob)
        s.commit()
        org_a, org_b = oa.id, ob.id
        ca = Customer(name=f"CP CustA-{_seq()}", org_id=org_a)
        cb = Customer(name=f"CP CustB-{_seq()}", org_id=org_b)
        s.add(ca)
        s.add(cb)
        s.commit()
        s.refresh(ca)
        s.refresh(cb)
        cid_a, cid_b = ca.id, cb.id
        # F3: give the foreign vendor a FULL surface (description + secret + rate card) so
        # the ring assertions below can actually catch a has_secret/description/rate leak,
        # not just the endpoint host.
        foreign_vid = vendors.create(
            s,
            FOREIGN_NAME,
            org_id=org_a,
            description=FOREIGN_DESC,
            endpoint_url=f"https://{FOREIGN_HOST}/intake",
            secret={"signing_secret": FOREIGN_SECRET},
            rate_card={"currency": "ZWL", "bands": {"new": "0.99"}},
        ).id
        own_vid = vendors.create(s, OWN_NAME, org_id=org_b).id
    c = _org_admin_client(f"cp-admin-b-{_seq()}@x.com", org_b)

    # CROSS-ORG LINK BY ID: org-B admin connects the FOREIGN vendor to org-B's own customer.
    r = c.post(
        f"/customers/{cid_b}/link-vendor",
        data={"vendor_id": str(foreign_vid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], (
        "cross-org link at the customer route must succeed",
        r.status_code,
        r.headers.get("location"),
    )
    with Session(engine) as s:
        assert vendors.is_linked(s, foreign_vid, cid_b), "cross-org link not persisted"
    # Also link an OWN-org vendor so the in-ring vs out-of-ring rendering can be compared.
    r = c.post(
        f"/customers/{cid_b}/link-vendor",
        data={"vendor_pick": str(own_vid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers

    # F(new): the foreign vendor has REAL operational state (an open leg) and an MSA rate
    # card, yet the panel's per-vendor FACTS (open_legs / has_rate_card) must stay behind
    # the ring — the out-of-ring row omits those keys entirely (never a side channel around
    # the vendor-admin 404 wall), while the in-ring own row carries them.
    _open_legs(
        cid_b,
        foreign_vid,
        [VendorDispatchStatus.DISPATCHED, VendorDispatchStatus.QUOTED],
        org_id=org_b,
    )
    # F1 setup: the OWN vendor is ALSO linked (unscoped, simulating a vendor shared across
    # orgs) to a SEPARATE org-A customer, so its unfiltered to_dict.customer_ids would carry
    # a FOREIGN org's customer id — the panel's in-ring row must filter it out. (A fresh
    # org-A customer, not cid_a, so the foreign-customer-404 assertion below is untouched.)
    with Session(engine) as s:
        fa = Customer(name=f"CP CustA2-{_seq()}", org_id=org_a)
        s.add(fa)
        s.commit()
        s.refresh(fa)
        foreign_cust_a = fa.id
        vendors.link_customer(s, own_vid, foreign_cust_a)  # org_id=None -> unrestricted cross-org
    with Session(engine) as s:
        rows = {r2["id"]: r2 for r2 in vendors.linked_vendor_views(s, cid_b, org_id=org_b)}
        assert rows[foreign_vid].get("out_of_ring") is True, "foreign vendor must be out of ring"
        assert "open_legs" not in rows[foreign_vid], (
            "a foreign vendor's open-legs count must NOT be disclosed"
        )
        assert "has_rate_card" not in rows[foreign_vid], (
            "a foreign vendor's MSA presence must NOT be disclosed"
        )
        assert "open_legs" in rows[own_vid] and "has_rate_card" in rows[own_vid], (
            "an in-ring own vendor must carry the panel facts"
        )
        # F1: the in-ring row's customer_ids is org-filtered — the foreign org-A customer
        # id must NOT leak, while this org's own customer id is present.
        assert foreign_cust_a not in rows[own_vid]["customer_ids"], (
            "an in-ring row leaked a FOREIGN org's customer id via customer_ids"
        )
        assert cid_b in rows[own_vid]["customer_ids"], (
            "the in-ring row dropped this org's own customer id"
        )

    # RING-SAFE PANEL: the foreign row shows name+status but NO endpoint/secret and NO
    # detail link (it would 404); the own row DOES link to its detail page.
    page = c.get(f"/customers/{cid_b}").text
    assert FOREIGN_NAME in page, "the ring-safe foreign row must still show the vendor name"
    assert "(external org)" in page, "the foreign vendor row must be flagged external"
    assert FOREIGN_HOST not in page, "foreign vendor endpoint leaked into the panel HTML"
    # F3: the full foreign surface must stay behind the ring — description, secret, the
    # has_secret presence flag, and the rate card are all absent from the panel HTML.
    assert FOREIGN_DESC not in page, "foreign vendor description leaked into the panel HTML"
    assert FOREIGN_SECRET not in page, "foreign vendor secret leaked into the panel HTML"
    assert "Credentials are set" not in page, "foreign has_secret flag leaked into the panel"
    assert "ZWL" not in page and "0.99" not in page, (
        "foreign vendor rate card leaked into the panel HTML"
    )
    assert f'href="/vendors/{foreign_vid}"' not in page, (
        "a foreign vendor must NOT link to its detail page (it 404s)"
    )
    assert f'href="/vendors/{own_vid}"' in page, "an own-org linked vendor should link out"

    # FOREIGN CUSTOMER refused non-enumerably: org-B admin cannot link against org-A's
    # customer path (the org ring 404s it, matching every sibling customer POST). No link.
    r = c.post(
        f"/customers/{cid_a}/link-vendor",
        data={"vendor_pick": str(own_vid)},
        follow_redirects=False,
    )
    assert r.status_code == 404, ("a foreign customer must be a non-enumerable 404", r.status_code)
    with Session(engine) as s:
        assert not vendors.is_linked(s, own_vid, cid_a), "link leaked onto a foreign customer"

    # EITHER-PARTY UNLINK: the org-B (buyer) admin severs the FOREIGN vendor's link.
    r = c.post(
        f"/customers/{cid_b}/unlink-vendor",
        data={"vendor_id": str(foreign_vid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert not vendors.is_linked(s, foreign_vid, cid_b), (
            "the buyer-side admin's either-party unlink did not sever the cross-org link"
        )

    # NON-ADMIN is blocked: a same-org translator sees no panel and cannot POST. Use a
    # FRESH unlinked vendor so the assertion measures the block, not the admin's earlier link.
    with Session(engine) as s:
        na_vid = vendors.create(s, f"CP na vendor-{_seq()}", org_id=org_b).id
    tc = _org_user_client(f"cp-tr-b-{_seq()}@x.com", org_b, Role.TRANSLATOR)
    page = tc.get(f"/customers/{cid_b}").text
    assert "Connected vendors" not in page, "the vendors panel must be hidden from a non-admin"
    r = tc.post(
        f"/customers/{cid_b}/link-vendor", data={"vendor_pick": str(na_vid)}, follow_redirects=False
    )
    assert r.status_code == 403, ("a non-admin POST must be blocked", r.status_code)
    with Session(engine) as s:
        assert not vendors.is_linked(s, na_vid, cid_b), "non-admin POST still created a link"

    # Reset the gate for hermetic neighbours.
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    print(
        "  customer-360 panel cross-org link by id + ring-safe row + either-party unlink "
        "+ foreign-customer 404 + non-admin blocked: OK"
    )


def test_cross_org_link_unlink_at_web_route():
    """ROUTE-LEVEL PIN (auth ON, multi-org ON): a scoped org-B admin may link a FOREIGN
    org-A vendor to org-B's OWN customer over HTTP (the vendor-ring pre-check on the
    link/unlink routes is gone; the service's customer-side authority governs). Vendor-
    ADMIN routes on that foreign vendor stay 404-walled. Either-party unlink at the route."""
    FOREIGN_HOST = "vwforeign-secret-host.example"
    FOREIGN_NAME = f"VW foreign vendor-{_seq()}"
    with Session(engine) as s:
        oa, ob = Organization(name=f"VWOrgA-{_seq()}"), Organization(name=f"VWOrgB-{_seq()}")
        s.add(oa)
        s.add(ob)
        s.commit()
        org_a, org_b = oa.id, ob.id
        cb = Customer(name=f"VW CustB-{_seq()}", org_id=org_b)
        s.add(cb)
        s.commit()
        s.refresh(cb)
        cid_b = cb.id
        foreign_vid = vendors.create(
            s, FOREIGN_NAME, org_id=org_a, endpoint_url=f"https://{FOREIGN_HOST}/intake"
        ).id
    c = _org_admin_client(f"vw-admin-b-{_seq()}@x.com", org_b)
    # LINK: org-B admin links the FOREIGN vendor to org-B's own customer -> success (saved=1).
    r = c.post(
        f"/vendors/{foreign_vid}/link-customer",
        data={"customer_id": str(cid_b)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], (
        "cross-org link at web route must succeed",
        r.status_code,
        r.headers.get("location"),
    )
    # The redirect body carries NO foreign-vendor config (it is a bare redirect).
    assert FOREIGN_HOST not in r.text, "foreign endpoint leaked in the link redirect body"
    with Session(engine) as s:
        assert vendors.is_linked(s, foreign_vid, cid_b), "cross-org link not persisted"
    # Vendor-ADMIN routes on the foreign vendor stay 404-walled for the org-B caller, and the
    # foreign vendor NEVER appears (name or config) in this caller's management list.
    assert c.get(f"/vendors/{foreign_vid}").status_code == 404, "foreign vendor detail must 404"
    r = c.post(f"/vendors/{foreign_vid}", data={"description": "x"}, follow_redirects=False)
    assert r.status_code == 404, "foreign vendor update must 404-wall"
    listing = c.get("/vendors").text
    assert FOREIGN_HOST not in listing and FOREIGN_NAME not in listing, (
        "the foreign vendor's name/config leaked into the org-B management list"
    )
    # UNLINK either-party: the org-B (buyer) admin severs the foreign vendor's link.
    r = c.post(
        f"/vendors/{foreign_vid}/unlink-customer",
        data={"customer_id": str(cid_b)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    assert FOREIGN_HOST not in r.text, "foreign endpoint leaked in the unlink redirect body"
    with Session(engine) as s:
        assert not vendors.is_linked(s, foreign_vid, cid_b), "either-party unlink did not sever"
    # Reset the gate for hermetic neighbours.
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    print(
        "  cross-org link/unlink succeed at the web route; foreign vendor config never "
        "rendered; vendor-admin 404-walled: OK"
    )


if __name__ == "__main__":
    main()
    test_customer_panel_link_unlink_auth_off()
    test_customer_panel_vendor_facts()
    test_cross_org_link_unlink_at_web_route()
    test_customer_panel_cross_org_and_gating()
    print("verify_vendors_web OK")
