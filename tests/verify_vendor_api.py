"""Vendor /api routes (app/web/api_cli.py) — the thin JSON surface over
app/services/vendors.py (external-processing Wave A).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; the
MockProvider, no network). Drives the real ASGI app through fastapi.testclient with
an authenticated ADMIN lsu_ key (login wall ON, multi-org OFF, so a single admin
clears every RBAC rung the routes gate on: create=manage, mutations=write,
delete=admin), exercising the full CRUD path:

  * POST /api/vendors                      create
  * POST /api/vendors/{vid}                update (TEXT/CONFIG only — the secret was
                                           RETRO-GATED off this docs-scoped route in R3)
  * GET  /api/vendors                      list
  * GET  /api/vendors/{vid}                show
  * POST /api/vendors/{vid}/rate-card      set rate card (normalized)
  * POST /api/vendors/{vid}/link-customer  link (idempotent)
  * POST /api/vendors/{vid}/delete         fail-closed 409 while a step references it,
                                           and a clean 200 on a fresh unreferenced one.

R3 RETRO-GATE (SPEC-full-remote-parity, deliberate hardening): setting the Fernet
signing_secret/auth_token is NO LONGER accepted on the docs-scoped update route — a
docs key must not set a vendor secret (egress-redirect risk). It now lives behind the
admin-gated POST /api/vendors/{vid}/secret (admin scope + remote_admin_enabled), proven
in tests/verify_api_r3_config_credentials.py. This suite asserts the update route
REFUSES the secret fields (400, nothing stored, no leak).

THE LOAD-BEARING ASSERTION: no stored credential value ever appears in ANY response
body on this surface — to_dict carries only a has_secret presence flag. Every response
is run through _no_secret.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_vendorapi_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Customer,
    Organization,
    Role,
    Scope,
    StepType,
    User,
    Workflow,
)
from app.services import apikeys, security, settings, vendors
from app.services import workflows as wf

init_db()

# The credential values the update route stores. NEITHER may ever surface in a body.
SECRET_VALUES = ("topsecret-signing-999", "authtoken-abc-777")


def _app():
    from app.main import app

    return app


_ADMIN_SEQ = [0]


def _admin_headers() -> dict:
    """Login wall ON, multi-org OFF — isolate RBAC from org scoping. An ADMIN token
    clears every rung the vendor routes require (all admin now, H1). Unique per call so
    multiple tests can each mint their own admin without a duplicate-email collision."""
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    _ADMIN_SEQ[0] += 1
    email = f"vendor-admin-{_ADMIN_SEQ[0]}@x.com"
    u = security.create_user(email, "Vendor Admin", Role.ADMIN, "vendor-admin-password-123")
    _row, raw = apikeys.create_key(u.id, f"vendor-api-test-{_ADMIN_SEQ[0]}", scopes="api docs")
    return {"X-Loc-Token": raw}


def _role_headers(role, email) -> dict:
    """A docs+api key for a user of `role` (login wall ON, multi-org OFF)."""
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    u = security.create_user(email, email, role, "role-user-password-123")
    _row, raw = apikeys.create_key(u.id, f"key-{role}", scopes="api docs")
    return {"X-Loc-Token": raw}


def test_h1_vendor_api_is_admin_only():
    """H1: EVERY vendor /api route (reads INCLUDED) is admin-only -- a viewer/translator is
    refused (privilege escalation -> secret exfiltration was the probe). Mirrors the web
    /vendors panel exactly."""
    api = TestClient(_app())
    admin = _admin_headers()
    viewer = _role_headers(Role.VIEWER, "v-view@x.com")
    translator = _role_headers(Role.TRANSLATOR, "v-trans@x.com")
    # A vendor + customer to target (created by the admin).
    vid = api.post(
        "/api/vendors",
        headers=admin,
        json={"name": "H1 Vendor", "endpoint_url": "https://v.example/intake"},
    ).json()["vendor"]["id"]
    with Session(engine) as s:
        c = Customer(name="H1 Cust")
        s.add(c)
        s.commit()
        s.refresh(c)
        cid = c.id
    routes = [
        ("get", "/api/vendors", None),
        ("post", "/api/vendors", {"name": "x"}),
        ("get", f"/api/vendors/{vid}", None),
        ("post", f"/api/vendors/{vid}", {"description": "x"}),
        ("post", f"/api/vendors/{vid}/rate-card", {"bands": {"new": 0.1}}),
        ("post", f"/api/vendors/{vid}/link-customer", {"customer_id": cid}),
        ("post", f"/api/vendors/{vid}/unlink-customer", {"customer_id": cid}),
        ("post", f"/api/vendors/{vid}/delete", None),
    ]
    for who, hdr in (("viewer", viewer), ("translator", translator)):
        for method, path, body in routes:
            fn = api.get if method == "get" else api.post
            r = fn(path, headers=hdr, json=body) if body is not None else fn(path, headers=hdr)
            assert r.status_code == 403, (
                f"{who} reached {method.upper()} {path} (status {r.status_code}) -- must be 403"
            )
    # Admin CAN read + mutate every route (sanity that the gate is not a blanket block).
    assert api.get("/api/vendors", headers=admin).status_code == 200
    assert api.get(f"/api/vendors/{vid}", headers=admin).status_code == 200
    assert (
        api.post(f"/api/vendors/{vid}", headers=admin, json={"description": "ok"}).status_code
        == 200
    )
    print("  H1: every vendor /api route (reads included) is admin-only; viewer+translator 403: OK")


def _org_admin_key(email, org_id) -> dict:
    """An api+docs ADMIN key for a user placed in `org_id` (multi-org ON scoped caller)."""
    u = security.create_user(
        email, email, Role.ADMIN, "vendor-scope-pw-123", org_id=org_id
    )  # #181: created IN its org
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.org_id = org_id
        s.add(row)
        s.commit()
    _row, raw = apikeys.create_key(u.id, f"scoped-{email}", scopes="api docs")
    return {"X-Loc-Token": raw}


def test_cross_org_link_unlink_at_route_layer():
    """ROUTE-LEVEL PIN (multi-org ON): a scoped org-B admin may link a FOREIGN org-A vendor
    to org-B's OWN customer over HTTP -- the vendor-ring pre-check on the /api link/unlink
    routes is gone; the service's customer-side (buyer) authority governs. Vendor-ADMIN /api
    routes on that foreign vendor stay 404-walled. Either-party unlink at the route too."""
    api = TestClient(_app())
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    _SECRET_KEYS = ("endpoint_url", "config", "has_secret", "rate_card")
    with Session(engine) as s:
        oa, ob = Organization(name="VApiOrgA"), Organization(name="VApiOrgB")
        s.add(oa)
        s.add(ob)
        s.commit()
        org_a, org_b = oa.id, ob.id
        cb = Customer(name="VApi CustB", org_id=org_b)
        ca = Customer(name="VApi CustA", org_id=org_a)  # a FOREIGN (org-A) customer
        s.add(cb)
        s.add(ca)
        s.commit()
        s.refresh(cb)
        s.refresh(ca)
        cid_b, cid_a = cb.id, ca.id
        # The foreign vendor carries a real endpoint + secret so the FULL view would leak
        # them; already linked to an org-A customer so its customer_ids span two orgs.
        foreign = vendors.create(
            s,
            "VApi foreign vendor",
            org_id=org_a,
            endpoint_url="https://foreign.example/intake",
            secret={"signing_secret": "foreign-shh"},
        )
        foreign_vid = foreign.id
        vendors.link_customer(s, foreign_vid, cid_a)  # install-admin, unscoped
        # An OWN org-B vendor for the same-org (full-shape) control.
        own_b = vendors.create(
            s, "VApi own-B vendor", org_id=org_b, endpoint_url="https://ownb.example/intake"
        )
        own_b_vid = own_b.id
    admin_b = _org_admin_key("vapi-admin-b@x.com", org_b)

    # LINK (cross-org): org-B admin links the FOREIGN vendor to org-B's own customer -> 200.
    r = api.post(
        f"/api/vendors/{foreign_vid}/link-customer", headers=admin_b, json={"customer_id": cid_b}
    )
    assert r.status_code == 200, ("cross-org link at /api route must succeed", r.text)
    j = r.json()
    assert j["linked"] is True
    view = j["vendor"]
    # MINIMAL ring-safe view: no connection/config/secret/rate-card leaked.
    for k in _SECRET_KEYS:
        assert k not in view, f"cross-org link view leaked {k!r}: {view}"
    assert view["id"] == foreign_vid and view.get("out_of_ring") is True
    # customer_ids FILTERED to the caller's org: cid_b present, the foreign cid_a NOT.
    assert view["customer_ids"] == [cid_b], ("customer_ids must be org-filtered", view)
    assert "foreign.example" not in r.text and "foreign-shh" not in r.text, r.text

    # LINK (same-org control): org-B admin links its OWN vendor -> FULL byte-identical shape.
    r = api.post(
        f"/api/vendors/{own_b_vid}/link-customer", headers=admin_b, json={"customer_id": cid_b}
    )
    assert r.status_code == 200, r.text
    own_view = r.json()["vendor"]
    for k in _SECRET_KEYS:
        assert k in own_view, f"same-org link view must keep {k!r} (full shape)"
    assert own_view["endpoint_url"] == "https://ownb.example/intake"
    assert cid_b in own_view["customer_ids"] and own_view.get("out_of_ring") is None

    # Vendor-ADMIN /api routes on the foreign vendor stay 404-walled for the org-B caller.
    assert api.get(f"/api/vendors/{foreign_vid}", headers=admin_b).status_code == 404, (
        "foreign vendor detail must 404"
    )
    assert (
        api.post(
            f"/api/vendors/{foreign_vid}", headers=admin_b, json={"description": "x"}
        ).status_code
        == 404
    ), "foreign vendor update must 404-wall"
    assert (
        api.post(
            f"/api/vendors/{foreign_vid}/rate-card", headers=admin_b, json={"bands": {"new": 0.1}}
        ).status_code
        == 404
    ), "foreign vendor rate-card must 404-wall"

    # UNLINK either-party: the org-B (buyer) admin severs the foreign vendor's link -> 200,
    # and the response is the SAME minimal, org-filtered shape (now no org-B customers left).
    r = api.post(
        f"/api/vendors/{foreign_vid}/unlink-customer", headers=admin_b, json={"customer_id": cid_b}
    )
    assert r.status_code == 200 and r.json()["removed"] is True, r.text
    uview = r.json()["vendor"]
    for k in _SECRET_KEYS:
        assert k not in uview, f"cross-org unlink view leaked {k!r}: {uview}"
    assert uview["customer_ids"] == [], ("unlink view must stay org-filtered", uview)
    assert "foreign.example" not in r.text, r.text
    # Reset the gate for hermetic neighbours.
    settings.set_many({"multi_org_enabled": "0"})
    settings.invalidate_cache()
    print(
        "  cross-org link/unlink: minimal org-filtered view; same-org full shape; "
        "foreign vendor-admin 404-walled: OK"
    )


def _no_secret(r) -> None:
    """No stored credential value may appear in this response body."""
    blob = r.text
    for v in SECRET_VALUES:
        assert v not in blob, f"SECRET LEAKED into a response body: {r.request.url}"


def test_vendor_api_crud_ratecard_link_and_fail_closed_delete():
    api = TestClient(_app())
    h = _admin_headers()

    # A customer to link to.
    with Session(engine) as s:
        c = Customer(name="Vendor API Cust")
        s.add(c)
        s.commit()
        s.refresh(c)
        cid = c.id

    # CREATE (only name required).
    r = api.post(
        "/api/vendors",
        headers=h,
        json={
            "name": "Acme LSP",
            "description": "d",
            "endpoint_url": "https://vendor.example/intake",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    vid = body["vendor"]["id"]
    assert body["vendor"]["has_secret"] is False
    assert body["vendor"]["endpoint_url"] == "https://vendor.example/intake"
    _no_secret(r)

    # UPDATE applies a text change; the secret fields are RETRO-GATED OFF this route.
    r = api.post("/api/vendors/%d" % vid, headers=h, json={"description": "updated"})  # noqa: UP031
    assert r.status_code == 200, r.text
    assert r.json()["vendor"]["description"] == "updated"
    _no_secret(r)
    # R3 RETRO-GATE: presenting signing_secret/auth_token here is a clean 400 that
    # names the admin-gated secret route; NOTHING is stored and nothing leaks.
    r = api.post(
        "/api/vendors/%d" % vid,  # noqa: UP031
        headers=h,
        json={
            "description": "should-not-apply",
            "signing_secret": SECRET_VALUES[0],
            "auth_token": SECRET_VALUES[1],
        },
    )
    assert r.status_code == 400, ("secret fields must be refused on the update route", r.text)
    assert "/secret" in r.json().get("error", ""), r.text
    _no_secret(r)
    # No secret was stored (the vendor still has none), and the sibling text change in
    # the same refused body did NOT apply.
    with Session(engine) as s:
        assert vendors.secret_dict(vendors.get(s, vid)) == {}, "a refused update stored a secret"
        assert vendors.get(s, vid).description == "updated", "a refused update applied text"

    # LIST.
    r = api.get("/api/vendors", headers=h)
    assert r.status_code == 200, r.text
    assert any(v["id"] == vid for v in r.json()["vendors"])
    _no_secret(r)

    # SHOW.
    r = api.get("/api/vendors/%d" % vid, headers=h)  # noqa: UP031
    assert r.status_code == 200, r.text
    # has_secret is False: the secret was never set here (retro-gated off this route).
    assert r.json()["vendor"]["id"] == vid and r.json()["vendor"]["has_secret"] is False
    _no_secret(r)

    # RATE CARD (normalized: currency upper-cased, missing bands -> 0.0).
    r = api.post(
        "/api/vendors/%d/rate-card" % vid,  # noqa: UP031
        headers=h,
        json={"currency": "eur", "bands": {"new": 0.2}},
    )
    assert r.status_code == 200, r.text
    card = r.json()["vendor"]["rate_card"]
    assert card["currency"] == "EUR" and card["bands"]["new"] == 0.2
    _no_secret(r)
    # H3: a negative / unknown-band / literal-NaN rate card is REFUSED (400), nothing
    # stored. (NaN is sent as a RAW JSON body -- an attacker's parser emits the `NaN`
    # literal, which Starlette's json.loads accepts -- since httpx refuses to serialize a
    # float NaN client-side.)
    r = api.post("/api/vendors/%d/rate-card" % vid, headers=h, json={"bands": {"new": -0.5}})  # noqa: UP031
    assert r.status_code == 400, r.text
    r = api.post("/api/vendors/%d/rate-card" % vid, headers=h, json={"bands": {"bogus_band": 0.1}})  # noqa: UP031
    assert r.status_code == 400, r.text
    r = api.post(
        "/api/vendors/%d/rate-card" % vid,  # noqa: UP031
        headers={**h, "content-type": "application/json"},
        content='{"bands": {"new": NaN}}',
    )
    assert r.status_code == 400, ("literal NaN over the wire must be refused", r.text)
    # M2: an http:// endpoint is refused at save (400), not silently stored.
    r = api.post(
        "/api/vendors/%d" % vid,  # noqa: UP031
        headers=h,
        json={"endpoint_url": "http://vendor.example/intake"},  # noqa: RUF100, UP031
    )
    assert r.status_code == 400, r.text

    # LINK CUSTOMER (idempotent).
    r = api.post("/api/vendors/%d/link-customer" % vid, headers=h, json={"customer_id": cid})  # noqa: UP031
    assert r.status_code == 200, r.text
    assert r.json()["linked"] is True and cid in r.json()["vendor"]["customer_ids"]
    r = api.post("/api/vendors/%d/link-customer" % vid, headers=h, json={"customer_id": cid})  # noqa: UP031
    assert r.json()["linked"] is False, "second link must be a no-op"
    _no_secret(r)

    # DELETE fails closed (409) while a workflow step selects the vendor.
    with Session(engine) as s:
        w = Workflow(name="VendorWF", scope=Scope.GLOBAL)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(s, w.id, name="Send out", step_type=StepType.EXTERNAL, vendor_id=vid)
    r = api.post("/api/vendors/%d/delete" % vid, headers=h)  # noqa: UP031
    assert r.status_code == 409, r.text
    assert r.json()["ok"] is False
    assert r.json()["references"]["workflow_steps"] == 1

    # DELETE a fresh unreferenced vendor -> 200, then it is gone (404).
    fresh = api.post("/api/vendors", headers=h, json={"name": "Throwaway"}).json()["vendor"]["id"]
    r = api.post("/api/vendors/%d/delete" % fresh, headers=h)  # noqa: UP031
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert api.get("/api/vendors/%d" % fresh, headers=h).status_code == 404  # noqa: UP031
    print("  vendor /api CRUD + rate-card + link + fail-closed delete, no secret leak: OK")


if __name__ == "__main__":
    test_h1_vendor_api_is_admin_only()
    test_vendor_api_crud_ratecard_link_and_fail_closed_delete()
    test_cross_org_link_unlink_at_route_layer()
    print("verify_vendor_api: all OK")
