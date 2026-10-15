"""Per-connector-KIND visibility guard (ffadmin Wave 5).

A DIFFERENT mechanism from the feature flags (verify_features.py): the Connectors
SECTION stays core/always-on; individual connector KINDS are enable/disableable at
runtime via the `connector_disabled_kinds` setting, and a disabled kind disappears
entirely following the kill-switch law -- and, per the DOCTRINE, is BYTE-IDENTICAL to
a never-built (unknown) kind on every user-facing surface (never a 404-vs-303/400 oracle).

REGISTRY-DERIVED: every per-kind assertion iterates app.services.connectors.REGISTRY
(no hand list of kinds), so a factory-added connector kind is swept automatically.

THE CHECKS
  1  accessor edge cases (unknown/malformed CSV ignored; CASE/WHITESPACE matrix -- MEDIUM-1
     fail-safe; set_kind_enabled prunes + is canonical; enabled_specs subtracts)
  2  DOCTRINE: disabled is byte-identical to unknown on GET/POST /connectors/{kind},
     /browse, and POST /api/connectors
  3  live per-kind sweep: disable -> tile absent, connection vanishes from /api list + its
     connection page + generic pull + per-kind translate route 404, create refused on
     web/api (== unknown) + CLI (names switch); re-enable restores each (reversible)
  4  deep seams: build(), build_fresh(), rotate_secret(), browse_containers_unsaved(),
     set_oauth_secret() EACH refuse a disabled kind independently (MEDIUM-2 + MEDIUM-3)
  5  ingest entrypoints (evidence A): translate_now/pull/push refuse readably naming the
     switch and NEVER stamp the healthy connection ERROR (Wave-3 MEDIUM-3 lesson)
  6  OAuth (HIGH): complete_authorize refuses BEFORE token egress (sabotage-proven load-
     bearing); set_oauth_secret writes no secret + leaves status untouched
  7  scheduler skips a disabled kind's connections (sync loop + airtable keep-alive), both
     directions
  8  MEDIUM cluster: /api/jobs/run single-tenant oracle 404; CLI `connector list` parity
     with /api/connectors (sabotage-proven); `oauth-status --kind` gated; prune-config
     --all excludes disabled; customer_connections filtered; a language-set delete is still
     BLOCKED (no orphan) by a disabled-kind connection
  9  manage + catalog render every REGISTRY kind; toggle POST validations (bad->400,
     unknown->404, valid->303, no-op skips write+audit)
 10  sabotage: no-op'ing the store.build deep seam lets a disabled kind build; dropping the
     /connectors enabled_specs filter un-hides the tile

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). ASCII-only prints. Final
line: "CONNECTOR VISIBILITY OK".
"""

import io
import os
import re
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta

import httpx

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_connvis_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from sqlmodel import func as _sf

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Connection, ConnStatus, Customer, Role
from app.services import (
    apikeys,
    customers,
    jobs,
    languages,
    scheduler,
    security,
    settings,
)
from app.services import (
    connectors as reg,
)
from app.services.connectors import oauth
from app.services.connectors import store as conn_store
from app.services.connectors.base import ConnectorError

_TOKEN: list = []
_TRANSLATE_RE = re.compile(r"^/api/connectors/\{cid\}/([^/]+)/translate$")


def _set(key, value):
    settings.set_many({key: value})
    settings.invalidate_cache()


def _dis(kind):
    _set("connector_disabled_kinds", kind)


def _en():
    _set("connector_disabled_kinds", "")


def _run_cli(argv):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli_main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue()


def _token() -> str:
    if _TOKEN:
        return _TOKEN[0]
    import secrets as _secrets

    email = f"connvis-{_secrets.token_hex(4)}@x.com"
    user = security.create_user(email, email, Role.ADMIN, "connvis-" + _secrets.token_hex(8))
    _row, raw = apikeys.create_key(user.id, "connvis", scopes="api docs")
    _TOKEN.append(raw)
    return raw


def _seed_connections() -> dict:
    cids = {}
    with Session(engine) as s:
        for kind in reg.REGISTRY:
            row = Connection(
                kind=kind,
                name=f"{kind} probe",
                config={},
                secret_enc=conn_store.enc_secret({}),
                created_by="guard",
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            cids[kind] = row.id
    return cids


def _kind_translate_routes() -> dict:
    from app.web._routes import walk_routes

    out = {}
    for route in walk_routes(app):
        p = getattr(route, "path", "") or ""
        m = _TRANSLATE_RE.match(p)
        if m and ("POST" in (getattr(route, "methods", None) or ())):
            out[m.group(1)] = p
    return out


# ---------------------------------------------------------------------------
# check 1 -- accessor edge cases + MEDIUM-1 case/whitespace matrix
# ---------------------------------------------------------------------------
def check_accessors():
    _en()
    assert reg.disabled_kinds() == set() and reg.kind_enabled("jira")
    assert len(reg.enabled_specs()) == len(reg.REGISTRY)

    _set("connector_disabled_kinds", "notarealkind")
    assert reg.disabled_kinds() == set() and reg.kind_enabled("jira")

    _set("connector_disabled_kinds", "jira,, ,notreal,  airtable")
    assert reg.disabled_kinds() == {"jira", "airtable"}, reg.disabled_kinds()
    assert reg.kind_enabled("shopify")

    # MEDIUM-1: case + whitespace must all DISABLE jira (the fail-safe direction). Before
    # normalization a mis-cased slug fell through and left jira ENABLED (fail-open).
    for v in ("JIRA", " Jira ", "jira,JIRA", "  jIrA,\tshopify "):
        _set("connector_disabled_kinds", v)
        assert not reg.kind_enabled("jira"), f"case/ws variant {v!r} left jira ENABLED"

    # set_kind_enabled writes a canonical sorted CSV, prunes unknowns, normalizes case
    _set("connector_disabled_kinds", "notreal, shopify")
    reg.set_kind_enabled("Jira", False)  # mixed-case input
    assert settings.get("connector_disabled_kinds") == "jira,shopify", settings.get(
        "connector_disabled_kinds"
    )
    reg.set_kind_enabled("shopify", True)
    assert settings.get("connector_disabled_kinds") == "jira"
    _en()
    print(
        "  [1] accessors: unknown/malformed CSV ignored; MEDIUM-1 case/whitespace matrix "
        "('JIRA'/' Jira '/'jira,JIRA') all disable jira; set_kind_enabled canonical OK"
    )


# ---------------------------------------------------------------------------
# check 2 -- DOCTRINE: disabled is byte-identical to unknown
# ---------------------------------------------------------------------------
def check_doctrine(cids):
    _set("security_auth_required", "0")
    tok = _token()
    H = {"X-Loc-Token": tok}
    c = TestClient(app)
    _dis("jira")
    unk = "zzznope"

    # GET /connectors/{kind}: disabled == unknown (both 303 -> /connectors)
    du = c.get(f"/connectors/{unk}", follow_redirects=False)
    dd = c.get("/connectors/jira", follow_redirects=False)
    assert (du.status_code, du.headers.get("location")) == (
        dd.status_code,
        dd.headers.get("location"),
    ), "GET /connectors/{{kind}}: disabled {!r} != unknown {!r}".format(
        (dd.status_code, dd.headers.get("location")),
        (du.status_code, du.headers.get("location")),
    )

    # POST /connectors/{kind} (web create): disabled == unknown
    pu = c.post(f"/connectors/{unk}", data={}, follow_redirects=False)
    pd = c.post("/connectors/jira", data={}, follow_redirects=False)
    assert (pu.status_code, pu.headers.get("location")) == (
        pd.status_code,
        pd.headers.get("location"),
    ), "POST /connectors/{kind}: disabled != unknown"

    # POST /connectors/{kind}/browse: disabled == unknown (byte-identical body)
    bu = c.post(f"/connectors/{unk}/browse", data={})
    bd = c.post("/connectors/jira/browse", data={})
    assert (bu.status_code, bu.json()) == (bd.status_code, bd.json()), (
        f"browse: disabled {(bd.status_code, bd.json())!r} != unknown {(bu.status_code, bu.json())!r}"  # noqa: E501
    )

    # POST /api/connectors: a disabled kind returns the SAME 400 'unknown connector kind:
    # <kind>' template an unknown slug of that same name would -- a client cannot tell
    # 'jira is disabled' from 'jira never existed' (both echo the input, same status).
    ad = c.post("/api/connectors", headers=H, json={"kind": "jira", "name": "x"})
    assert ad.status_code == 400 and ad.json() == {
        "ok": False,
        "error": "unknown connector kind: jira",
    }, (ad.status_code, ad.json())
    _en()
    print(
        "  [2] DOCTRINE: disabled == unknown byte-for-byte on GET/POST /connectors/{kind}, "
        "/browse, and POST /api/connectors (non-enumerable, no status/body oracle) OK"
    )


# ---------------------------------------------------------------------------
# check 3 -- live per-kind sweep (REGISTRY-derived)
# ---------------------------------------------------------------------------
def check_live_sweep(cids):
    _set("security_auth_required", "0")
    H = {"X-Loc-Token": _token()}
    tr = _kind_translate_routes()
    c = TestClient(app)
    unk_create = {"ok": False, "error": "Unknown connector."}

    for kind in reg.REGISTRY:
        cid = cids[kind]
        _en()
        listed = {x["id"] for x in c.get("/api/connectors", headers=H).json()["connections"]}
        assert cid in listed, f"enabled {kind}: cid missing from /api/connectors"
        assert c.get(f"/connectors/{kind}", follow_redirects=False).status_code == 200

        _dis(kind)
        assert not reg.kind_enabled(kind)
        home = c.get("/connectors").text
        assert (f'href="/connectors/{kind}"') not in home, f"disabled {kind} tile present"
        # type page == unknown (303 redirect)
        assert c.get(f"/connectors/{kind}", follow_redirects=False).status_code == 303
        # web create == unknown (303)
        assert c.post(f"/connectors/{kind}", data={}, follow_redirects=False).status_code == 303
        # browse == unknown body
        br = c.post(f"/connectors/{kind}/browse", data={})
        assert br.status_code == 404 and br.json() == unk_create, f"disabled {kind} browse"
        # api create == unknown-kind template for this kind
        ac = c.post("/api/connectors", headers=H, json={"kind": kind, "name": "x"})
        assert ac.status_code == 400 and ac.json() == {
            "ok": False,
            "error": f"unknown connector kind: {kind}",
        }, (kind, ac.json())
        # CLI create refuses naming the switch (local admin surface)
        rc, out = _run_cli(["connector", "create", "--kind", kind, "--name", "x"])
        assert rc != 0 and "connector_disabled_kinds" in out, f"disabled {kind} CLI create"
        # connection vanishes from /api/connectors
        listed = {x["id"] for x in c.get("/api/connectors", headers=H).json()["connections"]}
        assert cid not in listed, f"disabled {kind}: cid still listed"
        # connection page + generic pull + per-kind translate route 404 (indistinguishable
        # from a missing cid -- never-built)
        assert c.get("/connectors/connection/%d" % cid, follow_redirects=False).status_code == 404  # noqa: UP031
        assert c.post("/api/connectors/%d/pull" % cid, headers=H).status_code == 404  # noqa: UP031
        if kind in tr:
            assert (
                c.post("/api/connectors/%d/%s/translate" % (cid, kind), headers=H).status_code  # noqa: UP031
                == 404
            )
        rc, out = _run_cli(["connector", "show", str(cid)])
        assert rc != 0 and "connector_disabled_kinds" in out, f"disabled {kind} CLI show"

        _en()
        assert c.get(f"/connectors/{kind}", follow_redirects=False).status_code == 200
        listed = {x["id"] for x in c.get("/api/connectors", headers=H).json()["connections"]}
        assert cid in listed, f"re-enabled {kind}: cid not restored"
        assert c.get("/connectors/connection/%d" % cid, follow_redirects=False).status_code == 200  # noqa: UP031
    print(
        "  [3] live sweep: %d kinds -- disable hides tile, 404s connection/pull/translate, "  # noqa: UP031
        "refuses create (web/api == unknown, CLI names switch), drops from list; re-enable "
        "restores each OK" % len(reg.REGISTRY)
    )


# ---------------------------------------------------------------------------
# check 4 -- deep seams (MEDIUM-2 + MEDIUM-3: EACH pinned independently)
# ---------------------------------------------------------------------------
def check_deep_seams(cids):
    _dis("jira")
    with Session(engine) as s:
        conn = s.get(Connection, cids["jira"])

    def _refuses(fn):
        try:
            fn()
            return False
        except ConnectorError as e:
            return "connector_disabled_kinds" in str(e)

    # build() and build_fresh() pinned SEPARATELY (MEDIUM-3: removing one guard must go red)
    assert _refuses(lambda: conn_store.build(conn)), "build() did not refuse a disabled kind"
    assert _refuses(lambda: conn_store.build_fresh(cids["jira"])), "build_fresh() did not refuse"
    # browse_containers_unsaved + rotate_secret + set_oauth_secret (MEDIUM-2 sites)
    assert _refuses(lambda: conn_store.browse_containers_unsaved("jira", {}, {})), (
        "browse_containers_unsaved did not refuse"
    )
    ok, msg = conn_store.rotate_secret(cids["jira"], {"email": "a", "api_token": "b"})
    assert not ok and "connector_disabled_kinds" in msg, "rotate_secret did not refuse"
    assert _refuses(lambda: conn_store.set_oauth_secret(cids["jira"], {"access_token": "t"})), (
        "set_oauth_secret did not refuse"
    )
    _en()
    print(
        "  [4] deep seams: build / build_fresh / rotate_secret / browse_containers_unsaved / "
        "set_oauth_secret each refuse a disabled kind independently (all 4 client-build sites "
        "+ the direct credential write) OK"
    )


# ---------------------------------------------------------------------------
# check 5 -- ingest entrypoints never stamp a healthy connection ERROR (evidence A)
# ---------------------------------------------------------------------------
def check_ingest_no_error_stamp(cids):
    # The generic ingest entrypoints (jira connection) AND a FLOW-FAMILY kind
    # (zendesk_translate routes through flow.run_translate_flow -- the 9th seam) must ALL
    # return the clean (False, msg) contract and leave the healthy connection untouched.
    jcid, zcid = cids["jira"], cids["zendesk"]
    conn_store._set_status(jcid, ConnStatus.CONNECTED, "healthy pre-disable")
    conn_store._set_status(zcid, ConnStatus.CONNECTED, "healthy pre-disable")
    _set("connector_disabled_kinds", "jira,zendesk")
    entrypoints = (
        ("translate_now", jcid, lambda: conn_store.translate_now(jcid)),
        ("pull_connection", jcid, lambda: conn_store.pull_connection(jcid)),
        ("push_connection", jcid, lambda: conn_store.push_connection(jcid)),
        # flow family: a disabled generic-article kind must return the SAME clean contract
        # (not an uncaught ConnectorError logged as a crash + job FAILED).
        ("zendesk_translate", zcid, lambda: conn_store.zendesk_translate(zcid)),
    )
    for label, cid, fn in entrypoints:
        res = fn()
        ok, msg = res[0], res[1]
        assert ok is False and "connector_disabled_kinds" in msg, (
            f"{label}: refusal did not name the switch (ok={ok!r} msg={msg!r})"
        )
        with Session(engine) as s:
            st = s.get(Connection, cid).status
        assert st == ConnStatus.CONNECTED, (
            f"{label}: STAMPED the healthy connection {st!r} (must leave status untouched -- Wave-3 "  # noqa: E501
            "MEDIUM-3)"
        )

    # Sabotage (flow seam load-bearing): with _refuse_disabled_sync no-op'd, the flow family
    # falls through to build_fresh's deep seam, which RAISES an uncaught ConnectorError --
    # i.e. the clean (False, msg) contract at THIS seam is what the new flow gate provides.
    orig = conn_store._refuse_disabled_sync
    set_pieces("app.services.connectors.store", "_refuse_disabled_sync", lambda cid, **kw: None)
    try:
        raised = False
        try:
            conn_store.zendesk_translate(zcid)
        except ConnectorError as e:
            raised = "connector_disabled_kinds" in str(e)
        assert raised, (
            "sabotage: no-op'ing _refuse_disabled_sync did NOT make the flow family raise "
            "-- the flow-seam gate is not load-bearing (vacuous)"
        )
    finally:
        set_pieces("app.services.connectors.store", "_refuse_disabled_sync", orig)
    _en()
    print(
        "  [5] ingest (evidence A): translate_now/pull/push AND the flow family "
        "(zendesk_translate, the 9th seam) refuse readably naming the switch and leave "
        "conn.status CONNECTED; flow-seam gate is sabotage-proven load-bearing OK"
    )


# ---------------------------------------------------------------------------
# check 6 -- OAuth (HIGH): no egress + no secret write for a disabled kind
# ---------------------------------------------------------------------------
def check_oauth(cids):
    cid = cids["jira"]
    conn_store._set_status(cid, ConnStatus.CONNECTED, "healthy")
    _dis("jira")
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": "leak"})

    def _fake_req():
        # base_url is only reached AFTER the kind check (in the sabotage arm); provide it so
        # the sabotaged call gets far enough to egress.
        return types.SimpleNamespace(
            base_url="http://testserver/",
            session={"conn_oauth_state": "S", "conn_oauth_cid": cid, "conn_oauth_kind": "jira"},
        )

    # complete_authorize refuses BEFORE the token exchange -- no egress, clean error
    res, err = oauth.complete_authorize(
        _fake_req(), "code", "S", transport=httpx.MockTransport(handler)
    )
    assert res is None and "connector_disabled_kinds" in (err or ""), (res, err)
    assert calls["n"] == 0, "complete_authorize EGRESSED to the provider for a disabled kind"

    # sabotage: with the kind_enabled gate forced True, complete_authorize proceeds and
    # DOES egress -- proving the guard line is load-bearing (not vacuous).
    orig = reg.kind_enabled
    reg.kind_enabled = lambda k: True
    try:
        oauth.complete_authorize(_fake_req(), "code", "S", transport=httpx.MockTransport(handler))
    finally:
        reg.kind_enabled = orig
    assert calls["n"] >= 1, (
        "sabotage: forcing kind_enabled True did NOT make complete_authorize egress -- the "
        "OAuth refusal is not the load-bearing line (vacuous)"
    )

    # set_oauth_secret writes no secret + leaves status untouched
    try:
        conn_store.set_oauth_secret(cid, {"access_token": "t"})
        raise AssertionError("set_oauth_secret did not refuse")
    except ConnectorError as e:
        assert "connector_disabled_kinds" in str(e)
    with Session(engine) as s:
        row = s.get(Connection, cid)
    assert conn_store.dec_secret(row.secret_enc) == {}, "a secret was written for a disabled kind"
    assert row.status == ConnStatus.CONNECTED, "set_oauth_secret refusal stamped status"
    _en()
    print(
        "  [6] OAuth (HIGH): complete_authorize refuses BEFORE any token egress (sabotage-"
        "proven load-bearing); set_oauth_secret writes no secret + leaves status untouched OK"
    )


# ---------------------------------------------------------------------------
# check 7 -- scheduler skips a disabled kind (sync loop + airtable keep-alive)
# ---------------------------------------------------------------------------
def check_scheduler(cids):
    _set("sync_scheduler_enabled", "1")
    _en()
    fired, ka = {"c": set()}, {"c": set()}
    o_submit, o_ka = jobs.submit_unique, conn_store.refresh_webhooks
    jobs.submit_unique = lambda kind, payload, actor="", **kw: (
        fired["c"].add(payload.get("connection_id")) or (999, True)
    )
    set_pieces(
        "app.services.connectors.store", "refresh_webhooks", lambda cid, now=None: ka["c"].add(cid)
    )
    try:
        for kind in ("jira", "airtable"):
            conn_store.patch_config(
                cids[kind],
                {
                    "sync_every_minutes": 60,
                    "sync_next_at": (datetime.utcnow() - timedelta(minutes=1)).isoformat(
                        timespec="seconds"
                    ),
                },
            )
        _set("connector_disabled_kinds", "jira,airtable")
        fired["c"].clear()
        ka["c"].clear()
        scheduler.tick()
        assert cids["jira"] not in fired["c"] and cids["airtable"] not in fired["c"], (
            "disabled synced"
        )
        assert cids["airtable"] not in ka["c"], "disabled airtable ran keep-alive"
        _en()
        fired["c"].clear()
        ka["c"].clear()
        scheduler.tick()
        assert cids["jira"] in fired["c"] and cids["airtable"] in fired["c"], "enabled did not sync"
        assert cids["airtable"] in ka["c"], "enabled airtable keep-alive did not run"
    finally:
        jobs.submit_unique = o_submit
        set_pieces("app.services.connectors.store", "refresh_webhooks", o_ka)
        for kind in ("jira", "airtable"):
            conn_store.patch_config(cids[kind], remove=("sync_every_minutes", "sync_next_at"))
        _en()
    print(
        "  [7] scheduler: disabled kinds skipped by the sync loop AND the airtable keep-"
        "alive; both fire again when re-enabled OK"
    )


# ---------------------------------------------------------------------------
# check 8 -- MEDIUM cluster (jobs/run oracle, list parity, oauth-status, prune-all,
#            customer-360, language-set delete block)
# ---------------------------------------------------------------------------
def check_medium_cluster(cids):
    _set("security_auth_required", "0")
    H = {"X-Loc-Token": _token()}
    c = TestClient(app)

    # (a) /api/jobs/run single-tenant oracle: a disabled-kind connector_translate is 404'd
    # at the door (above the single-tenant early-return), never accepted-then-leaked.
    _dis("jira")
    body = {"kind": "connector_translate", "payload": {"connection_id": cids["jira"]}}
    r = c.post("/api/jobs/run", headers=H, json=body)
    assert r.status_code == 404, (
        "jobs/run accepted a disabled-kind job (oracle): %d" % r.status_code  # noqa: UP031
    )
    _en()
    r = c.post("/api/jobs/run", headers=H, json=body)
    assert r.status_code != 404, (
        "jobs/run 404'd an ENABLED-kind job (over-block): %d" % r.status_code  # noqa: UP031
    )

    # (b) CLI `connector list` parity with GET /api/connectors -- both exclude disabled.
    # (--json is a GLOBAL flag before the subcommand; plain output tables the kind column,
    # so a disabled 'jira' row simply must not appear.)
    _dis("jira")
    rc, out = _run_cli(["--json", "connector", "list"])
    assert rc == 0 and "jira" not in out, (
        f"CLI list disclosed a disabled-kind connection: {out[:300]!r}"
    )
    api_kinds = {x["kind"] for x in c.get("/api/connectors", headers=H).json()["connections"]}
    assert "jira" not in api_kinds, "api list disclosed a disabled kind"
    # sabotage: forcing kind_enabled True re-discloses jira in the CLI list (load-bearing)
    orig = reg.kind_enabled
    reg.kind_enabled = lambda k: True
    try:
        rc, out2 = _run_cli(["--json", "connector", "list"])
    finally:
        reg.kind_enabled = orig
    assert "jira" in out2, "sabotage: removing the list filter did not re-disclose jira (vacuous)"

    # (c) oauth-status --kind gated
    rc, out = _run_cli(["connector", "oauth-status", "--kind", "jira"])
    assert rc != 0 and "connector_disabled_kinds" in out, "oauth-status --kind disclosed disabled"
    _en()
    rc, out = _run_cli(["connector", "oauth-status", "--kind", "jira"])
    assert rc == 0, "oauth-status --kind refused an ENABLED kind"

    # (d) prune-config --all never names a disabled-kind connection id
    _dis("jira")
    agg = conn_store.prune_all_orphaned_config(apply=False)
    named = {rep["connection_id"] for rep in agg.get("connections", [])}
    assert cids["jira"] not in named, "prune --all named a disabled-kind connection id"
    _en()

    # (e) customer_connections excludes a disabled kind
    with Session(engine) as s:
        cust = Customer(name="connvis-cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        row = s.get(Connection, cids["jira"])
        row.customer_id = cust.id
        s.add(row)
        s.commit()
        custid = cust.id
    _dis("jira")
    with Session(engine) as s:
        seen = {x.id for x in customers.customer_connections(s, custid)}
    assert cids["jira"] not in seen, "customer_connections rendered a disabled-kind row"
    _en()
    with Session(engine) as s:
        seen = {x.id for x in customers.customer_connections(s, custid)}
    assert cids["jira"] in seen, "customer_connections dropped an ENABLED-kind row"

    # (f) a language-set delete stays BLOCKED (no orphan) by a disabled-kind connection
    ls = languages.create("connvis-set", "en-US", ["fr-FR"])
    conn_store.patch_config(cids["jira"], {"language_set_id": str(ls.id)})
    _dis("jira")
    try:
        languages.delete(ls.id)
        raise AssertionError(
            "language-set delete SUCCEEDED with a hidden referencing "
            "connection -- would orphan the ref on re-enable"
        )
    except languages.LanguageSetInUseError as exc:
        assert exc.refs["connections"] >= 1, "the referencing connection was not counted"
    conn_store.patch_config(cids["jira"], remove=("language_set_id",))
    _en()
    print(
        "  [8] MEDIUM cluster: jobs/run single-tenant oracle 404s; CLI list == /api list "
        "(sabotage-proven); oauth-status --kind gated; prune --all hides disabled; "
        "customer-360 filtered; language-set delete still blocks (no orphan) OK"
    )


# ---------------------------------------------------------------------------
# check 9 -- manage + catalog render every kind; toggle POST validations
# ---------------------------------------------------------------------------
def check_manage_catalog():
    _set("security_auth_required", "0")
    _en()
    c = TestClient(app)
    manage = c.get("/connectors/manage")
    catalog = c.get("/connectors/catalog")
    assert manage.status_code == 200 and catalog.status_code == 200
    for kind in reg.REGISTRY:
        assert (f'action="/connectors/manage/{kind}"') in manage.text, f"manage missing {kind}"
        assert (f'href="/connectors/{kind}"') in catalog.text, f"catalog missing {kind}"
    assert (
        c.post("/connectors/manage/jira", data={"enabled": "2"}, follow_redirects=False).status_code
        == 400
    )
    assert (
        c.post("/connectors/manage/nope", data={"enabled": "1"}, follow_redirects=False).status_code
        == 404
    )
    assert (
        c.post("/connectors/manage/jira", data={"enabled": "0"}, follow_redirects=False).status_code
        == 303
    )
    assert not reg.kind_enabled("jira")

    def _n():
        with Session(engine) as s:
            return s.exec(
                select(_sf.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "connector_kind.toggle")
            ).one()

    before = _n()
    c.post("/connectors/manage/jira", data={"enabled": "0"}, follow_redirects=False)
    assert _n() == before, "no-op resubmit wrote an audit row"
    c.post("/connectors/manage/jira", data={"enabled": "1"}, follow_redirects=False)
    assert _n() == before + 1, "a real toggle did not write exactly one audit row"
    _en()
    print(
        "  [9] manage + catalog render every kind; toggle validates (bad->400, unknown->404, "
        "valid->303) and a no-op resubmit skips the write + audit row OK"
    )


# ---------------------------------------------------------------------------
# check 10 -- sabotage (page filter)
# ---------------------------------------------------------------------------
def check_sabotage():
    _dis("jira")
    orig = conn_store._refuse_disabled_kind
    with Session(engine) as s:
        conn = s.exec(select(Connection).where(Connection.kind == "jira")).first()
    try:
        try:
            conn_store.build(conn)
            raise AssertionError("deep seam did not refuse baseline")
        except ConnectorError:
            pass
        set_pieces("app.services.connectors.store", "_refuse_disabled_kind", lambda kind: None)
        try:
            conn_store.build(conn)
            sab = True
        except ConnectorError as e:
            sab = "connector_disabled_kinds" not in str(e)
        assert sab, "no-op'ing store._refuse_disabled_kind still blocked build (vacuous)"
    finally:
        set_pieces("app.services.connectors.store", "_refuse_disabled_kind", orig)

    _set("security_auth_required", "0")
    c = TestClient(app)
    assert 'href="/connectors/jira"' not in c.get("/connectors").text
    orig_es = reg.enabled_specs
    try:
        reg.enabled_specs = reg.all_specs
        assert 'href="/connectors/jira"' in c.get("/connectors").text, (
            "removing the enabled_specs filter did not un-hide the tile (vacuous)"
        )
    finally:
        reg.enabled_specs = orig_es
    _en()
    print(
        "  [10] sabotage-proven: no-op'ing store.build's gate lets a disabled kind build; "
        "dropping the /connectors enabled_specs filter un-hides its tile OK"
    )


def main():
    init_db()
    cids = _seed_connections()
    check_accessors()
    check_doctrine(cids)
    check_live_sweep(cids)
    check_deep_seams(cids)
    check_ingest_no_error_stamp(cids)
    check_oauth(cids)
    check_scheduler(cids)
    check_medium_cluster(cids)
    check_manage_catalog()
    check_sabotage()
    print("CONNECTOR VISIBILITY OK")


if __name__ == "__main__":
    main()
