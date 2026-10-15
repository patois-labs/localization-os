"""#490: TM bulk delete is scoped to ONE customer and refuses malformed filters.

Before the fix the web route used the caller's whole project ring (the posted
`customer` only built the redirect), so a bulk delete on one customer's TM page
deleted every customer's matching rows; the API silently dropped a malformed filter
(widening the delete). Surfaces covered: web POST /tm/bulk-delete, POST
/api/tm/bulk-delete (the MCP tool is generated from the API), CLI `tm delete`.

HERMETIC: clears provider keys before importing app. ASCII-only.
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmbds_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, SegmentTM
from app.services import apikeys, content, security, settings

_PW = "tmbds-pw-123456"


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _seed(s, pid, tgt, text):
    s.add(
        SegmentTM(
            project_id=pid,
            source_locale="en-US",
            target_locale=tgt,
            source_text=text,
            target_text="x",
            source_hash=f"{pid}|{tgt}|{text}",
        )
    )
    s.commit()


def _fixture(tag):
    """Two customers, one project each, 2 de-DE + 1 fr-FR TM rows per project."""
    with Session(engine) as s:
        ca = content.customer_create(s, f"A{tag}")
        cb = content.customer_create(s, f"B{tag}")
        pa = content.project_create(s, f"PA{tag}", customer_id=str(ca.id), target_locales="de-DE")
        pb = content.project_create(s, f"PB{tag}", customer_id=str(cb.id), target_locales="de-DE")
        for pid in (pa.id, pb.id):
            _seed(s, pid, "de-DE", f"one {tag}")
            _seed(s, pid, "de-DE", f"two {tag}")
            _seed(s, pid, "fr-FR", f"three {tag}")
        return ca.id, cb.id, pa.id, pb.id


def _count(pid, tgt=None):
    with Session(engine) as s:
        stmt = select(SegmentTM).where(SegmentTM.project_id == pid)
        if tgt:
            stmt = stmt.where(SegmentTM.target_locale == tgt)
        return len(s.exec(stmt).all())


def test_web_scoped_to_customer():
    a, b, pa, pb = _fixture("w")
    c = TestClient(app)
    form = {"target_locale": "de-DE", "confirm": "1"}
    # No customer on a multi-customer install: refused, nothing deleted.
    r = c.post("/tm/bulk-delete", data=form, follow_redirects=False)
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert _count(pa, "de-DE") == 2 and _count(pb, "de-DE") == 2
    # A nonexistent customer: refused.
    r = c.post("/tm/bulk-delete", data={**form, "customer": "999999"}, follow_redirects=False)
    assert r.status_code == 400 and _count(pb, "de-DE") == 2
    # Preview counts only A's rows.
    r = c.post(
        "/tm/bulk-delete",
        data={"target_locale": "de-DE", "customer": str(a)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "confirm_n=2" in r.headers["location"], r.headers
    # Real delete on A's page touches A only.
    r = c.post("/tm/bulk-delete", data={**form, "customer": str(a)}, follow_redirects=False)
    assert r.status_code == 303 and "bulk_deleted=2" in r.headers["location"], r.headers
    assert _count(pa, "de-DE") == 0 and _count(pa, "fr-FR") == 1
    assert _count(pb, "de-DE") == 2 and _count(pb, "fr-FR") == 1, "B's TM must be untouched"
    # A malformed filter is refused (not dropped): nothing deleted.
    r = c.post(
        "/tm/bulk-delete",
        data={**form, "customer": str(b), "created_before": "05/01/2026"},
        follow_redirects=False,
    )
    assert r.status_code == 400 and _count(pb, "de-DE") == 2
    print("  web: delete scoped to the posted customer; missing/foreign/malformed refused: OK")


def test_api_scoped_and_strict():
    a, b, pa, pb = _fixture("a")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        u = security.create_user("tmbds-admin@x.com", "Admin", "admin", _PW)
        _row, raw = apikeys.create_key(u.id, "tmbds", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": raw}
        url = "/api/tm/bulk-delete"
        base = {"target_locale": "de-DE"}
        # No customer: refused, dry-run included.
        for extra in ({}, {"dry_run": True}):
            r = api.post(url, json={**base, **extra}, headers=h)
            assert r.status_code == 400, (r.status_code, r.text)
        assert _count(pa, "de-DE") == 2 and _count(pb, "de-DE") == 2
        # Malformed filters: 400, nothing deleted (was: dropped, widening the delete).
        for bad in (
            {"created_before": "05/01/2026"},
            {"created_before": 20260501},
            {"project_id": "abc"},
            {"min_use": "x"},
            {"content_type_id": "zz"},
            {"unused": "maybe"},
            {"dry_run": "maybe"},
        ):
            r = api.post(url, json={**base, "customer": a, **bad}, headers=h)
            assert r.status_code == 400, (bad, r.status_code, r.text)
        assert _count(pa, "de-DE") == 2 and _count(pb, "de-DE") == 2
        # Foreign/unknown customer: refused.
        r = api.post(url, json={**base, "customer": 999999}, headers=h)
        assert r.status_code == 400
        # Dry run counts one customer; real delete touches one customer.
        r = api.post(url, json={**base, "customer": b, "dry_run": True}, headers=h)
        assert r.status_code == 200 and r.json()["count"] == 2, r.text
        r = api.post(url, json={**base, "customer": b}, headers=h)
        assert r.status_code == 200 and r.json()["count"] == 2, r.text
        assert _count(pb, "de-DE") == 0 and _count(pa, "de-DE") == 2, "A must be untouched"
        with Session(engine) as s:
            rows = s.exec(select(AuditLog).where(AuditLog.action == "tm.bulk_delete")).all()
        assert any(f"customer {b}" in (r.target or "") for r in rows), [r.target for r in rows]
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  api: customer required, malformed filters 400, delete scoped: OK")


def test_cli_scoped():
    a, _b, pa, pb = _fixture("c")
    rc, out = _run_cli(["--json", "tm", "delete", "--target", "de-DE", "--yes"])
    assert rc != 0, ("CLI bulk delete without --customer on 2+ customers must refuse", out)
    assert _count(pa, "de-DE") == 2 and _count(pb, "de-DE") == 2
    rc, out = _run_cli(
        ["--json", "tm", "delete", "--customer", str(a), "--target", "de-DE", "--yes"]
    )
    assert rc == 0, out
    assert _json.loads(out)["count"] == 2, out
    assert _count(pa, "de-DE") == 0 and _count(pb, "de-DE") == 2, "B must be untouched"
    print("  cli: --customer required on a multi-customer install, delete scoped: OK")


_HEDGE = "As an AI, I can't do that. Please provide the source text you want translated."


def _seed_hedges(pids):
    with Session(engine) as s:
        for pid in pids:
            s.add(
                SegmentTM(
                    project_id=pid,
                    source_locale="en-US",
                    target_locale="de-DE",
                    source_text=f"hedge src {pid}",
                    target_text=_HEDGE,
                    source_hash=f"hedge|{pid}",
                )
            )
        s.commit()


def _hedges(pid):
    with Session(engine) as s:
        stmt = select(SegmentTM).where(SegmentTM.project_id == pid, SegmentTM.target_text == _HEDGE)
        return len(s.exec(stmt).all())


def test_scrub_hedges_scoped():
    """/api/tm/scrub-hedges and `tm scrub-hedges` mutate TM across the whole ring; they
    take the same one-customer scope as bulk delete (#490 follow-up)."""
    a, _b, pa, pb = _fixture("s")
    _seed_hedges((pa, pb))
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        u = security.create_user("tmbds-scrub@x.com", "Admin", "admin", _PW)
        _row, raw = apikeys.create_key(u.id, "tmbds-s", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": raw}
        url = "/api/tm/scrub-hedges"
        # No customer on a multi-customer install: refused, dry-run included.
        for body in ({}, {"apply": True}):
            r = api.post(url, json=body, headers=h)
            assert r.status_code == 400, (body, r.status_code, r.text)
        assert _hedges(pa) == 1 and _hedges(pb) == 1
        # Malformed inputs are refused, never dropped (a dropped project widens it).
        for bad in ({"project": "abc"}, {"apply": "maybe"}, {"customer": 999999}):
            r = api.post(
                url, json={"customer": a, **bad} if "customer" not in bad else bad, headers=h
            )
            assert r.status_code == 400, (bad, r.status_code, r.text)
        assert _hedges(pa) == 1 and _hedges(pb) == 1
        # Dry run reports one customer only, deletes nothing.
        r = api.post(url, json={"customer": a}, headers=h)
        assert r.status_code == 200 and r.json()["matched"] == 1, r.text
        assert {x["project_id"] for x in r.json()["segment"]} == {pa}
        assert _hedges(pa) == 1 and _hedges(pb) == 1
        # Apply on customer A touches A only.
        r = api.post(url, json={"customer": a, "apply": True}, headers=h)
        assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
        assert _hedges(pa) == 0 and _hedges(pb) == 1, "B's TM must be untouched"
        # A project filter alone is a single-customer scope.
        r = api.post(url, json={"project": pb}, headers=h)
        assert r.status_code == 200 and r.json()["matched"] == 1, r.text
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    _seed_hedges((pa,))
    rc, out = _run_cli(["--json", "tm", "scrub-hedges", "--apply"])
    assert rc != 0, ("CLI scrub-hedges without --customer on 2+ customers must refuse", out)
    assert _hedges(pa) == 1 and _hedges(pb) == 1
    rc, out = _run_cli(["--json", "tm", "scrub-hedges", "--customer", str(a), "--apply"])
    assert rc == 0 and _json.loads(out)["deleted"] == 1, out
    assert _hedges(pa) == 0 and _hedges(pb) == 1, "B must be untouched"
    print("  scrub-hedges: api + cli scoped to one customer, malformed refused: OK")


def test_multi_org_foreign_customer():
    """Under multi-org an org-A key can neither name org B's customer nor reach B's TM by
    omitting the customer (its ring is org A only), on bulk delete AND scrub-hedges."""
    from app.models import Customer, Organization, Project, User

    with Session(engine) as s:
        oa, ob = Organization(name="TMBDS Org A"), Organization(name="TMBDS Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        ids = {}
        for tag, org in (("a", oa), ("b", ob)):
            c = Customer(name=f"MO {tag}", org_id=org.id)
            s.add(c)
            s.commit()
            p = Project(name=f"MOP {tag}", org_id=org.id, customer_id=c.id, created_by="t@x.test")
            s.add(p)
            s.commit()
            ids[tag] = (org.id, c.id, p.id)
    for tag in ("a", "b"):
        _seed_hedges((ids[tag][2],))
        with Session(engine) as s:
            _seed(s, ids[tag][2], "de-DE", f"mo {tag}")
    u = security.create_user("tmbds-mo@x.com", "MO Admin", "admin", _PW)
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            row = s.get(User, u.id)
            row.org_id = ids["a"][0]
            s.add(row)
            s.commit()
        _row, raw = apikeys.create_key(u.id, "tmbds-mo", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": raw}
        cust_b, pid_a, pid_b = ids["b"][1], ids["a"][2], ids["b"][2]
        for url, extra in (
            ("/api/tm/bulk-delete", {"dry_run": True}),
            ("/api/tm/scrub-hedges", {}),
        ):
            r = api.post(url, json={"customer": cust_b, **extra}, headers=h)
            assert r.status_code == 400, (url, r.status_code, r.text)
        assert _hedges(pid_b) == 1 and _count(pid_b, "de-DE") == 2
        r = api.post("/api/tm/scrub-hedges", json={"apply": True}, headers=h)
        assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
        assert _hedges(pid_a) == 0 and _hedges(pid_b) == 1, "org B hedge must survive"
        r = api.post("/api/tm/bulk-delete", json={"target_locale": "de-DE"}, headers=h)
        assert r.status_code == 200, r.text
        assert _count(pid_b, "de-DE") == 2, "org B TM must survive an org A bulk delete"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  multi-org: foreign customer refused, ring confined to own org: OK")


def test_customer_bound_key():
    """A customer-BOUND key (R2-3) on a multi-customer install: naming another customer
    is refused on both routes; omitting the customer stays inside its own customer."""
    from app.models import Role, User

    a, b, pa, pb = _fixture("k")
    _seed_hedges((pa, pb))
    u = security.create_user("tmbds-bound@x.com", "Bound", Role.LINGUIST, _PW)
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.customer_id = a
        s.add(row)
        s.commit()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        _row, raw = apikeys.create_key(u.id, "tmbds-bound", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": raw}
        for url, extra in (
            ("/api/tm/bulk-delete", {"target_locale": "de-DE"}),
            ("/api/tm/scrub-hedges", {"apply": True}),
        ):
            r = api.post(url, json={"customer": b, **extra}, headers=h)
            assert r.status_code == 400, (url, r.status_code, r.text)
        # A project filter naming the other customer's project matches nothing.
        r = api.post(
            "/api/tm/bulk-delete", json={"project_id": pb, "target_locale": "de-DE"}, headers=h
        )
        assert r.status_code == 200 and r.json()["count"] == 0, r.text
        r = api.post("/api/tm/scrub-hedges", json={"project": pb, "apply": True}, headers=h)
        assert r.status_code == 200 and r.json()["deleted"] == 0, r.text
        assert _count(pb, "de-DE") == 3 and _hedges(pb) == 1
        # No customer: the bound ring is one customer, so it runs on A only.
        r = api.post("/api/tm/bulk-delete", json={"target_locale": "de-DE"}, headers=h)
        assert r.status_code == 200 and r.json()["count"] == 3, r.text
        assert _count(pa, "de-DE") == 0 and _count(pb, "de-DE") == 3
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  customer-bound key: other customer refused, own ring only: OK")


def main():
    init_db()
    test_web_scoped_to_customer()
    test_api_scoped_and_strict()
    test_cli_scoped()
    test_scrub_hedges_scoped()
    test_multi_org_foreign_customer()
    test_customer_bound_key()
    print("TM BULK DELETE SCOPE OK")


if __name__ == "__main__":
    main()
