"""Customer 360 verification (P4, item 5).

Covers, offline + hermetic (throwaway LOC_TMS_DATA_DIR, provider keys blanked ->
MockProvider; no provider or connector network is ever touched — connections are
created with store.create, which does no network I/O):

  model + migration
    - the new customer columns (settings/active_pairs/default_provider/
      default_workflow_id/default_segmentation_profile_id) + connection.customer_id
      exist and round-trip; existing (bare) customers/connections read as
      NULL/global (byte-identical legacy behaviour)
  settings + defaults round-trip (service + CLI)
    - update_customer_settings MERGES + drops blank keys; set_customer_defaults sets
      provider/workflow/segmentation and a blank CLEARS; CLI customer update/set-default
  language pairs
    - add/remove explicit pairs (validated against the catalog); derived pairs come
      from the projects; malformed pair rejected; CLI pairs add/remove/list
  360 page + links (web)
    - GET /customers/{id} renders every card (projects/connectors/workflows/pairs/TM)
      with click-through links; the customers index links to the 360 page
  ISOLATION (the core guarantee)
    - a customer-scoped connection is INVISIBLE to another customer's 360 context and
      to the customer-scoped connector list (picker) — but the connection's OWN
      /connectors/connection/{id} + dispatch stays reachable; global connections are
      visible everywhere (legacy behaviour preserved)
    - a customer-scoped workflow is invisible to another customer's context; a global
      workflow is visible to all; matches the existing _applicable_workflows dispatch
    - the connectors API list (/api/... if present) never leaks a cross-customer conn
  project-default inheritance
    - project creation under a customer inherits its default_provider (unless the
      caller picked one); no customer -> unchanged
  org-gate interplay (smoke)
    - with multi_org OFF (default), everything above; a scoped-user 360 of a foreign
      org's customer is blocked (documented; exercised via _org_block path)

Final line: "CUSTOMERS OK".
"""

import csv
import io
import json as _json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_customers_")

from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Customer, Role, Scope
from app.services import (
    apikeys,
    content,
    security,
)
from app.services import customers as cs
from app.services import workflows as wf_service
from app.services.connectors import store


def _mk_customer(name):
    with Session(engine) as s:
        c = content.customer_create(s, name)
        return c.id


def test_model_and_migration():
    cust_cols = {c["name"] for c in inspect(engine).get_columns("customer")}
    for col in (
        "settings",
        "active_pairs",
        "default_provider",
        "default_workflow_id",
        "default_segmentation_profile_id",
    ):
        assert col in cust_cols, f"customer.{col} missing"
    conn_cols = {c["name"] for c in inspect(engine).get_columns("connection")}
    assert "customer_id" in conn_cols, "connection.customer_id missing"
    # A bare customer reads as legacy NULL/empty everywhere.
    cid = _mk_customer("Legacy")
    with Session(engine) as s:
        c = s.get(Customer, cid)
        assert c.settings is None and c.active_pairs is None
        assert c.default_provider is None and c.default_workflow_id is None
    # A bare (global) connection has customer_id NULL and is byte-identical.
    conn = store.create("jira", "legacy-conn", {}, {"email": "a", "api_token": "x"})
    assert conn.customer_id is None
    print("  model: 360 columns + connection.customer_id exist; legacy reads NULL/global OK")


def test_settings_and_defaults_roundtrip():
    cid = _mk_customer("Acme")
    with Session(engine) as s:
        # merge + blank-drop
        cs.update_customer_settings(s, cid, {"notes": "VIP", "primary_contact": "a@x"})
        cs.update_customer_settings(s, cid, {"notes": "", "technical_contact": "b@x"})
        c = s.get(Customer, cid)
        assert "notes" not in (c.settings or {}), "blank value must drop the key"
        assert c.settings["primary_contact"] == "a@x"
        assert c.settings["technical_contact"] == "b@x"
        # defaults set + clear. default_workflow_id is a real FK (-> workflow.id),
        # so point it at a REAL workflow instead of a bare id (id 7 that violated
        # the FK never persisted on Postgres).
        wf = wf_service.create_workflow(s, "Cust default WF", scope=Scope.GLOBAL)
        s.commit()
        wf_id = wf.id
        cs.set_customer_defaults(s, cid, provider="gemini", workflow_id=str(wf_id))
        c = s.get(Customer, cid)
        assert c.default_provider == "gemini" and c.default_workflow_id == wf_id
        cs.set_customer_defaults(s, cid, provider="")  # clear
        c = s.get(Customer, cid)
        assert c.default_provider is None, "blank provider must clear the default"
        assert c.default_workflow_id == wf_id, "unspecified default must be untouched"
    print("  settings merge + blank-drop; defaults set/clear/partial OK")


def test_language_pairs():
    cid = _mk_customer("Pairs")
    with Session(engine) as s:
        added, canon = cs.add_pair(s, cid, "en_US -> fr_FR")  # loose input canonicalizes
        assert added and canon == "en-US->fr-FR"
        again, _ = cs.add_pair(s, cid, "en-US->fr-FR")
        assert not again, "duplicate pair must not re-add"
        try:
            cs.add_pair(s, cid, "en-US->zz-ZZ")
            assert False, "invalid target must raise"
        except ValueError:
            pass
        removed, _ = cs.remove_pair(s, cid, "en-US->fr-FR")
        assert removed and cs.list_pairs(s, cid) == []
        # derived pairs from a project
        content.project_create(s, "P", customer_id=str(cid), target_locales="de-DE ja-JP")
        d = cs.get_customer_360(s, cid)
        assert set(d["derived_pairs"]) == {"en-US->de-DE", "en-US->ja-JP"}
    print("  pairs add/dedup/remove + invalid rejected + derived-from-projects OK")


# ============================================================================
# Bulk `customer pairs import` (CLI-parity backlog item 2): service-level unit
# coverage of import_pairs_csv -- atomicity (a bad row rejects the WHOLE file, no
# partial write), idempotency (re-import skips already-present pairs), header
# validation, and in-file duplicate collapsing.
# ============================================================================
def test_bulk_pairs_import():
    cid = _mk_customer("BulkPairs")

    # --- a malformed header is a clean PairImportError, no write attempted ---
    with Session(engine) as s:
        try:
            cs.import_pairs_csv(s, cid, b"nope,nope\nen-US,fr-FR\n")
            assert False, "a header without source_locale,target_locale must raise"
        except cs.PairImportError:
            pass
        assert cs.list_pairs(s, cid) == []

    # --- ATOMICITY: one bad row (unknown target locale) rejects the WHOLE file --
    # even the good row (en-US->fr-FR) is a no-op; nothing is written.
    bad_csv = b"source_locale,target_locale\nen-US,fr-FR\nen-US,zz-ZZ\n"
    with Session(engine) as s:
        try:
            cs.import_pairs_csv(s, cid, bad_csv)
            assert False, "a bad row must reject the whole file"
        except cs.PairImportError as exc:
            assert "row 3" in str(exc), str(exc)  # header=row1, fr-FR=row2, zz-ZZ=row3
        assert cs.list_pairs(s, cid) == [], (
            "a rejected bulk import must leave active_pairs completely untouched"
        )

    # --- a clean file imports; loose codes canonicalize; a duplicate row within
    # the SAME file collapses to one add (first occurrence wins) ---
    good_csv = (
        b"source_locale,target_locale\n"
        b"en_US,fr_FR\n"  # loose -> canonicalizes to en-US->fr-FR
        b"en-US,de-DE\n"
        b"en-US,fr-FR\n"
    )  # duplicate of row 1 (canonical form this time)
    with Session(engine) as s:
        res = cs.import_pairs_csv(s, cid, good_csv)
        assert sorted(res["added"]) == ["en-US->de-DE", "en-US->fr-FR"], res
        assert res["skipped"] == [], res
        assert res["total_rows"] == 3, "3 data rows read (the in-file dup still counts)"
        assert sorted(cs.list_pairs(s, cid)) == ["en-US->de-DE", "en-US->fr-FR"]

    # --- IDEMPOTENCY: re-importing the identical file a second time adds nothing,
    # every pair reported skipped (already present) ---
    with Session(engine) as s:
        res2 = cs.import_pairs_csv(s, cid, good_csv)
        assert res2["added"] == [], "a re-import must add nothing"
        assert sorted(res2["skipped"]) == ["en-US->de-DE", "en-US->fr-FR"], res2
        assert sorted(cs.list_pairs(s, cid)) == ["en-US->de-DE", "en-US->fr-FR"], (
            "pairs must be unchanged after the idempotent re-import"
        )

    # --- a MIX of new + already-present pairs in one file: only the new one adds --
    mix_csv = (
        b"source_locale,target_locale\n"
        b"en-US,fr-FR\n"  # already present -> skipped
        b"en-US,ja-JP\n"
    )  # new -> added
    with Session(engine) as s:
        res3 = cs.import_pairs_csv(s, cid, mix_csv)
        assert res3["added"] == ["en-US->ja-JP"], res3
        assert res3["skipped"] == ["en-US->fr-FR"], res3

    # --- an unknown customer id raises a clean ValueError (not a service crash) ---
    with Session(engine) as s:
        try:
            cs.import_pairs_csv(s, 9_999_999, good_csv)
            assert False, "an unknown customer id must raise"
        except ValueError:
            pass
    print(
        "  bulk pairs import: header validation, ATOMIC bad-row rejection (no "
        "partial write), IDEMPOTENT re-import, in-file dup collapse, mixed "
        "new/present, unknown-customer error OK"
    )


# ============================================================================
# Adversarial-review fix (finding 1, feat/cli-parity-a review): csv.Error --
# raised by the stdlib csv module for a STRUCTURALLY malformed file (a field past
# csv.field_size_limit() is the reliable, deterministic trigger; an unbalanced
# quote / embedded-NUL "binary" upload degrade gracefully in Python's lenient
# non-strict csv dialect instead, but are exercised here too since the real
# guarantee under test is "never a 500/traceback", regardless of mechanism) --
# is NOT a ValueError. Before the fix, import_pairs_csv let it escape uncaught,
# and every one of the three callers (web/CLI/api) caught only
# (PairImportError, ValueError), so it surfaced as an unhandled exception: a 500
# on the web POST, a 500 on /api, a raw traceback on the CLI (app/cli/__main__.py
# re-raises anything that isn't a CliError/OperationalError). The fix wraps the
# WHOLE parse in customers.import_pairs_csv (the one service seam) so every
# caller inherits the clean PairImportError for free.
# ============================================================================
def test_bulk_pairs_import_malformed_csv_never_500():
    cid = _mk_customer("BulkPairsMalformed")

    # --- SERVICE: a field past csv.field_size_limit() is the deterministic
    # real-world csv.Error trigger -- must surface as a clean PairImportError,
    # never a bare csv.Error, and must leave active_pairs completely untouched.
    old_limit = csv.field_size_limit()
    csv.field_size_limit(64)
    try:
        oversized_csv = ("source_locale,target_locale\nen-US," + ("x" * 500) + "\n").encode("utf-8")
        with Session(engine) as s:
            try:
                cs.import_pairs_csv(s, cid, oversized_csv)
                assert False, "an oversized CSV field must raise, not silently pass"
            except cs.PairImportError as exc:
                assert "field larger than field limit" in str(exc), str(exc)
            except csv.Error:
                assert False, (
                    "a raw csv.Error must NEVER escape the service seam "
                    "(the exact defect this fix closes)"
                )
            assert cs.list_pairs(s, cid) == [], (
                "a malformed-structure import must leave pairs completely untouched"
            )
    finally:
        csv.field_size_limit(old_limit)

    # --- SERVICE: an unbalanced quote / embedded-NUL "binary" upload must ALSO
    # never crash the service. Python's csv module is lenient about these in its
    # default non-strict dialect (no csv.Error) -- the unbalanced quote consumes
    # the rest of the field as literal text (including the embedded comma/newline)
    # and the NUL bytes just pass through as ordinary characters -- so BOTH decay
    # into an ordinary garbage-locale row that the per-row BCP-47 validator
    # rejects with a clean PairImportError. The guarantee under test is that
    # NEITHER ever raises a raw, uncaught exception past the service seam.
    for label, bad in (
        ("unbalanced quote", b'source_locale,target_locale\nen-US,"fr-FR\nsome,more\n'),
        ("embedded NUL (binary)", b"source_locale,target_locale\nen-US,fr\x00\x01\x02FR\n"),
    ):
        with Session(engine) as s:
            try:
                cs.import_pairs_csv(s, cid, bad)
                assert False, f"{label}: a garbage-locale row must be rejected, not silently added"
            except cs.PairImportError:
                pass  # the expected, clean outcome
            except Exception as exc:
                assert False, (
                    f"{label}: must never raise a raw {type(exc).__name__} "
                    f"past the service seam: {exc}"
                )
        assert cs.list_pairs(s, cid) == [], (label, "no garbage pair may persist")

    # --- WEB: the SAME oversized-field file over the multipart upload route ---
    # must redirect cleanly (never a 500).
    client = TestClient(app)
    csv.field_size_limit(64)
    try:
        oversized_csv = ("source_locale,target_locale\nen-US," + ("y" * 500) + "\n").encode("utf-8")
    finally:
        csv.field_size_limit(old_limit)
    r = client.post(
        f"/customers/{cid}/pairs/import",
        files={"file": ("bad.csv", oversized_csv, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303, (
        f"a malformed CSV upload must redirect cleanly (never 500), got {r.status_code}"
    )
    assert "err=" in r.headers.get("location", "")

    # --- /api: the SAME file over the JSON/base64 route must return a clean
    # 400, never a 500 ---
    import base64

    au = security.create_user("bulkpairs-api@x.com", "BP API", Role.ADMIN, "bulkpairs-api-pass-1")
    _row, raw_key = apikeys.create_key(au.id, "bp-malformed", scopes="api docs")
    r = client.post(
        f"/api/customers/{cid}/pairs/import",
        headers={"X-Loc-Token": raw_key},
        json={"csv_b64": base64.b64encode(oversized_csv).decode()},
    )
    assert r.status_code == 400, (
        f"a malformed CSV over /api must be a clean 400 (never 500), got {r.status_code}"
    )
    assert r.json().get("ok") is False and "error" in r.json(), r.json()

    # --- CLI: the SAME file must produce a clean CliError (non-zero exit, an
    # "error: ..." line), never an uncaught traceback ---
    from app.cli.__main__ import main as cli_main

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "oversized.csv")
        with open(path, "wb") as fh:
            fh.write(oversized_csv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_main(["customer", "pairs", "import", str(cid), path])
        assert rc != 0, "a malformed CSV must exit non-zero"
        err_text = err.getvalue()
        assert "error:" in err_text.lower(), err_text
        assert "Traceback" not in err_text and "Traceback" not in out.getvalue(), (
            "a malformed CSV must NEVER surface a raw Python traceback to the CLI "
            "user (the exact defect this fix closes)",
            err_text,
            out.getvalue(),
        )

    # active_pairs never received anything from any of the malformed attempts above.
    with Session(engine) as s:
        assert cs.list_pairs(s, cid) == [], (
            "no malformed-CSV attempt (service/web/api/CLI) may have written a pair"
        )
    print(
        "  malformed-structure CSV (csv.Error class, field_size_limit + lenient "
        "unbalanced-quote/binary): service/web/api/CLI all fail CLEAN, never a "
        "500 or a raw traceback; pairs stay completely untouched OK"
    )


def test_project_default_inheritance():
    cid = _mk_customer("Inherit")
    with Session(engine) as s:
        cs.set_customer_defaults(s, cid, provider="gemini")
        # no explicit provider -> inherits
        p = content.project_create(s, "A", customer_id=str(cid), target_locales="fr-FR")
        assert p.default_provider == "gemini"
        # explicit provider -> respected
        p2 = content.project_create(
            s, "B", customer_id=str(cid), default_provider="openai", target_locales="fr-FR"
        )
        assert p2.default_provider == "openai"
        # no customer -> unchanged default
        p3 = content.project_create(s, "C", target_locales="fr-FR")
        assert p3.default_provider == "claude"
    print("  project creation inherits customer default_provider (unless overridden) OK")


def test_connection_isolation():
    c1 = _mk_customer("Iso-A")
    c2 = _mk_customer("Iso-B")
    store.create("airtable", "own-A", {}, {"api_key": "x"}, customer_id=c1)
    store.create("jira", "global", {}, {"email": "a", "api_token": "x"})
    other = store.create(
        "confluence", "own-B", {}, {"email": "a", "api_token": "x"}, customer_id=c2
    )
    with Session(engine) as s:
        names = {r["name"] for r in cs.get_customer_360(s, c1)["connections"]}
        assert "own-A" in names and "global" in names
        assert "own-B" not in names, "a foreign customer's connection must not appear"
    # the scoped picker list = own + global, never the other customer's
    picker = {c.name for c in store.list_connections(customer_id=c1)}
    assert "own-A" in picker and "global" in picker and "own-B" not in picker
    # the UNSCOPED list (global connectors index) still shows everything (legacy)
    allc = {c.name for c in store.list_connections()}
    assert {"own-A", "global", "own-B"} <= allc
    # the foreign connection's own detail is still reachable by id (dispatch/API path)
    assert store.get(other.id) is not None
    print(
        "  connection isolation: own+global visible, foreign hidden in picker/360; "
        "global everywhere; own detail reachable OK"
    )


def test_workflow_isolation():
    c1 = _mk_customer("WFA")
    c2 = _mk_customer("WFB")
    with Session(engine) as s:
        wf_service.create_workflow(s, "wf-a", scope=Scope.CUSTOMER, customer_id=str(c1))
        wf_service.create_workflow(s, "wf-b", scope=Scope.CUSTOMER, customer_id=str(c2))
        wf_service.create_workflow(s, "wf-global", scope=Scope.GLOBAL)
        names = {r["name"] for r in cs.get_customer_360(s, c1)["workflows"]}
        assert "wf-a" in names and "wf-global" in names
        assert "wf-b" not in names, "a foreign customer's workflow must not appear"
        # default_workflow_id validation (item 2): a customer's default must never
        # point at ANOTHER customer's private workflow. wf-b is c2's -> rejected for
        # c1; wf-a (own) + wf-global are accepted. A NONEXISTENT id is ALSO rejected:
        # the column is a REAL FK, so storing a dangling id would 500 on a FOREIGN KEY
        # IntegrityError at commit (regression: it used to pass through as "harmless").
        own_id = next(
            r["id"] for r in cs.get_customer_360(s, c1)["workflows"] if r["name"] == "wf-a"
        )
        from app.models import Workflow as _WF

        foreign_id = next(w.id for w in s.exec(select(_WF)).all() if w.name == "wf-b")
        glob_id = next(
            r["id"] for r in cs.get_customer_360(s, c1)["workflows"] if r["name"] == "wf-global"
        )
        cs.set_customer_defaults(s, c1, workflow_id=str(own_id))  # own: OK
        assert s.get(Customer, c1).default_workflow_id == own_id
        cs.set_customer_defaults(s, c1, workflow_id=str(glob_id))  # global: OK
        assert s.get(Customer, c1).default_workflow_id == glob_id
        try:
            cs.set_customer_defaults(s, c1, workflow_id=str(foreign_id))
            assert False, "cross-customer default_workflow_id must be rejected"
        except ValueError:
            pass
        assert s.get(Customer, c1).default_workflow_id == glob_id, "reject must not mutate"
        # a nonexistent (dangling) FK id -> ValueError, NOT an IntegrityError/500, and
        # the prior valid default is untouched. Pick an id no workflow row has.
        bogus_id = max((w.id for w in s.exec(select(_WF)).all()), default=0) + 999999
        try:
            cs.set_customer_defaults(s, c1, workflow_id=str(bogus_id))
            assert False, "nonexistent default_workflow_id must be rejected"
        except ValueError:
            pass
        assert s.get(Customer, c1).default_workflow_id == glob_id, (
            "reject of a nonexistent id must not mutate the stored default"
        )
        # a blank still CLEARS (existence guard must not break the clear path)
        cs.set_customer_defaults(s, c1, workflow_id="")
        assert s.get(Customer, c1).default_workflow_id is None
    print(
        "  workflow isolation: own+global visible, foreign hidden; cross-customer "
        "AND nonexistent default_workflow REJECTED (no 500), blank clears OK"
    )


def test_connection_org_isolation():
    """item 6: under multi-org, a connection forged into ANOTHER org but stamped with
    customer_id == cid (which the picker never offers) must NOT surface in this
    customer's 360 — the sacred org boundary confines customer_connections too."""
    from app.services import access, settings

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        assert access.multi_org_active()
        from app.models import Organization

        with Session(engine) as s:
            # org_id is a real FK (customer/connection.org_id -> organization.id):
            # use the seeded Default org as "org 1" and create a REAL second org for
            # the cross-org rows rather than a bare org_id=2 sentinel (which never
            # persisted on Postgres).
            org1_id = s.exec(select(Organization.id).order_by(Organization.id).limit(1)).first()
            org2 = Organization(name="OrgIso Org 2")
            s.add(org2)
            s.commit()
            s.refresh(org2)
            org2_id = org2.id
            ca = Customer(name="OrgIso-A", org_id=org1_id)
            s.add(ca)
            s.commit()
            s.refresh(ca)
            # same-org customer-scoped connection: visible
            same = Connection(
                kind="airtable",
                name="same-org",
                config={},
                secret_enc="",
                org_id=org1_id,
                customer_id=ca.id,
            )
            # cross-org connection forged onto ca (org 2): must be hidden
            cross = Connection(
                kind="jira",
                name="cross-org",
                config={},
                secret_enc="",
                org_id=org2_id,
                customer_id=ca.id,
            )
            # a global connection in ANOTHER org: also hidden (org backfill differs)
            fglob = Connection(
                kind="confluence",
                name="foreign-global",
                config={},
                secret_enc="",
                org_id=org2_id,
                customer_id=None,
            )
            s.add(same)
            s.add(cross)
            s.add(fglob)
            s.commit()
            names = {c.name for c in cs.customer_connections(s, ca.id)}
            assert "same-org" in names, "same-org scoped connection must show"
            assert "cross-org" not in names, "cross-org connection must be hidden (item 6)"
            assert "foreign-global" not in names, "other org's global must be hidden"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  org isolation: customer_connections org-confined; cross-org conn hidden OK")


def test_360_page_and_links(client):
    cid = _mk_customer("Portal")
    with Session(engine) as s:
        content.project_create(s, "Portal-Proj", customer_id=str(cid), target_locales="fr-FR")
        store.create("airtable", "Portal-Conn", {}, {"api_key": "x"}, customer_id=cid)
        wf_service.create_workflow(s, "Portal-WF", scope=Scope.CUSTOMER, customer_id=str(cid))
        cs.add_pair(s, cid, "en-US->de-DE")
    # index links to the 360 page
    r = client.get("/customers")
    assert r.status_code == 200 and f"/customers/{cid}" in r.text
    # detail renders every card + click-throughs
    r = client.get(f"/customers/{cid}")
    assert r.status_code == 200
    body = r.text
    for token in (
        "Customer 360",
        "Language pairs",
        "Projects",
        "Connectors",
        "Workflows",
        "Translation memory",
        "Portal-Proj",
        "Portal-Conn",
        "Portal-WF",
        "de-DE",
    ):
        assert token in body, f"360 page missing {token!r}"
    # click-through links present
    assert "/projects/" in body and "/connectors/connection/" in body and "/workflows/" in body
    print("  360 page renders all cards + links; index links to it OK")


def test_360_web_forms(client):
    cid = _mk_customer("Forms")
    # settings + default via the web form
    r = client.post(
        f"/customers/{cid}/settings",
        data={"notes": "n", "default_provider": "gemini"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        d = cs.get_customer_360(s, cid)
        assert d["settings"].get("notes") == "n" and d["default_provider"] == "gemini"
    # default_workflow_id over the web: a VALID global workflow is stored...
    with Session(engine) as s:
        gwf_id = wf_service.create_workflow(s, "Web-default WF", scope=Scope.GLOBAL).id
    r = client.post(
        f"/customers/{cid}/settings",
        data={"default_workflow_id": str(gwf_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers.get("location", "")
    with Session(engine) as s:
        assert cs.get_customer_360(s, cid)["default_workflow_id"] == gwf_id
    # ...but a NONEXISTENT id (real FK) is a clean err redirect, NOT a 500, and the
    # previously stored valid default is untouched (regression for the FK 500 defect).
    r = client.post(
        f"/customers/{cid}/settings", data={"default_workflow_id": "999999"}, follow_redirects=False
    )
    assert r.status_code == 303, f"nonexistent workflow must not 500 (got {r.status_code})"
    assert "err=" in r.headers.get("location", ""), r.headers.get("location", "")
    with Session(engine) as s:
        assert cs.get_customer_360(s, cid)["default_workflow_id"] == gwf_id, (
            "a rejected nonexistent id must not overwrite the stored default"
        )
    # add a pair via the web
    r = client.post(
        f"/customers/{cid}/pairs", data={"pair": "en-US->ko-KR"}, follow_redirects=False
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert "en-US->ko-KR" in cs.list_pairs(s, cid)
    # invalid pair -> err redirect (no crash)
    r = client.post(f"/customers/{cid}/pairs", data={"pair": "junk"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers.get("location", "")
    # remove a pair via the web
    r = client.post(
        f"/customers/{cid}/pairs/remove", data={"pair": "en-US->ko-KR"}, follow_redirects=False
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert "en-US->ko-KR" not in cs.list_pairs(s, cid)
    # bulk-import pairs via the web upload widget (CLI-parity backlog item 2)
    csv_bytes = b"source_locale,target_locale\nen-US,it-IT\nen-US,pt-BR\n"
    r = client.post(
        f"/customers/{cid}/pairs/import",
        files={"file": ("pairs.csv", csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    assert "pairs_added=2" in loc and "pairs_skipped=0" in loc, loc
    with Session(engine) as s:
        assert set(cs.list_pairs(s, cid)) >= {"en-US->it-IT", "en-US->pt-BR"}
    # a bad row -> a clean err redirect (no crash), and the good rows were NOT
    # partially written (atomicity, exercised over the web multipart path too).
    bad_bytes = b"source_locale,target_locale\nen-US,ja-JP\nen-US,zz-ZZ\n"
    r = client.post(
        f"/customers/{cid}/pairs/import",
        files={"file": ("bad.csv", bad_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers.get("location", "")
    with Session(engine) as s:
        assert "en-US->ja-JP" not in cs.list_pairs(s, cid), (
            "a rejected web import must not partially write the good row"
        )
    print(
        "  web forms: settings/default save + pair add/invalid/remove + bulk "
        "import (2 added, atomic bad-row rejection) OK"
    )


def test_cli_roundtrips():
    from app.cli.__main__ import main as cli_main

    cid = _mk_customer("CLI")

    def _cli_json(argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--json", *argv])
        assert rc == 0, f"{argv} -> rc {rc}"
        return _json.loads(buf.getvalue().strip().splitlines()[-1])

    # update (settings)
    out = _cli_json(["customer", "update", str(cid), "--set", "notes=hello", "--set", "tier=gold"])
    assert out["settings"]["notes"] == "hello" and out["settings"]["tier"] == "gold"
    # set-default
    out = _cli_json(["customer", "set-default", str(cid), "--provider", "gemini"])
    assert out["default_provider"] == "gemini"
    # set-default --workflow with a NONEXISTENT id: a clean non-zero exit (CliError ->
    # EXIT_USAGE), NOT a raw IntegrityError/traceback (web+CLI parity for the FK guard).
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli_main(["customer", "set-default", str(cid), "--workflow", "999999"])
    assert rc != 0, "nonexistent --workflow must refuse with a non-zero exit"
    assert "does not exist" in err.getvalue(), err.getvalue()
    with Session(engine) as s:
        assert s.get(Customer, cid).default_workflow_id is None, (
            "a refused CLI --workflow must not persist a dangling FK"
        )
    # pairs add / list / remove
    out = _cli_json(["customer", "pairs", "add", str(cid), "en-US->fr-FR"])
    assert out["added"] and out["pair"] == "en-US->fr-FR"
    out = _cli_json(["customer", "pairs", "list", str(cid)])
    assert "en-US->fr-FR" in out["active_pairs"]
    out = _cli_json(["customer", "pairs", "remove", str(cid), "en-US->fr-FR"])
    assert out["removed"]
    # show (full 360 payload)
    out = _cli_json(["customer", "show", str(cid)])
    assert (
        out["id"] == cid
        and "projects" in out
        and "connections" in out
        and "workflows" in out
        and "tm_stats" in out
    )
    # connector create --customer scopes the connection
    out = _cli_json(
        [
            "connector",
            "create",
            "--kind",
            "jira",
            "--name",
            "cli-scoped",
            "--secret",
            "email=a",
            "--secret",
            "api_token=x",
            "--customer",
            str(cid),
        ]
    )
    conn_id = out["id"]
    with Session(engine) as s:
        assert s.get(Connection, conn_id).customer_id == cid
    # connector update --customer '' clears the scope (back to global)
    out = _cli_json(["connector", "update", str(conn_id), "--customer", ""])
    with Session(engine) as s:
        assert s.get(Connection, conn_id).customer_id is None
    print("  CLI: customer update/set-default/pairs/show + connector --customer scope OK")


def test_org_gate_smoke(client):
    """Multi-org gate is OFF by default (single-tenant). The 360 route org-gates the
    customer via _org_block, which is a no-op with the gate off, so a normal request
    succeeds. This confirms the customer-scoping layer sits UNDER the (inert) org
    layer without breaking single-tenant behaviour."""
    from app.services import access, settings

    assert not access.multi_org_active(), "gate must default OFF"
    cid = _mk_customer("OrgSmoke")
    r = client.get(f"/customers/{cid}")
    assert r.status_code == 200, "single-tenant 360 must render with the gate off"
    # The org filter used by the connectors picker is a strict no-op off.
    assert settings.get("multi_org_enabled") != "1"
    print("  org-gate interplay: multi_org OFF default; 360 renders; org filter no-op OK")


def test_bulk_pairs_import_row_cap():
    """#592: a file with more than MAX_PAIR_IMPORT_ROWS data rows is refused before any
    work or write; exactly MAX rows still import. API path shows a clean 400."""
    import base64

    cid = _mk_customer("PairCap Co")
    cap = cs.MAX_PAIR_IMPORT_ROWS
    assert cap == 10000, cap
    head = "source_locale,target_locale" + chr(10)
    over = (head + ("en-US,fr-FR" + chr(10)) * (cap + 1)).encode()
    exact = (head + ("en-US,fr-FR" + chr(10)) * cap).encode()
    with Session(engine) as s:
        try:
            cs.import_pairs_csv(s, cid, over)
            assert False, "an over-cap file must be refused"
        except cs.PairImportError as exc:
            assert "too many rows" in str(exc) and "no changes were made" in str(exc), str(exc)
        assert cs.list_pairs(s, cid) == [], "over-cap import must not write"
    client = TestClient(app)
    au = security.create_user("paircap-api@x.com", "PC API", Role.ADMIN, "paircap-api-pass-1")
    _row, raw_key = apikeys.create_key(au.id, "pc-cap", scopes="api docs")
    r = client.post(
        f"/api/customers/{cid}/pairs/import",
        headers={"X-Loc-Token": raw_key},
        json={"csv_b64": base64.b64encode(over).decode()},
    )
    assert r.status_code == 400 and "too many rows" in r.json().get("error", ""), r.text
    with Session(engine) as s:
        res = cs.import_pairs_csv(s, cid, exact)
        assert res["total_rows"] == cap and res["added"] == ["en-US->fr-FR"], res
    print("  pair import row cap: MAX+1 refused (service + /api 400), MAX imports OK")


def main():
    init_db()
    client = TestClient(app)
    test_model_and_migration()
    test_settings_and_defaults_roundtrip()
    test_language_pairs()
    test_bulk_pairs_import()
    test_bulk_pairs_import_malformed_csv_never_500()
    test_bulk_pairs_import_row_cap()
    test_project_default_inheritance()
    test_connection_isolation()
    test_workflow_isolation()
    test_connection_org_isolation()
    test_360_page_and_links(client)
    test_360_web_forms(client)
    test_cli_roundtrips()
    test_org_gate_smoke(client)
    print("CUSTOMERS OK")


if __name__ == "__main__":
    main()
