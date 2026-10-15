"""Content-type TM scoping verification (SPEC-content-types phase 3) — hermetic, offline.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys AND LOCAL_BASE_URL. ZERO live
calls. The mock engine is deterministic, so every match/no-match is asserted
EXACTLY.

The isolation guarantee that is the whole point (spec §4.4): a 100% match stamped with
one content type MUST NOT surface — on ANY of the three lookup paths (exact-hash, fuzzy and
full-text) — for a project of a DIFFERENT content type. Because segment TM is
project-scoped (`project_id IN (pid, NULL)`), the content-type predicate is isolated as
the deciding variable by seeding rows of DIFFERENT content types INTO ONE project's
scope (same project_id) and reading that project — so only the content-type leg can be
what filters them.

Covers:
  stamp-on-write   — write_back / ft_store / import_tmx stamp the project's content type
                     on INSERT.
  read isolation   — all three paths: a Marketing (type X) row does NOT surface for a
                     Product (type Y) project; the own-type row + the General(NULL) row do.
  General bucket   — typed reads own-type + NULL; untyped reads NULL only; a pre-existing
                     NULL row is visible to a NEW typed project.
  no retro-restamp — reassigning a project's content type leaves its TM rows' type as-is.
  byte-identical   — an untyped project (zero content types) gets today's results on all
                     three paths (the backward-compat invariant).
  browse filter    — tm_module + CLI (--content-type) + /api (?content_type_id=) parity.
  connector default— _ensure_project copies content_type_id AND (adjacent fix) customer_id.
  delete closed    — reference_count counts TM rows + connections; delete is refused.
  multi-tenant     — customer-scoped reference_count confinement.

Final line: "CONTENT TYPE TM OK".
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

init_env("loctms_cttm_")

from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Connection,
    Customer,
    FullTextTM,
    Organization,
    Project,
    SegmentTM,
)
from app.services import (
    apikeys,
    content,
    security,
    settings,
    tm,
    tm_module,
)
from app.services import (
    content_types as ctsvc,
)
from app.services.connectors import store as conn_store

SRC, TGT = "en-US", "fr-FR"


def _project(s, name, content_type_id=None, customer_id=None):
    p = Project(
        name=name, source_locale=SRC, content_type_id=content_type_id, customer_id=customer_id
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _seed_seg(s, project_id, content_type_id, source, target):
    """Insert a SegmentTM row DIRECTLY with an explicit content_type_id (so different
    content types can share one project scope — the way we isolate the content-type
    predicate from the project-scope predicate)."""
    row = SegmentTM(
        project_id=project_id,
        source_locale=SRC,
        target_locale=TGT,
        source_text=source,
        target_text=target,
        source_hash=tm.hash_text(source, SRC, TGT),
        content_type_id=content_type_id,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def _seed_ft(s, project_id, content_type_id, source, target):
    row = FullTextTM(
        project_id=project_id,
        source_locale=SRC,
        target_locale=TGT,
        source_hash=tm.hash_text(source, SRC, TGT),
        source_text=source,
        target_text=target,
        content_type_id=content_type_id,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


# ===========================================================================
def test_stamp_on_write():
    """write_back / ft_store / import_tmx stamp the owning project's content_type_id on
    INSERT."""
    with Session(engine) as s:
        X = ctsvc.create(s, "Marketing").id
        p = _project(s, "Mkt", content_type_id=X)

        # write_back -> stamped X
        r = tm.write_back(s, p.id, "Adventure awaits.", "L'aventure attend.", SRC, TGT)
        assert r.content_type_id == X, r.content_type_id

        # ft_store -> stamped X
        ft = tm.ft_store(s, p.id, "Whole doc source.", "Doc cible.", SRC, TGT)
        assert ft.content_type_id == X, ft.content_type_id

        # import_tmx -> stamps the TARGET project's type
        tmx = (
            """<?xml version="1.0"?><tmx version="1.4"><body>"""
            f"""<tu><tuv xml:lang="{SRC}"><seg>Imported unit.</seg></tuv>"""
            f"""<tuv xml:lang="{TGT}"><seg>Unite importee.</seg></tuv></tu>"""
            """</body></tmx>"""
        ).encode()
        res = tm.import_tmx(s, p.id, tmx, SRC, TGT)
        assert res["imported"] == 1, res
        imp = s.exec(select(SegmentTM).where(SegmentTM.source_text == "Imported unit.")).first()
        assert imp.content_type_id == X, imp.content_type_id

        # untyped project -> everything stamps NULL (byte-identical)
        pu = _project(s, "Untyped", content_type_id=None)
        ru = tm.write_back(s, pu.id, "Neutral phrase.", "Phrase neutre.", SRC, TGT)
        assert ru.content_type_id is None, ru.content_type_id
    print(
        "  stamp-on-write: write_back / ft_store / import_tmx all stamp "
        "the project's type (NULL for untyped): OK"
    )


def test_read_isolation_three_paths():
    """The #1 review target. One project scope holds a Marketing(X), a Product(Y) and a
    General(NULL) row so the CONTENT-TYPE leg is the only possible filter. Reading the
    project (typed Y = Product) must see Y + NULL and NEVER the X (Marketing) row — on
    exact-hash, fuzzy and full-text."""
    with Session(engine) as s:
        X = ctsvc.create(s, "Mkt2").id
        Y = ctsvc.create(s, "Prod2").id
        p = _project(s, "Shared-Product", content_type_id=Y)  # reads as Product

        # Mutually DISSIMILAR sources (<75% fuzzy) so a no-match is unambiguous —
        # a returned row could only be the same-content-type row, never a fuzzy
        # bleed from a sibling. The point is content-type filtering, not fuzz math.
        mkt = "Quarterly brand campaign messaging."
        prod = "Database migration rollback procedure."
        gen = "Standard company greeting header."
        _seed_seg(s, p.id, X, mkt, "Slogan marketing.")
        _seed_seg(s, p.id, Y, prod, "Slogan produit.")
        _seed_seg(s, p.id, None, gen, "Slogan general.")
        # 1) EXACT-HASH: Marketing row is invisible; Product + General are 100%.
        assert tm.lookup(s, p.id, mkt, SRC, TGT) is None, "exact-hash leaked Marketing!"
        assert (tm.lookup(s, p.id, prod, SRC, TGT) or {}).get("match_pct") == 100
        assert (tm.lookup(s, p.id, gen, SRC, TGT) or {}).get("match_pct") == 100

        # 2) FUZZY: a near-variant (not an exact hash) of each source.
        mkt_v = "Quarterly brand campaign messaging!!"
        prod_v = "Database migration rollback procedure!!"
        assert tm.lookup(s, p.id, mkt_v, SRC, TGT) is None, "fuzzy leaked Marketing!"
        fp = tm.lookup(s, p.id, prod_v, SRC, TGT)
        assert fp is not None and fp["target_text"] == "Slogan produit.", fp

        # 3) FULL-TEXT: seed ft rows of each type into the same project scope.
        _seed_ft(s, p.id, X, "FT marketing whole doc.", "FT doc marketing.")
        _seed_ft(s, p.id, Y, "FT product whole doc.", "FT doc produit.")
        assert tm.ft_lookup(s, p.id, "FT marketing whole doc.", SRC, TGT) is None, (
            "full-text leaked Marketing!"
        )
        assert tm.ft_lookup(s, p.id, "FT product whole doc.", SRC, TGT) == "FT doc produit."
    print(
        "  read isolation (all 3 paths): Marketing match never surfaces for a Product "
        "project; own-type + General do - exact/fuzzy/full-text: OK"
    )


def test_general_bucket_rule():
    """Typed reads own-type + NULL; untyped reads NULL ONLY; a pre-existing NULL row is
    visible to a NEW typed project (the leverage-preserving General bucket, §8.4)."""
    with Session(engine) as s:
        X = ctsvc.create(s, "Mkt3").id

        # Untyped project scope carrying a NULL row and an X row: untyped reads only NULL.
        pu = _project(s, "Untyped3", content_type_id=None)
        _seed_seg(s, pu.id, None, "Untyped sees general.", "cible g.")
        _seed_seg(s, pu.id, X, "Untyped must not see typed.", "cible x.")
        assert (tm.lookup(s, pu.id, "Untyped sees general.", SRC, TGT) or {}).get(
            "match_pct"
        ) == 100
        assert tm.lookup(s, pu.id, "Untyped must not see typed.", SRC, TGT) is None, (
            "untyped project leaked a TYPED row!"
        )

        # A pre-existing NULL (General) row is visible to a NEW typed project.
        pt = _project(s, "TypedX3", content_type_id=X)
        _seed_seg(s, pt.id, None, "Legacy general row.", "cible legacy.")
        _seed_seg(s, pt.id, X, "Own typed row.", "cible own.")
        assert (tm.lookup(s, pt.id, "Legacy general row.", SRC, TGT) or {}).get(
            "match_pct"
        ) == 100, "a typed project must still read the pre-existing General(NULL) row!"
        assert (tm.lookup(s, pt.id, "Own typed row.", SRC, TGT) or {}).get("match_pct") == 100
    print(
        "  General bucket: typed reads own+NULL; untyped reads NULL-only; a legacy NULL "
        "row is visible to a new typed project: OK"
    )


def test_no_retro_restamp():
    """Reassigning a project's content type does NOT restamp its existing TM rows —
    they keep the type they were written under (honest provenance, §4.5)."""
    with Session(engine) as s:
        X = ctsvc.create(s, "Mkt4").id
        Y = ctsvc.create(s, "Prod4").id
        p = _project(s, "Reassign", content_type_id=X)
        r = tm.write_back(s, p.id, "Written under X.", "cible.", SRC, TGT)
        assert r.content_type_id == X
        content.set_project_content_type(s, p.id, Y, actor="test")
        s.refresh(r)
        assert r.content_type_id == X, "TM row was WRONGLY retro-restamped on reassignment!"
        # And the row is now INVISIBLE to the project it lives in (project is now Y).
        assert tm.lookup(s, p.id, "Written under X.", SRC, TGT) is None
    print(
        "  no retro-restamp: a reassigned project's old TM rows keep their original "
        "content type (and fall out of the new type's read scope): OK"
    )


def test_byte_identical_untyped():
    """The backward-compat invariant: with NO content type in play, an untyped project's
    three lookup paths behave EXACTLY as today (all matches surface)."""
    with Session(engine) as s:
        p = _project(s, "PlainOld", content_type_id=None)
        tm.write_back(s, p.id, "Plain exact source.", "Cible exacte.", SRC, TGT)
        tm.ft_store(s, p.id, "Plain ft source.", "Cible ft.", SRC, TGT)
        # exact
        assert (tm.lookup(s, p.id, "Plain exact source.", SRC, TGT) or {}).get("match_pct") == 100
        # fuzzy
        fz = tm.lookup(s, p.id, "Plain exact source!!", SRC, TGT)
        assert fz is not None and fz["target_text"] == "Cible exacte.", fz
        # full-text
        assert tm.ft_lookup(s, p.id, "Plain ft source.", SRC, TGT) == "Cible ft."
    print(
        "  byte-identical: an untyped project's exact/fuzzy/full-text all "
        "return today's results (NULL-in-scope predicate is a no-op): OK"
    )


def test_browse_filter_three_surfaces():
    """TM browse content-type filter parity: service (tm_module) + CLI (--content-type)
    + /api (?content_type_id=)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        X = ctsvc.create(s, "BrowseX").id
        # 2026-07-14: TM browse is customer-first — attach the project to a
        # customer so the /api customer-first browse (step 3) can scope to it.
        _bc = Customer(name="BrowseCo")
        s.add(_bc)
        s.commit()
        s.refresh(_bc)
        browse_cid = _bc.id
        p = _project(s, "BrowseProj", content_type_id=None, customer_id=browse_cid)
        rX = _seed_seg(s, p.id, X, "browse typed row.", "cible bx.")
        rN = _seed_seg(s, p.id, None, "browse general row.", "cible bn.")
        pid, rXid, rNid = p.id, rX.id, rN.id

    # (1) SERVICE: filter to X returns only the X row; "__none__" returns only NULL.
    with Session(engine) as s:
        only_x = tm_module.list_entries(s, project_id=pid, content_type_id=X)
        ids_x = {e["id"] for e in only_x["entries"]}
        assert ids_x == {rXid}, ids_x
        only_gen = tm_module.list_entries(s, project_id=pid, content_type_id="__none__")
        ids_g = {e["id"] for e in only_gen["entries"]}
        assert ids_g == {rNid}, ids_g
        # entry_to_dict surfaces content_type_id
        assert only_x["entries"][0]["content_type_id"] == X

    # (2) CLI: `tm list --project <pid> --content-type <X>` (JSON) returns only the X row.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "tm", "list", "--project", str(pid), "--content-type", str(X)])
    assert rc == 0, rc
    out = _json.loads(buf.getvalue())
    assert {e["id"] for e in out["entries"]} == {rXid}, out
    # 'general' keyword -> the NULL bucket
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main(["--json", "tm", "list", "--project", str(pid), "--content-type", "general"])
    out = _json.loads(buf.getvalue())
    assert {e["id"] for e in out["entries"]} == {rNid}, out

    # (3) /api: GET /api/tm?content_type_id=X (needs a valid token; GET reads have no
    #     write gate).
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        u = security.create_user("cttm-admin@x.com", "Admin", "admin", "cttm-pw-123456")
        _row, raw = apikeys.create_key(u.id, "cttm", scopes="api docs")
        api = TestClient(app)
        # Customer-first browse: pass &customer= so /api/tm scopes to the customer.
        r = api.get(
            f"/api/tm?customer={browse_cid}&project_id={pid}&content_type_id={X}",
            headers={"X-Loc-Token": raw},
        )
        assert r.status_code == 200, r.text
        assert {e["id"] for e in r.json()["entries"]} == {rXid}, r.json()
        r = api.get(
            f"/api/tm?customer={browse_cid}&project_id={pid}&content_type_id=__none__",
            headers={"X-Loc-Token": raw},
        )
        assert {e["id"] for e in r.json()["entries"]} == {rNid}, r.json()
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print(
        "  browse filter: service + CLI (--content-type id|general) + /api "
        "(?content_type_id=) all filter to the right content type: OK"
    )


def test_connector_default_and_customer_fix():
    """_ensure_project copies the connection's content_type_id AND (the adjacent fix)
    its customer_id onto the auto-created sync project."""
    with Session(engine) as s:
        X = ctsvc.create(s, "ConnX").id
        cust = Customer(name="Acme")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cust_id, ctx = cust.id, X

    conn = conn_store.create(
        "airtable",
        "Acme Sync",
        {"source_locale": SRC},
        {},
        actor="test",
        customer_id=cust_id,
        content_type_id=ctx,
    )
    with Session(engine) as s:
        c = s.get(Connection, conn.id)
        assert c.content_type_id == ctx and c.customer_id == cust_id, (
            c.content_type_id,
            c.customer_id,
        )
        proj = conn_store._ensure_project(s, c)
        assert proj.content_type_id == ctx, "sync project did not inherit content type!"
        assert proj.customer_id == cust_id, (
            "ADJACENT FIX regressed: sync project did not inherit the connection's customer_id!"
        )
    print(
        "  connector default: _ensure_project inherits content_type_id AND customer_id "
        "(adjacent fix) onto the auto-created project: OK"
    )


def test_connection_content_type_confined_at_seam():
    """Findings 1+2: content_type_id is validated at the store.create/update SEAM against
    the connection's RESOLVED customer + org — never the caller's binding. So a
    customer's own type attaches to that customer's connection (functional case 1a), but
    can NOT be planted onto a global, other-customer, or other-org connection (1b + 2)."""
    with Session(engine) as s:
        cA = Customer(name="ConfA")
        cB = Customer(name="ConfB")
        s.add(cA)
        s.add(cB)
        s.commit()
        s.refresh(cA)
        s.refresh(cB)
        typeA = ctsvc.create(s, "ConfTypeA", customer_id=cA.id).id
        glob = ctsvc.create(s, "ConfGlobal").id
        cA_id, cB_id = cA.id, cB.id

    def _ct_of(cid):
        with Session(engine) as s:
            return s.get(Connection, cid).content_type_id

    # (1a) FUNCTIONAL: a connection scoped to customer A CAN carry customer A's own type.
    cn = conn_store.create(
        "airtable", "A-conn", {}, {}, actor="t", customer_id=cA_id, content_type_id=typeA
    )
    assert _ct_of(cn.id) == typeA, "Finding 1a: customer's own type was wrongly dropped"

    # (1b) a customer's PRIVATE type can NOT be planted onto a GLOBAL connection ...
    cg = conn_store.create(
        "airtable", "G-conn", {}, {}, actor="t", customer_id="", content_type_id=typeA
    )
    assert _ct_of(cg.id) is None, "Finding 1b LEAK: customer type planted on a GLOBAL connection"
    # ... nor onto a DIFFERENT customer's connection.
    cb = conn_store.create(
        "airtable", "B-conn", {}, {}, actor="t", customer_id=cB_id, content_type_id=typeA
    )
    assert _ct_of(cb.id) is None, (
        "Finding 1b LEAK: customer-A type planted on a customer-B connection"
    )

    # A GLOBAL type attaches to any customer's connection (sanity: valid case not over-blocked).
    cga = conn_store.create(
        "airtable", "GA-conn", {}, {}, actor="t", customer_id=cA_id, content_type_id=glob
    )
    assert _ct_of(cga.id) == glob, "a global type must attach to a customer connection"

    # update() enforces the SAME seam: setting customer-A's type on the global conn drops it.
    conn_store.update(cg.id, "G-conn", {}, None, content_type_id=typeA)
    assert _ct_of(cg.id) is None, "update seam let a cross-scope content type through"

    # Finding 2: an --org B connection can NOT attach an org-A content type.
    with Session(engine) as s:
        oA = Organization(name="ConfOrgA")
        oB = Organization(name="ConfOrgB")
        s.add(oA)
        s.add(oB)
        s.commit()
        s.refresh(oA)
        s.refresh(oB)
        typeOrgA = ctsvc.create(s, "ConfOrgAType", org_id=oA.id).id
        oa_id, ob_id = oA.id, oB.id
    c_ob = conn_store.create(
        "airtable", "orgB-conn", {}, {}, actor="t", org_id=ob_id, content_type_id=typeOrgA
    )
    assert _ct_of(c_ob.id) is None, "Finding 2 LEAK: org-A type planted on an org-B connection"
    c_oa = conn_store.create(
        "airtable", "orgA-conn", {}, {}, actor="t", org_id=oa_id, content_type_id=typeOrgA
    )
    assert _ct_of(c_oa.id) == typeOrgA, "same-org type must attach"
    print(
        "  Findings 1+2: content_type validated at the store SEAM against the "
        "connection's resolved customer/org (own-type attaches; cross-customer/-org "
        "plant dropped; update enforces it too): OK"
    )


def test_delete_fails_closed_counts_tm_and_connections():
    """reference_count now also counts TM rows + connections; delete is refused while
    any reference (incl. a written TM row) exists — a type with stored TM is undeletable."""
    with Session(engine) as s:
        # A type referenced ONLY by a TM row (no project/glossary/customer default).
        T = ctsvc.create(s, "TMonly").id
        p = _project(s, "TMonlyProj", content_type_id=None)  # project untyped, row typed
        _seed_seg(s, p.id, T, "held by tm row.", "cible.")
        refs = ctsvc.reference_count(s, T)
        assert refs["tm_rows"] >= 1 and refs["projects"] == 0, refs
        assert refs["total"] > 0, refs
        try:
            ctsvc.delete(s, T)
            raise AssertionError("delete must be refused while a TM row references the type")
        except ctsvc.ContentTypeError as exc:
            assert "translation-memory" in str(exc), str(exc)

        # A type referenced ONLY by a connection.
        C = ctsvc.create(s, "Connonly").id
    conn_store.create("airtable", "Conn ref", {}, {}, actor="test", content_type_id=C)
    with Session(engine) as s:
        refs = ctsvc.reference_count(s, C)
        assert refs["connections"] >= 1, refs
        try:
            ctsvc.delete(s, C)
            raise AssertionError("delete must be refused while a connection references the type")
        except ctsvc.ContentTypeError as exc:
            assert "connection" in str(exc), str(exc)
    print(
        "  delete fails closed: reference_count counts TM rows + connections; a type "
        "with stored TM / a bound connection cannot be deleted: OK"
    )


def test_customer_scoped_reference_count():
    """The org/customer-scoped reference_count confines TM + connection counts through
    the owning project's scope (multi-tenant probe)."""
    with Session(engine) as s:
        T = ctsvc.create(s, "ScopedT").id
        c1 = Customer(name="Cust1")
        c2 = Customer(name="Cust2")
        s.add(c1)
        s.add(c2)
        s.commit()
        s.refresh(c1)
        s.refresh(c2)
        p1 = _project(s, "P-c1", content_type_id=None, customer_id=c1.id)
        p2 = _project(s, "P-c2", content_type_id=None, customer_id=c2.id)
        _seed_seg(s, p1.id, T, "c1 tm row.", "cible1.")
        _seed_seg(s, p2.id, T, "c2 tm row.", "cible2.")
        # Unscoped counts both; customer=c1 counts only c1's TM row.
        assert ctsvc.reference_count(s, T)["tm_rows"] == 2
        assert ctsvc.reference_count(s, T, customer_id=c1.id)["tm_rows"] == 1
    print(
        "  multi-tenant: customer-scoped reference_count confines the TM count through "
        "the owning project: OK"
    )


def _cli_json(argv):
    """Run cli_main(["--json", *argv]) and return (rc, parsed-json-or-None)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", *argv])
    text = buf.getvalue().strip()
    out = None
    if text:
        try:
            out = _json.loads(text.splitlines()[-1])
        except ValueError:
            out = None
    return rc, out


def test_cli_connector_content_type_explicit_errors():
    """SPEC-content-types phase-3 re-review LOW (CLI-flag UX only, hygiene wave 1 item
    3): `connector create`/`update`'s store seam (_resolve_content_type_id) silently
    drops an invalid/out-of-scope --content-type id to untyped (unchanged -- proven in
    test_connection_content_type_confined_at_seam). An operator who EXPLICITLY passes
    --content-type on the CLI gets a clean, non-zero-exit error instead, using the SAME
    validation (store.content_type_attachable -> content_types.attachable). Web/API are
    untouched: they call store.create/update directly with no CLI pre-flight."""
    with Session(engine) as s:
        glob = ctsvc.create(s, "CliGlobalType").id
        custA = Customer(name="CliCustA")
        s.add(custA)
        s.commit()
        s.refresh(custA)
        typeA = ctsvc.create(s, "CliCustAType", customer_id=custA.id).id
        custA_id = custA.id

    # (1) a NONEXISTENT id on create -> clean error, non-zero exit, nothing created.
    rc, out = _cli_json(
        [
            "connector",
            "create",
            "--kind",
            "airtable",
            "--name",
            "CliBogus",
            "--content-type",
            "999999",
        ]
    )
    assert rc != 0, ("bogus content-type id must fail create", rc, out)
    with Session(engine) as s:
        names = {c.name for c in conn_store.list_connections()}
    assert "CliBogus" not in names, "a failed create must not have left a connector behind"

    # (2) a CUSTOMER-PRIVATE type on a GLOBAL connection (out-of-scope) -> error.
    rc, out = _cli_json(
        [
            "connector",
            "create",
            "--kind",
            "airtable",
            "--name",
            "CliOOS",
            "--content-type",
            str(typeA),
        ]
    )
    assert rc != 0, ("out-of-scope content-type id must fail create", rc, out)
    names = {c.name for c in conn_store.list_connections()}
    assert "CliOOS" not in names, "a failed create must not have left a connector behind"

    # (3) a VALID global type -> succeeds, content_type_id set.
    rc, out = _cli_json(
        [
            "connector",
            "create",
            "--kind",
            "airtable",
            "--name",
            "CliOK",
            "--content-type",
            str(glob),
        ]
    )
    assert rc == 0 and out["content_type_id"] == glob, (rc, out)
    conn_id = out["id"]

    # (4) NO --content-type at all -> still succeeds untyped (omitting the flag is
    # unaffected by this fix).
    rc, out = _cli_json(["connector", "create", "--kind", "airtable", "--name", "CliUntyped"])
    assert rc == 0 and out["content_type_id"] is None, (rc, out)

    # (5) update with a bogus id -> clean error, non-zero exit, and the existing content
    # type is left UNCHANGED (the pre-flight runs before store.update mutates anything).
    rc, out = _cli_json(["connector", "update", str(conn_id), "--content-type", "999999"])
    assert rc != 0, ("update with bogus content-type id must fail", rc, out)
    with Session(engine) as s:
        assert s.get(Connection, conn_id).content_type_id == glob, (
            "a failed update must not silently change the existing content type"
        )

    # (6) update re-scoping --customer AND --content-type in the SAME call, where the
    # type is only valid for that customer -> succeeds (mirrors the seam's use of the
    # just-applied customer scope, not the connection's old one).
    rc, out = _cli_json(
        [
            "connector",
            "update",
            str(conn_id),
            "--customer",
            str(custA_id),
            "--content-type",
            str(typeA),
        ]
    )
    assert rc == 0 and out["content_type_id"] == typeA, (rc, out)

    # (7) explicit clear ("") is NOT an error -- the explicit-clear contract is preserved.
    rc, out = _cli_json(["connector", "update", str(conn_id), "--content-type", ""])
    assert rc == 0 and out["content_type_id"] is None, (rc, out)

    print(
        "  CLI connector create/update: an EXPLICIT invalid/out-of-scope --content-type "
        "is a clean non-zero-exit error (nothing created/changed); omitted/blank/valid "
        "ids are unaffected: OK"
    )


def test_cli_tm_browse_content_type_typo_error():
    """SPEC-content-types phase-3 re-review LOW (CLI-flag UX only, hygiene wave 1 item
    3): TM browse's --content-type accepts an id or 'general'/'none'/'untyped'.
    Previously ANY other explicitly-typed value silently fell back to "no filter" (the
    SAME behavior as an OMITTED flag), which would show the operator the FULL unfiltered
    TM instead of erroring on what is almost certainly a typo. `list`/`search`/`delete`
    all share the one `_tm_filters._content_type` helper, so exercising `list` proves
    the fix for all three. Omitting the flag is unaffected -- still means no filter,
    byte-identical to web/api/_remote (their own malformed-value handling lives in
    separate functions untouched by this fix)."""
    with Session(engine) as s:
        p = _project(s, "TypoFilterProj", content_type_id=None)
        _seed_seg(s, p.id, None, "typo filter source.", "typo filter cible.")
        pid = p.id

    # a typo'd value -> clean error, non-zero exit.
    rc, out = _cli_json(["tm", "list", "--project", str(pid), "--content-type", "genral"])
    assert rc != 0, ("a typo'd --content-type must fail", rc, out)

    # omitting --content-type entirely is unaffected -- still lists (unfiltered).
    rc, out = _cli_json(["tm", "list", "--project", str(pid)])
    assert rc == 0 and out["entries"], ("omitted filter must still list entries", rc, out)

    # the recognized 'general' keyword still works (not a typo).
    rc, out = _cli_json(["tm", "list", "--project", str(pid), "--content-type", "general"])
    assert rc == 0 and out["entries"], ("the 'general' keyword must still work", rc, out)

    print(
        "  CLI tm list/search/delete: a typo'd --content-type is a clean non-zero-exit "
        "error; omitting the flag / the recognized keywords are unaffected: OK"
    )


def main():
    init_db()
    test_stamp_on_write()
    test_read_isolation_three_paths()
    test_general_bucket_rule()
    test_no_retro_restamp()
    test_byte_identical_untyped()
    test_browse_filter_three_surfaces()
    test_connector_default_and_customer_fix()
    test_connection_content_type_confined_at_seam()
    test_delete_fails_closed_counts_tm_and_connections()
    test_customer_scoped_reference_count()
    test_cli_connector_content_type_explicit_errors()
    test_cli_tm_browse_content_type_typo_error()
    print("CONTENT TYPE TM OK")


if __name__ == "__main__":
    main()
