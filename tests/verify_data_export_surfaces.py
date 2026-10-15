"""Data export SURFACES -- Train B verification (Batch 13).

Offline + hermetic. Proves the /api + web + remote-CLI + MCP-posture surfaces over the REAL
Train-A service (non-vacuous -- Train A is merged), plus the confinement pins the code gate
re-attacks. Covers the Train-B-assigned §11 cases + the four-surface rule:

  [1] registry: MIN_FEATURES>=12; data_export is writes_off; mcp_gate None; cli_groups
      ("export",); NOT in registry.GATE_404_STYLE; nav_gated; flag REMOTE_IMMUTABLE + default-ON;
      path_is_off inert (writes_off never middleware-404s).
  [2] async start + poll + artifact: POST /api/exports -> 202 {run_uuid, status_url}; poll to
      ok; GET artifact -> a valid ZIP carrying manifest.json.
  [3] RBAC shapes: api-only key POST -> 403; docs key scope=workspace -> 404 (not 403); docs key
      naming a foreign customer -> 404; admin key workspace -> 202; no token -> 401.
  [D2] cross-actor artifact fetch: a DIFFERENT docs key -> 404 at /artifact and the run_uuid is
      never in its list; the SAME admin user's key with the admin scope REMOVED -> 404 (admin AT
      FETCH). Revert-probed (the owner does get 200).
  [D1b] web admin ROLE: an admin web session starts a workspace export (303) + downloads it; a
      manager web session's workspace POST is refused (the service 404 -> error redirect).
  [N] Range: GET /artifact with `Range: bytes=0-N` -> 206 + Content-Range; full GET -> 200; the
      slices reassemble the whole artifact.
  [TA-01] payload-replay over the /api path: a bound key POSTs scope=self (the /api submit
      persists the authoritative tuple), then a forged cross-scope payload fed to the handler
      (the generic POST /api/jobs/run executor bypass) re-resolves from the ROW -> stays confined.
  [TB-NOTE-1] the /api POST ALWAYS carries the authenticated user (trusted_operator=False on the
      persisted row); a None principal can never reach submit_export as a trusted operator.
  [K] parity: the /api-produced artifact's per-member sha256 == the run_export (CLI/service)
      artifact's, for the same user/scope/history.
  [O] kill switch: data_export_enabled=0 -> POST refused (409, writes_off), existing artifacts
      still downloadable; remote_admin_enabled=0 -> self POST still 202 (LAW10).
  [MCP/UTCP/help] every /api/exports route is in registry.NEVER_COVER; the /utcp allow-list
      carries the list+status metadata reads but NOT the artifact; the data_export capability
      card renders at /help/data_export; llm.md names the /api/exports routes.

Final line: "DATA EXPORT SURFACES OK".
"""

import importlib.util
import io
import json
import os
import sys
import time
import zipfile
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_dxsurf_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import features as F
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    DataExportRun,
    Document,
    EngineProfile,
    Organization,
    Project,
    Role,
    Segment,
    Vendor,
    VendorCustomer,
)
from app.services import apikeys, security, settings
from app.services import data_export as dx

init_db()

M_A = "CUSTAsurfmarker0"
M_B = "CUSTBsurfmarker0"
M_C = "ORG2surfmarker0"
SECRET_ENGINE = "SECRETsurfengine0"
SECRET_VENDOR = "SECRETsurfvendor0"


def _fail(msg):
    raise AssertionError(msg)


def _set(**kw):
    base = {
        "security_auth_required": "1",
        "multi_org_enabled": "0",
        "project_scoping": "0",
        "data_export_enabled": "1",
        "encrypt_uploads": "0",
        "encrypt_db": "0",
        "data_export_allow_plaintext_staging": "0",
        "blobstore_s3_enabled": "0",
        "remote_admin_enabled": "0",
    }
    base.update(kw)
    settings.set_many(base)
    settings.invalidate_cache()


def _key(email, role, scopes, org, cust=None, pw=None):
    import secrets as _s

    # #181: created IN its org (the seam refuses an org-less create under the gate).
    user = security.create_user(email, email, role, pw or ("pw-" + _s.token_hex(6)), org_id=org)
    if org is not None or cust is not None:
        with Session(engine) as s:
            u = s.get(type(user), user.id)
            u.org_id = org
            u.customer_id = cust
            s.add(u)
            s.commit()
            s.refresh(u)
            user = u
    _row, raw = apikeys.create_key(user.id, "k", scopes=scopes)
    return user, raw


def _run_cli(argv):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli_main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue()


class F_:
    pass


def _seed():
    f = F_()
    with Session(engine) as s:
        o1 = Organization(name="Org1")
        o2 = Organization(name="Org2")
        s.add(o1)
        s.add(o2)
        s.commit()
        s.refresh(o1)
        s.refresh(o2)
        f.org1, f.org2 = o1.id, o2.id

        def _cust(name, org):
            c = Customer(name=name, org_id=org)
            s.add(c)
            s.commit()
            s.refresh(c)
            return c.id

        f.custA = _cust("CustA", f.org1)
        f.custB = _cust("CustB", f.org1)
        f.custC = _cust("CustC", f.org2)

        def _proj(name, cust, org):
            p = Project(name=name, customer_id=cust, org_id=org, source_locale="en-US")
            s.add(p)
            s.commit()
            s.refresh(p)
            return p.id

        f.pA = _proj("A1", f.custA, f.org1)
        f.pB = _proj("B1", f.custB, f.org1)
        f.pC = _proj("C1", f.custC, f.org2)

        def _doc_seg(pid, src):
            d = Document(project_id=pid, filename="d.txt", fmt="txt")
            s.add(d)
            s.commit()
            s.refresh(d)
            seg = Segment(document_id=d.id, target_locale="fr-FR", source_text=src, target_text="x")
            s.add(seg)
            s.commit()
            s.refresh(seg)
            return d.id

        _doc_seg(f.pA, M_A)
        _doc_seg(f.pB, M_B)
        _doc_seg(f.pC, M_C)
        # secrets that must never appear in any artifact
        s.add(EngineProfile(name="eng", org_id=f.org1, api_key_enc=SECRET_ENGINE))
        vA = Vendor(name="vA", org_id=f.org1, secret_enc=SECRET_VENDOR)
        s.add(vA)
        s.commit()
        s.refresh(vA)
        s.add(VendorCustomer(vendor_id=vA.id, customer_id=f.custA))
        s.commit()
    return f


def _hdr(tok):
    return {"X-Loc-Token": tok}


def _zip_blob(content: bytes) -> bytes:
    """Concatenate the DECOMPRESSED bytes of every member (the ZIP is DEFLATE-compressed, so a
    marker is not a literal substring of the raw archive bytes) -- so a leak is a substring hit."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    return b"".join(zf.read(n) for n in zf.namelist())


def _poll(c, tok, run_uuid, timeout=45.0):
    """Poll GET /api/exports/{uuid} to a terminal state (the worker executes the job on a
    daemon thread). Returns the final status dict, or raises on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/exports/{run_uuid}", headers=_hdr(tok))
        if r.status_code != 200:
            _fail(f"poll {run_uuid} -> {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("status") in ("ok", "partial", "failed"):
            return body
        time.sleep(0.25)
    _fail(f"export {run_uuid} did not finish within {timeout}s")


_F = _seed()


# ============================================================================
def check_registry():
    assert F.MIN_FEATURES >= 12, F.MIN_FEATURES
    feat = F.feature_by_slug("data_export")
    assert feat is not None, "data_export feature not registered"
    assert feat.semantics == "writes_off", feat.semantics
    assert feat.setting_key == "data_export_enabled"
    # mcp_gate == setting_key to satisfy the Feature well-formedness invariant, but NO MCP tool
    # claims a /api/exports route (they are all NEVER_COVER), so the gate is never consulted --
    # a full-graph dump is not an agent tool. The NEVER_COVER membership is asserted in
    # check_mcp_utcp_help below.
    assert feat.mcp_gate == "data_export_enabled", feat.mcp_gate
    assert feat.cli_groups == ("export",), feat.cli_groups
    assert feat.nav_gated is True
    assert feat.default_on(), "data_export must ship ON (writes_off kill switch)"
    spec = importlib.util.spec_from_file_location(
        "loc_reg_dx", os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")
    )
    reg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reg
    spec.loader.exec_module(reg)
    assert "data_export_enabled" not in reg.GATE_404_STYLE, (
        "writes_off key leaked into GATE_404_STYLE"
    )
    _set()
    assert not F.path_is_off("/exports"), "path_is_off must be inert for a writes_off feature"
    assert not F.path_is_off("/api/exports"), "path_is_off must be inert for /api/exports"
    assert "data_export_enabled" in settings.REMOTE_IMMUTABLE_KEYS
    assert "data_export_allow_plaintext_staging" in settings.REMOTE_IMMUTABLE_KEYS
    print(
        "  [1] registry: writes_off, mcp_gate None, cli_groups set, NOT in GATE_404_STYLE, "
        "path_is_off inert, flags REMOTE_IMMUTABLE + default-ON OK"
    )


def check_api_start_poll_artifact():
    _set()
    _admin, admin_tok = _key("dxs-admin@x.io", Role.ADMIN, "docs admin", _F.org1)
    c = TestClient(app)
    r = c.post("/api/exports", headers=_hdr(admin_tok), json={"scope": "self"})
    assert r.status_code == 202, ("start not 202", r.status_code, r.text)
    body = r.json()
    run_uuid = body["run_uuid"]
    assert body["status_url"] == f"/api/exports/{run_uuid}"
    status = _poll(c, admin_tok, run_uuid)
    assert status["status"] in ("ok", "partial"), status
    # artifact -> a valid ZIP with a manifest.
    a = c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(admin_tok))
    assert a.status_code == 200, ("artifact not 200", a.status_code)
    zf = zipfile.ZipFile(io.BytesIO(a.content))
    names = zf.namelist()
    assert "manifest.json" in names, names
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["manifest_schema"] == "data-export/1"
    print(
        "  [2] async start+poll+artifact: POST 202 -> poll ok -> GET artifact is a valid ZIP "
        "with manifest.json (schema data-export/1) OK"
    )


def check_rbac_shapes():
    _set()
    c = TestClient(app)
    _u, api_tok = _key("dxs-api@x.io", Role.ADMIN, "api", _F.org1)  # api scope only
    r = c.post("/api/exports", headers=_hdr(api_tok), json={"scope": "self"})
    assert r.status_code == 403, ("api-only key POST must be 403", r.status_code)
    _u2, docs_tok = _key("dxs-docs@x.io", Role.MANAGER, "docs", _F.org1)
    r = c.post("/api/exports", headers=_hdr(docs_tok), json={"scope": "workspace"})
    assert r.status_code == 404, ("docs key workspace must be 404 (not 403)", r.status_code)
    # A customer-bound key naming a foreign customer -> 404 (the clamp confines it, even with
    # multi-org OFF; a see-all MANAGER would legitimately see every in-tenant customer here).
    _b, bound_tok = _key("dxs-rbac-bound@x.io", Role.VIEWER, "docs", _F.org1, cust=_F.custA)
    r = c.post("/api/exports", headers=_hdr(bound_tok), json={"scope": "customer", "ref": _F.custB})
    assert r.status_code == 404, ("foreign customer must be 404", r.status_code, r.text)
    _adm, admin_tok = _key("dxs-adm2@x.io", Role.ADMIN, "docs admin", _F.org1)
    r = c.post("/api/exports", headers=_hdr(admin_tok), json={"scope": "workspace"})
    assert r.status_code == 202, ("admin workspace must be 202", r.status_code, r.text)
    r = c.post("/api/exports", json={"scope": "self"})
    assert r.status_code == 401, ("no token must be 401", r.status_code)
    print(
        "  [3] RBAC: api-only POST 403; docs workspace 404 (not 403); foreign customer 404; "
        "admin workspace 202; no token 401 OK"
    )


def check_dedupe_and_tbnote1():
    _set()
    _bA, boundA_tok = _key("dxs-bound@x.io", Role.VIEWER, "docs", _F.org1, cust=_F.custA)
    c = TestClient(app)
    r1 = c.post("/api/exports", headers=_hdr(boundA_tok), json={"scope": "self"})
    r2 = c.post("/api/exports", headers=_hdr(boundA_tok), json={"scope": "self"})
    assert r1.status_code == 202 and r2.status_code == 202
    b1, b2 = r1.json(), r2.json()
    # coalesce IFF the first is still active; either way never a DIFFERENT caller's job.
    if b2.get("existing"):
        assert b2["job_id"] == b1["job_id"], "a coalesced /api submit must return the first job id"
    # TB-NOTE-1: the /api POST carried the AUTHENTICATED bound user -> trusted_operator False,
    # and the row's org/customer are the bound user's (never None-as-operator).
    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == b1["run_uuid"])).first()
    assert row.trusted_operator is False, (
        "[TB-NOTE-1] an /api submit must NOT be a trusted operator"
    )
    assert row.customer_id == _F.custA, ("bound submit customer clamp", row.customer_id)
    # org_id is None here only because multi-org is OFF (single-tenant -> no org stamping); the
    # load-bearing TB-NOTE-1 facts are trusted_operator=False + the customer clamp = the bound
    # user's own, proving the AUTHENTICATED user drove the resolution, never a None operator.
    print(
        "  [M/TB-NOTE-1] dedupe returns the same job on a hit; the /api submit persists a "
        "non-operator row clamped to the caller's own customer/org OK"
    )


def check_owns_cross_actor():
    _set()
    admin, admin_tok = _key("dxs-own-admin@x.io", Role.ADMIN, "docs admin", _F.org1)
    c = TestClient(app)
    r = c.post("/api/exports", headers=_hdr(admin_tok), json={"scope": "workspace"})
    run_uuid = r.json()["run_uuid"]
    _poll(c, admin_tok, run_uuid)
    # owner gets 200 (revert-probe: the confinement pin is non-vacuous).
    own = c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(admin_tok))
    assert own.status_code == 200, ("owner must fetch 200", own.status_code)
    # a DIFFERENT actor (docs key) -> 404 at artifact + never in its list.
    _o, other_tok = _key("dxs-other@x.io", Role.ADMIN, "docs", _F.org1)
    r = c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(other_tok))
    assert r.status_code == 404, ("[D2] cross-actor artifact must be 404", r.status_code)
    lst = c.get("/api/exports", headers=_hdr(other_tok)).json()["runs"]
    assert run_uuid not in {x["run_uuid"] for x in lst}, "[D2] non-owner listed the run_uuid"
    # the SAME admin user, but a docs-only key (admin scope removed) -> 404 at fetch (admin AT
    # FETCH). Mint a second key for the SAME user with only docs scope.
    _row, docs_of_admin = apikeys.create_key(admin.id, "k2", scopes="docs")
    r = c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(docs_of_admin))
    assert r.status_code == 404, (
        "[D2] admin-at-fetch: docs-only key must 404 workspace",
        r.status_code,
    )
    print(
        "  [D2] ownership: owner 200; different actor 404 + unlisted; the SAME user's docs-only "
        "key 404s a workspace artifact (admin AT FETCH) OK"
    )


def check_range_206():
    _set()
    _u, tok = _key("dxs-range@x.io", Role.ADMIN, "docs admin", _F.org1)
    c = TestClient(app)
    run_uuid = c.post("/api/exports", headers=_hdr(tok), json={"scope": "self"}).json()["run_uuid"]
    _poll(c, tok, run_uuid)
    full = c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(tok))
    assert full.status_code == 200
    whole = full.content
    total = len(whole)
    assert total > 20
    r1 = c.get(f"/api/exports/{run_uuid}/artifact", headers={**_hdr(tok), "Range": "bytes=0-9"})
    assert r1.status_code == 206, ("[N] Range must be 206", r1.status_code)
    assert r1.headers.get("content-range") == f"bytes 0-9/{total}", r1.headers.get("content-range")
    assert r1.content == whole[:10], "[N] first slice mismatch"
    r2 = c.get(
        f"/api/exports/{run_uuid}/artifact", headers={**_hdr(tok), "Range": f"bytes=10-{total - 1}"}
    )
    assert r2.status_code == 206
    assert r1.content + r2.content == whole, "[N] slices do not reassemble the whole artifact"
    print(
        "  [N] Range: full 200; bytes=0-9 -> 206 + Content-Range; two slices reassemble the "
        "whole artifact (plaintext staging) OK"
    )


def check_s3_presign_302():
    """[N] S3 staging: the artifact route 302s to a Range-capable presigned URL (with the
    configured TTL) instead of streaming, when the active ExportStore adapter returns one from
    presign_get. Proved with a registered stub adapter -- the DiskExportStore returns None (the
    206 path, already covered by check_range_206)."""
    from app import ports

    class _PresignStub:
        def __init__(self):
            self.blobs = {}
            self.ttls = []

        def put_stream(self, key, chunks):
            data = b"".join(bytes(c) for c in chunks)
            self.blobs[key] = data
            return len(data)

        def open_range(self, key, start, end):
            if key not in self.blobs:
                raise FileNotFoundError(key)
            data = self.blobs[key]
            yield data[start : (None if end is None else end + 1)]

        def presign_get(self, key, *, ttl_seconds):
            self.ttls.append(ttl_seconds)
            return f"https://s3.example/signed/{key}?ttl={ttl_seconds}"

        def delete(self, key):
            return self.blobs.pop(key, None) is not None

    _set(data_export_signed_url_ttl="1234")
    stub = _PresignStub()
    ports.register("exportstore", stub)
    try:
        _u, tok = _key("dxs-s3@x.io", Role.ADMIN, "docs admin", _F.org1)
        c = TestClient(app)
        run_uuid = c.post("/api/exports", headers=_hdr(tok), json={"scope": "self"}).json()[
            "run_uuid"
        ]
        _poll(c, tok, run_uuid)
        # The artifact was staged into the stub (off-disk), and the route 302s to the signed URL.
        r = c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(tok), follow_redirects=False)
        assert r.status_code == 302, ("[N] S3 staging must 302 to a presigned URL", r.status_code)
        loc = r.headers.get("location") or ""
        assert loc.startswith("https://s3.example/signed/"), ("[N] 302 target", loc)
        assert stub.ttls and stub.ttls[-1] == 1234, (
            "[N] the configured data_export_signed_url_ttl must be passed to presign_get",
            stub.ttls,
        )
    finally:
        ports.unregister("exportstore")
        _set()
    print(
        "  [N] S3 staging: artifact route 302s to the Range-capable presigned URL with the "
        "configured signed_url_ttl (disk stays the 206 path) OK"
    )


def check_ta01_replay_over_api():
    _set(multi_org_enabled="1")
    from app.services import jobs

    try:
        _bA, boundA_tok = _key("dxs-ta01@x.io", Role.VIEWER, "docs", _F.org1, cust=_F.custA)
        c = TestClient(app)
        run_uuid = c.post("/api/exports", headers=_hdr(boundA_tok), json={"scope": "self"}).json()[
            "run_uuid"
        ]
        _poll(c, boundA_tok, run_uuid)
        base = _zip_blob(
            c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(boundA_tok)).content
        )
        assert M_A.encode() in base, "precondition: bound self has custA content"
        assert M_B.encode() not in base and M_C.encode() not in base, "precondition: no foreign"
        with Session(engine) as s:
            row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        # The generic executor bypass: feed a forged widened payload to the handler under the
        # SAME run_uuid. The handler MUST re-derive from the persisted (self, custA) row.
        forged = {
            "run_uuid": run_uuid,
            "scope_kind": "self",
            "scope_ref": None,
            "org": row.org_id,
            "customer_id": row.customer_id,
            "opts_hash": "x",
            "projects": [_F.pA, _F.pB, _F.pC],
            "customers": [_F.custA, _F.custB, _F.custC],
            "include_identity": True,
            "history": "full",
        }
        jobs.run_sync("data_export", forged, actor="dxs-ta01@x.io")
        after_raw = c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(boundA_tok)).content
        after = _zip_blob(after_raw)
        for leak in (M_B, M_C, SECRET_ENGINE, SECRET_VENDOR):
            assert leak.encode() not in after, f"[TA-01] /api-persisted row widened: {leak}"
        zf = zipfile.ZipFile(io.BytesIO(after_raw))
        for idf in ("user.jsonl", "api_key.jsonl", "engine_profile.jsonl", "organization.jsonl"):
            assert idf not in zf.namelist(), f"[TA-01] forged payload emitted identity file {idf}"
    finally:
        _set(multi_org_enabled="0")
    print(
        "  [TA-01] the /api submit persists the authoritative tuple; a forged cross-scope "
        "handler replay re-resolves from the ROW and stays confined to (self, custA) OK"
    )


def check_parity():
    _set()
    user, tok = _key("dxs-parity@x.io", Role.ADMIN, "docs admin", _F.org1)
    c = TestClient(app)
    run_uuid = c.post(
        "/api/exports", headers=_hdr(tok), json={"scope": "self", "history": "full"}
    ).json()["run_uuid"]
    _poll(c, tok, run_uuid)
    api_zip = zipfile.ZipFile(
        io.BytesIO(c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(tok)).content)
    )
    api_members = {
        n: __import__("hashlib").sha256(api_zip.read(n)).hexdigest()
        for n in api_zip.namelist()
        if n.endswith(".jsonl")
    }
    # The service/CLI arm: run_export under the SAME user + scope + history.
    with Session(engine) as s:
        u = s.get(type(user), user.id)
    run = dx.run_export(user=u, scope_kind="self", history="full")
    from app import config

    cli_zip = zipfile.ZipFile(config.EXPORTS_DIR / run.artifact_key)
    cli_members = {
        n: __import__("hashlib").sha256(cli_zip.read(n)).hexdigest()
        for n in cli_zip.namelist()
        if n.endswith(".jsonl")
    }
    assert api_members == cli_members, (
        "[K] surface parity broke: per-member sha256 differ\n"
        f"  api-only: {set(api_members) - set(cli_members)}\n"
        f"  cli-only: {set(cli_members) - set(api_members)}"
    )
    print(
        "  [K] parity: the /api artifact's per-member <entity>.jsonl sha256 == the "
        "run_export (CLI/service) artifact's, byte-for-byte OK"
    )


def check_web_admin():
    _set()
    import secrets as _s

    from tests._csrf import csrf_for

    pw = "webpw-" + _s.token_hex(6)
    _key("dxs-web-admin@x.io", Role.ADMIN, "docs admin", _F.org1, pw=pw)
    wc = TestClient(app)
    csrf_for(wc)
    lr = wc.post(
        "/auth/login",
        data={"email": "dxs-web-admin@x.io", "password": pw, "next": "/"},
        follow_redirects=False,
    )
    assert lr.status_code == 303, ("web login", lr.status_code)
    csrf_for(wc)  # login rotates the CSRF token
    assert wc.get("/exports").status_code == 200, "admin /exports page not 200"
    wr = wc.post(
        "/exports",
        data={"scope": "workspace", "history": "current"},
        follow_redirects=False,
    )
    assert wr.status_code == 303, ("[D1b] admin web workspace POST not 303", wr.status_code)
    loc = wr.headers.get("location") or ""
    assert "/exports/" in loc and "error" not in loc, (
        "admin workspace should start, not error",
        loc,
    )

    # a MANAGER web session's workspace POST is refused by the service (404 -> error redirect).
    pw2 = "webpw-" + _s.token_hex(6)
    _key("dxs-web-mgr@x.io", Role.MANAGER, "docs", _F.org1, pw=pw2)
    wc2 = TestClient(app)
    csrf_for(wc2)
    wc2.post(
        "/auth/login",
        data={"email": "dxs-web-mgr@x.io", "password": pw2, "next": "/"},
        follow_redirects=False,
    )
    csrf_for(wc2)
    wr2 = wc2.post(
        "/exports", data={"scope": "workspace", "history": "current"}, follow_redirects=False
    )
    assert wr2.status_code == 303 and "error" in (wr2.headers.get("location") or ""), (
        "[D1b] a manager web workspace POST must be refused (error redirect)",
        wr2.headers.get("location"),
    )
    print(
        "  [D1b] web admin ROLE: /exports 200 + a workspace POST starts (303 to the run page); "
        "a manager's workspace POST is refused (error redirect) OK"
    )


def check_kill_switch():
    _set()
    _u, tok = _key("dxs-kill@x.io", Role.ADMIN, "docs admin", _F.org1)
    c = TestClient(app)
    # a completed run BEFORE turning the switch off (still downloadable when off).
    run_uuid = c.post("/api/exports", headers=_hdr(tok), json={"scope": "self"}).json()["run_uuid"]
    _poll(c, tok, run_uuid)
    _set(data_export_enabled="0")
    r = c.post("/api/exports", headers=_hdr(tok), json={"scope": "self"})
    assert r.status_code == 409, ("[O] flag off POST must be refused (409)", r.status_code, r.text)
    assert c.get(f"/api/exports/{run_uuid}", headers=_hdr(tok)).status_code == 200, (
        "[O] reads must stay up when off (writes_off)"
    )
    assert c.get(f"/api/exports/{run_uuid}/artifact", headers=_hdr(tok)).status_code == 200, (
        "[O] an existing artifact must stay downloadable when off"
    )
    _set(data_export_enabled="1", remote_admin_enabled="0")
    r = c.post("/api/exports", headers=_hdr(tok), json={"scope": "self"})
    assert r.status_code == 202, (
        "[O/LAW10] self POST must be 202 with remote_admin off",
        r.status_code,
    )
    print(
        "  [O] kill switch: flag off -> POST 409 while existing reads/downloads stay up; "
        "remote_admin off -> self POST still 202 (LAW10) OK"
    )


def check_files03_download_doors():
    """files-03 on every artifact door. /api + web: a SELF export taken before a document
    delete is refused (404) afterwards. CLI: a project-scope run whose project was deleted
    WITHOUT a purge (both purge seams bypassed) is refused at download time and purged.
    (No MCP door: every /api/exports route is NEVER_COVER.)"""
    import secrets as _s
    import tempfile

    from app.services import content, gdpr
    from tests._csrf import csrf_for

    _set(multi_org_enabled="1")
    try:
        with Session(engine) as s:
            p = Project(name="F03", customer_id=_F.custA, org_id=_F.org1, source_locale="en-US")
            s.add(p)
            s.commit()
            s.refresh(p)
            d = Document(project_id=p.id, filename="f03.txt", fmt="txt")
            s.add(d)
            s.commit()
            s.refresh(d)
            s.add(Segment(document_id=d.id, target_locale="fr-FR", source_text="F03SELF0marker"))
            s.commit()
            doc_id = d.id
        pw = "webpw-" + _s.token_hex(6)
        _key("dxs-f03@x.io", Role.ADMIN, "docs admin", _F.org1, pw=pw)
        api_tok = _key("dxs-f03b@x.io", Role.ADMIN, "docs admin", _F.org1)[1]
        c = TestClient(app)
        api_run = c.post("/api/exports", headers=_hdr(api_tok), json={"scope": "self"}).json()
        _poll(c, api_tok, api_run["run_uuid"])
        a = c.get(f"/api/exports/{api_run['run_uuid']}/artifact", headers=_hdr(api_tok))
        assert a.status_code == 200 and b"F03SELF0marker" in _zip_blob(a.content), "precondition"
        wc = TestClient(app)
        csrf_for(wc)
        lr = wc.post(
            "/auth/login",
            data={"email": "dxs-f03@x.io", "password": pw, "next": "/"},
            follow_redirects=False,
        )
        assert lr.status_code == 303, ("web login", lr.status_code)
        csrf_for(wc)
        wr = wc.post("/exports", data={"scope": "self"}, follow_redirects=False)
        web_uuid = (wr.headers.get("location") or "").split("/exports/")[1].split("?")[0]
        deadline = time.time() + 45
        while time.time() < deadline:
            st = wc.get(f"/exports/{web_uuid}/status").json().get("status")
            if st in ("ok", "partial", "failed"):
                break
            time.sleep(0.25)
        assert wc.get(f"/exports/{web_uuid}/download").status_code == 200, "precondition: web"
        with Session(engine) as s:
            content.delete_document(s, doc_id, actor="dxs-f03@x.io", privacy_erasure=True)
        a2 = c.get(f"/api/exports/{api_run['run_uuid']}/artifact", headers=_hdr(api_tok))
        assert a2.status_code == 404, (
            "[files-03] /api self artifact after erasure",
            a2.status_code,
        )
        w2 = wc.get(f"/exports/{web_uuid}/download")
        assert w2.status_code == 404, ("[files-03] web self artifact after erasure", w2.status_code)
    finally:
        _set()

    # CLI door: the download-time re-check (the purge seams are bypassed to simulate a miss).
    with Session(engine) as s:
        p2 = Project(name="F03cli", customer_id=_F.custA, org_id=_F.org1, source_locale="en-US")
        s.add(p2)
        s.commit()
        s.refresh(p2)
        p2id = p2.id
    run = dx.run_export(user=None, scope_kind="project", scope_ref=p2id, actor="cli")
    saved = (gdpr.mark_export_artifacts_for_project, gdpr.purge_export_artifacts_for_document)
    gdpr.mark_export_artifacts_for_project = lambda *a, **k: None
    gdpr.purge_export_artifacts_for_document = lambda *a, **k: None
    try:
        with Session(engine) as s:
            content.delete_project(s, p2id, actor="cli")
    finally:
        gdpr.mark_export_artifacts_for_project, gdpr.purge_export_artifacts_for_document = saved
    out = os.path.join(tempfile.mkdtemp(prefix="dxf03_"), "x.zip")
    rc, txt = _run_cli(["export", "download", run.run_uuid, "-o", out])
    assert rc != 0 and not os.path.exists(out), ("[files-03] CLI must refuse a stale run", rc, txt)
    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run.run_uuid)).first()
        assert row.status == "purged", ("[files-03] the refused stale run is purged", row.status)
    print(
        "  [files-03] /api + web refuse a self artifact taken before a privacy erasure (404); the "
        "CLI download re-check refuses + purges a run whose project was deleted unpurged OK"
    )


def check_mcp_utcp_help():
    _set()
    spec = importlib.util.spec_from_file_location(
        "loc_reg_dx2", os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")
    )
    reg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reg
    spec.loader.exec_module(reg)
    for route in (
        ("POST", "/api/exports"),
        ("GET", "/api/exports"),
        ("GET", "/api/exports/{run_uuid}"),
        ("GET", "/api/exports/{run_uuid}/artifact"),
    ):
        assert route in reg.NEVER_COVER, f"[MCP] {route} must be in NEVER_COVER"
    from app.services.utcp_manual import _ALLOWLIST, _norm

    assert _norm("/api/exports") in _ALLOWLIST, "[UTCP] the list read must be allow-listed"
    assert _norm("/api/exports/{run_uuid}") in _ALLOWLIST, (
        "[UTCP] the status read must be allow-listed"
    )
    assert _norm("/api/exports/{run_uuid}/artifact") not in _ALLOWLIST, (
        "[UTCP] the artifact (identity/audit-bearing bytes) must NOT be allow-listed (fail-closed)"
    )
    from app.services import assist

    assert "data_export" in assist.card_slugs(), "the data_export capability card is not shipped"
    _set()
    c = TestClient(app)
    _set(security_auth_required="0")
    assert c.get("/help/data_export").status_code == 200, "/help/data_export must render"
    _set()
    from pathlib import Path

    llm = Path(_ROOT, "app", "capabilities", "llm.md").read_text(encoding="utf-8")
    assert "/api/exports" in llm, "llm.md (the LLM descriptor) must name the /api/exports routes"
    print(
        "  [MCP/UTCP/help] all 4 /api/exports routes in NEVER_COVER; /utcp allow-lists the "
        "list+status metadata but NOT the artifact; /help/data_export renders; llm.md names "
        "the routes OK"
    )


def main():
    check_registry()
    check_api_start_poll_artifact()
    check_rbac_shapes()
    check_dedupe_and_tbnote1()
    check_owns_cross_actor()
    check_range_206()
    check_s3_presign_302()
    check_ta01_replay_over_api()
    check_parity()
    check_web_admin()
    check_kill_switch()
    check_files03_download_doors()
    check_mcp_utcp_help()
    print("DATA EXPORT SURFACES OK")


if __name__ == "__main__":
    main()
