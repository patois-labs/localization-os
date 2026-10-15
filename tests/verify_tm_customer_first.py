"""Customer-first TM browse + projects-require-a-customer (2026-07-14).

Covers the feat/tm-customer-first change (offline, hermetic):

  (A) LOCALE-DEDUP filter bug — the /tm source/target filter dropdowns build from a
      de-duplicated single-column DISTINCT (distinct_source_locales /
      distinct_target_locales), NOT distinct_pairs. Seed one source locale paired with
      several targets and assert the source dropdown offers it ONCE (the old bug showed
      it once per pair).

  (B) CUSTOMER-FIRST TM browse matrix — TM is organized by customer (there is NO global
      TM). Asserts: a chosen customer scopes the rows; exactly-one-visible auto-selects;
      no selection => the page shows the picker + empty state, NEVER an all-customers
      dump; /api/tm returns needs_customer with no selection and scopes with ?customer=;
      a bound user's customer is forced. TM MATCHING is untouched (browse-only).

  (C) PROJECTS REQUIRE A CUSTOMER on the user-facing seams — web POST /projects, CLI
      `project create`, /api create, and the promptable/template DRAFT warning all
      require/flag a customer WHEN ONE EXISTS. The documented seam decision: a
      zero-customer install is NOT gated (a customer-less project is allowed there and
      gets the assignment banner), so hermetic fixtures that never create a customer
      stay green.

  (D) NULL -> assigned flow — a customer-LESS project can be assigned a customer
      (audited); assignment is from-NULL only (reassigning an already-set customer is
      refused). Web + CLI + /api + service parity for `set-customer`.

HERMETIC: clears provider keys + LOCAL_BASE_URL before importing app. ASCII-only.
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

init_env("loctms_tmcf_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Customer, Project, SegmentTM
from app.services import (
    apikeys,
    content,
    security,
    settings,
    tm_module,
)
from app.services import (
    templates as tplsvc,
)


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _seed_tm(s, project_id, src, tgt, source_text, target_text="cible"):
    row = SegmentTM(
        project_id=project_id,
        source_locale=src,
        target_locale=tgt,
        source_text=source_text,
        target_text=target_text,
        source_hash=f"{src}|{tgt}|{source_text}",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row.id


# ---------------------------------------------------------------------------
def test_zero_customer_seam_allows_customerless():
    """SEAM DECISION (documented): with ZERO customers in the org, the user-facing
    create seams are NOT gated — a customer-less project is allowed (and later flagged
    for assignment). This is what keeps hermetic fixtures that never make a customer
    green. Runs FIRST, before any customer exists in this process's DB."""
    assert content.org_has_selectable_customer(Session(engine)) is False, (
        "precondition: no customers yet"
    )
    # Web seam
    c = TestClient(app)
    r = c.post(
        "/projects",
        data={
            "name": "ZeroCustWeb",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": "",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/projects/" in r.headers["location"], r.headers
    # CLI seam
    rc, out = _run_cli(["--json", "project", "create", "ZeroCustCli", "--targets", "fr-FR"])
    assert rc == 0, out
    with Session(engine) as s:
        got = s.get(Project, _json.loads(out)["id"])
        assert got.customer_id is None, "zero-customer install: customer-less allowed"
    print("  (C) zero-customer seam: customer-less create ALLOWED (fixtures survive): OK")


def test_locale_dedup():
    """(A) One source locale paired with several targets appears ONCE in the source
    filter dropdown (de-duplicated), while distinct_pairs still has one row per pair."""
    with Session(engine) as s:
        cust = content.customer_create(s, "DedupCo")
        p = content.project_create(s, "DedupProj", customer_id=str(cust.id), target_locales="fr-FR")
        cid, pid = cust.id, p.id
        for tgt in ("fr-FR", "de-DE", "es-ES", "it-IT", "ja-JP"):
            _seed_tm(s, pid, "en-US", tgt, f"hello {tgt}")
        pids = [pid]
        srcs = tm_module.distinct_source_locales(s, visible_pids=pids)
        tgts = tm_module.distinct_target_locales(s, visible_pids=pids)
        pairs = tm_module.distinct_pairs(s, visible_pids=pids)
    assert srcs == ["en-US"], ("source locale must appear ONCE, not per-pair", srcs)
    assert sorted(tgts) == ["de-DE", "es-ES", "fr-FR", "it-IT", "ja-JP"], tgts
    assert len(pairs) == 5, ("distinct_pairs still one row per pair", pairs)

    # Render the page: the source dropdown must offer en-US exactly once.
    c = TestClient(app)
    html = c.get(f"/tm?customer={cid}").text
    seg = html.split('name="source_locale"', 1)[1].split("</select>", 1)[0]
    assert seg.count(">en-US<") == 1, ("source dropdown must list en-US once", seg)
    print("  (A) locale-dedup: source/target filter dropdowns de-duplicated: OK")


def test_customer_first_browse_matrix():
    """(B) customer selection scopes the rows; single auto-selects; no selection =>
    picker + empty (no dump); /api/tm needs_customer + ?customer= scoping."""
    with Session(engine) as s:
        # A second customer + project + TM so 'multiple customers' holds.
        cA = s.exec(select(Customer).where(Customer.name == "DedupCo")).first()
        cB = content.customer_create(s, "OtherCo")
        pB = content.project_create(s, "OtherProj", customer_id=str(cB.id), target_locales="fr-FR")
        _seed_tm(s, pB.id, "en-US", "fr-FR", "other row")
        aid, bid = cA.id, cB.id

    # resolve_browse_customer: requested-in-set wins; single auto-selects; else None.
    with Session(engine) as s:
        customers = tm_module.browsable_customers(s)
        assert {c.id for c in customers} >= {aid, bid}
        assert tm_module.resolve_browse_customer(customers, str(aid)).id == aid
        assert tm_module.resolve_browse_customer(customers, None) is None, (
            "multiple customers + no request => NO auto-select (no dump)"
        )
        assert tm_module.resolve_browse_customer(customers, "999999") is None, (
            "a foreign/unknown id never scopes the browse"
        )
        one = [c for c in customers if c.id == aid]
        assert tm_module.resolve_browse_customer(one, None).id == aid, (
            "exactly one visible customer auto-selects"
        )
        # scoping: A's browse never sees B's rows.
        a_pids = tm_module.scoped_pids_for_customer(s, None, aid)
        b_pids = tm_module.scoped_pids_for_customer(s, None, bid)
        assert set(a_pids).isdisjoint(b_pids), (a_pids, b_pids)

    c = TestClient(app)
    # No ?customer= with multiple customers => picker + empty state, no entries table.
    html = c.get("/tm").text
    assert "pick a customer" in html.lower(), "picker/empty state must render"
    assert "browse row" not in html and "other row" not in html, (
        "no-selection page must NOT dump any customer's TM"
    )
    # ?customer=A scopes to A's rows only.
    htmlA = c.get(f"/tm?customer={aid}").text
    assert "other row" not in htmlA, "A's browse leaked B's TM row"

    # /api/tm: needs_customer with no selection; scoped with ?customer=.
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        u = security.create_user("tmcf-admin@x.com", "Admin", "admin", "tmcf-pw-123456")
        _row, raw = apikeys.create_key(u.id, "tmcf", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": raw}
        r = api.get("/api/tm", headers=h)
        assert r.json().get("needs_customer") is True and r.json()["entries"] == [], (
            "no customer => needs_customer, never a dump",
            r.json(),
        )
        r = api.get(f"/api/tm?customer={bid}", headers=h)
        body = r.json()
        assert body.get("customer_id") == bid, body
        assert all("other" in e["source_text"] for e in body["entries"]), body
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  (B) customer-first browse: scope + auto-select + no-dump + /api parity: OK")


def test_requires_customer_web_cli_api():
    """(C) with a customer EXISTING, every user-facing create seam requires one."""
    # Web POST /projects with no customer -> 303 to /projects?err=...
    c = TestClient(app)
    r = c.post(
        "/projects",
        data={
            "name": "NeedsCustWeb",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": "",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert (
        r.status_code == 303
        and "err=" in r.headers["location"]
        and "customer" in r.headers["location"].lower()
    ), r.headers.get("location")
    with Session(engine) as s:
        assert s.exec(select(Project).where(Project.name == "NeedsCustWeb")).first() is None, (
            "a rejected create must NOT leave a project row"
        )

    # CLI project create with no --customer-id -> non-zero exit.
    rc, out = _run_cli(["project", "create", "NeedsCustCli", "--targets", "fr-FR"])
    assert rc != 0, ("CLI create must require a customer", out)

    # /api create with no customer_id -> clean 400.
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        u = security.create_user("tmcf-c2@x.com", "Admin", "admin", "tmcf-pw-654321")
        _row, raw = apikeys.create_key(u.id, "tmcf2", scopes="api docs")
        api = TestClient(app)
        r = api.post(
            "/api/projects",
            headers={"X-Loc-Token": raw},
            json={"name": "NeedsCustApi", "targets": "fr-FR"},
        )
        assert r.status_code == 400 and "customer" in r.text.lower(), r.text
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  (C) requires-customer: web + CLI + /api all reject a customer-less create: OK")


def test_promptable_and_template_draft_warns():
    """(C) the DRAFT/confirm seam: a template prefill (a global template, no captured
    customer) WARNS 'pick a customer' when a customer exists to pick — the promptable/
    template review screen's flag before the POST /projects confirm hard-rejects."""
    with Session(engine) as s:
        # A GLOBAL template (no customer) built from validated fields.
        tpl, _w = tplsvc.create_template_from_fields(
            s,
            "GlobDraftTpl",
            source_locale="en-US",
            target_locales="fr-FR, de-DE",
            default_provider="mock",
            customer_id=None,
            scope="global",
        )
        tid = tpl.id
        res = tplsvc.prefill_draft(s, tid)
    assert "customer_id" not in res["draft"], res
    assert any("Pick a customer" in w for w in res["warnings"]), (
        "a global template must warn to pick a customer (a project needs one)",
        res["warnings"],
    )
    print("  (C) draft warning: global-template prefill flags 'pick a customer': OK")


def test_null_to_assigned_flow_and_from_null_only():
    """(D) a customer-LESS project can be assigned a customer (audited); assignment is
    from-NULL only (a second, different assignment is refused). Service + CLI + web."""
    with Session(engine) as s:
        # A customer-less project (internal service call — not gated).
        p = content.project_create(s, "OrphanProj", target_locales="fr-FR")
        assert p.customer_id is None
        pid = p.id
        target = content.customer_create(s, "AssignCo")
        other = content.customer_create(s, "OtherAssignCo")
        tcid, ocid = target.id, other.id

    # Service assign (NULL -> set) + audit row.
    with Session(engine) as s:
        got = content.assign_project_customer(s, pid, tcid, actor="tester")
        assert got is not None and got.customer_id == tcid, got
        aud = s.exec(
            select(AuditLog).where(
                AuditLog.action == "project.assign_customer", AuditLog.target == f"project {pid}"
            )
        ).first()
        assert aud is not None, "assign must be audited"

    # from-NULL only: a SECOND assignment (now non-NULL) is refused.
    with Session(engine) as s:
        try:
            content.assign_project_customer(s, pid, ocid, actor="tester")
            raise AssertionError("reassignment must be refused (from-NULL only)")
        except content.ProjectCustomerAssignError:
            pass

    # CLI parity: set-customer on an already-set project -> non-zero.
    rc, out = _run_cli(["project", "set-customer", str(pid), str(ocid)])
    assert rc != 0, ("CLI set-customer must refuse reassignment", out)

    # Web parity: a fresh customer-less project assigned via the banner route.
    with Session(engine) as s:
        p2 = content.project_create(s, "OrphanProj2", target_locales="fr-FR")
        p2id = p2.id
        cc = content.customer_create(s, "BannerCo")
        ccid = cc.id
    c = TestClient(app)
    r = c.post(
        f"/projects/{p2id}/set-customer", data={"customer_id": str(ccid)}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert s.get(Project, p2id).customer_id == ccid
        # banner renders on a still-orphan project.
        p3 = content.project_create(s, "OrphanProj3", target_locales="fr-FR")
        p3id = p3.id
    assert "no customer" in c.get(f"/projects/{p3id}").text.lower(), (
        "customer-less project page must show the assign banner"
    )
    print("  (D) NULL->assigned: service+CLI+web assign, audited, from-NULL only: OK")


def main():
    init_db()
    test_zero_customer_seam_allows_customerless()
    test_locale_dedup()
    test_customer_first_browse_matrix()
    test_requires_customer_web_cli_api()
    test_promptable_and_template_draft_warns()
    test_null_to_assigned_flow_and_from_null_only()
    print("TM CUSTOMER-FIRST OK")


if __name__ == "__main__":
    main()
