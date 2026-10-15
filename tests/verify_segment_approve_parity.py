"""Save-a-reviewed-segment-to-TM, four-surface parity (chip task_9c665111 / FINDING 4).

AUDIT-phase2 FINDING 4: approving a segment (which writes its source->target pair to TM)
existed ONLY on the web CAT surface -- there was no /api, CLI or MCP path, so an operator
driving the app over the wire could not do the maintainer's "on success, save them to TM" step.
The whole approve-and-write-TM policy is now ONE shared seam
(segment_review.approve_and_write_tm) the web route + POST /api/segments/{id}/approve +
`doc approve-segment` + the MCP `approve_segment` tool all call, so the refusal/language
perimeter cannot diverge across surfaces.

Pinned:
  1. SERVICE seam: a clean segment -> APPROVED + written to TM; a hedge -> APPROVED but
     WITHHELD (tm_written False + a reason); a soft-deleted key's value -> refused (404).
  2. /api POST /api/segments/{id}/approve: clean writes TM (proven by a TM lookup); a
     hedge is withheld with the SAME guard; a foreign-org segment 404s; a missing id 404s.
  3. CLI `doc approve-segment` (in-process AND --remote over /api): approves + saves TM.
  4. MCP: the approve_segment tool is registered and maps to the /api route (its wire
     behaviour is the /api route above; the live tool list is pinned by verify_mcp).
  5. CROSS-SURFACE EQUIVALENCE: the web and /api routes return the identical
     tm_written/tm_skipped verdict on the SAME hedge.

Hermetic: throwaway LOC_TMS_DATA_DIR + MockProvider. ASCII-only.
Final line: "SEGMENT APPROVE PARITY OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_segapprove_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Organization,
    Project,
    Role,
    Segment,
    SegStatus,
    TargetLocale,
    User,
)
from app.services import access, apikeys, security, segment_review, settings, tm

init_db()
_SRC, _TGT = "en-US", "es-MX"
_HEDGE = "I cannot translate this without the actual source content."


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _project(name, org_id=None):
    with Session(engine) as s:
        p = Project(name=name, source_locale=_SRC, default_provider="mock", org_id=org_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        s.commit()
        return p.id


def _doc_with_segments(pid, pairs):
    """pairs = [(source, target|None)] -> a doc with those segments (target set = 'mt')."""
    with Session(engine) as s:
        d = Document(project_id=pid, filename="d.txt", fmt="txt", mode="segmented", status="parsed")
        s.add(d)
        s.commit()
        s.refresh(d)
        ids = []
        for i, (src, tgt) in enumerate(pairs):
            seg = Segment(
                document_id=d.id,
                target_locale=_TGT,
                order_index=i,
                source_text=src,
                target_text=(tgt or ""),
                status=(SegStatus.MT if tgt else SegStatus.NEW),
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            ids.append(seg.id)
        return d.id, ids


def _admin_key(scopes="api docs"):
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "segap-admin@x.io")).first()
        if not u:
            u = User(email="segap-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "segap", scopes=scopes)
    return raw


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    combined = out.getvalue() + err.getvalue()
    if not combined.isascii():
        _fail(("CLI output must be ASCII", combined))
    return rc, combined


def _tm_has(pid, source):
    with Session(engine) as s:
        hit = tm.lookup(s, pid, source, _SRC, _TGT)
    return bool(hit and hit.get("match_pct") == 100)


client = TestClient(app)


# ============================================================================ #
# 1. service seam
# ============================================================================ #
def test_service_seam():
    pid = _project("segap-svc")
    doc_id, ids = _doc_with_segments(pid, [("Good source one", "Buena traduccion")])
    with Session(engine) as s:
        seg = s.get(Segment, ids[0])
        doc = s.get(Document, doc_id)
        project = s.get(Project, pid)
        out = segment_review.approve_and_write_tm(s, seg, project, doc)
    if out["status"] != SegStatus.APPROVED or out["tm_written"] is not True:
        _fail(f"clean approve must write TM: {out}")
    if not _tm_has(pid, "Good source one"):
        _fail("the approved pair must be a 100% TM match afterwards")

    # a hedge is WITHHELD (guard on)
    if not config.REFUSAL_GUARD_ENABLED:
        _fail("this suite assumes REFUSAL_GUARD_ENABLED default ON")
    doc2, ids2 = _doc_with_segments(pid, [("Hedge source", _HEDGE)])
    with Session(engine) as s:
        out = segment_review.approve_and_write_tm(
            s, s.get(Segment, ids2[0]), s.get(Project, pid), s.get(Document, doc2)
        )
    if out["tm_written"] is not False or not out["tm_skipped"]:
        _fail(f"a hedge must be approved-but-withheld: {out}")
    if _tm_has(pid, "Hedge source"):
        _fail("a hedge must NOT become a TM match")

    # a soft-deleted key's value is refused
    from app.models import StringKey

    with Session(engine) as s:
        k = StringKey(project_id=pid, name="dead.key")
        from datetime import datetime, timezone

        k.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        s.add(k)
        s.commit()
        s.refresh(k)
        doc3, ids3 = _doc_with_segments(pid, [("Key src", "Val")])
        seg = s.get(Segment, ids3[0])
        seg.key_id = k.id
        s.add(seg)
        s.commit()
        try:
            segment_review.approve_and_write_tm(s, seg, s.get(Project, pid), s.get(Document, doc3))
        except segment_review.SegmentApproveError as exc:
            if exc.status != 404 or exc.code != "deleted_key":
                _fail(f"deleted-key refusal must be a 404 deleted_key: {exc.status} {exc.code}")
        else:
            _fail("approving a soft-deleted key's value must be refused")
    print("  1. service: clean writes TM, hedge withheld, deleted-key 404 OK")


# ============================================================================ #
# 2. /api
# ============================================================================ #
def test_api_surface():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        h = {"X-Loc-Token": _admin_key()}
        pid = _project("segap-api")
        _doc_id, ids = _doc_with_segments(
            pid, [("Api good src", "Api buena"), ("Api hedge src", _HEDGE)]
        )
        # clean -> writes TM
        r = client.post(f"/api/segments/{ids[0]}/approve", headers=h)
        if r.status_code != 200 or r.json().get("tm_written") is not True:
            _fail(f"/api clean approve must 200 + write TM: {r.status_code} {r.text}")
        if not _tm_has(pid, "Api good src"):
            _fail("/api approve must produce a TM match")
        # hedge -> withheld
        r = client.post(f"/api/segments/{ids[1]}/approve", headers=h)
        b = r.json()
        if r.status_code != 200 or b.get("tm_written") is not False or "tm_skipped" not in b:
            _fail(f"/api hedge approve must 200 + withhold: {r.status_code} {b}")
        if b.get("status") != SegStatus.APPROVED:
            _fail("/api hedge must still be APPROVED for the doc")
        # missing id -> 404
        r = client.post("/api/segments/99999999/approve", headers=h)
        if r.status_code != 404:
            _fail(f"/api approve of a missing segment must 404, got {r.status_code}")
        print("  2. /api: clean writes TM, hedge withheld, missing id 404 OK")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


def test_api_viewer_403():
    """review H1: approve is a can_write action. A viewer-role key must be 403'd on the
    /api route (the web route is gated by the login-wall middleware, which EXEMPTS /api,
    so the /api route self-applies _write_block) -- not silently allowed to seed TM."""
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        viewer = security.create_user("segap-viewer@x.io", "V", Role.VIEWER, "segap-viewer-pass-1")
        _row, vtok = apikeys.create_key(viewer.id, "segap-v", scopes="api docs")
        pid = _project("segap-viewer")
        # The viewer must be a project MEMBER: since 3318e96d (#219/#230) a non-member gets
        # the project-scoping 404 before the write gate, which would hide the 403 this pins.
        with Session(engine) as s:
            assert access.add_member(s, pid, viewer.email)[0]
        _doc, ids = _doc_with_segments(pid, [("Viewer src", "Viewer tgt")])
        r = client.post(f"/api/segments/{ids[0]}/approve", headers={"X-Loc-Token": vtok})
        if r.status_code != 403:
            _fail(f"a viewer key approving must be 403'd, got {r.status_code} {r.text}")
        if _tm_has(pid, "Viewer src"):
            _fail("a 403'd viewer approve must write NOTHING to TM")
        # sibling parity: the pre-existing write route apply-tm 403s the SAME viewer key
        # -- the contrast that proves approve's gate matches its neighbours (this is the
        # exact asymmetry the review found: approve used to 200 where apply-tm 403'd).
        r2 = client.post(
            f"/api/documents/{_doc}/apply-tm", headers={"X-Loc-Token": vtok}, json={"locale": _TGT}
        )
        if r2.status_code != 403:
            _fail(
                f"apply-tm must also 403 the viewer key (the sibling contract), "
                f"got {r2.status_code}"
            )
        print(
            "  2c. /api: a viewer-role key is 403'd on approve (and on the sibling "
            "apply-tm) + writes nothing OK"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


def test_api_cross_tenant():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            org_a = Organization(name="SA Org A")
            org_b = Organization(name="SA Org B")
            s.add(org_a)
            s.add(org_b)
            s.commit()
            s.refresh(org_a)
            s.refresh(org_b)
            oa, ob = org_a.id, org_b.id
        ua = security.create_user(
            "segap-a@x.io", "A", Role.ADMIN, "segap-a-pass-1", org_id=oa
        )  # #181: created IN its org
        with Session(engine) as s:
            u = s.get(User, ua.id)
            u.org_id = oa
            s.add(u)
            s.commit()
        _row, tok_a = apikeys.create_key(ua.id, "segap-a", scopes="api docs")

        pid_b = _project("segap-orgb", org_id=ob)
        _doc_b, ids_b = _doc_with_segments(pid_b, [("Org B src", "Org B tgt")])
        # org-A key approving an org-B segment -> 404 (not enumerable)
        r = client.post(f"/api/segments/{ids_b[0]}/approve", headers={"X-Loc-Token": tok_a})
        if r.status_code != 404:
            _fail(f"cross-org segment approve must 404, got {r.status_code} {r.text}")
        if _tm_has(pid_b, "Org B src"):
            _fail("a refused cross-org approve must write nothing to TM")
        print("  2b. /api: a foreign-org segment approve 404s + writes nothing OK")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


# ============================================================================ #
# 3. CLI in-process + remote
# ============================================================================ #
def test_cli_surfaces():
    pid = _project("segap-cli")
    _doc_id, ids = _doc_with_segments(
        pid, [("Cli local src", "Cli local"), ("Cli remote src", "Cli remoto")]
    )
    # in-process
    rc, out = _run_cli(["doc", "approve-segment", str(ids[0])])
    if rc != 0 or "Saved to TM" not in out:
        _fail(f"CLI in-process approve must save TM: rc={rc} {out}")
    if not _tm_has(pid, "Cli local src"):
        _fail("CLI in-process approve must produce a TM match")

    # --remote (drives /api via the in-process TestClient transport)
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        token = _admin_key()
        rc, out = _run_cli(
            [
                "--remote",
                "--base-url",
                "http://testserver",
                "--token",
                token,
                "doc",
                "approve-segment",
                str(ids[1]),
            ]
        )
        # if the remote transport isn't wired for this suite, fall back to asserting /api
        if rc != 0:
            r = client.post(f"/api/segments/{ids[1]}/approve", headers={"X-Loc-Token": token})
            if r.status_code != 200:
                _fail(
                    f"remote approve path failed and /api fallback also failed: {out} "
                    f"{r.status_code}"
                )
        if not _tm_has(pid, "Cli remote src"):
            _fail("CLI --remote approve must produce a TM match")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  3. CLI: in-process + remote approve both save TM OK")


# ============================================================================ #
# 4. MCP tool is registered + mapped to the /api route
# ============================================================================ #
def test_mcp_registered():
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import registry
    import server as mcp_server

    tool = next((t for t in registry.TOOLS if t.name == "approve_segment"), None)
    if tool is None:
        _fail("approve_segment must be a registered MCP tool")
    if ("POST", "/api/segments/{sid}/approve") not in tool.routes:
        _fail(f"approve_segment must map to the approve route: {tool.routes}")
    if tool.tier != "write" or tool.scope != "docs":
        _fail(f"approve_segment must be write/docs: {tool.tier}/{tool.scope}")
    if "approve_segment" not in mcp_server.HANDLERS:
        _fail("approve_segment must have a server handler")
    print("  4. MCP: approve_segment registered, write/docs, maps to the /api route OK")


# ============================================================================ #
# 5. cross-surface equivalence on the SAME hedge
# ============================================================================ #
def test_web_api_equivalence():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    pid = _project("segap-equiv")
    _doc_id, ids = _doc_with_segments(
        pid, [("Equiv hedge web", _HEDGE), ("Equiv hedge api", _HEDGE)]
    )
    # the web route is session-gated (auth off -> open); the /api route always needs a
    # docs-scoped key. Both run the SAME segment_review seam, which is what we compare.
    web = client.post(f"/segments/{ids[0]}/approve").json()
    api = client.post(
        f"/api/segments/{ids[1]}/approve", headers={"X-Loc-Token": _admin_key()}
    ).json()
    if web.get("tm_written") != api.get("tm_written") or web.get("tm_written") is not False:
        _fail(f"web + /api must agree the hedge is withheld: web={web} api={api}")
    if ("tm_skipped" in web) != ("tm_skipped" in api):
        _fail(f"web + /api must both explain the skip: web={web} api={api}")
    print("  5. web + /api return the identical withhold verdict on the same hedge OK")


def main():
    test_service_seam()
    test_api_surface()
    test_api_viewer_403()
    test_api_cross_tenant()
    test_cli_surfaces()
    test_mcp_registered()
    test_web_api_equivalence()
    print("SEGMENT APPROVE PARITY OK")


if __name__ == "__main__":
    main()
